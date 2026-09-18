"""Conservative SSH coverage, enrolled before a managed SSH process may start.

The launch gate and this enrollment share one interprocess lock. A disconnected
proxy does not prove its remote server stopped, so remote hosts remain recorded.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import os
import time
from uuid import uuid4

from .store import Store, identifier, now
from .instances import process_identity
from .process_state import process_liveness
from .updates import UpdateError, _lock_file, _unlock_file


class SshInventory:
    def __init__(self, root, *, identity=process_identity, liveness=process_liveness):
        self.store = Store(root)
        self.identity, self.liveness = identity, liveness

    @contextmanager
    def _admission(self):
        # Several inherited hosts connect at once. The lock protects a short
        # enrollment transaction; contention is not an update/connection error.
        deadline = time.monotonic() + 5
        while True:
            try:
                lock = _lock_file(self.store.directory / 'updates' / 'maintenance' / 'launch-admission.lock')
                break
            except UpdateError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        try:
            yield
        finally:
            _unlock_file(lock)

    def prepare(self, profile_id, generation):
        profile_id, generation = identifier(profile_id), identifier(generation)
        def write(data):
            inventories = data.setdefault('ssh_inventory', {})
            previous = inventories.get(profile_id, {})
            inventories[profile_id] = dict(generation=generation, adapter='tracked-ssh-v1',
                hosts=previous.get('hosts', []), operations=previous.get('operations', {}),
                unclassified=previous.get('unclassified', False),
                updated_at=now())
        self.store.mutate(write)

    def _reconcile(self, profile_id):
        inventory = self.store.read().get('ssh_inventory', {}).get(profile_id, {})
        retired = {}
        for operation_id, operation in inventory.get('operations', {}).items():
            try:
                state = self.liveness(dict(pid=operation.get('pid'), created=operation.get('process_created')))
            except (OSError, ValueError, TypeError):
                state = 'unknown'
            if state in ('exited', 'reused'):
                retired[operation_id] = operation
        if not retired:
            return inventory

        def retire(data):
            current = data.get('ssh_inventory', {}).get(profile_id, {})
            operations = current.get('operations', {})
            changed = False
            for operation_id, observed in retired.items():
                # A concurrent enrollment or generation change must survive.
                if operations.get(operation_id) == observed:
                    del operations[operation_id]
                    changed = True
            if changed:
                current['updated_at'] = now()
            # Local exit never clears hosts or the unclassified-command flag.
            return deepcopy(current)
        return self.store.mutate(retire)

    def coverage(self, profile):
        inventory = self._reconcile(identifier(profile['id']))
        complete = (profile.get('runtime_channel') != 'packaged'
                    and inventory.get('generation') == profile.get('generation')
                    and inventory.get('adapter') == 'tracked-ssh-v1'
                    and not inventory.get('unclassified')
                    and not inventory.get('operations'))
        return dict(complete=complete, generation=inventory.get('generation'),
                    hosts=['local', *inventory.get('hosts', [])],
                    maintenance_complete=(profile.get('runtime_channel') != 'packaged'
                        and inventory.get('generation') == profile.get('generation')
                        and inventory.get('adapter') == 'tracked-ssh-v1'
                        and not inventory.get('unclassified')
                        and all(op.get('operation') == 'native-proxy' for op in inventory.get('operations', {}).values())),
                    operations=list(inventory.get('operations', {}).values()))

    @contextmanager
    def execution(self, profile_id, generation, event):
        profile_id, generation = identifier(profile_id), identifier(generation)
        operation_id = str(uuid4())
        pid = os.getpid()
        observed = self.identity(pid) or {}
        created = observed.get('process_created')
        if observed.get('process_id') != pid or type(created) is not int or created <= 0:
            created = None
        def enroll(data):
            for gate in (data.get('update_maintenance'), data.get('profile_maintenance', {}).get(profile_id)):
                if gate and gate.get('state') != 'released':
                    restored = gate.get('restoring_generations', {}).get(profile_id) == generation
                    if not restored or event.get('operation') not in ('native-version', 'native-probe', 'native-start', 'native-proxy'):
                        raise UpdateError('profile_restarting', 'This profile is applying settings. Reconnect after it reopens.')
            inventory = data.get('ssh_inventory', {}).get(profile_id)
            if not inventory or inventory.get('generation') != generation:
                raise UpdateError('ssh_generation_changed', 'This profile SSH generation is no longer current.')
            # A server start or a proxy attachment can leave work behind after
            # the local process exits. Never clear that host on transport EOF.
            if event['operation'] in ('native-start', 'native-proxy'):
                host = event['alias']
                if host not in inventory['hosts']:
                    inventory['hosts'].append(host)
                    inventory['hosts'].sort()
            elif event['operation'] == 'passthrough':
                inventory['unclassified'] = True
            inventory['operations'][operation_id] = dict(operation=event['operation'],
                alias=event.get('alias'), revision=event.get('revision'),
                pid=pid, process_created=created, generation=generation, started_at=now())
            inventory['updated_at'] = now()
        with self._admission():
            self.store.mutate(enroll)
        try:
            yield
        finally:
            def retire(data):
                inventory = data.get('ssh_inventory', {}).get(profile_id)
                if inventory:
                    inventory.get('operations', {}).pop(operation_id, None)
                    inventory['updated_at'] = now()
            self.store.mutate(retire)
