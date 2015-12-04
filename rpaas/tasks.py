import os
import logging
import sys

from celery import Celery, Task
import hm.managers.cloudstack  # NOQA
import hm.lb_managers.cloudstack  # NOQA
import hm.lb_managers.networkapi_cloudstack  # NOQA
from hm import config
from hm.model.host import Host
from hm.model.load_balancer import LoadBalancer

from rpaas import hc, nginx, storage
import rpaas.ssl_plugins
# from rpaas.ssl_plugins import default, le
import inspect
import json


redis_host = os.environ.get('REDIS_HOST', 'localhost')
redis_port = os.environ.get('REDIS_PORT', '6379')
redis_password = os.environ.get('REDIS_PASSWORD', '')
if redis_password:
    redis_password = ':{}@'.format(redis_password)
redis_broker = "redis://{}{}:{}/0".format(redis_password, redis_host, redis_port)
app = Celery('tasks', broker=redis_broker, backend=redis_broker)
app.conf.update(
    CELERY_TASK_SERIALIZER='json',
    CELERY_RESULT_SERIALIZER='json',
    CELERY_ACCEPT_CONTENT=['json'],
)


class BaseManagerTask(Task):
    ignore_result = True
    store_errors_even_if_ignored = True

    def init_config(self, config=None):
        self.config = config
        self.nginx_manager = nginx.Nginx(config)
        self.host_manager_name = self._get_conf("HOST_MANAGER", "cloudstack")
        self.lb_manager_name = self._get_conf("LB_MANAGER", "networkapi_cloudstack")
        self.hc = hc.Dumb()
        self.storage = storage.MongoDBStorage(config)
        hc_url = self._get_conf("HCAPI_URL", None)
        if hc_url:
            self.hc = hc.HCAPI(self.storage,
                               url=hc_url,
                               user=self._get_conf("HCAPI_USER"),
                               password=self._get_conf("HCAPI_PASSWORD"),
                               hc_format=self._get_conf("HCAPI_FORMAT", "http://{}:8080/"))

    def _get_conf(self, key, default=config.undefined):
        return config.get_config(key, default, self.config)

    def _add_host(self, name, lb=None):
        healthcheck_timeout = int(self._get_conf("RPAAS_HEALTHCHECK_TIMEOUT", 600))
        host = Host.create(self.host_manager_name, name, self.config)
        created_lb = None
        try:
            if not lb:
                lb = created_lb = LoadBalancer.create(self.lb_manager_name, name, self.config)
            lb.add_host(host)
            self.nginx_manager.wait_healthcheck(host.dns_name, timeout=healthcheck_timeout)
            self.hc.create(name)
            self.hc.add_url(name, host.dns_name)
            self.storage.remove_task(name)
        except:
            exc_info = sys.exc_info()
            rollback = self._get_conf("RPAAS_ROLLBACK_ON_ERROR", "0") in ("True", "true", "1")
            if not rollback:
                raise
            try:
                if created_lb is not None:
                    created_lb.destroy()
            except Exception as e:
                logging.error("Error in rollback trying to destroy load balancer: {}".format(e))
            try:
                host.destroy()
            except Exception as e:
                logging.error("Error in rollback trying to destroy host: {}".format(e))
            try:
                self.hc.destroy(name)
            except Exception as e:
                logging.error("Error in rollback trying to remove healthcheck: {}".format(e))
            raise exc_info[0], exc_info[1], exc_info[2]


class NewInstanceTask(BaseManagerTask):

    def run(self, config, name):
        self.init_config(config)
        self._add_host(name)


class RemoveInstanceTask(BaseManagerTask):

    def run(self, config, name):
        self.init_config(config)
        lb = LoadBalancer.find(name, self.config)
        if lb is None:
            raise storage.InstanceNotFoundError()
        for host in lb.hosts:
            host.destroy()
        lb.destroy()
        self.hc.destroy(name)


class ScaleInstanceTask(BaseManagerTask):

    def _delete_host(self, lb, host):
        host.destroy()
        lb.remove_host(host)
        self.hc.remove_url(lb.name, host.dns_name)

    def run(self, config, name, quantity):
        try:
            self.init_config(config)
            lb = LoadBalancer.find(name, self.config)
            if lb is None:
                raise storage.InstanceNotFoundError()
            diff = quantity - len(lb.hosts)
            if diff == 0:
                return
            for i in xrange(abs(diff)):
                if diff > 0:
                    self._add_host(name, lb=lb)
                else:
                    self._delete_host(lb, lb.hosts[i])
        finally:
            self.storage.remove_task(name)


class DownloadCertTask(BaseManagerTask):

    def run(self, config, name, plugin, csr, key, domain):
        try:
            self.init_config(config)
            lb = LoadBalancer.find(name, self.config)
            if lb is None:
                raise storage.InstanceNotFoundError()

            crt = None

            #  Get plugin class
            p_ssl = getattr(getattr(__import__('rpaas'), 'ssl_plugins'), plugin)
            for obj_name, obj in inspect.getmembers(p_ssl):
                if obj_name != 'BaseSSLPlugin' and \
                   inspect.isclass(obj) and \
                   issubclass(obj, rpaas.ssl_plugins.BaseSSLPlugin):
                    hosts = [host.dns_name for host in lb.hosts]
                    c_ssl = obj(domain, os.environ.get('RPAAS_PLUGIN_LE_EMAIL', 'admin@'+domain), hosts)

            #  Upload csr and get an Id
            plugin_id = c_ssl.upload_csr(csr)
            crt = c_ssl.download_crt(id=str(plugin_id))

            #  Download the certificate and update nginx with it
            if crt:
                try:
                    js_crt = json.loads(crt)
                    cert = js_crt['crt']
                    cert = cert+js_crt['chain'] if 'chain' in js_crt else cert
                    key = js_crt['key'] if 'key' in js_crt else key
                except:
                    cert = crt

                for host in lb.hosts:
                    self.nginx_manager.update_certificate(host.dns_name, cert, key)

            else:
                raise Exception('Could not download certificate')
        except Exception, e:
            logging.error("Error in ssl plugin task: {}".format(e))
            raise e
        finally:
            self.storage.remove_task(name)


class RevokeCertTask(BaseManagerTask):

    def run(self, config, name, plugin, domain):
        try:
            self.init_config(config)
            lb = LoadBalancer.find(name, self.config)
            if lb is None:
                raise storage.InstanceNotFoundError()

            p_ssl = getattr(getattr(__import__('rpaas'), 'ssl_plugins'), plugin)
            for obj_name, obj in inspect.getmembers(p_ssl):
                if obj_name != 'BaseSSLPlugin' and \
                        inspect.isclass(obj) and \
                        issubclass(obj, rpaas.ssl_plugins.BaseSSLPlugin):
                    hosts = [host.dns_name for host in lb.hosts]
                    c_ssl = obj(domain, os.environ.get('RPAAS_PLUGIN_LE_EMAIL', 'admin@'+domain), hosts)

            c_ssl.revoke()

        except Exception, e:
            logging.error("Error in ssl plugin task: {}".format(e))
            raise e
        finally:
            self.storage.remove_task(name)
