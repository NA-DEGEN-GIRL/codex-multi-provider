"""Admin RPC on the existing SSH WebSocket connection, without another reader.

This fences one Windows transport. It does not certify remote daemon exit or
coverage of other remote clients; callers still need remote lifecycle evidence.
"""
from __future__ import annotations

import json
import re
import threading
import time
from uuid import UUID, uuid5

from .app_transport import RuntimeObserver
from .proxy_auth import AuthProxyResult
from .runtime_admin import AdminError, AdminRpcBroker, MaintenanceBarrier
from .websocket_auth import encode_frame
from .notification_policy import NotificationPolicy
from .catalog_origin import CatalogOrigins


def endpoint_id(profile_id, host_alias):
    from .ssh_shim import ALIAS
    if not isinstance(host_alias, str) or not ALIAS.fullmatch(host_alias):
        raise ValueError('Invalid SSH host alias.')
    return str(uuid5(UUID(profile_id), 'codex-manager-ssh-admin-v1:' + host_alias))


class SshRuntimeControl:
    def __init__(self, auth, profile_id, generation, runtime_pid, send_frame, *, lock=None,
                 host_alias=None, revision=None, record_delete=None):
        self.auth = auth
        self.binding = None
        if host_alias is not None:
            endpoint_id(profile_id, host_alias)
            if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
                raise ValueError('Invalid SSH revision.')
            self.binding = {'profileId': str(UUID(profile_id)), 'hostAlias': host_alias, 'revision': revision}
        self.catalog_origins = CatalogOrigins()
        self.record_delete = None
        if record_delete is not None:
            from .ssh_record_delete import RecordDelete
            self.record_delete = RecordDelete(self.catalog_origins, record_delete)
        self.observer = RuntimeObserver(profile_id, runtime_pid=runtime_pid,
                                        read_only_projection=self.catalog_origins)
        self.maintenance = MaintenanceBarrier(generation)
        self.lock = lock or threading.RLock()
        self.send_frame = send_frame
        self.closed = False
        self.broker = AdminRpcBroker(self._send)
        self.notifications = NotificationPolicy()

    def _ready(self):
        state = self.observer.snapshot()
        return (not self.closed and self.auth.state == 'ready'
                and all(state.get(name) is True for name in ('initialized', 'stream_complete', 'connected')))

    def _send(self, message, deadline, lease):
        with self.lock:
            if time.monotonic() >= deadline:
                raise AdminError('timeout')
            if not self._ready():
                raise AdminError('not_ready')
            self.maintenance.authorize_admin(message['method'], lease)
            body = json.dumps(message, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_frame(encode_frame(body, masked=True))
            self.observer.consume('client', message)
            self.maintenance.observe_client(message)

    def dispatch(self, method, params, timeout, lease):
        if method.startswith('manager/maintenance/'):
            deadline = time.monotonic() + timeout
            with self.lock:
                if time.monotonic() >= deadline:
                    raise AdminError('timeout')
                if self.closed:
                    raise AdminError('closed')
                if method == 'manager/maintenance/acquire' and self.broker.pending_mutation_count():
                    raise AdminError('busy')
                result = self.maintenance.request(method, params, self.observer.snapshot(), self.auth.state == 'ready')
                result['pendingMutationCount'] += self.broker.pending_mutation_count()
                if self.binding is not None:
                    result['sshBinding'] = {**self.binding, 'authState': self.auth.state,
                                           'accountFingerprint': self.auth.account_fingerprint}
                return result
        return self.broker.request(method, params, timeout, lease)

    def _observe_outcome(self, result):
        result.runtime = [self.notifications.to_runtime(message) for message in result.runtime]
        for message in result.runtime:
            self.observer.consume('client', message)
            self.maintenance.observe_client(message)
        result.frontend = [message for message in result.frontend if self.notifications.to_frontend(message)]
        return result

    def process(self, direction, message):
        with self.lock:
            if direction == 'runtime':
                self.observer.consume('server', message)
                self.maintenance.observe_runtime(message)
                if self.broker.consume_runtime(message):
                    return AuthProxyResult()
            else:
                reserved = AdminRpcBroker.is_reserved_request(message)
                if reserved or self.maintenance.blocks(message):
                    result = AuthProxyResult()
                    if type(message.get('id')) in (int, str):
                        result.frontend.append({'id': message['id'], 'error': {
                            'code': -32043 if reserved else -32044,
                            'message': 'Managed SSH request IDs are reserved.' if reserved else
                            'This profile SSH connection is applying settings. New work is temporarily paused.'}})
                    return result
            if direction=='frontend' and self.record_delete is not None and self._ready():
                if self.record_delete.submit(message):
                    self.observer.consume('client',message)
                    self.maintenance.observe_client(message)
                    return AuthProxyResult()
            return self._observe_outcome(self.auth.process(direction, message))

    def poll(self):
        with self.lock:
            result=self._observe_outcome(self.auth.poll())
            if self.record_delete is not None:
                deleted=self.record_delete.poll()
                for message in deleted.frontend:
                    self.observer.consume('server',message)
                    self.maintenance.observe_runtime(message)
                result.frontend.extend(deleted.frontend)
            return result

    def close(self):
        with self.lock:
            self.closed = True
            self.observer.gap()
            self.catalog_origins.close()
        if self.record_delete is not None:self.record_delete.close()
        self.broker.close()
