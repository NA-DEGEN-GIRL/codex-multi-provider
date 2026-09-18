import json
import os
import subprocess
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.catalog import list_source
from manager_core.catalog_origin import CatalogOrigins
from manager_core.record_edits import RecordEdits, rename_record
from manager_core.source_catalog import build, inventory, projection_id
from manager_core.store import Store


class RecordEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root)
        self.home = self.root / 'original'
        sessions = self.home / 'sessions/2026/09/15'
        sessions.mkdir(parents=True)
        self.tid = str(uuid4())
        self.rollout = sessions / ('rollout-' + self.tid + '.jsonl')
        self.rollout.write_text(json.dumps(dict(type='session_meta', payload=dict(id=self.tid))) + '\n' +
                                json.dumps(dict(type='event_msg', payload=dict(message='original history'))) + '\n')
        self.history_before = self.rollout.read_bytes()
        (self.home / 'auth.json').write_text('sentinel: never read or write')
        self.database = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, rollout_path TEXT, model TEXT, reasoning_effort TEXT, active INT)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)',
                       (self.tid, 'old', str(self.rollout), 'gpt-fixture', 'high', 1))
        self.store.mutate(lambda d: Store._source(d, self.home, 'original:local', 'original'))
        self.sources = self.store.read()['sources']
        self.catalog = Path(build(self.root, self.sources)['path'])
        _, bindings, _ = inventory(self.sources)
        source_id = next(iter(bindings))
        self.projection = projection_id(source_id, self.tid)
        self.origin = dict(hostId='local', sourceStoreId=source_id, canonicalThreadId=self.tid)
        self.origins = CatalogOrigins()
        self.addCleanup(self.origins.close)
        self.origins.observe_thread(dict(id=self.projection, path=None, canAcceptDirectInput=False,
                                        extra=dict(managedRecord=self.origin)))
        self.edits = RecordEdits(dict(CODEX_MANAGER_ROOT=str(self.root),
            CODEX_MANAGER_SHARED_CATALOG=str(self.catalog)), self.origins)

    def request(self, name='새 제목'):
        return dict(id=17, method='thread/name/set', params=dict(threadId=self.projection, name=name))

    def title(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute('SELECT name FROM threads WHERE id=?', (self.tid,)).fetchone()[0]

    def test_projection_rename_updates_canonical_name_and_index_without_history_or_model_changes(self):
        result = self.edits.handle(self.request())
        self.assertEqual(result, [dict(id=17, result={}), dict(method='thread/name/updated',
            params=dict(threadId=self.projection, threadName='새 제목'))])
        self.assertEqual(self.title(), '새 제목')
        index = json.loads((self.home / 'session_index.jsonl').read_text(encoding='utf-8'))
        self.assertEqual((index['id'], index['thread_name']), (self.tid, '새 제목'))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT model,reasoning_effort,active FROM threads').fetchall(),
                             [('gpt-fixture', 'high', 1)])
        self.assertEqual(self.rollout.read_bytes(), self.history_before)
        self.assertEqual((self.home / 'auth.json').read_text(), 'sentinel: never read or write')
        for _ in range(3):
            self.assertEqual(list_source(self.sources[0])[0]['title'], '새 제목')

    def test_three_simultaneous_profile_renames_have_one_consistent_last_name(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda name: rename_record(self.root, self.catalog, self.origin,
                self.projection, name), ['from 02', 'from 03', 'from 04']))
        lines = (self.home / 'session_index.jsonl').read_text().splitlines()
        self.assertCountEqual(results, ['from 02', 'from 03', 'from 04'])
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[-1])['thread_name'], self.title())
        self.assertEqual(self.rollout.read_bytes(), self.history_before)

    def test_native_unobserved_and_non_rename_requests_fall_through(self):
        request = self.request()
        request['params']['threadId'] = self.tid
        self.assertIsNone(self.edits.handle(request))
        request = self.request()
        request['method'] = 'thread/settings/update'
        self.assertIsNone(self.edits.handle(request))
        self.assertEqual(self.title(), 'old')

    @unittest.skipUnless(os.name == 'nt', 'Windows extended paths')
    def test_native_windows_extended_rollout_path_remains_the_same_record(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('UPDATE threads SET rollout_path=?', ('\\\\?\\' + str(self.rollout),))
        self.assertEqual(self.edits.handle(self.request())[0], dict(id=17, result={}))
        self.assertEqual(self.title(), '새 제목')
        self.assertEqual(self.rollout.read_bytes(), self.history_before)

    def test_removed_source_cannot_be_edited_from_an_old_sidebar_entry(self):
        self.store.mutate(lambda d: d.update(sources=[]))
        self.assertIn('error', self.edits.handle(self.request())[0])
        self.assertEqual(self.title(), 'old')

    def test_mismatched_rollout_id_cannot_be_renamed(self):
        self.rollout.write_text(json.dumps(dict(type='session_meta', payload=dict(id=str(uuid4())))))
        self.assertIn('error', self.edits.handle(self.request())[0])
        self.assertEqual(self.title(), 'old')

    def test_index_write_failure_rolls_back_database_title(self):
        with patch('manager_core.record_edits.os.open', side_effect=OSError('fixture')):
            self.assertIn('error', self.edits.handle(self.request())[0])
        self.assertEqual(self.title(), 'old')

    def test_empty_title_and_projection_mismatch_do_not_write(self):
        self.assertIn('error', self.edits.handle(self.request('  '))[0])
        with self.assertRaises(ValueError):
            rename_record(self.root, self.catalog, self.origin, str(uuid4()), 'x')
        self.assertEqual(self.title(), 'old')

    def test_catalog_viewer_does_not_acquire_edit_support(self):
        edits = RecordEdits(dict(CODEX_MANAGER_ROOT=str(self.root), CODEX_MANAGER_RECORD_CATALOG=str(self.catalog)), self.origins)
        self.assertIsNone(edits.handle(self.request()))
        self.assertEqual(self.title(), 'old')

    def test_real_proxy_stdio_routes_title_edit_and_emits_native_notification(self):
        fake = self.root / 'runtime.py'
        thread = dict(id=self.projection, path=None, canAcceptDirectInput=False,
            status=dict(type='notLoaded'), extra=dict(managedRecord=self.origin), turns=[])
        fake.write_text('import json,sys\nthread=' + repr(thread) + '\n' +
            'for line in sys.stdin:\n'
            ' m=json.loads(line)\n'
            ' if "id" not in m: continue\n'
            ' if m["method"]=="initialize": r={"id":m["id"],"result":{}}\n'
            ' elif m["method"]=="thread/read": r={"id":m["id"],"result":{"thread":thread}}\n'
            ' elif m["method"]=="account/read": r={"id":m["id"],"result":{"account":{"type":"chatgpt","email":"fixture@example.test"}}}\n'
            ' else: r={"id":m["id"],"error":{"code":-32600,"message":"runtime catalog rejects edits"}}\n'
            ' print(json.dumps(r),flush=True)\n', encoding='utf-8')
        bootstrap = self.root / 'proxy.py'
        bootstrap.write_text('import os,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n'
            'from manager_core.runtime_proxy import proxy\n'
            'env={**os.environ,"CODEX_MANAGER_ROOT":sys.argv[5],"CODEX_MANAGER_SHARED_CATALOG":sys.argv[6],"CODEX_MANAGER_REAL_RUNTIME":sys.executable}\n'
            'raise SystemExit(proxy(Path(sys.executable),[sys.argv[2]],Path(sys.argv[3]),sys.argv[4],env))\n', encoding='utf-8')
        snapshot = self.root / 'snapshot.json'
        environment = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'OPENAI_', 'CHATGPT_'))}
        process = subprocess.Popen([sys.executable, str(bootstrap), str(Path(__file__).resolve().parents[1] / 'scripts'),
            str(fake), str(snapshot), str(uuid4()), str(self.root), str(self.catalog)], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with ThreadPoolExecutor(max_workers=1) as reader:
            def read():
                line = reader.submit(process.stdout.readline).result(timeout=10)
                if not line:
                    raise RuntimeError(process.stderr.read())
                return json.loads(line)
            def send(message):
                process.stdin.write(json.dumps(message) + '\n'); process.stdin.flush()
            try:
                send(dict(id=1, method='initialize', params=dict(capabilities={})))
                self.assertEqual(read(), dict(id=1, result={}))
                send(dict(id=2, method='thread/read', params=dict(threadId=self.projection)))
                self.assertEqual(read()['result']['thread'], thread)
                send(self.request())
                self.assertIn('error', read())
                self.assertEqual(self.title(), 'old')
                send(dict(id=3, method='account/read', params={}))
                self.assertEqual(read()['result']['account']['type'], 'chatgpt')
                send(self.request())
                self.assertEqual(read(), dict(id=17, result={}))
                self.assertEqual(read(), dict(method='thread/name/updated',
                    params=dict(threadId=self.projection, threadName='새 제목')))
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0, process.stderr.read())
                self.assertEqual(self.title(), '새 제목')
                self.assertEqual(self.rollout.read_bytes(), self.history_before)
                diagnostics = json.loads(snapshot.read_text())['recent_diagnostics']
                self.assertTrue(any(x['method']=='thread/name/set' and x['state']=='completed' for x in diagnostics))
                self.assertNotIn('새 제목', snapshot.read_text(encoding='utf-8'))
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


if __name__ == '__main__':
    unittest.main()
