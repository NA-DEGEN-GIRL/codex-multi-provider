import sys, threading, time, unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from control_center import ControlCenter

class RequestConcurrencyTests(unittest.TestCase):
    def center(self):
        center=ControlCenter.__new__(ControlCenter)
        center._mutex=threading.RLock()
        center._request_gates={}
        center._request_gate_lock=threading.Lock()
        return center

    def test_other_profile_is_independent_and_same_profile_is_ordered(self):
        center=self.center(); entered=threading.Event(); release=threading.Event(); events=[]
        def dispatch(command,args):
            if args.get('slow'):
                entered.set(); self.assertTrue(release.wait(3))
            events.append(args['label']); return args
        center.dispatch=dispatch
        with patch('manager_core.rust_service.enabled',return_value=True),ThreadPoolExecutor(3) as executor:
            slow=executor.submit(center.request,dict(id='a',command='profile.show',args=dict(profile_id='one',slow=True,label='first')))
            self.assertTrue(entered.wait(3))
            same=executor.submit(center.request,dict(id='b',command='profile.restart',args=dict(profile_id='one',label='second')))
            other=executor.submit(center.request,dict(id='c',command='profile.show',args=dict(profile_id='two',label='independent')))
            try:
                self.assertTrue(other.result(timeout=1)['ok']); self.assertFalse(same.done())
            finally: release.set()
            self.assertTrue(slow.result(timeout=3)['ok']); self.assertTrue(same.result(timeout=3)['ok'])
            self.assertEqual(events,['independent','first','second'])

if __name__=='__main__':unittest.main()
