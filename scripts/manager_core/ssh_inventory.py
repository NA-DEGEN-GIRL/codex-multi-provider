"""Conservative SSH coverage, enrolled before a managed SSH process may start.

The maintenance gate and enrollment share the atomic state transaction. Desktop
launches must not stall SSH enrollment. A disconnected proxy does not prove its
remote server stopped, so remote hosts remain recorded.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import os
import threading
from uuid import uuid4

from .store import Store, Unchanged, identifier, now
from .instances import process_identity
from .process_state import process_liveness
from .updates import UpdateError


class SshInventory:
    def __init__(self, root, *, identity=process_identity, liveness=process_liveness):
        self.store = Store(root)
        self.identity, self.liveness = identity, liveness
        self._sweeper = self._sweeper_stop = None
        self._sweeper_lock = threading.Lock()

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

    def _retired(self, operations, stopping=None):
        retired = {}
        for operation_id, operation in operations.items():
            if stopping is not None and stopping.is_set():
                return None
            try:
                state = self.liveness(dict(pid=operation.get('pid'), created=operation.get('process_created')))
            except (OSError, ValueError, TypeError):
                state = 'unknown'
            if state in ('exited', 'reused'):
                retired[operation_id] = operation
        return retired

    @staticmethod
    def _retire(current, retired):
        operations = current.get('operations', {})
        count = 0
        for operation_id, observed in retired.items():
            # A concurrent enrollment or generation change must survive.
            if operations.get(operation_id) == observed:
                del operations[operation_id]
                count += 1
        if count:
            current['updated_at'] = now()
        # Local exit never clears hosts or the unclassified-command flag.
        return count

    def _reconcile(self, profile_id):
        inventory = self.store.read().get('ssh_inventory', {}).get(profile_id, {})
        retired = self._retired(inventory.get('operations', {}))
        if not retired:
            return inventory

        def retire(data):
            current = data.get('ssh_inventory', {}).get(profile_id, {})
            # A peer that already retired the same records leaves nothing to write.
            return deepcopy(current) if self._retire(current, retired) else Unchanged(current)
        return self.store.mutate(retire)

    def reconcile_all(self, *, stopping=None):
        """Retire proven-dead operations of every profile in one transaction.

        Killed native-proxy shims never run their finally, and coverage() only
        reconciles during maintenance. Liveness checks stay outside the store
        lock because thousands of leaked records take about a second to probe.
        """
        observed = {}
        for profile_id, inventory in self.store.read().get('ssh_inventory', {}).items():
            retired = self._retired(inventory.get('operations', {}), stopping)
            if retired is None:
                return 0
            if retired:
                observed[profile_id] = retired
        if not observed:
            return 0

        def retire(data):
            # A profile removed meanwhile gets a detached {} and no new entry.
            inventories = data.get('ssh_inventory', {})
            total = sum(self._retire(inventories.get(profile_id, {}), retired)
                        for profile_id, retired in observed.items())
            # A concurrent retire can empty the evidence; keep the large file as is.
            return total if total else Unchanged(0)
        return self.store.mutate(retire)

    def start_sweeper(self, interval=60, *, delay=5):
        with self._sweeper_lock:
            if self._sweeper and self._sweeper.is_alive() and not self._sweeper_stop.is_set():
                return False
            stopping = self._sweeper_stop = threading.Event()

            def sweep():
                # The first pass waits so it does not contend with startup.
                wait = delay
                while not stopping.wait(wait):
                    try:
                        self.reconcile_all(stopping=stopping)
                    except (OSError, ValueError, RuntimeError):
                        pass  # A busy or briefly unreadable store is retried next pass.
                    wait = interval
            self._sweeper = threading.Thread(target=sweep, name='ssh-inventory-sweeper', daemon=True)
            self._sweeper.start()
            return True

    def stop_sweeper(self, timeout=2):
        with self._sweeper_lock:
            worker, stopping = self._sweeper, self._sweeper_stop
            if stopping:
                stopping.set()
        if worker and worker is not threading.current_thread():
            worker.join(timeout)

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
            ssh_gate = data.get('ssh_maintenance', {}).get(profile_id)
            if ssh_gate and ssh_gate.get('state') != 'released':
                raise UpdateError('ssh_settings_pending', 'SSH settings are being prepared; local work remains available.')
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
        # Gate validation and enrollment commit are indivisible. Maintenance
        # snapshots use this same Store lock before publishing their gate, so
        # an admitted operation is either included or rejected. Do not acquire
        # launch-admission here: it spans slow, unrelated desktop launches.
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
