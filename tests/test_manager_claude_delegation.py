"""Offline authenticated MCP transport checks; no provider processes are started."""
import json
import os
from pathlib import Path
import queue
import sys
import threading
import unittest
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.claude_delegation import DelegationBridge, MAX_REQUEST_BYTES
from manager_core.claude_delegation_mcp import call_native, dispatch
from manager_core.claude_runner import private_temporary_directory


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.stopped = threading.Event()
        self.catalog = [{'name':'spawn_agent', 'description':'Configured native role',
                         'inputSchema':{'type':'object'}}]
        self.bridge = DelegationBridge(self.events.put, self.stopped, self.catalog, private_temporary_directory)
        self.addCleanup(self.bridge.close)

    def test_only_admitted_typed_requests_can_reach_native_dispatch(self):
        for request in ({'name':'exec_command','arguments':{}},
                        {'name':[], 'arguments':{}},
                        {'name':'spawn_agent','arguments':[],},
                        {'name':'spawn_agent','arguments':{},'id':'spoofed-parent'},
                        {'name':'spawn_agent','arguments':{'message':'x' * MAX_REQUEST_BYTES}}):
            self.assertIn('error', self.bridge.call(request))
        self.assertTrue(self.events.empty())
        response = dispatch({'id':1,'method':'tools/call','params':{'name':'wait_agent','arguments':{}}}, self.catalog)
        self.assertTrue(response['result']['isError'])
        self.assertTrue(self.events.empty())

    def test_authenticated_roundtrip_ignores_unknown_and_duplicate_response_ids(self):
        answers = queue.Queue()
        env = self.bridge.configuration()['codex_agents']['env']
        with patch.dict(os.environ, env):
            worker = threading.Thread(target=lambda: answers.put(call_native('spawn_agent', {'message':'Review.'})), daemon=True)
            worker.start()
            event = self.events.get(timeout=10)
            self.assertEqual({key:value for key,value in event.items() if key != 'id'},
                             {'type':'agent_request','name':'spawn_agent','arguments':{'message':'Review.'}})
            self.bridge.respond({'id':'unknown','result':'wrong'})
            self.bridge.respond({'id':event['id'],'result':{'agent_id':'native-child'}})
            self.bridge.respond({'id':event['id'],'result':'duplicate'})
            self.assertEqual(answers.get(timeout=10), {'result':{'agent_id':'native-child'}})
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())

    def test_wrong_ipc_key_cannot_submit_an_agent_request(self):
        with self.assertRaises(AuthenticationError):
            Client(self.bridge.address,family=self.bridge.family,authkey=b'wrong-fixture-key')
        self.assertTrue(self.events.empty())
        self.assertEqual(self.bridge.pending,{})

    def test_interrupt_releases_a_pending_delegation_without_fabricating_a_result(self):
        answers = queue.Queue()
        worker = threading.Thread(target=lambda: answers.put(self.bridge.call({'name':'spawn_agent','arguments':{}})), daemon=True)
        worker.start()
        self.events.get(timeout=10)
        self.stopped.set()
        self.assertIn('error', answers.get(timeout=10))
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.bridge.pending, {})

    def test_malformed_mcp_params_and_catalog_fail_closed(self):
        self.assertEqual(dispatch({'id':1,'method':'tools/call','params':['bad']}, self.catalog),
                         {'jsonrpc':'2.0','id':1,'error':{'code':-32602,'message':'Invalid params'}})
        with self.assertRaises(ValueError):
            DelegationBridge(self.events.put, self.stopped, [{'name':[]}], private_temporary_directory)
        self.assertEqual(json.loads(self.bridge.catalog.read_text(encoding='utf-8')), self.catalog)


if __name__ == '__main__':
    unittest.main()
