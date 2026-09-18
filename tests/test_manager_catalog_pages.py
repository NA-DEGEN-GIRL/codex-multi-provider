"""Large metadata snapshots, complete picker search and stable page traversal."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_center import ControlCenter
from manager_core import catalog, catalog_frames
from manager_core.catalog_pages import page
from manager_core.remote_catalog import RemoteCatalog, groups
from manager_core.store import atomic_json
from test_manager_remote_catalog import helper, register


class CatalogPageTests(unittest.TestCase):
    def test_long_metadata_is_split_by_bytes_and_old_shell_response_is_bounded(self):
        from manager_core.catalog_pages import legacy_view
        rows = [dict(thread_id=str(UUID(int=i + 1)), host_id='ssh:fixture', source_store_id='same',
                     title='대' * 512, cwd='경' * 4096, source_home='로' * 4096, updated_at=i) for i in range(500)]
        value = dict(conversations=rows)
        first = page(value, dict(limit=500), 'scope')
        self.assertLess(len(first['conversations']), 500)
        self.assertLess(len(json.dumps(first, ensure_ascii=False).encode()), 8 * 1024 * 1024)
        following = page(value, dict(limit=500, cursor=first['next_cursor']), 'scope')
        self.assertEqual(len(first['conversations']) + len(following['conversations']), 500)
        older = legacy_view(value)
        self.assertTrue(older['possibly_truncated'])
        self.assertLess(len(json.dumps(older, ensure_ascii=False).encode()), 8 * 1024 * 1024)

    def test_full_local_store_pages_searches_and_keeps_cursor_when_new_rows_arrive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            center = ControlCenter(root)
            profile = center.store.add_profile('04')
            home = Path(profile['home'])
            home.mkdir(parents=True)
            ids = [str(UUID(int=i + 1)) for i in range(5001)]
            with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT,updated_at INTEGER,archived INTEGER)')
                db.executemany('INSERT INTO threads VALUES(?,?,?,?)',
                    [(tid, '오래된 목표 작업' if i == 0 else 'Task ' + str(i), i // 5, int(i == 0)) for i, tid in enumerate(ids)])
            first = center.dispatch('catalog.list', dict(limit=137))
            self.assertEqual(first['total_count'], 5001)
            self.assertEqual(len(first['conversations']), 137)
            self.assertFalse(first['possibly_truncated'])
            with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
                db.execute('INSERT INTO threads VALUES(?,?,?,?)', (str(uuid4()), 'New work', 2000, 0))
            cursor = first['next_cursor']
            found = [r['thread_id'] for r in first['conversations']]
            while cursor:
                result = center.dispatch('catalog.list', dict(limit=137, cursor=cursor))
                found.extend(r['thread_id'] for r in result['conversations'])
                cursor = result['next_cursor']
            self.assertEqual(len(found), len(set(found)))
            self.assertEqual(set(found), set(ids))
            match = center.dispatch('catalog.list', dict(query='오래된 목표'))
            self.assertEqual(match['matching_count'], 1)
            self.assertEqual(match['conversations'][0]['thread_id'], ids[0])
            self.assertTrue(match['conversations'][0]['archived'])
            self.assertEqual(center.dispatch('catalog.list', dict(thread_id=ids[0]))['conversations'], match['conversations'])
            self.assertEqual(center.dispatch('catalog.list', dict(paged=True))['total_count'], 5002)

    def test_cursor_is_bound_to_search_and_registered_source_scope(self):
        rows = [dict(thread_id=str(UUID(int=i + 1)), host_id='local', source_store_id='same', title='Find me', updated_at=1) for i in range(3)]
        value = dict(conversations=rows)
        cursor = page(value, dict(limit=1, query='Find'), 'scope')['next_cursor']
        with self.assertRaises(ValueError):
            page(value, dict(cursor=cursor), 'scope')
        with self.assertRaises(ValueError):
            page(value, dict(cursor=cursor, query='Find'), 'replacement')
        self.assertEqual(page(value, dict(cursor=cursor, query='find'), 'scope')['matching_count'], 3)
        for args in (dict(cursor=[]), dict(limit=True), dict(limit=501), dict(query='a' * 513)):
            with self.assertRaises(ValueError):
                page(value, args, 'scope')

    def test_jsonl_metadata_fallback_has_no_4096_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = dict(home=directory, id='source', alias='04', host_id='local')
            with (home / 'session_index.jsonl').open('w', encoding='utf-8') as stream:
                for i in range(5001):
                    stream.write(json.dumps(dict(id=str(UUID(int=i + 1)), title='Task ' + str(i))) + '\n')
            self.assertEqual(len(catalog.list_source(source, None)), 5001)
            self.assertEqual(len(catalog.list_source(source, 512)), 512)

    def test_remote_helper_reads_beyond_both_previous_limits_without_history_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile_id = str(uuid4())
            home = root / '.local/share/codex-control-center/profiles' / profile_id / 'codex'
            home.mkdir(parents=True)
            source = dict(id='manager:' + profile_id, home=str(home))
            atomic_json(home / 'managed-source.json', dict(host_id='local', store_id=source['id']))
            with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,rollout_path TEXT)')
                db.executemany('INSERT INTO threads VALUES(?,?,?,?)',
                    [(str(UUID(int=i + 1)), 'Metadata only ' + str(i), i, str(home / 'sessions' / (str(i) + '.jsonl'))) for i in range(5001)])
            before = {p.name: p.read_bytes() for p in home.iterdir()}
            result = helper.read(dict(host_identity='a' * 64, sources=[source]), user_home=root, identity='a' * 64)
            self.assertEqual(len(result['conversations']), 5001)
            self.assertFalse(result['errors'])
            self.assertFalse(result['possibly_truncated'])
            self.assertEqual(before, {p.name: p.read_bytes() for p in home.iterdir()})


class CatalogFrameTests(unittest.TestCase):
    def test_large_transport_cache_restart_and_small_picker_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            center = ControlCenter(root)
            profile = center.store.add_profile('04')
            register(center.store, profile)
            rows = [dict(thread_id=str(UUID(int=i + 1)), source_store_id='manager:' + profile['id'],
                         title='대화' * 256, cwd='/fixture/' + '경로' * 1024, updated_at=i) for i in range(2001)]
            value = dict(conversations=rows, errors=[])
            class Remote:
                def _alias(self, alias): return alias
                def _run(self, alias, command, **kwargs):
                    catalog_frames.write(kwargs['stdout'], value)
                    return subprocess.CompletedProcess([], 0)
            remote = Remote()
            query = RemoteCatalog(ROOT, center.store, remote)
            cache = RemoteCatalog(root, center.store, remote, reader=query._read)
            group = groups(center.store.read())['remote-dev']
            cache._refresh_host(group)
            self.assertEqual(len(cache.snapshot()['conversations']), 2001)
            self.assertGreater(cache._path('remote-dev').stat().st_size, 8 * 1024 * 1024)
            restarted = RemoteCatalog(root, center.store, object())
            self.assertEqual(len(restarted.snapshot()['conversations']), 2001)
            center.remote_catalog = restarted
            result = center.dispatch('catalog.list', dict(limit=200))
            self.assertEqual(result['total_count'], 2001)
            self.assertEqual(len(result['conversations']), 200)
            encoded = json.dumps(dict(id=1, ok=True, result=result), ensure_ascii=False).encode()
            self.assertLess(len(encoded), 8 * 1024 * 1024)
            # A truncated next snapshot cannot replace the last complete cache.
            original = cache._path('remote-dev').read_bytes()
            class Broken(Remote):
                def _run(self, alias, command, **kwargs):
                    kwargs['stdout'].write(b'{"kind":"catalog","version":1,"metadata":{}}\n')
                    return subprocess.CompletedProcess([], 0)
            cache.reader = RemoteCatalog(ROOT, center.store, Broken())._read
            cache._refresh_host(group)
            self.assertTrue(cache.snapshot()['hosts'][0]['stale'])
            self.assertEqual(cache._path('remote-dev').read_bytes(), original)

    def test_old_json_cache_is_still_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            center = ControlCenter(root)
            profile = center.store.add_profile('04')
            register(center.store, profile)
            cache = RemoteCatalog(root, center.store, object())
            group = groups(center.store.read())['remote-dev']
            row = dict(thread_id=str(uuid4()), source_store_id='manager:' + profile['id'], title='Earlier cache')
            atomic_json(cache._path('remote-dev').with_suffix('.json'), dict(scope=group['scope'], conversations=[row], errors=[]))
            self.assertEqual(cache.snapshot()['conversations'][0]['title'], 'Earlier cache')

    def test_incomplete_extra_or_mismatched_frames_are_rejected(self):
        stream = io.BytesIO()
        catalog_frames.write(stream, dict(conversations=[dict(title='Fixture')], errors=[]))
        payload = stream.getvalue()
        for broken in (payload[:-1], payload + b'{}\n', payload.replace(b'"count":1', b'"count":2')):
            with self.assertRaises(ValueError):
                catalog_frames.read(io.BytesIO(broken))


if __name__ == '__main__':
    unittest.main()
