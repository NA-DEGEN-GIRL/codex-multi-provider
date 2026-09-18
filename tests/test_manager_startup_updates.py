"""Startup lifecycle checks use temporary stores, with no native UI or network."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.startup_updates import StartupUpdates, selected_manager_proxy
from manager_core.store import Store


class StartupUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('selected')
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(generation=str(uuid4())))
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d)['policy'].update(
            launched_revision=self.store.profile(self.profile['id'], d)['policy']['desired_revision']))
        self.profile = self.store.profile(self.profile['id'])
        self.statuses = {self.profile['id']: 'running'}
        self.versions = {self.profile['id']: dict(state='outdated', restart_required=True)}
        self.observations, self.scheduled, self.workers = [], [], []
        self.jobs = {}
        self.selected = dict(runtime=str(self.root / 'artifacts/manager-runtime/releases/new/codex.exe'),
                             sha256='verified-selected-digest', capabilities={'shared_record_execution': True})
        self.manager = None
        self.startup = StartupUpdates(self.root, self.store, self, self, spawn=self.workers.append,
            runtime_resolver=lambda _: self.selected, describe_runtime=self.describe,
            identity=lambda _: None, manager_resolver=lambda _: self.manager)

    def observe(self, profile):
        self.observations.append(profile['id'])
        return {'status': self.statuses.get(profile['id'], 'not_started')}

    def describe(self, root, profile, **kwargs):
        return self.versions.get(profile['id'], {'state': 'current', 'restart_required': False})

    def schedule(self, profile_id, **kwargs):
        self.scheduled.append(dict(profile_id=profile_id, **kwargs))
        return dict(phase='waiting', id=str(uuid4()))

    def status(self):
        return self.jobs

    def run_startup(self):
        self.startup.start()
        self.workers.pop()()
        return self.startup.status()

    def test_start_returns_before_process_checks_and_status_never_restarts(self):
        first = self.startup.start()
        self.assertEqual(first['state'], 'checking')
        self.assertTrue(first['worker_active'])
        self.assertEqual(self.startup.start(), first)
        self.assertEqual(len(self.workers), 1)
        self.assertFalse(self.observations)
        self.assertFalse(self.scheduled)
        snapshot = self.startup.status()
        snapshot['profiles'].append('caller edit')
        self.assertEqual(self.startup.status()['profiles'], first['profiles'])
        self.assertEqual(self.startup.status()['state'], first['state'])

    def test_only_outdated_running_profile_is_scheduled(self):
        self.store.add_profile('unused')
        result = self.run_startup()
        self.assertEqual(result['state'], 'applying')
        self.assertEqual(len(self.scheduled), 1)
        self.assertEqual(self.scheduled[0]['profile_id'], self.profile['id'])
        self.assertEqual(self.scheduled[0]['expected_generation'], self.profile['generation'])
        self.assertEqual(result['profiles'][1]['state'], 'latest_on_open')

    def test_current_profile_is_reused_without_restart(self):
        self.versions[self.profile['id']] = dict(state='current', restart_required=False)
        self.run_startup()
        self.assertFalse(self.scheduled)

    def test_no_release_change_does_not_invent_runtime_evidence(self):
        self.versions[self.profile['id']] = dict(state='unknown', restart_required=False)
        self.run_startup()
        self.assertFalse(self.scheduled)
        self.assertEqual(self.startup.status()['profiles'][0]['state'], 'unknown')

    def test_login_and_catalog_windows_are_not_interrupted(self):
        for changes in ({'runtime_channel': 'packaged'}, {'view_only': True}, {'removed_at': 'fixture'}):
            with self.subTest(changes=changes):
                def change(data):
                    p = self.store.profile(self.profile['id'], data)
                    p.update(runtime_channel='managed', view_only=False, removed_at=None)
                    p.update(changes)
                self.store.mutate(change)
                self.run_startup()
        self.assertFalse(self.scheduled)
        self.assertFalse(self.observations)

    def test_version_generation_and_policy_form_stable_deduplication_key(self):
        self.run_startup(); self.run_startup()
        self.assertEqual(self.scheduled[0]['automatic_key'], self.scheduled[1]['automatic_key'])
        old = self.scheduled[-1]['automatic_key']
        self.selected['sha256'] = 'another-verified-release'
        self.run_startup()
        self.assertNotEqual(old, self.scheduled[-1]['automatic_key'])
        old = self.scheduled[-1]['automatic_key']
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(generation=str(uuid4())))
        self.run_startup()
        self.assertNotEqual(old, self.scheduled[-1]['automatic_key'])

    def test_new_manager_helpers_are_applied_even_when_rust_version_is_current(self):
        self.versions[self.profile['id']] = dict(state='current', restart_required=False)
        self.manager = str(self.root / 'artifacts/manager/releases/new/RuntimeProxy.exe')
        self.run_startup()
        self.assertEqual(len(self.scheduled), 1)
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(manager_release=self.manager))
        self.run_startup()
        self.assertEqual(len(self.scheduled), 1)
        self.manager = str(self.root / 'artifacts/manager/releases/newer/RuntimeProxy.exe')
        self.run_startup()
        self.assertNotEqual(self.scheduled[0]['automatic_key'], self.scheduled[1]['automatic_key'])

    def test_shell_only_release_does_not_restart_profiles_with_same_frozen_runtime(self):
        self.versions[self.profile['id']] = dict(state='current', restart_required=False)
        for folder in ('one', 'two'):
            release = self.root / 'artifacts/manager/releases' / folder
            release.mkdir(parents=True)
            (release / 'runtime-manifest.json').write_text(json.dumps(dict(version=1, runtime_revision='a'*64)))
        self.manager = str(self.root / 'artifacts/manager/releases/two/RuntimeProxy.exe')
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d).update(
            manager_release=str(self.root / 'artifacts/manager/releases/one/RuntimeProxy.exe'),
            manager_runtime_revision='a'*64))
        self.run_startup()
        self.assertEqual(self.scheduled, [])
        (Path(self.manager).parent / 'runtime-manifest.json').write_text(json.dumps(dict(version=1, runtime_revision='b'*64)))
        self.run_startup()
        self.assertEqual(len(self.scheduled), 1)

    def test_login_failure_in_one_profile_does_not_stop_or_restart_it_or_block_others(self):
        broken = self.store.add_profile('03')
        self.store.mutate(lambda d: self.store.profile(broken['id'], d).update(source_home=str(self.root / 'missing-login')))
        self.statuses[broken['id']] = 'running'
        result = self.run_startup()
        self.assertEqual([p['profile_id'] for p in self.scheduled], [self.profile['id']])
        entry = next(p for p in result['profiles'] if p['profile_id'] == broken['id'])
        self.assertEqual(entry['state'], 'login_pending')
        self.assertIn('로그인', entry['message'])

    def test_changed_profile_settings_are_applied_without_a_new_runtime(self):
        self.versions[self.profile['id']] = dict(state='current', restart_required=False)
        self.store.mutate(lambda d: self.store.profile(self.profile['id'], d)['policy'].update(desired_revision=2))
        self.run_startup()
        self.assertEqual(len(self.scheduled), 1)

    def test_unfinished_automatic_restore_is_reconciled_even_if_new_runtime_already_started(self):
        self.versions[self.profile['id']] = dict(state='current', restart_required=False)
        self.store.mutate(lambda d: d.update(profile_restarts={self.profile['id']: {
            'phase': 'connecting', 'automatic_key': 'previous-launch', 'transaction_id': str(uuid4())}}))
        self.run_startup()
        self.assertEqual(len(self.scheduled), 1)

    def test_interrupted_automatic_update_between_close_and_open_is_reconciled(self):
        self.statuses[self.profile['id']] = 'not_started'
        self.store.mutate(lambda d: d.update(profile_restarts={self.profile['id']: {
            'phase': 'opening', 'automatic_key': 'previous-launch', 'transaction_id': str(uuid4())}}))
        self.run_startup()
        self.assertEqual(len(self.scheduled), 1)

    def test_failed_profile_does_not_block_other_profiles(self):
        peer = self.store.add_profile('peer')
        self.statuses[peer['id']] = 'running'
        def fail_first(root, profile, **kwargs):
            if profile['id'] == self.profile['id']:
                raise RuntimeError('private diagnostic')
            return dict(state='outdated', restart_required=True)
        self.startup.describe = fail_first
        result = self.run_startup()
        self.assertEqual([p['profile_id'] for p in self.scheduled], [peer['id']])
        self.assertEqual(result['profiles'][0]['state'], 'attention')
        self.assertNotIn('private diagnostic', json.dumps(result))

    def test_shutdown_before_background_check_does_not_schedule_a_restart(self):
        self.startup.start()
        self.startup.shutdown()
        self.workers.pop()()
        self.assertFalse(self.scheduled)
        self.assertEqual(self.startup.status()['state'], 'stopped')
        with self.assertRaises(RuntimeError):
            self.startup.start()

    def test_progress_tracks_all_profiles_and_refreshes_when_jobs_complete(self):
        peer = self.store.add_profile('peer')
        self.statuses[peer['id']] = 'running'
        self.versions[peer['id']] = dict(state='outdated', restart_required=True)
        stopped = self.store.add_profile('closed')
        first = self.run_startup()
        self.assertEqual({p['profile_id'] for p in self.scheduled}, {self.profile['id'], peer['id']})
        self.assertEqual(first['counts'], dict(current=1, pending=2, attention=0))
        for entry in first['profiles']:
            if entry.get('job_id'):
                self.jobs[entry['profile_id']] = dict(id=entry['job_id'], phase='complete', message='applied')
        after = self.startup.status()
        self.assertEqual(after['state'], 'complete')
        self.assertEqual(after['counts'], dict(current=3, pending=0, attention=0))
        self.assertEqual(len(self.scheduled), 2)

    def legacy(self, *profiles):
        candidates = []
        def save(data):
            for original in profiles:
                p = self.store.profile(original['id'], data)
                p.setdefault('generation', str(uuid4()))
                job = dict(id=str(uuid4()), phase='attention', code='runtime_state_unavailable')
                data.setdefault('profile_restarts', {})[p['id']] = job
                candidates.append(dict(profile_id=p['id'], generation=p['generation'], job_id=job['id']))
        self.store.mutate(save)
        return candidates

    def test_legacy_bulk_cleanup_is_generation_pinned_and_requires_confirmation(self):
        peer = self.store.add_profile('peer')
        candidates = self.legacy(self.profile, peer)
        with self.assertRaises(ValueError): self.startup.recover_legacy(candidates)
        self.assertFalse(self.workers)
        stopped, opened = [], []
        self.startup.stopper = lambda store, instances, pid, **kwargs: stopped.append((pid, kwargs))
        self.show = lambda pid, **kwargs: opened.append(pid)
        self.startup.recover_legacy(candidates, interrupt_running_work=True)
        self.workers.pop()()
        self.assertEqual([p for p, _ in stopped], [self.profile['id'], peer['id']])
        self.assertEqual(opened, [self.profile['id'], peer['id']])
        for (pid, options), expected in zip(stopped, candidates):
            self.assertEqual(options, dict(expected_generation=expected['generation'], interrupt_running_work=True))

    def test_stale_or_unlisted_recovery_target_does_not_schedule_any_stop(self):
        candidates = self.legacy(self.profile)
        candidates[0]['generation'] = str(uuid4())
        with self.assertRaises(ValueError): self.startup.recover_legacy(candidates, interrupt_running_work=True)
        self.assertFalse(self.workers)

    def test_explicit_retry_rechecks_failed_jobs_for_every_profile(self):
        self.startup.start(retry_failed=True)
        self.workers.pop()()
        self.assertTrue(self.scheduled[0]['retry_failed'])

    def test_manager_selection_requires_an_installed_owned_binary(self):
        self.assertIsNone(selected_manager_proxy(self.root))
        path = self.root / 'artifacts/manager/current.json'
        path.parent.mkdir(parents=True)
        binary = path.parent / 'releases/new/RuntimeProxy.exe'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'fixture')
        path.write_text(json.dumps({'runtime_proxy': str(binary)}), encoding='utf-8')
        self.assertEqual(Path(selected_manager_proxy(self.root)), binary)
        external = self.root / 'foreign.exe'
        external.write_bytes(b'fixture')
        path.write_text(json.dumps({'runtime_proxy': str(external)}), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            selected_manager_proxy(self.root)


if __name__ == '__main__':
    unittest.main()
