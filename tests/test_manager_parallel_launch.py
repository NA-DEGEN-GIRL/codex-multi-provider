"""Parallel profile launches use fixture stores and file locks, never apps or SSH."""
import itertools
import json
from pathlib import Path
import sys
import tempfile
import threading
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import browser_bundle, canonical_storage
from manager_core.instances import Instances, SharedCheck
from manager_core.providers import ProviderRegistry
from manager_core.store import Store, atomic_json
from manager_core.update_hooks import UpdateHooks
from manager_core.updates import UpdateError, _lock_file, _unlock_file


class CountingLock:
    """SharedCheck's guard, counting entries: one per caller, one per started check."""

    def __init__(self):
        self.lock, self.count, self.changed = threading.Lock(), 0, threading.Condition()

    def __enter__(self):
        self.lock.acquire()
        with self.changed:
            self.count += 1
            self.changed.notify_all()

    def __exit__(self, *_):
        self.lock.release()


class BlockingInstances:
    """Records admitted launches; each holds admission until released."""

    def __init__(self, store):
        self.store, self.hooks = store, None
        self.entered, self.release = {}, {}
        self.active, self.peak, self.shown = set(), [0], []
        self.lock = threading.Lock()
        self.exclusive = False

    def gate(self, profile_id):
        self.entered.setdefault(profile_id, threading.Event())
        return self.release.setdefault(profile_id, threading.Event())

    def launch_requires_exclusive(self):
        return self.exclusive

    def paths(self, profile):
        expected = self.store.directory / 'profiles' / profile['id']
        assert Path(profile['home']) == expected / 'codex'

    def show(self, profile_id, *, reopen_existing=True, wait_for_window=True):
        release = self.gate(profile_id)
        with self.hooks.launch_admission(profile_id):
            with self.lock:
                self.active.add(profile_id)
                self.peak[0] = max(self.peak[0], len(self.active))
            try:
                self.entered[profile_id].set()
                if not release.wait(5):
                    raise TimeoutError('fixture launch was not released')
                generation = str(uuid4())
                profile = self.store.mutate(lambda data: self.store.profile(profile_id, data).update(
                    generation=generation) or dict(self.store.profile(profile_id, data)))
                self.hooks.authorize_restoration_generation(profile)
                self.shown.append(profile_id)
                return dict(state='launched', profile_id=profile_id, profile=profile)
            finally:
                with self.lock:
                    self.active.discard(profile_id)

    def finish_show(self, result):
        return result


class Call:
    def __init__(self, target):
        self.results, self.errors, self.done = [], [], threading.Event()
        self.thread = threading.Thread(target=self.run, args=(target,))
        self.thread.start()

    def run(self, target):
        try:
            self.results.append(target())
        except Exception as error:
            self.errors.append(error)
        finally:
            self.done.set()


class ParallelLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profiles = [self.store.add_profile(f'parallel {index}')['id'] for index in range(4)]
        self.instances = BlockingInstances(self.store)
        self.hooks = UpdateHooks(self.root, self.store, self.instances)
        self.instances.hooks = self.hooks
        self.calls = []
        self.enterContext(patch('subprocess.Popen',
            side_effect=AssertionError('fixture must not launch an app or SSH')))
        self.enterContext(patch('manager_core.rust_service.launch',
            side_effect=AssertionError('fixture must not launch through the service')))
        self.addCleanup(self.drain)

    def drain(self):
        for event in self.instances.release.values():
            event.set()
        for call in self.calls:
            call.thread.join(5)

    def call(self, target):
        call = Call(target)
        self.calls.append(call)
        return call

    def launch(self, profile_id):
        self.instances.gate(profile_id)
        return self.call(lambda: self.instances.show(profile_id))

    def queued(self, count):
        with self.hooks._launch_queue.condition:
            self.assertTrue(self.hooks._launch_queue.condition.wait_for(
                lambda: len(self.hooks._launch_queue.waiting) == count, 2))

    def assert_cross_process_lock_held(self, held):
        try:
            lock = _lock_file(self.hooks.directory / 'launch-admission.lock')
        except UpdateError:
            self.assertTrue(held, 'the cross-process lock stayed held')
            return
        _unlock_file(lock)
        self.assertFalse(held, 'another process could interleave with an admitted launch')

    def test_two_profiles_prepare_together_while_maintenance_waits_then_excludes(self):
        first, second, late = self.profiles[:3]
        a, b = self.launch(first), self.launch(second)
        self.assertTrue(self.instances.entered[first].wait(2))
        self.assertTrue(self.instances.entered[second].wait(2), 'the second profile waited for the first')
        self.assertEqual(self.instances.peak[0], 2)
        self.assert_cross_process_lock_held(True)
        maintenance = self.call(lambda: self.hooks._begin_global(str(uuid4())))
        self.queued(1)
        after = self.launch(late)
        self.queued(2)
        self.instances.release[first].set()
        self.assertTrue(a.done.wait(2))
        self.assertFalse(maintenance.done.wait(.2), 'maintenance began beside an admitted launch')
        self.assertNotIn('update_maintenance', self.store.read())
        self.instances.release[second].set()
        self.assertTrue(maintenance.done.wait(2))
        self.assertTrue(after.done.wait(2))
        self.assertEqual(maintenance.errors, [])
        self.assertEqual(self.store.read()['update_maintenance']['state'], 'held')
        self.assertEqual([type(e) for e in after.errors], [UpdateError])
        self.assertEqual(after.errors[0].code, 'update_maintenance')
        self.assertFalse(self.instances.entered[late].is_set())
        self.assertEqual(sorted(self.instances.shown), sorted([first, second]))
        self.assert_cross_process_lock_held(False)

    def test_instances_show_prepares_different_profiles_in_parallel(self):
        instances = Instances(self.root, self.store, None)
        hooks = UpdateHooks(self.root, self.store, instances)
        instances.launch_admission = hooks.launch_admission
        entered = {profile_id: threading.Event() for profile_id in self.profiles[:2]}
        release = threading.Event()
        self.addCleanup(release.set)
        def prepare_and_spawn(profile_id, **_):
            entered[profile_id].set()
            if not release.wait(5):
                raise TimeoutError('fixture preparation was not released')
            return dict(state='existing', profile_id=profile_id, profile=self.store.profile(profile_id))
        with patch.object(instances, '_show', side_effect=prepare_and_spawn):
            calls = [self.call(lambda p=p: instances.show(p, wait_for_window=False)) for p in entered]
            for event in entered.values():
                self.assertTrue(event.wait(2), 'profile preparation waited for another profile')
            self.assertFalse(instances.launch_requires_exclusive())
            release.set()
            for call in calls:
                self.assertTrue(call.done.wait(2))
                self.assertEqual(call.errors, [])
        self.assertEqual(instances.launch_status()['active'], 0)
        self.assertEqual(hooks._launch_queue.held, {})

    def test_same_profile_and_pending_migration_still_launch_one_at_a_time(self):
        profile, other = self.profiles[:2]
        first, again = self.launch(profile), self.launch(profile)
        self.assertTrue(self.instances.entered[profile].wait(2))
        self.queued(1)
        peer = self.launch(other)
        self.assertTrue(self.instances.entered[other].wait(2), 'a waiting same-profile launch blocked another')
        self.instances.release[other].set()
        self.assertEqual(self.instances.peak[0], 2)
        self.instances.release[profile].set()
        for call in (first, again, peer):
            self.assertTrue(call.done.wait(2))
            self.assertEqual(call.errors, [])
        self.assertEqual(self.instances.shown.count(profile), 2)
        # While the one-time history migration may still run, launches are exclusive.
        self.instances.exclusive = True
        self.instances.peak[0] = 0
        self.instances.release.clear()
        self.instances.entered.clear()
        serial = [self.launch(self.profiles[2])]
        self.assertTrue(self.instances.entered[self.profiles[2]].wait(2))
        serial.append(self.launch(self.profiles[3]))
        self.queued(1)
        self.assertFalse(self.instances.entered[self.profiles[3]].wait(.2))
        for profile_id in self.profiles[2:]:
            self.instances.release[profile_id].set()
        for call in serial:
            self.assertTrue(call.done.wait(2))
            self.assertEqual(call.errors, [])
        self.assertEqual(self.instances.peak[0], 1)

    def test_pending_migration_lets_a_clicked_profile_go_ahead_of_queued_warmup(self):
        self.instances.exclusive = True
        running, *warmup, clicked = self.profiles
        calls = [self.launch(running)]
        self.assertTrue(self.instances.entered[running].wait(2))
        for index, profile_id in enumerate([*warmup, clicked]):
            calls.append(self.launch(profile_id))
            self.queued(index + 1)
        self.hooks.prioritize_launch(clicked)
        self.instances.release[running].set()
        self.assertTrue(self.instances.entered[clicked].wait(2), 'the clicked profile waited behind warmup')
        self.assertFalse(any(self.instances.entered[p].is_set() for p in warmup))
        for profile_id in (clicked, *warmup):
            self.instances.release[profile_id].set()
        for call in calls:
            self.assertTrue(call.done.wait(2))
            self.assertEqual(call.errors, [])
        self.assertEqual(self.instances.shown, [running, clicked, *warmup])
        self.assertEqual(self.instances.peak[0], 1)

    def test_guard_rechecks_at_admission_after_scoped_maintenance(self):
        running, scoped, other = self.profiles[:3]
        a = self.launch(running)
        self.assertTrue(self.instances.entered[running].wait(2))
        transaction = str(uuid4())
        maintenance = self.call(lambda: self.hooks._begin_global(transaction, [scoped]))
        self.queued(1)
        blocked, allowed = self.launch(scoped), self.launch(other)
        self.queued(3)
        self.instances.release[running].set()
        for call in (a, maintenance, blocked):
            self.assertTrue(call.done.wait(2))
        self.assertEqual(maintenance.errors, [])
        self.assertEqual(self.store.read()['profile_maintenance'][scoped]['transaction_id'], transaction)
        self.assertEqual(blocked.errors[0].code, 'update_maintenance')
        self.assertFalse(self.instances.entered[scoped].is_set())
        self.assertTrue(self.instances.entered[other].wait(2), 'scoped maintenance blocked another profile')
        self.instances.release[other].set()
        self.assertTrue(allowed.done.wait(2))
        self.assertEqual(allowed.errors, [])

    def test_restoration_owner_stays_with_its_worker_during_parallel_launches(self):
        restored, ordinary = self.profiles[:2]
        transaction = str(uuid4())
        self.hooks._begin_global(transaction)
        def restore():
            self.hooks._restoration.transaction_id = transaction
            try:
                with self.hooks.launch_admission(restored):
                    return self.instances.show(restored)
            finally:
                self.hooks._restoration.transaction_id = None
        self.instances.gate(restored)
        restoring = self.call(restore)
        self.assertTrue(self.instances.entered[restored].wait(2))
        # Admitted beside the restore, but it does not inherit its permit.
        other = self.launch(ordinary)
        self.assertTrue(other.done.wait(2))
        self.assertEqual(other.errors[0].code, 'update_maintenance')
        same = self.launch(restored)
        self.queued(1)
        self.instances.release[restored].set()
        self.assertTrue(restoring.done.wait(2))
        self.assertTrue(same.done.wait(2))
        self.assertEqual(restoring.errors, [])
        generation = restoring.results[0]['profile']['generation']
        gate = self.store.read()['update_maintenance']
        self.assertEqual(gate['restoring_generations'], {restored: generation})
        self.assertEqual(same.errors[0].code, 'update_maintenance')
        self.assertEqual(self.instances.shown, [restored])

    def test_ssh_gate_is_published_inside_the_fence_beside_other_launches(self):
        ssh_profile, other = self.profiles[:2]
        self.instances.gate(ssh_profile)
        opened = self.call(lambda: self.hooks.open_local_for_remote_reconcile(ssh_profile))
        self.assertTrue(self.instances.entered[ssh_profile].wait(2))
        gate = self.store.read()['ssh_maintenance'][ssh_profile]
        self.assertEqual(gate['state'], 'held')
        peer = self.launch(other)
        self.assertTrue(self.instances.entered[other].wait(2), 'the SSH open blocked another profile')
        same = self.launch(ssh_profile)
        maintenance = self.call(lambda: self.hooks._begin_global(str(uuid4())))
        self.queued(2)
        self.instances.release[other].set()
        self.assertTrue(peer.done.wait(2))
        self.assertFalse(maintenance.done.wait(.2))
        self.instances.release[ssh_profile].set()
        self.assertTrue(opened.done.wait(2))
        self.assertEqual(opened.errors, [])
        shown, transaction = opened.results[0]
        gate = self.store.read()['ssh_maintenance'][ssh_profile]
        self.assertEqual((gate['transaction_id'], gate['generation']),
                         (transaction, shown['profile']['generation']))
        self.assertTrue(maintenance.done.wait(2))
        # The published SSH gate still excludes a full update.
        self.assertEqual(maintenance.errors[0].code, 'profile_maintenance')
        self.assertTrue(same.done.wait(2))
        self.assertEqual(same.errors, [])
        self.assertEqual(self.instances.shown.count(ssh_profile), 2)
        self.assertEqual(self.hooks._launch_queue.held, {})


class MigrationAndBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'original'
        self.home.mkdir()

    def complete(self, home):
        marker = canonical_storage.marker_path(self.root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(dict(version=1, state='complete', home=str(home))), encoding='utf8')

    def test_completed_migration_is_read_by_parallel_launches_without_the_guard(self):
        self.complete(self.home)
        results, errors = [], []
        def migrate():
            try:
                results.append(canonical_storage.migrate(self.root, self.home))
            except Exception as error:
                errors.append(error)
        with patch('manager_core.authority._authority_guard',
                   side_effect=RuntimeError('another caller holds the guard')):
            workers = [threading.Thread(target=migrate) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
        self.assertEqual(errors, [])
        self.assertEqual([r['state'] for r in results], ['complete'] * 8)
        # An incomplete migration still takes the guard and its closed-profile check.
        canonical_storage.marker_path(self.root).unlink()
        with patch('manager_core.authority._authority_guard',
                   side_effect=RuntimeError('guarded')), self.assertRaisesRegex(RuntimeError, 'guarded'):
            canonical_storage.migrate(self.root, self.home)

    def test_launches_are_exclusive_only_until_an_enabled_migration_completes(self):
        instances = Instances(self.root, Store(self.root), None)
        capabilities = dict(capabilities=dict(canonical_record_storage=True))
        with patch('manager_core.runtime_build.resolve', return_value=capabilities):
            self.assertTrue(instances.launch_requires_exclusive())
            # The marker names the default original home; nothing reads that home.
            self.complete(Path.home() / '.codex')
            self.assertFalse(instances.launch_requires_exclusive())
        canonical_storage.marker_path(self.root).unlink()
        with patch('manager_core.runtime_build.resolve', return_value=dict(capabilities={})):
            self.assertFalse(instances.launch_requires_exclusive())
        with patch('manager_core.runtime_build.resolve', side_effect=RuntimeError('unverified runtime')):
            self.assertTrue(instances.launch_requires_exclusive())

    def test_desktop_check_is_shared_only_by_callers_that_asked_before_it_started(self):
        shared = SharedCheck()
        counted = shared.lock = CountingLock()
        started, release, runs = threading.Event(), threading.Event(), []
        def check():
            runs.append(len(runs))
            started.set()
            if not release.wait(5):
                raise TimeoutError('fixture check was not released')
            return runs[-1]
        key = str(uuid4())
        first = Call(lambda: shared(key, check))
        self.assertTrue(started.wait(2))
        # These ask while the first check runs, so they need a later check, which
        # they all share. The first caller entered the lock twice (join, start).
        later = [Call(lambda: shared(key, check)) for _ in range(5)]
        with counted.changed:
            self.assertTrue(counted.changed.wait_for(lambda: counted.count == 7, 2))
        self.assertTrue(all(not call.done.is_set() for call in later))
        release.set()
        for call in (first, *later):
            self.assertTrue(call.done.wait(3))
            self.assertEqual(call.errors, [])
        self.assertEqual(first.results, [0])
        self.assertEqual([call.results[0] for call in later], [1] * 5)
        self.assertEqual(len(runs), 2)

        def fail():
            raise ValueError('desktop copy changed')
        with self.assertRaisesRegex(ValueError, 'desktop copy changed'):
            shared(key, fail)
        self.assertEqual(shared(key, lambda: 'recovered'), 'recovered')


class RealPreparationParallelTests(unittest.TestCase):
    """The real Instances._show preparation, with a fixture user home and app.

    Only package discovery, the desktop copy check, the environment and the
    process spawn are fakes; configuration, shared skills and plugins, Browser
    publication and identity publication run for three profiles at once.
    """

    VERSION = '26.999.1.0'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-parallel-show-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        user = self.root / 'user'
        self.source = user / '.codex'
        self.source.mkdir(parents=True)
        # A personal skill outside source/skills, so no profile junction is made.
        (user / '.agents/skills/shared-demo').mkdir(parents=True)
        (user / '.agents/skills/shared-demo/SKILL.md').write_text(
            '---\nname: shared-demo\ndescription: Fixture skill.\n---\n', encoding='utf-8')
        (self.source / 'config.toml').write_text(
            '[mcp_servers."fixture.tool"]\ncommand = "fixture-command"\n'
            '[desktop]\nappearanceTheme = "dark"\n', encoding='utf-8')
        # Nothing may read or write the real user's home or launch anything.
        self.enterContext(patch.object(Path, 'home', return_value=user))
        self.app = self.root / 'app/Codex.exe'
        browser = self.app.parent / 'resources/plugins/openai-bundled/plugins/browser'
        for name in browser_bundle._REQUIRED:
            (browser / name).parent.mkdir(parents=True, exist_ok=True)
            (browser / name).write_text(name, encoding='utf-8')
        (browser / '.codex-plugin/plugin.json').write_text(
            json.dumps(dict(name='browser', version='1.2.3')), encoding='utf-8')
        self.app.write_bytes(b'fixture executable, never started')
        self.store = Store(self.root)
        self.profiles = [self.store.add_profile(f'real {index}')['id'] for index in range(3)]
        atomic_json(self.store.directory / 'personal-skills.json', dict(version=1, skills={}, observations={}))
        self.instances = Instances(self.root, self.store, ProviderRegistry(self.root))
        self.hooks = UpdateHooks(self.root, self.store, self.instances)
        self.instances.launch_admission = self.hooks.launch_admission
        pids = itertools.count(40000)
        def spawn(profile, executable, env, **_):
            self.assertEqual(Path(executable), self.app)
            return SimpleNamespace(pid=next(pids))
        def identity(pid):
            return None if pid is None else dict(process_id=pid, process_created=pid * 7,
                                                 executable_path=str(self.app))
        self.enterContext(patch('manager_core.runtime_build.resolve', return_value=dict(capabilities={})))
        self.enterContext(patch('subprocess.Popen',
            side_effect=AssertionError('fixture must not start any process')))
        self.enterContext(patch('manager_core.rust_service.enabled', return_value=True))
        self.enterContext(patch('manager_core.rust_service.launch', side_effect=spawn))
        self.enterContext(patch('manager_core.instances.process_identity', side_effect=identity))
        self.enterContext(patch.object(self.instances, 'installed_app',
            return_value=dict(executable=str(self.app), Version=self.VERSION)))
        self.enterContext(patch.object(self.instances, 'environment',
            side_effect=lambda profile: dict(CODEX_HOME=profile['home'])))

    def test_real_show_prepares_profiles_together_sharing_the_desktop_check(self):
        checks = self.instances._desktop_checks.lock = CountingLock()
        runs = []
        def check_desktop(root, installed):
            runs.append(installed)
            if len(runs) == 1:
                # The two other launches ask while the first check runs:
                # 2 entries for the first caller, 1 for each later caller.
                with checks.changed:
                    if not checks.changed.wait_for(lambda: checks.count >= 4, 10):
                        raise TimeoutError('other launches did not reach the desktop check')
            return dict(executable=installed['executable'], Version=installed['Version'],
                        desktop_isolation_revision='fixture')
        publish, together, held = browser_bundle._publish, threading.Barrier(3, timeout=10), []
        def publish_together(source, parent, *args, **kwargs):
            # Every profile publishes inside its own home's Browser lock at once.
            try:
                _unlock_file(_lock_file(parent / '.manager-browser.lock'))
            except UpdateError:
                held.append(parent)
            together.wait()
            return publish(source, parent, *args, **kwargs)
        with patch('manager_core.desktop_bundle.prepare', side_effect=check_desktop), \
                patch.object(browser_bundle, '_publish', side_effect=publish_together):
            calls = [Call(lambda p=p: self.instances.show(p, wait_for_window=False)) for p in self.profiles]
            self.addCleanup(lambda: [call.thread.join(30) for call in calls])  # Before the home patch ends.
            for call in calls:
                self.assertTrue(call.done.wait(30))
        for call in calls:
            self.assertEqual(call.errors, [])
        self.assertEqual(len(runs), 2, 'three launches did not share the later desktop check')
        self.assertEqual(len(held), 3)
        self.assertEqual(self.hooks._launch_queue.held, {})
        self.assertEqual(self.instances.launch_status()['active'], 0)
        for call in calls:
            result = call.results[0]
            self.assertEqual(result['state'], 'launched')
            common = result['preparation']['common']
            self.assertEqual(common['personal_skills']['errors'], [])
            self.assertEqual(common['shared_plugins']['errors'], [])
            self.assertEqual(result['preparation']['browser_plugin']['state'], 'prepared')
            home = Path(result['profile']['home'])
            config = tomllib.loads((home / 'config.toml').read_text(encoding='utf-8'))
            # Every writer's edit survived the others' passes over this home.
            self.assertIn('fixture.tool', config['mcp_servers'])
            self.assertEqual(config['desktop']['appearanceTheme'], 'dark')
            self.assertEqual(config['subagent_model_selection'], 'automatic')
            self.assertTrue(any(rule.get('path', '').endswith('SKILL.md') for rule in config['skills']['config']))
            self.assertTrue((home / 'plugins/cache/openai-bundled/browser/1.2.3/scripts/browser-service.mjs').is_file())
        stored = {p['id']: p for p in self.store.read()['profiles']}
        self.assertEqual({stored[p]['status'] for p in self.profiles}, {'running'})
        self.assertEqual(len({stored[p]['process_id'] for p in self.profiles}), 3)


if __name__ == '__main__':
    unittest.main()
