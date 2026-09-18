from pathlib import Path
import sys,threading,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from manager_core.catalog_refresh import CatalogRefresh


class CatalogRefreshTests(unittest.TestCase):
    def test_only_one_builder_can_write_at_a_time(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def build(*args,**kwargs):
            entered.set();release.wait(2);finished.set()
            return dict(entries=2,complete=True)
        refresh=CatalogRefresh('.',interval=0,builder=build)
        self.assertTrue(refresh.refresh([],include_paginated=True))
        self.assertTrue(entered.wait(1))
        self.assertFalse(refresh.refresh([],include_paginated=True))
        with self.assertRaises(RuntimeError):refresh.ensure([],include_paginated=True)
        release.set();self.assertTrue(finished.wait(1))
        # Wait on a lock only: the daemon finishes result publication immediately.
        for _ in range(100):
            if not refresh.status()['refreshing']:break
            threading.Event().wait(.01)
        self.assertEqual(refresh.status()['entries'],2)
    def test_failed_refresh_retains_previous_usable_index(self):
        def first(*args,**kwargs):return dict(entries=17,complete=True,path='fixture')
        refresh=CatalogRefresh('.',builder=first)
        refresh.ensure([],include_paginated=True)
        def fail(*args,**kwargs):raise OSError('fixture')
        refresh.builder=fail
        with self.assertRaises(OSError):refresh.ensure([],include_paginated=True)
        state=refresh.status()
        self.assertEqual(state['entries'],17)
        self.assertFalse(state['refreshing'])
        self.assertIsNotNone(state['error'])


if __name__=='__main__':unittest.main()
