"""Real pidfd/socket integration using a fake npm package; no network installs."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/remote_helpers/stock_versions.py'
SERVER = r'''
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void stop(int sig) { _exit(0); }
int main(int argc, char **argv) {
 char file[4096], resolved[4096], ver[64], endpoint[108];
 if (!realpath(argv[0],resolved)) return 3;
 snprintf(file,sizeof(file),"%s.version",resolved);
 FILE *f=fopen(file,"r"); if (!f || !fgets(ver,sizeof(ver),f)) return 3;
 fclose(f); ver[strcspn(ver,"\r\n")]=0;
 snprintf(endpoint,sizeof(endpoint),"%s/.codex/app-server-control/app-server-control.sock",getenv("HOME"));
 struct sockaddr_un addr={.sun_family=AF_UNIX}; strcpy(addr.sun_path,endpoint);
 if (argc==2 && !strcmp(argv[1],"--version")) { printf("codex-cli %s\n",ver); return 0; }
 if (argc==4 && !strcmp(argv[2],"daemon") && !strcmp(argv[3],"version")) {
  int s=socket(AF_UNIX,SOCK_STREAM,0); if(connect(s,(void*)&addr,sizeof(addr))) return 4;
  char live[64]={0}; if(read(s,live,63)<=0) return 5; close(s);
  printf("{\"status\":\"running\",\"appServerVersion\":\"%s\",\"managedCodexVersion\":null,\"managedCodexPath\":\"%s/.codex/packages/standalone/current/codex\"}\n",live,getenv("HOME")); return 0;
 }
 if (argc==5 && !strcmp(argv[4],"--help")) { puts("codex app-server daemon update"); return 0; }
 if (argc>=4 && !strcmp(argv[argc-3],"app-server") && !strcmp(argv[argc-2],"--listen")) {
  signal(SIGTERM,stop); signal(SIGPIPE,SIG_IGN);
  int s=socket(AF_UNIX,SOCK_STREAM,0); unlink(endpoint);
  if(bind(s,(void*)&addr,sizeof(addr)) || listen(s,16)) return 6;
  while(1) { int c=accept(s,0,0); if(c>=0) { send(c,ver,strlen(ver),MSG_NOSIGNAL); close(c); } }
 }
 return 2;
}
'''


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('cc'), 'Requires Linux pidfd and C fixture compiler')
class NpmStockLinuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='npm80-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.package = self.home / 'lib/node_modules/@openai/codex'
        (self.package / 'bin').mkdir(parents=True)
        self.cli = self.package / 'bin/codex.js'
        self.manifest = self.package / 'package.json'
        self.write_version('0.154.0')
        source = self.home / 'fixture.c'
        source.write_text(SERVER)
        subprocess.run(['cc', str(source), '-o', str(self.cli)], check=True, capture_output=True)
        (self.bin / 'codex').symlink_to(self.cli)
        npm = self.bin / 'npm'
        npm.write_text('''#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
h=Path.home(); p=h/'lib/node_modules/@openai/codex'
if sys.argv[1:] == ['root','-g']:
 print(h/'lib/node_modules')
elif sys.argv[1:] == ['view','@openai/codex@latest','version','--json']:
 print(json.dumps((h/'latest-version').read_text() if (h/'latest-version').exists() else '0.155.2'))
elif sys.argv[1:] == ['install','--global','@openai/codex@0.155.2','--no-audit','--no-fund']:
 with (h/'npm-count').open('a') as f: f.write('install\\n')
 if (h/'fail-install').exists(): sys.exit(7)
 time.sleep(.3)
 (p/'package.json').write_text(json.dumps(dict(name='@openai/codex',version='0.155.2')))
 (p/'bin/codex.js.version').write_text('0.155.2')
else:
 sys.exit(8)
''')
        npm.chmod(0o755)
        (self.home / '.codex/app-server-control').mkdir(parents=True)
        env = patch.dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ['PATH'])
        env.start(); self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location('npm_stock_fixture', SOURCE)
        self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
        self.children = []
        self.addCleanup(self.stop_fixture)

    def write_version(self, value):
        self.manifest.write_text(json.dumps(dict(name='@openai/codex', version=value)))
        Path(str(self.cli) + '.version').write_text(value)

    def start_fixture(self, *, manager=False):
        environment = self.module.environment()
        if manager:
            environment['CODEX_MANAGER_PROFILE_ID'] = 'fixture-only'
        self.old = subprocess.Popen([str(self.cli), '-c', 'features.code_mode_host=true',
                                     'app-server', '--listen', 'unix://'], env=environment)
        self.children.append(self.old)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (self.home / '.codex/app-server-control/app-server-control.sock').exists():
                break
            time.sleep(.02)
        else:
            self.fail('fixture socket missing')
        self.write_version('0.155.1')

    def stop_fixture(self):
        # Stop only this test's peer after verifying its private fixture root.
        installation = self.module.npm_installation()
        try:
            peer = self.module.npm_peer(installation) if installation else None
            if peer and peer['executable'].startswith(str(self.home) + '/'):
                os.kill(peer['pid'], 15)
        except (OSError, ValueError):
            pass
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)

    def test_npm_updates_and_restarts_only_verified_default_peer(self):
        self.start_fixture()
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.children.append(unrelated)
        before = self.module.probe()
        self.assertEqual('npm', before['update_mode'])
        self.assertTrue(before['update_supported'])
        self.assertEqual('0.154.0', before['daemon_version'])
        request = dict(confirmed=True, observation_id=before['observation_id'])
        started = self.module.start(request, SOURCE.read_text())
        again = self.module.start(request, SOURCE.read_text())
        self.assertEqual(started['update_job']['id'], again['update_job']['id'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            after = self.module.probe()
            if after['update_job']['state'] not in ('starting', 'applying'):
                break
            time.sleep(.05)
        self.assertEqual('complete', after['update_job']['state'], after)
        self.assertEqual('0.155.2', after['cli_version'])
        self.assertEqual('0.155.2', after['daemon_version'])
        self.old.wait(timeout=3)
        self.assertIsNone(unrelated.poll())
        peer = self.module.npm_peer(self.module.npm_installation())
        self.assertNotEqual(self.old.pid, peer['pid'])
        self.assertEqual(['-c', 'features.code_mode_host=true'], peer['feature_args'])
        self.assertEqual(['install'], (self.home / 'npm-count').read_text().splitlines())
        with self.assertRaisesRegex(ValueError, 'changed_refresh'):
            self.module.start(request, SOURCE.read_text())

    def test_npm_failure_never_stops_the_existing_daemon(self):
        self.start_fixture()
        (self.home / 'fail-install').touch()
        before = self.module.probe()
        self.module.start(dict(confirmed=True, observation_id=before['observation_id']), SOURCE.read_text())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            after = self.module.probe()
            if after['update_job']['state'] not in ('starting', 'applying'):
                break
            time.sleep(.05)
        self.assertEqual('failed', after['update_job']['state'])
        self.assertEqual('stock_npm_install_failed', after['update_job']['code'])
        self.assertIsNone(self.old.poll())
        self.assertEqual('0.154.0', after['daemon_version'])

    def test_workspace_process_is_never_eligible_for_npm_restart(self):
        self.start_fixture(manager=True)
        before = self.module.probe()
        self.assertFalse(before['update_supported'])
        self.assertIsNone(self.module.npm_plan('0.155.1'))
        self.assertIsNone(self.old.poll())
        self.assertFalse((self.home / 'npm-count').exists())

    def test_registry_older_than_cli_only_refreshes_daemon_without_downgrade(self):
        self.start_fixture()
        (self.home / 'latest-version').write_text('0.154.0')
        before = self.module.probe()
        self.module.start(dict(confirmed=True, observation_id=before['observation_id']), SOURCE.read_text())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            after = self.module.probe()
            if after['update_job']['state'] not in ('starting', 'applying'):
                break
            time.sleep(.05)
        self.assertEqual('complete', after['update_job']['state'], after)
        self.assertEqual('0.155.1', after['cli_version'])
        self.assertEqual('0.155.1', after['daemon_version'])
        self.assertFalse((self.home / 'npm-count').exists())

    def test_replaced_socket_peer_cannot_use_old_confirmation(self):
        self.start_fixture()
        before = self.module.probe()
        self.old.terminate(); self.old.wait(timeout=5)
        (self.home / '.codex/app-server-control/app-server-control.sock').unlink()
        self.start_fixture()
        job = dict(npm_cli_version=before['cli_version'], npm_plan_fingerprint=before['npm_plan_fingerprint'])
        with self.assertRaisesRegex(ValueError, 'stock_npm_target_changed'):
            self.module.npm_update(job, self.home / 'unused.json')
        self.assertIsNone(self.old.poll())
        self.assertFalse((self.home / 'npm-count').exists())


if __name__ == '__main__':
    unittest.main()
