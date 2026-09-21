"""Linux-only detached-worker integration against a fake CLI in a temporary HOME."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/remote_helpers/stock_versions.py'


@unittest.skipUnless(sys.platform == 'linux', 'Requires Linux flock and /proc')
class StockUpdateLinuxTests(unittest.TestCase):
    def test_detached_worker_lock_completion_and_consumed_confirmation(self):
        spec = importlib.util.spec_from_file_location('stock_fixture', SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='codex-stock-fixture-') as temporary:
            home = Path(temporary)
            binary = home / 'bin/codex'
            binary.parent.mkdir()
            (binary.parent / 'npm').write_text('#!/bin/sh\nexit 1\n')
            (binary.parent / 'npm').chmod(0o755)
            binary.write_text('''#!/usr/bin/env python3
from pathlib import Path
import json,sys,time
home=Path.home()
args=sys.argv[1:]
if args == ['--version']:
 print('codex-cli 0.155.1')
elif args == ['app-server','daemon','version']:
 print(json.dumps(dict(status='running',appServerVersion='0.155.1',managedCodexVersion='0.155.1')))
elif args == ['app-server','daemon','update','--help']:
 print('codex app-server daemon update')
elif args == ['app-server','daemon','update']:
 with (home/'update-count').open('a') as stream: stream.write('update\\n')
 time.sleep(0.3)
 print(json.dumps(dict(status='noUpdate',installedVersion='0.155.1',runningVersion='0.155.1')))
else:
 sys.exit(2)
''', encoding='utf-8')
            binary.chmod(0o775)
            with patch.dict(os.environ, HOME=str(home), PATH=str(binary.parent) + os.pathsep + os.environ['PATH']):
                before = module.probe()
                request = dict(confirmed=True, observation_id=before['observation_id'])
                started = module.start(request, SOURCE.read_text(encoding='utf-8'))
                self.assertEqual('starting', started['update_job']['state'])
                # An immediate retry races the worker and must not restart it.
                duplicate = module.start(request, SOURCE.read_text(encoding='utf-8'))
                self.assertEqual(started['update_job']['id'], duplicate['update_job']['id'])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    after = module.probe()
                    if after['update_job']['state'] in ('complete', 'attention'):
                        break
                    time.sleep(0.05)
                self.assertEqual('complete', after['update_job']['state'])
                self.assertEqual(['update'], (home / 'update-count').read_text().splitlines())
                self.assertNotEqual(before['observation_id'], after['observation_id'])
                with self.assertRaisesRegex(ValueError, 'changed_refresh'):
                    module.start(request, SOURCE.read_text(encoding='utf-8'))
                self.assertEqual(['update'], (home / 'update-count').read_text().splitlines())

    def test_unsupported_exit_zero_is_terminal_not_permanent_verification(self):
        spec = importlib.util.spec_from_file_location('stock_unsupported_fixture', SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='codex-stock-unsupported-') as temporary:
            home = Path(temporary)
            binary = home / 'bin/codex'
            binary.parent.mkdir()
            (binary.parent / 'npm').write_text('#!/bin/sh\nexit 1\n')
            (binary.parent / 'npm').chmod(0o755)
            binary.write_text('''#!/usr/bin/env python3
from pathlib import Path
import json,sys
args=sys.argv[1:]
if args == ['--version']:
 print('codex-cli 0.155.1')
elif args == ['app-server','daemon','version']:
 print(json.dumps(dict(status='running',appServerVersion='0.154.0',managedCodexVersion='0.155.1')))
elif args == ['app-server','daemon','update','--help']:
 print('codex app-server daemon update')
elif args == ['app-server','daemon','update']:
 with (Path.home()/'update-count').open('a') as stream: stream.write('update\\n')
 print(json.dumps(dict(status='unsupported',installedVersion='0.155.1',runningVersion='0.154.0',message='private')))
else:
 sys.exit(2)
''', encoding='utf-8')
            binary.chmod(0o755)
            with patch.dict(os.environ, HOME=str(home), PATH=str(binary.parent) + os.pathsep + os.environ['PATH']):
                before = module.probe()
                request = dict(confirmed=True, observation_id=before['observation_id'])
                module.start(request, SOURCE.read_text(encoding='utf-8'))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    after = module.probe()
                    if after['update_job']['state'] not in ('starting', 'applying'):
                        break
                    time.sleep(.05)
                self.assertEqual('unsupported', after['update_job']['state'])
                self.assertFalse(after['update_job']['verification_pending'])
                self.assertFalse(after['update_supported'])
                self.assertEqual('installation_unsupported', after['update_block_reason'])
                self.assertEqual(['update'], (home / 'update-count').read_text().splitlines())
                self.assertNotEqual(before['observation_id'], after['observation_id'])
                with self.assertRaisesRegex(ValueError, 'changed_refresh'):
                    module.start(request, SOURCE.read_text(encoding='utf-8'))
                self.assertEqual(['update'], (home / 'update-count').read_text().splitlines())


if __name__ == '__main__':
    unittest.main()
