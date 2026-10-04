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
from .execution_preset_replay import ExecutionPresetReplay


def endpoint_id(profile_id, host_alias):
    from .ssh_shim import ALIAS
    if not isinstance(host_alias, str) or not ALIAS.fullmatch(host_alias):
        raise ValueError('Invalid SSH host alias.')
    return str(uuid5(UUID(profile_id), 'codex-manager-ssh-admin-v1:' + host_alias))


class SshRuntimeControl:
    def __init__(self, auth, profile_id, generation, runtime_pid, send_frame, *, lock=None,
                 host_alias=None, revision=None, record_delete=None, preset_source=None):
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
        self.preset_replay = ExecutionPresetReplay(preset_source) if preset_source is not None else None

    def _replay_ready(self):
        return self._ready() and getattr(self.auth, 'execution_presets_version', 0) == 1

    def _replay_count(self):
        return self.preset_replay.pending_count() if self.preset_replay is not None else 0

    def _replay_outcome(self, result):
        """Run generated requests through the existing account gate, never a new reader."""
        outcome = AuthProxyResult(frontend=list(result.frontend), events=list(result.events))
        for message in result.runtime:
            forwarded = self.auth.process('frontend', message)
            outcome.runtime.extend(forwarded.runtime)
            outcome.frontend.extend(forwarded.frontend)
            outcome.events.extend(forwarded.events)
        return self._observe_outcome(outcome)

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
        if method == 'manager/executionPresets/status':
            with self.lock:
                if not self._ready() or self.binding is None:
                    raise AdminError('not_ready')
                applied = self.observer.snapshot()['execution_presets']
                version = getattr(self.auth, 'execution_presets_version', 0)
                return {'known': params['threadId'] in applied,
                        'executionPreset': applied.get(params['threadId']),
                        'version': 1 if type(version) is int and version == 1 else 0,
                        'sshBinding': dict(self.binding)}
        if method.startswith('manager/maintenance/'):
            deadline = time.monotonic() + timeout
            with self.lock:
                if time.monotonic() >= deadline:
                    raise AdminError('timeout')
                if self.closed:
                    raise AdminError('closed')
                if method == 'manager/maintenance/acquire' and (self.broker.pending_mutation_count() or self._replay_count()):
                    raise AdminError('busy')
                result = self.maintenance.request(method, params, self.observer.snapshot(), self.auth.state == 'ready')
                result['pendingMutationCount'] += self.broker.pending_mutation_count() + self._replay_count()
                if self.binding is not None:
                    result['sshBinding'] = {**self.binding, 'authState': self.auth.state,
                                           'accountFingerprint': self.auth.account_fingerprint}
                return result
        return self.broker.request(method, params, timeout, lease)

    def _observe_outcome(self, result):
        result.runtime = [self.notifications.to_runtime(message) for message in result.runtime]
        for message in result.runtime:
            self.observer.consume('client', message)
            if not ExecutionPresetReplay.reserved(message):
                self.maintenance.observe_client(message)
        result.frontend = [message for message in result.frontend if self.notifications.to_frontend(message)]
        return result

    def process(self, direction, message):
        with self.lock:
            replayed = AuthProxyResult()
            if direction == 'runtime':
                self.observer.consume('server', message)
                self.maintenance.observe_runtime(message)
                if self.broker.consume_runtime(message):
                    return AuthProxyResult()
                if self.preset_replay is not None:
                    consumed, replayed = self.preset_replay.runtime(message, ready=self._replay_ready())
                    replayed = self._replay_outcome(replayed)
                    if consumed:
                        return replayed
            else:
                reserved = AdminRpcBroker.is_reserved_request(message) or ExecutionPresetReplay.reserved(message)
                if reserved or self.maintenance.blocks(message):
                    result = AuthProxyResult()
                    if type(message.get('id')) in (int, str):
                        result.frontend.append({'id': message['id'], 'error': {
                            'code': -32043 if reserved else -32044,
                            'message': 'Managed SSH request IDs are reserved.' if reserved else
                            'This profile SSH connection is applying settings. New work is temporarily paused.'}})
                    return result
                if self.preset_replay is not None:
                    replay = self.preset_replay.frontend(message, ready=self._replay_ready(),
                        applied=self.observer.snapshot()['execution_presets'])
                    if replay is not None:
                        return self._replay_outcome(replay)
            if direction=='frontend' and self.record_delete is not None and self._ready():
                if self.record_delete.submit(message):
                    self.observer.consume('client',message)
                    self.maintenance.observe_client(message)
                    return AuthProxyResult()
            result = self._observe_outcome(self.auth.process(direction, message))
            result.runtime.extend(replayed.runtime)
            result.frontend.extend(replayed.frontend)
            result.events.extend(replayed.events)
            return result

    def poll(self):
        with self.lock:
            result=self._observe_outcome(self.auth.poll())
            if self.preset_replay is not None:
                replayed = self._replay_outcome(self.preset_replay.poll(ready=self._replay_ready()))
                result.runtime.extend(replayed.runtime)
                result.frontend.extend(replayed.frontend)
                result.events.extend(replayed.events)
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
            outcome = self.preset_replay.close() if self.preset_replay is not None else AuthProxyResult()
        if self.record_delete is not None:self.record_delete.close()
        self.broker.close()
        return outcome
