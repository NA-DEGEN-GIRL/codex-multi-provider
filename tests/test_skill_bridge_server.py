import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.skill_bridge_server import BridgeError, BridgeServer, CHUNK_BYTES, OUTPUT_BYTES


_MODULE = '''import pathlib, sys, time
assert sys.argv[1] == '--root'
root = pathlib.Path(sys.argv[2])
command = sys.argv[3]
count = root / '.work/count.txt'
count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')
if command == 'sleep':
    time.sleep(float(sys.argv[4]))
if command == 'loud':
    sys.stdout.write('O' * 700000)
    sys.stderr.write('E' * 700000)
else:
    print('root=' + str(root))
    print('arguments=' + repr(sys.argv[4:]))
    print('fixture stderr', file=sys.stderr)
if command == 'fail':
    sys.exit(7)
(root / '.assets/result.bin').write_bytes(b'fixture artifact')
'''


class SkillBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.runtime = self.base / 'runtime'
        self.skill_dir = self.base / 'skill'
        for path in (self.runtime / 'docs', self.runtime / '.assets', self.runtime / '.work',
                     self.skill_dir / 'references'):
            path.mkdir(parents=True)
        (self.runtime / 'fixture_cli.py').write_text(_MODULE, encoding='utf-8')
        (self.skill_dir / 'SKILL.md').write_text('# Fixture skill', encoding='utf-8')
        (self.skill_dir / 'references/use.md').write_text('Skill reference', encoding='utf-8')
        (self.runtime / 'docs/readme.md').write_text('Runtime documentation', encoding='utf-8')
        (self.runtime / 'private.json').write_text('fixture secret', encoding='utf-8')
        self.registration = dict(name='fixture', root=str(self.runtime), skill_dir=str(self.skill_dir),
                                 python=sys.executable, module='fixture_cli',
                                 commands=['make', 'fail', 'sleep', 'loud'], description='Fixture skill')
        self.tokens = {'token-for-scope-a-123456789': 'host:a', 'token-for-scope-b-123456789': 'host:b'}
        self.token = next(iter(self.tokens))
        self.token_b = list(self.tokens)[1]
        self.servers = []
        self.server = self.new_server()

    def tearDown(self):
        for server in self.servers:
            server.close()
            if server.worker is not None:
                server.worker.join(timeout=5)
        self.temporary.cleanup()

    def new_server(self, *, start=True, **kwargs):
        server = BridgeServer(self.base / 'state', [self.registration], self.tokens, **kwargs)
        self.servers.append(server)
        if start:
            self.port = server.start()['port']
        return server

    def call(self, op, *, token=None, expected=200, **payload):
        request = Request(f'http://127.0.0.1:{self.port}/v1',
                          data=json.dumps(dict(op=op, **payload)).encode(),
                          headers={'Authorization': 'Bearer ' + (self.token if token is None else token),
                                   'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=5) as response:
                status, result = response.status, json.load(response)
        except HTTPError as response:
            status, result = response.code, json.load(response)
        self.assertEqual(status, expected, result)
        return result

    def submit(self, argv=None, **kwargs):
        return self.call('run', skill='fixture', argv=argv or ['make'],
                         request_id=kwargs.pop('request_id', str(uuid4())), **kwargs)

    def finished(self, job_id, **kwargs):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.call('job', job_id=job_id, **kwargs)
            if result['status'] not in ('queued', 'running'):
                return result
            time.sleep(0.02)
        self.fail('Fixture job did not finish.')

    def close_server(self):
        self.server.close()
        if self.server.worker is not None:
            self.server.worker.join(timeout=5)

    def test_loopback_auth_catalog_and_document_boundaries(self):
        self.assertEqual(self.server.httpd.server_address[0], '127.0.0.1')
        self.assertEqual(self.call('catalog', token='bad', expected=401)['error'], 'unauthorized')
        self.assertEqual(self.call('catalog')['skills'],
                         [dict(name='fixture', root=str(self.runtime), description='Fixture skill')])
        for path, text in [('skill:/SKILL.md', '# Fixture skill'),
                           ('skill:/references/use.md', 'Skill reference'),
                           ('runtime:/docs/readme.md', 'Runtime documentation')]:
            self.assertEqual(self.call('read', skill='fixture', path=path)['text'], text)
        self.call('read', skill='fixture', path='runtime:/private.json', expected=403)
        self.call('read', skill='fixture', path='skill:/references/../../private.md', expected=400)
        self.call('read', skill='fixture', path='runtime:/docs/../private.md', expected=400)

    def test_actual_command_result_and_artifact_download(self):
        job = self.submit(['make', 'literal;not-a-shell', 'spaces remain one argument'])
        result = self.finished(job['job_id'])
        self.assertEqual((result['status'], result['exit_code']), ('complete', 0))
        self.assertIn('literal;not-a-shell', result['stdout'])
        self.assertIn('spaces remain one argument', result['stdout'])
        self.assertIn('fixture stderr', result['stderr'])
        stat = self.call('stat', skill='fixture', path='runtime:/.assets/result.bin')
        self.assertEqual(stat['sha256'], hashlib.sha256(b'fixture artifact').hexdigest())
        chunk = self.call('download', skill='fixture', path=str(self.runtime / '.assets/result.bin'), offset=3, length=5)
        self.assertEqual(base64.b64decode(chunk['data']), b'ture ')
        self.call('download', skill='fixture', path='runtime:/.assets/result.bin', length=CHUNK_BYTES + 1, expected=400)
        self.call('stat', skill='fixture', path=str(self.runtime / 'private.json'), expected=403)

    def test_concurrent_idempotency_and_changed_payload_conflict(self):
        request_id = str(uuid4())
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.submit(request_id=request_id), range(12)))
        self.assertEqual(len({r['job_id'] for r in results}), 1)
        self.finished(results[0]['job_id'])
        self.assertEqual((self.runtime / '.work/count.txt').read_text(), '1')
        self.submit(['make', 'different'], request_id=request_id, expected=409)
        self.close_server()
        self.server = self.new_server()
        repeated = self.submit(request_id=request_id)
        self.assertEqual(repeated['job_id'], results[0]['job_id'])
        self.assertEqual(repeated['status'], 'complete')
        self.assertEqual((self.runtime / '.work/count.txt').read_text(), '1')

    def test_scope_separates_job_visibility_and_request_identity(self):
        request_id = str(uuid4())
        first = self.submit(request_id=request_id)
        second = self.submit(request_id=request_id, token=self.token_b)
        self.assertNotEqual(first['job_id'], second['job_id'])
        self.call('job', job_id=first['job_id'], token=self.token_b, expected=404)
        self.finished(first['job_id'])
        self.finished(second['job_id'], token=self.token_b)
        self.assertEqual((self.runtime / '.work/count.txt').read_text(), '2')

    def test_command_allowlist_and_root_override(self):
        for argv in (['unknown'], ['--help'], ['make', '--root', str(self.base)],
                     ['make', '--root=' + str(self.base)]):
            self.submit(argv, expected=403)
        self.assertFalse((self.runtime / '.work/count.txt').exists())

    def test_upload_chunk_offsets_hash_atomic_visibility_and_scope(self):
        first_data, last_data = b'A' * CHUNK_BYTES, b'final bytes'
        first = self.call('upload', name='mesh.glb', data=base64.b64encode(first_data).decode(), final=False)
        self.assertFalse(Path(first['path']).exists())
        self.call('stat', skill='fixture', path=first['path'], expected=404)
        self.call('upload', name='mesh.glb', upload_id=first['upload_id'], data='', offset=0, final=False, expected=409)
        self.call('upload', token=self.token_b, name='mesh.glb', upload_id=first['upload_id'],
                  data='', offset=CHUNK_BYTES, final=False, expected=404)
        digest = hashlib.sha256(first_data + last_data).hexdigest()
        final = self.call('upload', name='mesh.glb', upload_id=first['upload_id'],
                          data=base64.b64encode(last_data).decode(), offset=CHUNK_BYTES, final=True, sha256=digest)
        self.assertEqual(final['sha256'], digest)
        self.assertEqual(Path(final['path']).read_bytes(), first_data + last_data)
        self.call('stat', skill='fixture', path=final['path'], token=self.token_b, expected=403)
        virtual = 'upload:/' + final['upload_id'] + '/mesh.glb'
        self.assertEqual(self.call('stat', path=virtual)['size'], len(first_data + last_data))
        result = self.call('download', path=virtual, offset=CHUNK_BYTES, length=len(last_data))
        self.assertEqual(base64.b64decode(result['data']), last_data)
        self.call('upload', name='../bad.glb', data='', final=True, expected=400)

    def test_upload_hash_mismatch_does_not_publish_and_can_be_finalized(self):
        self.call('upload', name='hash.bin', data=base64.b64encode(b'bytes').decode(),
                  final=True, sha256='0' * 64, expected=409)
        self.assertEqual(list((self.base / 'state/uploads').glob('*/*/hash.bin')), [])
        meta = next((self.base / 'state/incoming').glob('*/*.json'))
        result = self.call('upload', name='hash.bin', upload_id=meta.stem, offset=5,
                           data='', final=True, sha256=hashlib.sha256(b'bytes').hexdigest())
        self.assertTrue(result['final'])

    def test_failed_command_and_bounded_output_persist_full_logs(self):
        failed = self.finished(self.submit(['fail'])['job_id'])
        self.assertEqual((failed['status'], failed['exit_code']), ('failed', 7))
        job_id = self.submit(['loud'])['job_id']
        loud = self.finished(job_id)
        self.assertEqual(loud['status'], 'complete')
        for stream in ('stdout', 'stderr'):
            self.assertEqual(len(loud[stream]), OUTPUT_BYTES)
            self.assertTrue(loud[stream + '_truncated'])
            self.assertEqual((self.base / 'state/jobs' / job_id / (stream + '.log')).stat().st_size, 700000)

    def test_enabled_callback_and_registration_refresh(self):
        enabled = {'fixture'}
        self.server.enabled_callback = lambda name: name in enabled
        enabled.clear()
        self.assertEqual(self.call('catalog')['skills'], [])
        self.submit(expected=403)
        enabled.add('fixture')
        self.assertEqual(len(self.call('catalog')['skills']), 1)
        self.server.update_registrations([])
        self.assertEqual(self.call('catalog')['skills'], [])
        self.submit(expected=404)

    def test_token_refresh_revokes_old_access_without_listener_restart(self):
        original_port = self.port
        replacement = 'replacement-token-for-scope-a-123456'
        self.server.update_tokens({replacement: 'host:a'})
        self.call('catalog', expected=401)
        self.assertEqual(len(self.call('catalog', token=replacement)['skills']), 1)
        self.assertEqual(self.server.start()['port'], original_port)
        self.server.update_tokens({})
        self.call('catalog', token=replacement, expected=401)

    def test_restart_marks_running_interrupted_without_resubmission(self):
        self.close_server()
        self.server = self.new_server(start=False)
        request_id = str(uuid4())
        job = self.server.dispatch('host:a', dict(op='run', skill='fixture', argv=['make'], request_id=request_id))
        record = self.server.jobs[job['job_id']]
        record['status'] = 'running'  # Durable state left by an abruptly lost worker.
        self.server._save_job(record)
        self.server.close()
        self.server = self.new_server()
        result = self.submit(request_id=request_id)
        self.assertEqual(result, {'job_id': job['job_id'], 'status': 'interrupted'})
        self.assertEqual(self.call('job', job_id=job['job_id'])['stdout'], '')
        self.assertFalse((self.runtime / '.work/count.txt').exists())

    def test_close_keeps_running_process_and_state_lease_until_completion(self):
        job_id = self.submit(['sleep', '1.5'])['job_id']
        deadline = time.monotonic() + 3
        while self.call('job', job_id=job_id)['status'] != 'running' and time.monotonic() < deadline:
            time.sleep(0.01)
        self.server.close()
        with self.assertRaises(BridgeError) as error:
            self.new_server(start=False)
        self.assertEqual(error.exception.code, 'state_busy')
        self.server.worker.join(timeout=5)
        self.assertEqual(self.server.jobs[job_id]['status'], 'complete')
        self.assertTrue((self.runtime / '.assets/result.bin').exists())
        self.server = self.new_server()
        self.assertEqual(self.call('job', job_id=job_id)['status'], 'complete')

    def test_hardlinks_and_symlinks_do_not_expose_outside_files(self):
        private = self.runtime / 'private.json'
        hardlink = self.runtime / '.assets/linked.bin'
        os.link(private, hardlink)
        self.call('stat', skill='fixture', path='runtime:/.assets/linked.bin', expected=404)
        symlink = self.skill_dir / 'references/linked.md'
        try:
            symlink.symlink_to(private)
        except OSError:
            return  # Windows may not grant symlink creation to the test account.
        self.call('read', skill='fixture', path='skill:/references/linked.md', expected=400)


if __name__ == '__main__':
    unittest.main()
