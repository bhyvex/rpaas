# Copyright 2014 rpaas authors. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

# coding: utf-8

import hm.managers.cloudstack  # NOQA
import hm.lb_managers.networkapi_cloudstack  # NOQA
from hm.model.load_balancer import LoadBalancer

from rpaas import storage, tasks, nginx


PENDING = 'pending'
FAILURE = 'failure'


class Manager(object):
    def __init__(self, config=None):
        self.config = config
        self.storage = storage.MongoDBStorage(config)
        self.nginx_manager = nginx.NginxDAV(config)

    def new_instance(self, name):
        lb = LoadBalancer.find(name)
        if lb is not None:
            raise storage.DuplicateError(name)
        self.storage.store_task(name)
        task = tasks.NewInstanceTask().delay(self.config, name)
        self.storage.update_task(name, task.task_id)

    def remove_instance(self, name):
        self.storage.remove_task(name)
        tasks.RemoveInstanceTask().delay(self.config, name)

    def bind(self, name, app_host):
        self._ensure_ready(name)
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        binding_data = self.storage.find_binding(name)
        if binding_data:
            binded_host = binding_data.get('app_host')
            if binded_host == app_host:
                # Nothing to do, already binded
                return
            raise BindError('This service can only be binded to one application.')
        for host in lb.hosts:
            self.nginx_manager.update_binding(host.dns_name, '/', app_host)
        self.storage.store_binding(name, app_host)

    def unbind(self, name, app_host):
        self._ensure_ready(name)
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        binding_data = self.storage.find_binding(name)
        if not binding_data:
            return
        self.storage.remove_binding(name)
        paths = binding_data.get('paths') or []
        for path_data in paths:
            for host in lb.hosts:
                self.nginx_manager.delete_binding(host.dns_name, path_data['path'])

    def info(self, name):
        addr = self._get_address(name)
        return [{"label": "Address", "value": addr}]

    def status(self, name):
        return self._get_address(name)

    def update_certificate(self, name, cert, key):
        self._ensure_ready(name)
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        self.storage.update_binding_certificate(name, cert, key)
        for host in lb.hosts:
            self.nginx_manager.update_certificate(host.dns_name, cert, key)

    def _get_address(self, name):
        task = self.storage.find_task(name)
        if task:
            result = tasks.NewInstanceTask().AsyncResult(task['task_id'])
            if result.status in ['FAILURE', 'REVOKED']:
                return FAILURE
            return PENDING
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        return lb.address

    def scale_instance(self, name, quantity):
        self._ensure_ready(name)
        if quantity <= 0:
            raise ScaleError("Can't have 0 instances")
        self.storage.store_task(name)
        task = tasks.ScaleInstanceTask().delay(self.config, name, quantity)
        self.storage.update_task(name, task.task_id)

    def add_redirect(self, name, path, destination, content):
        self._ensure_ready(name)
        path = path.strip()
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        self.storage.replace_binding_path(name, path, destination, content)
        for host in lb.hosts:
            self.nginx_manager.update_binding(host.dns_name, path, destination, content)

    def delete_redirect(self, name, path):
        self._ensure_ready(name)
        path = path.strip()
        if path == '/':
            raise RedirectError("You cannot remove a redirect for / location, unbind the app.")
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        self.storage.delete_binding_path(name, path)
        for host in lb.hosts:
            self.nginx_manager.delete_binding(host.dns_name, path)

    def list_redirects(self, name):
        return self.storage.find_binding(name)

    def _ensure_ready(self, name):
        task = self.storage.find_task(name)
        if task:
            raise NotReadyError("Async task still running")


class BindError(Exception):
    pass


class NotReadyError(Exception):
    pass


class ScaleError(Exception):
    pass


class RedirectError(Exception):
    pass
