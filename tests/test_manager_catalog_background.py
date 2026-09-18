import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from manager_core.catalog_refresh import CatalogRefresh
from manager_core.store import Store,atomic_json


def record(home):
    path=home/'sessions'/(str(uuid4())+'.jsonl')
    path.parent.mkdir(parents=True,exist_ok=True)
    payload=dict(id=str(uuid4()),cwd=str(home.parent),history_mode='legacy')
    path.write_text(json.dumps(dict(type='session_meta',payload=payload))+'\n',encoding='utf-8')
    return path


def wait_entries(path,count,timeout=12):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            result=json.loads(path.read_text(encoding='utf-8'))
            if len(result['entries'])==count:return result
        except (OSError,ValueError):pass
        threading.Event().wait(.025)
    raise AssertionError(f'Common index did not reach {count} entries without a UI request.')


class CatalogBackgroundTests(unittest.TestCase):
    def test_monitor_reads_new_sources_and_stops_without_changing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root)
            first=store.add_profile('first');one=record(Path(first['home']))
            original=one.read_bytes()
            refresh=CatalogRefresh(root,interval=.025)
            self.assertTrue(refresh.start(lambda:(store.read()['sources'],True)))
            try:
                self.assertFalse(refresh.start(lambda:None))
                path=root/'work/control-center/catalog/local-records.json'
                wait_entries(path,1)
                second=store.add_profile('second');two=record(Path(second['home']))
                manifest=wait_entries(path,2)
                self.assertEqual({entry['sourceStoreId'] for entry in manifest['entries']},
                                 {'manager:'+first['id'],'manager:'+second['id']})
                self.assertEqual(one.read_bytes(),original)
                self.assertTrue(two.is_file())
            finally:refresh.stop()
            self.assertFalse(refresh._monitor.is_alive())
            before=path.read_bytes();record(Path(first['home']))
            threading.Event().wait(.08)
            self.assertEqual(path.read_bytes(),before)

    def test_configuration_failure_recovers_without_restarting_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root);profile=store.add_profile('one')
            record(Path(profile['home']))
            healthy=threading.Event();failed=threading.Event()
            def config():
                if not healthy.is_set():
                    failed.set();raise OSError('temporary configuration read failure')
                return store.read()['sources'],True
            refresh=CatalogRefresh(root,interval=.025)
            refresh.start(config)
            try:
                self.assertTrue(failed.wait(1));healthy.set()
                wait_entries(root/'work/control-center/catalog/local-records.json',1)
                self.assertIsNone(refresh.status()['error'])
            finally:refresh.stop()

    def test_serving_backend_updates_index_with_no_stdin_or_state_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root);profile=store.add_profile('fixture')
            runtime=root/'artifacts/manager-runtime/releases/fixture/codex.exe'
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b'Only hashed as a fixture; never executed.')
            atomic_json(root/'artifacts/manager-runtime/current.json',dict(
                runtime=str(runtime),sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
                capabilities=dict(native_record_catalog=True,paginated_record_catalog=True)))
            process=subprocess.Popen([sys.executable,'-X','utf8',str(SCRIPTS/'control_center.py'),
                '--root',str(root),'--serve'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,cwd=root)
            try:
                path=root/'work/control-center/catalog/local-records.json'
                wait_entries(path,0)
                source=record(Path(profile['home']));before=source.read_bytes()
                result=wait_entries(path,1)
                self.assertEqual(result['entries'][0]['sourceStoreId'],'manager:'+profile['id'])
                self.assertEqual(source.read_bytes(),before)
                stdout,stderr=process.communicate(timeout=5)
                self.assertEqual((process.returncode,stdout,stderr),(0,b'',b''))
            finally:
                if process.poll() is None:
                    process.terminate();process.communicate(timeout=5)


if __name__=='__main__':unittest.main()
