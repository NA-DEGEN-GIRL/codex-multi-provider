"""Headless update hook fixtures: no HWND operations, real IPC, or installation."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager_core.store import Store, atomic_json
from manager_core.update_hooks import UpdateHooks, _cim_birth
from manager_core.updates import UpdateError
from manager_core.runtime_admin import AdminClient, AdminError


ROOT_THREAD = "10000000-0000-0000-0000-000000000001"
CHILD_THREAD = "10000000-0000-0000-0000-000000000002"


class FakeInstances:
    def __init__(self, store):
        self.store = store
        self.running = {}
        self.live_pids = {}
        self.show_calls = []
        self.hooks = None
        self.auth_ready = True
        self.initialized = True
        self.stale_generation = False

    def paths(self, profile):
        expected = self.store.directory / "profiles" / profile["id"]
        if Path(profile["home"]) != expected / "codex" or Path(profile["ui_home"]) != expected / "ui":
            raise ValueError("foreign paths")
        return expected / "codex", expected / "ui"

    def start(self, profile_id):
        profile = self.store.profile(profile_id)
        process_id = 100 + len(self.show_calls) * 10
        identity = {"process_id": process_id, "process_created": 134019360000000008,
                    "executable_path": r"C:\Program Files\WindowsApps\fixture\app\ChatGPT.exe"}
        generation = str(uuid4())
        profile = self.store.mutate(lambda data: self._save_start(data, profile_id, identity, generation))
        self.running[profile_id] = identity
        self.live_pids[process_id] = identity
        for pid in (process_id + 1, process_id + 2):
            self.live_pids[pid] = {**identity, "process_id": pid, "process_created": identity["process_created"] + pid}
        return profile

    def _save_start(self, data, profile_id, identity, generation):
        p = self.store.profile(profile_id, data)
        p.update(identity, generation=generation, account_fingerprint="fixture-account",
                 usage_account_id=str(uuid4()), window_handle=42)
        return p

    def observe(self, profile):
        current = self.running.get(profile["id"])
        if current is None:
            return {"status": "not_started", "process_id": None}
        observer = {
            "profile_id": profile["id"], "generation": profile["generation"] if not self.stale_generation else "stale",
            "observed_at": datetime.now(timezone.utc).isoformat(), "connected": True,
            "initialized": self.initialized, "stream_complete": True,
            "runtime_process_id": current["process_id"] + 2,
            "auth_binding": {"bound": True, "state": "ready",
                             "account_fingerprint": "fixture-account" if self.auth_ready else "wrong-account"},
        }
        return {**current, "status": "running", "window_handle": 42, "runtime_state": observer}

    def identity(self, pid):
        return copy.deepcopy(self.live_pids.get(pid))

    def liveness(self, expected):
        value = self.live_pids.get(expected["pid"])
        if value is None:
            return "exited"
        return "alive" if value["process_created"] == expected["created"] else "reused"

    def close(self, profile):
        self.running.pop(profile["id"], None)
        for pid in (profile["process_id"], profile["process_id"] + 1, profile["process_id"] + 2):
            self.live_pids.pop(pid, None)

    def finish_show(self, result):
        return result

    def show(self, profile_id):
        # Simulate root's production reservation around the entire launch.
        with self.hooks.launch_admission(profile_id):
            self.show_calls.append(profile_id)
            if profile_id not in self.running:
                self.start(profile_id)
            profile = self.store.profile(profile_id)
            return {"profile_id": profile_id, "profile": {**profile, **self.observe(profile)}}


class FakeAdmin:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []
        self.parentage = {ROOT_THREAD: None, CHILD_THREAD: ROOT_THREAD}
        self.owner = None
        self.token = str(uuid4())
        self.busy = False
        self.active_processes = 0
        self.scope_missing = False
        self.close_coverage_missing = False
        self.close_new_thread = False
        self.acquire_error = None
        self.generation_override = None

    def profile(self):
        return self.fixture.store.profile(self.fixture.profile["id"])

    def health(self):
        return {
            "generation": self.generation_override or self.profile()["generation"],
            "connected": True, "initialized": True, "streamComplete": True, "accountReady": True,
            "pendingMutationCount": 0, "pendingApprovalCount": 0,
            "activeProcessCount": self.active_processes, "activeToolCount": 0,
            "activeTurnCount": 0, "activeChildCount": 0,
            "held": self.owner is not None, "frontendMutationBlocked": self.owner is not None,
            "transactionId": self.owner, "leaseToken": self.token,
        }

    def identities(self):
        profile = self.profile()
        ids = self.fixture.instances.live_pids
        # Use the actual client output contract: extra validated executable
        # metadata previously made production fail while the smaller fake passed.
        descriptor = {kind: dict(pid=profile['process_id'] + offset,
                                created=ids[profile['process_id'] + offset]['process_created'])
                      for kind, offset in (('proxy', 1), ('runtime', 2))}
        client = object.__new__(AdminClient)
        client.generation = profile['generation']
        with patch.object(client, '_descriptor', return_value=(descriptor, b'')), \
                patch('manager_core.runtime_admin.process_identity', side_effect=ids.get):
            return client.identities()

    def request(self, method, params, timeout=15, **kwargs):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "manager/maintenance/status":
            if params:
                assert params["transactionId"] == self.owner
                assert params["leaseToken"] == self.token
            return self.health()
        if method == "manager/maintenance/acquire":
            if self.acquire_error is not None:
                if self.acquire_error.uncertain:
                    self.owner = params["transactionId"]
                error, self.acquire_error = self.acquire_error, None
                raise error
            if self.owner not in (None, params["transactionId"]):
                raise AdminError("busy")
            self.owner = params["transactionId"]
            return self.health()
        if method == "manager/maintenance/release":
            assert params == {"transactionId": self.owner, "leaseToken": self.token}
            self.owner = None
            return self.health()
        if method == "thread/loaded/list":
            return {"data": list(self.parentage), "nextCursor": None}
        if method == "thread/read":
            tid = params["threadId"]
            return {"thread": {"id": tid, "parentThreadId": self.parentage[tid],
                               "sessionId": ROOT_THREAD, "forkedFromId": None, "status": {"type": "idle"}}}
        if method == "thread/managedIdleStatus":
            tid = params["threadId"]
            ids = [tid] if self.scope_missing else list(self.parentage)
            return {"threadId": tid, "hostId": "local", "sourceStoreId": "manager:" + self.profile()["id"],
                    "observedThreadIds": ids, "idle": not self.busy,
                    "blockers": [] if not self.busy else [{"threadId": tid, "kind": "activeTurn"}],
                    "proofScope": "advisory"}
        if method == "thread/managedCloseIdle":
            assert self.owner is not None
            tid = params["threadId"]
            ids = [tid] if self.close_coverage_missing else list(self.parentage)
            self.parentage.clear()
            if self.close_new_thread:
                self.parentage[str(uuid4())] = None
            return {"threadId": tid, "closedThreadIds": ids, "writerReleaseVerified": True}
        raise AssertionError(method)


class HookFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile("fixture")
        self.instances = FakeInstances(self.store)
        self.profile = self.instances.start(self.profile["id"])
        self.admin = FakeAdmin(self)
        self.time = 0.0
        self.closes = []
        self.navigation = []
        self.hooks = UpdateHooks(
            self.root, self.store, self.instances, admin_factory=lambda _: self.admin,
            identity=self.instances.identity, liveness=self.instances.liveness,
            native_close=self.native_close, host_inventory=self.coverage,
            navigate=self.navigate, clock=lambda: self.time, sleep=self.sleep, timeout=.3)
        self.instances.hooks = self.hooks

    def sleep(self, amount):
        self.time += amount

    def coverage(self, profile):
        return {"complete": True, "generation": profile["generation"], "hosts": ["local"]}

    def native_close(self, profile):
        self.closes.append(profile["id"])
        self.instances.close(profile)

    def navigate(self, profile, entry):
        self.navigation.append((profile["id"], copy.deepcopy(entry)))
        return {"state": "request_sent", "selection_verified": False}

    def acquire(self):
        snapshot = self.hooks.snapshot_instances()
        return self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4())), snapshot[0]

    def test_direct_login_with_usage_source_can_restart_without_credential_proxy(self):
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(
            auth_mode='native', usage_account_id='usage-display-only'))
        observe = self.instances.observe
        def direct_login(profile):
            current = observe(profile)
            current['runtime_state']['auth_binding'] = {'bound': False, 'state': 'disabled'}
            return current
        with patch.object(self.instances, 'observe', side_effect=direct_login):
            snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile['id']])
            self.assertTrue(snapshot[0]['idle_verified'])
            self.assertNotIn('update_blocker', snapshot[0])
            lease = self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()),
                                                   profile_scope=[self.profile['id']])
            self.assertTrue(self.hooks.close_instance(snapshot[0]))
            self.assertEqual(self.closes, [self.profile['id']])
            self.hooks.release_maintenance(lease)

    def test_native_login_does_not_bypass_stale_or_incomplete_runtime_observations(self):
        profile = {**self.profile, 'auth_mode': 'native', 'usage_account_id': 'usage-display-only'}
        current = self.instances.observe(profile)
        current['runtime_state']['auth_binding'] = {'bound': False, 'state': 'disabled'}
        self.assertTrue(self.hooks._observer_ready(profile, current))
        for change in ({'stream_complete': False}, {'connected': False}, {'generation': str(uuid4())},
                       {'auth_binding': {'bound': True, 'state': 'ready'}}):
            observed = copy.deepcopy(current)
            observed['runtime_state'].update(change)
            self.assertFalse(self.hooks._observer_ready(profile, observed))

    def test_linked_account_still_requires_matching_ready_credential_proxy(self):
        current = self.instances.observe(self.profile)
        self.assertTrue(self.hooks._observer_ready(self.profile, current))
        current['runtime_state']['auth_binding'] = {'bound': False, 'state': 'disabled'}
        self.assertFalse(self.hooks._observer_ready(self.profile, current))

    def test_profile_scope_closes_only_selected_instance_and_keeps_peer_launchable(self):
        peer = self.store.add_profile("peer")
        self.instances.show_calls.append(peer["id"])
        peer = self.instances.start(peer["id"])
        peer_identity = copy.deepcopy(self.instances.running[peer["id"]])
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile["id"]])
        self.assertEqual([item["id"] for item in snapshot], [self.profile["id"]])
        lease = self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()),
                                               profile_scope=[self.profile["id"]])
        with self.assertRaises(UpdateError):
            self.hooks.guard_launch(self.profile["id"])
        self.assertEqual(self.hooks.guard_launch(peer["id"]), peer["id"])
        self.assertTrue(self.hooks.close_instance(snapshot[0]))
        self.hooks.release_maintenance(lease)
        self.assertEqual(self.closes, [self.profile["id"]])
        self.assertEqual(self.instances.running[peer["id"]], peer_identity)
        self.assertEqual(self.hooks.guard_launch(self.profile["id"]), self.profile["id"])

    def test_global_update_cannot_overlap_profile_maintenance(self):
        snapshot = self.hooks.snapshot_instances(profile_ids=[self.profile["id"]])
        lease = self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()),
                                               profile_scope=[self.profile["id"]])
        with self.assertRaises(UpdateError):
            self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()))
        self.assertNotIn("update_maintenance", self.store.read())
        self.hooks.release_maintenance(lease)

    def test_profile_scope_rejects_mismatched_instance_without_freezing_any_launch(self):
        peer = self.store.add_profile("peer")
        with self.assertRaises(UpdateError):
            self.hooks.acquire_maintenance(self.hooks.snapshot_instances(), transaction_id=str(uuid4()),
                                           profile_scope=[peer["id"]])
        self.hooks.guard_launch(peer["id"])
        self.hooks.guard_launch(self.profile["id"])

    def test_uncertain_scoped_acquisition_does_not_block_an_unrelated_profile(self):
        peer = self.store.add_profile("peer")
        self.admin.acquire_error = AdminError("timeout", uncertain=True)
        with self.assertRaises(AdminError):
            self.hooks.acquire_maintenance(self.hooks.snapshot_instances(profile_ids=[self.profile["id"]]),
                                           transaction_id=str(uuid4()), profile_scope=[self.profile["id"]])
        with self.assertRaises(UpdateError):
            self.hooks.guard_launch(self.profile["id"])
        self.assertEqual(self.hooks.guard_launch(peer["id"]), peer["id"])

    def test_callbacks_have_only_updater_constructor_arguments(self):
        self.assertEqual(set(self.hooks.callbacks()), {
            "snapshot_instances", "close_instance", "restore_instance", "verify_compatibility",
            "acquire_maintenance", "release_maintenance", "verify_recovery", "remote_snapshot",
            "check_restored_connections", "recover_instance", "recover_maintenance"})

    def test_filetime_conversion_matches_cim_microsecond_precision(self):
        self.assertTrue(_cim_birth(134019360000000008).endswith(".0000000Z"))

    def test_idle_advisory_snapshot_does_not_claim_writer_release(self):
        item = self.hooks.snapshot_instances()[0]
        self.assertEqual(item["job_state"], "idle")
        self.assertTrue(item["idle_verified"])
        self.assertFalse(item["writer_release_verified"])
        self.assertEqual(item["loaded_thread_ids"], [ROOT_THREAD, CHILD_THREAD])
        self.assertFalse(self.closes)

    def test_absent_or_remote_host_coverage_is_unknown(self):
        for inventory in [None, lambda _: {"complete": True, "hosts": ["local", "ssh-alpha"]}]:
            self.hooks.host_inventory = inventory
            snapshot = self.hooks.snapshot_instances()[0]
            self.assertFalse(snapshot["idle_verified"])
            self.assertEqual(snapshot["update_blocker"], "remote_runtime_coverage_unverified")

    def test_empty_thread_list_does_not_hide_running_detached_process(self):
        self.admin.parentage = {}
        self.admin.active_processes = 1
        result = self.hooks.snapshot_instances()[0]
        self.assertFalse(result["idle_verified"])
        self.assertEqual(result['update_blocker'], 'runtime_not_idle')

    def test_active_turn_is_waiting_work_not_broken_administration(self):
        health = self.admin.health()
        health['activeTurnCount'] = 1
        with patch.object(self.admin, 'health', return_value=health):
            result = self.hooks.snapshot_instances()[0]
        self.assertFalse(result['idle_verified'])
        self.assertEqual(result['update_blocker'], 'runtime_not_idle')
        health['streamComplete'] = False
        with patch.object(self.admin, 'health', return_value=health):
            self.assertEqual(self.hooks.snapshot_instances()[0]['update_blocker'], 'runtime_admin_not_ready')

    def test_wrong_account_or_stale_generation_cannot_certify_idle(self):
        self.instances.auth_ready = False
        self.assertFalse(self.hooks.snapshot_instances()[0]["idle_verified"])
        self.instances.auth_ready = True
        self.instances.stale_generation = True
        self.assertFalse(self.hooks.snapshot_instances()[0]["idle_verified"])

    def test_scope_must_cover_every_loaded_child(self):
        self.admin.scope_missing = True
        self.assertFalse(self.hooks.snapshot_instances()[0]["idle_verified"])

    def test_maintenance_prevents_ordinary_launches(self):
        lease, _ = self.acquire()
        with self.assertRaises(UpdateError):
            self.instances.show(self.profile["id"])
        self.hooks.release_maintenance(lease)
        self.instances.show(self.profile["id"])
        self.assertEqual(len(self.instances.show_calls), 1)

    def test_launch_reservation_and_maintenance_transition_exclude_each_other(self):
        with self.hooks.launch_admission(self.profile["id"]):
            with self.assertRaises(UpdateError):
                self.hooks._begin_global(str(uuid4()))
        self.assertNotIn("update_maintenance", self.store.read())

    def test_new_profile_between_plan_and_maintenance_is_detected(self):
        snapshots = self.hooks.snapshot_instances()
        other = self.store.add_profile("new")
        self.instances.start(other["id"])
        with self.assertRaises(UpdateError) as caught:
            self.hooks.acquire_maintenance(snapshots, transaction_id=str(uuid4()))
        self.assertEqual(caught.exception.code, "instances_changed")
        self.assertFalse(self.closes)
        self.assertEqual(self.store.read()["update_maintenance"]["state"], "released")

    def test_work_race_after_lease_acquire_never_closes(self):
        snapshot = self.hooks.snapshot_instances()
        self.admin.busy = True
        with self.assertRaises(UpdateError):
            self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()))
        self.assertFalse(self.closes)
        self.assertIsNone(self.admin.owner)

    def test_busy_health_after_acquire_releases_known_lease(self):
        snapshot = self.hooks.snapshot_instances()
        self.admin.active_processes = 1
        with self.assertRaises(UpdateError):
            self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()))
        self.assertFalse(self.closes)
        self.assertIsNone(self.admin.owner)
        self.assertEqual(self.store.read()["update_maintenance"]["state"], "released")

    def test_incomplete_endpoint_identity_never_acquires_or_closes(self):
        self.admin.identities = lambda: {"generation": self.profile["generation"]}
        with self.assertRaises(UpdateError):
            self.acquire()
        self.assertFalse(self.closes)
        self.assertIsNone(self.admin.owner)

    def test_strict_release_covers_parent_and_children_before_native_close(self):
        lease, snapshot = self.acquire()
        self.assertTrue(self.hooks.close_instance(snapshot))
        methods = [m for m, _ in self.admin.calls]
        self.assertEqual(methods.count("thread/managedCloseIdle"), 1)
        self.assertEqual(self.closes, [self.profile["id"]])
        self.hooks.release_maintenance(lease)
        self.assertEqual(self.store.read()["update_maintenance"]["state"], "released")

    def test_disappearing_unproved_child_does_not_authorize_native_close(self):
        _, snapshot = self.acquire()
        self.admin.close_coverage_missing = True
        with self.assertRaises(UpdateError) as caught:
            self.hooks.close_instance(snapshot)
        self.assertEqual(caught.exception.code, "writer_release_unverified")
        self.assertFalse(self.closes)

    def test_new_loaded_thread_after_close_proof_blocks_native_close(self):
        _, snapshot = self.acquire()
        self.admin.close_new_thread = True
        with self.assertRaises(UpdateError):
            self.hooks.close_instance(snapshot)
        self.assertFalse(self.closes)

    def test_process_identity_change_never_closes_reused_pid(self):
        _, snapshot = self.acquire()
        self.instances.live_pids[snapshot["process_id"]]["process_created"] += 1
        with self.assertRaises(UpdateError):
            self.hooks.close_instance(snapshot)
        self.assertFalse(self.closes)

    def test_unknown_endpoint_liveness_is_not_verified_exit(self):
        lease, snapshot = self.acquire()
        self.hooks.liveness = lambda _: "unknown"
        self.assertFalse(self.hooks.close_instance(snapshot))
        with self.assertRaises(UpdateError):
            self.hooks.release_maintenance(lease)

    def test_close_window_without_process_exit_is_not_forced(self):
        lease, snapshot = self.acquire()
        self.hooks.native_close = lambda _: self.closes.append("normal-close-request")
        self.hooks.idle_stop = lambda *_: self.fail('ordinary close must not finish process exit')
        self.assertFalse(self.hooks.close_instance(snapshot))
        self.assertEqual(self.closes, ["normal-close-request"])
        with self.assertRaises(UpdateError):
            self.hooks.release_maintenance(lease)

    def test_automatic_update_finishes_only_writer_drained_tray_process(self):
        lease, snapshot = self.acquire()
        self.hooks.native_close = lambda _: self.closes.append('tray-close')
        def finish(profile, verify):
            self.assertEqual(profile['id'], self.profile['id'])
            self.assertTrue(verify())
            self.assertIsNotNone(self.admin.owner)
            self.assertFalse(self.admin.parentage)
            self.instances.close(profile)
            self.closes.append('verified-exit')
            return {'state': 'stopped'}
        self.hooks.idle_stop = finish
        self.assertTrue(self.hooks.close_instance(snapshot, finish_idle_exit=True))
        self.assertEqual(self.closes, ['tray-close', 'verified-exit'])
        self.hooks.release_maintenance(lease)

    def assert_automatic_finish_preserves_changed_proof(self, lose_proof):
        _, snapshot = self.acquire()
        self.hooks.native_close = lose_proof
        self.hooks.idle_stop = lambda *_: self.fail('lost proof must preserve the process')
        with self.assertRaises(UpdateError):
            self.hooks.close_instance(snapshot, finish_idle_exit=True)
        self.assertIn(self.profile['id'], self.instances.running)

    def test_automatic_finish_rechecks_mutation_gate_after_tray_close(self):
        health = self.admin.health
        def lose_lease(profile):
            self.admin.health = lambda: {**health(), 'held': False, 'frontendMutationBlocked': False}
        self.assert_automatic_finish_preserves_changed_proof(lose_lease)

    def test_automatic_finish_preserves_newly_loaded_work(self):
        self.assert_automatic_finish_preserves_changed_proof(
            lambda _: self.admin.parentage.update({ROOT_THREAD: None}))

    def test_automatic_finish_preserves_newly_started_tool(self):
        self.assert_automatic_finish_preserves_changed_proof(
            lambda _: setattr(self.admin, 'active_processes', 1))

    def test_uncertain_acquire_can_be_explicitly_reconciled(self):
        snapshot = self.hooks.snapshot_instances()
        transaction_id = str(uuid4())
        self.admin.acquire_error = AdminError("timeout", uncertain=True)
        with self.assertRaises(AdminError):
            self.hooks.acquire_maintenance(snapshot, transaction_id=transaction_id)
        self.assertEqual(self.store.read()["update_maintenance"]["state"], "held")
        proof = self.hooks.verify_recovery({"transaction_id": transaction_id}, {})
        self.assertTrue(proof["maintenance_released"])
        self.assertIsNone(self.admin.owner)
        self.assertEqual([m for m, _ in self.admin.calls].count("manager/maintenance/acquire"), 2)

    def test_known_rejected_acquire_does_not_leave_global_admission_closed(self):
        snapshot = self.hooks.snapshot_instances()
        self.admin.acquire_error = AdminError("busy", uncertain=False)
        with self.assertRaises(AdminError):
            self.hooks.acquire_maintenance(snapshot, transaction_id=str(uuid4()))
        self.assertEqual(self.store.read()["update_maintenance"]["state"], "released")

    def test_dead_proxy_resolves_uncertain_acquisition_without_resubmission(self):
        snapshot = self.hooks.snapshot_instances()
        transaction_id = str(uuid4())
        self.admin.acquire_error = AdminError("timeout", uncertain=True)
        with self.assertRaises(AdminError):
            self.hooks.acquire_maintenance(snapshot, transaction_id=transaction_id)
        self.instances.close(self.profile)
        result = self.hooks.verify_recovery({"transaction_id": transaction_id}, {})
        self.assertTrue(result["maintenance_released"])
        self.assertEqual([m for m, _ in self.admin.calls].count("manager/maintenance/acquire"), 1)

    def test_restore_verifies_profile_runtime_and_requests_view_without_running_turn(self):
        lease, snapshot = self.acquire()
        self.assertTrue(self.hooks.close_instance(snapshot))
        # The new proxy has no previous-generation lease; profile starts normally.
        self.admin.owner = None
        self.admin.parentage = {}
        result = self.hooks.restore_instance({
            "profile_id": self.profile["id"], "thread_id": ROOT_THREAD,
            "host_id": "local", "source_store_id": "manager:" + self.profile["id"]})
        self.assertTrue(result["verified"])
        self.assertTrue(result["profile_reopened"])
        self.assertFalse(result["selection_verified"])
        self.assertEqual(result["conversation_restore"], "requested")
        self.assertEqual(self.navigation[0][1]["thread_id"], ROOT_THREAD)
        self.assertFalse(any(m.startswith("turn/") for m, _ in self.admin.calls))
        self.hooks.release_maintenance(lease)

    def test_restore_wrong_account_is_not_success(self):
        self.instances.auth_ready = False
        result = self.hooks.restore_instance({"profile_id": self.profile["id"]})
        self.assertFalse(result["verified"])
        self.assertFalse(self.navigation)

    def test_recovery_never_guesses_installer_settled_from_version(self):
        self.assertNotIn("installer_settled", self.hooks.verify_recovery(
            {"transaction_id": str(uuid4()), "maintenance_state": "requested"}, {"version": "26.999.1.0"}))

    def build_compatibility_fixture(self):
        runtime = self.root / "artifacts/manager-runtime/releases/test/codex.exe"
        proxy = self.root / "artifacts/manager/releases/test/RuntimeProxy.exe"
        for path in (runtime, proxy, self.root / "scripts/manager_core/runtime_admin.py",
                     self.root / "scripts/manager_core/runtime_proxy.py"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture bytes " + path.name.encode())
        from manager_core.update_hooks import _hash
        self.hooks.runtime_resolver = lambda _: {
            "runtime": str(runtime), "sha256": _hash(runtime),
            "capabilities": {"managed_close_idle": True, "managed_idle_status": True}}
        atomic_json(self.root / "artifacts/manager/current.json", {"runtime_proxy": str(proxy)})
        fingerprint = self.hooks.compatibility_fingerprint()
        report = {"verified": True, "app_version": "26.904.1.0", **fingerprint}
        atomic_json(self.root / "work/fixture-verified.json", report)
        entry = {**fingerprint, "app_version": "26.904.1.0", "status": "verified",
                 "checks": {key: True for key in (
                     "initialize", "managed_idle_status", "managed_close_idle", "proxy_maintenance", "auth_binding")},
                 "evidence_path": "work/fixture-verified.json"}
        atomic_json(self.root / "artifacts/manager-runtime/update-compatibility.json",
                    {"version": 1, "entries": [entry]})
        return entry

    def test_compatibility_requires_exact_version_runtime_and_proxy_hashes(self):
        self.assertFalse(self.hooks.verify_compatibility({"version": "26.904.1.0"})["compatible"])
        self.build_compatibility_fixture()
        self.assertTrue(self.hooks.verify_compatibility({"version": "26.904.1.0"})["compatible"])
        self.assertFalse(self.hooks.verify_compatibility({"version": "26.905.1.0"})["compatible"])
        (self.root / "scripts/manager_core/runtime_admin.py").write_text("changed")
        self.assertFalse(self.hooks.verify_compatibility({"version": "26.904.1.0"})["compatible"])

    def test_compatibility_evidence_cannot_escape_workspace(self):
        entry = self.build_compatibility_fixture()
        entry["evidence_path"] = str(self.root.parent / "other-report.json")
        atomic_json(self.root / "artifacts/manager-runtime/update-compatibility.json",
                    {"version": 1, "entries": [entry]})
        self.assertFalse(self.hooks.verify_compatibility({"version": "26.904.1.0"})["compatible"])


if __name__ == "__main__":
    unittest.main()
