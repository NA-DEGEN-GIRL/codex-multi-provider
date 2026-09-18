import copy
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_center import ControlCenter
from manager_core import catalog
from manager_core import catalog_frames
from manager_core.remote_catalog import RemoteCatalog, groups
from manager_core.store import Store, atomic_json

spec = importlib.util.spec_from_file_location('remote_catalog_reader_test', ROOT / 'scripts/remote_helpers/catalog_reader.py')
helper = importlib.util.module_from_spec(spec)
legacy_spec = importlib.util.spec_from_file_location('catalog_legacy', ROOT / 'scripts/remote_helpers/catalog_legacy.py')
legacy = importlib.util.module_from_spec(legacy_spec)
legacy_spec.loader.exec_module(legacy)
with patch.dict(sys.modules, catalog=catalog, catalog_legacy=legacy):
    spec.loader.exec_module(helper)


def register(store, profile, alias='remote-dev', identity='a' * 64):
    base = '/home/fixture/.local/share/codex-control-center/profiles/' + profile['id']
    binding = dict(profile_id=profile['id'], alias=alias, revision='b' * 64, prepared=True,
        remote_python='/usr/bin/python3', remote_launcher=base + '/launch.py', remote_profile_home=base + '/codex',
        host_identity=identity)
    def save(data):
        store.profile(profile['id'], data).setdefault('remote_bindings', []).append(binding)
        Store.remote_source(data, binding, profile['alias'])
    store.mutate(save)
    return binding


class RemoteCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profiles = [self.store.add_profile(alias) for alias in ('04', '02')]
        for profile in self.profiles:
            register(self.store, profile)
        self.rows = [dict(thread_id=str(uuid4()), source_store_id='manager:' + p['id'], title='SSH ' + p['alias'],
                         cwd='/home/fixture/work', updated_at=1789300000, archived=False) for p in self.profiles]
        self.calls = []
        self.cache = RemoteCatalog(self.root, self.store, object(), reader=self.read, interval=.02)
        self.addCleanup(self.cache.stop)

    def read(self, group):
        self.calls.append(copy.deepcopy(group))
        return dict(conversations=copy.deepcopy(self.rows), errors=[])

    def done(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.cache._lock:
                if not self.cache._workers:
                    return
            time.sleep(.005)
        self.fail('SSH catalog worker did not finish')

    def refresh(self):
        with self.cache._lock:
            self.cache._next.clear()
        self.cache.refresh()
        self.done()

    def test_refresh_groups_profiles_without_mutating_state_and_lists_do_no_ssh(self):
        before = self.store.read()
        self.assertEqual(self.cache.snapshot()['conversations'], [])
        self.assertEqual(self.calls, [])
        self.refresh()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0]['sources']), 2)
        value = self.cache.snapshot()
        self.assertEqual(len(value['conversations']), 2)
        self.assertTrue(all(r['host_id'] == 'ssh:remote-dev' for r in value['conversations']))
        self.assertEqual(self.store.read(), before)
        self.assertEqual(len(self.calls), 1)

    def test_background_monitor_updates_titles_and_new_records_without_ui_requests(self):
        self.cache.start()
        try:
            deadline = time.monotonic() + 3
            while not self.cache.snapshot()['conversations'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.rows = [{**self.rows[0], 'title': 'Changed'}, {**self.rows[1], 'thread_id': str(uuid4())}]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                titles = {r['title'] for r in self.cache.snapshot()['conversations']}
                if 'Changed' in titles:
                    break
                time.sleep(.01)
            self.assertIn('Changed', titles)
        finally:
            self.cache.stop()
            self.done()

    def test_slow_host_does_not_block_cached_reads_or_start_duplicate_queries(self):
        entered, release = threading.Event(), threading.Event()
        def slow(group):
            entered.set()
            release.wait(5)
            return self.read(group)
        self.cache.reader = slow
        self.cache.refresh()
        try:
            self.assertTrue(entered.wait(1))
            for _ in range(3):
                self.cache.refresh()
                self.assertTrue(self.cache.snapshot()['hosts'][0]['refreshing'])
            self.assertEqual(self.calls, [])
        finally:
            release.set()
            self.done()
        self.assertEqual(len(self.calls), 1)

    def test_offline_or_partial_error_keeps_previous_records_and_marks_them_stale(self):
        self.refresh()
        self.cache.reader = lambda _: (_ for _ in ()).throw(OSError('fixture offline'))
        self.refresh()
        result = self.cache.snapshot()
        self.assertEqual(len(result['conversations']), 2)
        self.assertTrue(result['hosts'][0]['stale'])
        self.cache.reader = lambda _: dict(conversations=[{**self.rows[0], 'title': 'New title'}],
            errors=[self.rows[1]['source_store_id']])
        self.refresh()
        self.assertEqual({r['title'] for r in self.cache.snapshot()['conversations']}, {'New title', 'SSH 02'})

    def test_slow_host_does_not_delay_another_host(self):
        register(self.store, self.profiles[0], alias='second-host')
        entered, release = threading.Event(), threading.Event()
        def read(group):
            if group['alias'] == 'remote-dev':
                entered.set()
                release.wait(5)
            source_ids = {s['id'] for s in group['sources']}
            return dict(conversations=[r for r in self.rows if r['source_store_id'] in source_ids], errors=[])
        self.cache.reader = read
        self.cache.refresh()
        try:
            self.assertTrue(entered.wait(1))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rows = self.cache.snapshot()['conversations']
                if rows:
                    break
                time.sleep(.005)
            self.assertEqual({r['host_id'] for r in rows}, {'ssh:second-host'})
            self.assertTrue(next(h for h in self.cache.snapshot()['hosts'] if h['host_id'] == 'ssh:remote-dev')['refreshing'])
        finally:
            release.set()
            self.done()

    def test_old_disk_cache_is_marked_stale_until_a_fresh_read(self):
        self.refresh()
        path = self.cache._path('remote-dev')
        with path.open('rb') as stream:
            saved = catalog_frames.read(stream)
        saved['observed_at'] = '2026-01-01T00:00:00+00:00'
        catalog_frames.atomic_write(path, saved)
        restarted = RemoteCatalog(self.root, self.store, object())
        self.assertTrue(restarted.snapshot()['hosts'][0]['stale'])
        self.assertTrue(all(r['catalog_stale'] for r in restarted.snapshot()['conversations']))

    def test_restarted_manager_reads_cache_and_rename_does_not_need_remote_sync(self):
        self.refresh()
        renamed = self.profiles[0]
        self.store.mutate(lambda data: [s.update(alias='Personal') for s in data['sources'] if s['id'] == 'manager:' + renamed['id']])
        replacement = RemoteCatalog(self.root, self.store, object(), reader=lambda _: self.fail('Snapshot must not connect'))
        self.assertIn('Personal', {r['source_alias'] for r in replacement.snapshot()['conversations']})
        self.assertEqual(len(self.calls), 1)

    def test_changed_host_identity_does_not_reuse_old_host_records(self):
        self.refresh()
        self.store.mutate(lambda data: [b.update(host_identity='c' * 64) for p in data['profiles'] for b in p.get('remote_bindings', [])])
        self.assertEqual(self.cache.snapshot()['conversations'], [])

    def test_conflicting_registered_host_identities_do_not_connect_or_show_cached_rows(self):
        self.store.mutate(lambda data: data['profiles'][0]['remote_bindings'][0].update(host_identity='c' * 64))
        self.refresh()
        self.assertFalse(self.calls)
        self.assertEqual(self.cache.snapshot()['conversations'], [])
        self.assertIn('등록 정보', self.cache.snapshot()['hosts'][0]['message'])

    def test_foreign_source_response_is_rejected_and_old_records_survive(self):
        self.refresh()
        self.rows[0]['source_store_id'] = 'manager:' + str(uuid4())
        self.refresh()
        self.assertEqual(len(self.cache.snapshot()['conversations']), 2)
        self.assertTrue(self.cache.snapshot()['hosts'][0]['stale'])

    def test_control_center_merges_local_and_remote_and_shortcut_keeps_qualified_identity(self):
        center = ControlCenter(self.root)
        center.remote_catalog = self.cache
        local_home = Path(self.profiles[0]['home'])
        local_home.mkdir(parents=True)
        local_id = str(uuid4())
        (local_home / 'session_index.jsonl').write_text(json.dumps(dict(id=local_id, title='Windows',
            updated_at='2026-09-14T22:00:00Z')) + '\n', encoding='utf-8')
        self.refresh()
        result = center.dispatch('catalog.list', {})
        self.assertEqual(len(result['conversations']), 3)
        self.assertEqual(result['conversations'][0]['thread_id'], local_id)
        row = next(r for r in result['conversations'] if r['host_id'].startswith('ssh:'))
        link = center.dispatch('shortcut.add', dict(alias='Remote work', profile_id=self.profiles[1]['id'],
            **{key: row[key] for key in ('thread_id', 'host_id', 'source_store_id')}))
        self.assertEqual(link['host_id'], 'ssh:remote-dev')
        self.assertEqual(link['source_store_id'], row['source_store_id'])
        self.assertEqual(len(self.calls), 1)

    def test_serving_backend_refreshes_remote_metadata_without_incoming_ui_requests(self):
        payload = self.root / 'fixture-remote-records.json'
        atomic_json(payload, dict(conversations=self.rows, errors=[]))
        wrapper = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
root=Path(sys.argv[2])
from manager_core.remote_catalog import RemoteCatalog
RemoteCatalog._read=lambda self,group: json.loads((root/'fixture-remote-records.json').read_text(encoding='utf-8'))
from control_center import main
sys.argv=['control_center','--serve','--root',str(root)]
main()
'''
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', wrapper, str(ROOT / 'scripts'), str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.root,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def wait_title(title):
            deadline = time.monotonic() + 12
            path = self.cache._path('remote-dev')
            while time.monotonic() < deadline:
                try:
                    with path.open('rb') as stream:
                        rows = catalog_frames.read(stream)['conversations']
                    if title in {r['title'] for r in rows}:
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(.025)
            self.fail('Serving backend did not refresh the remote cache without a request')
        try:
            wait_title('SSH 04')
            changed = [{**self.rows[0], 'title': 'Background update'}, self.rows[1]]
            atomic_json(payload, dict(conversations=changed, errors=[]))
            wait_title('Background update')
            request = json.dumps(dict(id=1, command='catalog.list', args={})).encode() + b'\n'
            stdout, stderr = process.communicate(request, timeout=6)
            self.assertEqual((process.returncode, stderr), (0, b''))
            response = json.loads(stdout)
            self.assertTrue(response['ok'])
            self.assertIn('Background update', {r['title'] for r in response['result']['conversations']})
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


class RemoteCatalogReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = []
        for _ in range(2):
            profile = str(uuid4())
            home = self.root / '.local/share/codex-control-center/profiles' / profile / 'codex'
            home.mkdir(parents=True)
            atomic_json(home / 'managed-source.json', dict(host_id='local', store_id='manager:' + profile))
            self.sources.append(dict(id='manager:' + profile, home=str(home)))
        self.tid = str(uuid4())
        self.rollout = Path(self.sources[0]['home']) / 'sessions/fixture.jsonl'
        self.rollout.parent.mkdir()
        self.rollout.write_text('Fixture history; never read by the metadata query.', encoding='utf-8')
        for source in self.sources:
            with closing(sqlite3.connect(Path(source['home']) / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads(id TEXT, title TEXT, cwd TEXT, updated_at INTEGER, rollout_path TEXT)')
                db.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (self.tid, 'Title', '/work/project', 1789300000, str(self.rollout)))
        self.request = dict(host_identity='a' * 64, sources=self.sources)

    def read(self):
        return helper.read(self.request, user_home=self.root, identity='a' * 64)

    def test_reads_titles_and_origin_without_copying_or_changing_history(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.read()
        self.assertFalse(result['errors'])
        self.assertEqual(len(result['conversations']), 1)
        self.assertEqual(result['conversations'][0]['source_store_id'], self.sources[0]['id'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        with closing(sqlite3.connect(Path(self.sources[0]['home']) / 'state_5.sqlite')) as db, db:
            db.execute('UPDATE threads SET title=?,updated_at=?', ('Renamed', 1789300001))
        result = self.read()
        self.assertEqual(result['conversations'][0]['title'], 'Renamed')

    def test_host_or_source_scope_mismatch_is_rejected(self):
        self.request['host_identity'] = 'b' * 64
        with self.assertRaises(ValueError):
            self.read()
        self.request['host_identity'] = 'a' * 64
        self.sources[0]['home'] = str(self.root)
        with self.assertRaises(ValueError):
            self.read()

    def test_unavailable_database_is_an_error_not_an_empty_successful_source(self):
        path = Path(self.sources[0]['home']) / 'state_5.sqlite'
        path.write_bytes(b'Invalid database fixture')
        result = self.read()
        self.assertIn(self.sources[0]['id'], result['errors'])


if __name__ == '__main__':
    unittest.main()
