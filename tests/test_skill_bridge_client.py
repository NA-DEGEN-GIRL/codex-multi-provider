"""Portable client protocol checks against an authenticated, isolated loopback worker."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_helpers import skill_bridge_client as bridge


class LocalWorker:
    def __init__(self):
        self.token = "fixture-capability-" + uuid4().hex
        self.requests = []
        self.uploads = {}
        self.artifacts = {}
        self.jobs = {}
        self.job_creations = 0
        self.lose_run_response = False
        self.on_download = None
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/v1" or self.headers.get("Authorization") != "Bearer " + worker.token:
                    self.reply(401, {"message": "Fixture capability rejected."})
                    return
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                worker.requests.append(request)
                try:
                    result = worker.dispatch(request)
                except (ValueError, KeyError) as error:
                    self.reply(400, {"message": str(error)})
                    return
                if request["op"] == "run" and worker.lose_run_response:
                    worker.lose_run_response = False
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(200, result)

            def reply(self, status, value):
                content = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def dispatch(self, request):
        operation = request["op"]
        if operation == "catalog":
            return {"skills": []}
        if operation == "upload":
            upload_id = request.get("upload_id") or str(uuid4())
            entry = self.uploads.setdefault(upload_id, {"name": request["name"], "data": bytearray()})
            if request["offset"] != len(entry["data"]):
                raise ValueError("Unexpected upload offset.")
            entry["data"].extend(base64.b64decode(request["data"], validate=True))
            result = {"upload_id": upload_id, "size": len(entry["data"])}
            if request["final"]:
                result["sha256"] = hashlib.sha256(entry["data"]).hexdigest()
                if request["sha256"] != result["sha256"]:
                    raise ValueError("Upload checksum mismatch.")
            return result
        if operation in ("stat", "download"):
            content = self.artifacts[request["path"]]
            if operation == "stat":
                return {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            result = {"offset": request["offset"], "data": base64.b64encode(
                content[request["offset"]:request["offset"] + request["length"]]).decode("ascii")}
            if self.on_download:
                self.on_download(request, result)
            return result
        if operation == "run":
            payload = {key: request[key] for key in ("skill", "argv", "request_id")}
            previous = self.jobs.get(request["request_id"])
            if previous:
                if previous["payload"] != payload:
                    raise ValueError("Request ID belongs to another command.")
                return previous["result"]
            self.job_creations += 1
            result = {"job_id": str(uuid4()), "status": "queued"}
            self.jobs[request["request_id"]] = {"payload": payload, "result": result}
            return result
        if operation == "job":
            entry = next((entry for entry in self.jobs.values()
                          if entry['result']['job_id'] == request['job_id']), None)
            if entry is None:
                raise ValueError('Unknown fixture job.')
            return entry.get('detail', entry['result'])
        raise ValueError("Unexpected fixture operation.")


class SkillBridgeClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worker = LocalWorker()
        self.addCleanup(self.worker.close)
        self.connection = self.root / "연결 설정" / "connection.json"
        self.connection.parent.mkdir()
        self.write_connection()

    def write_connection(self, **changes):
        value = {"version": 1, "endpoint": self.worker.endpoint, "token": self.worker.token}
        value.update(changes)
        self.connection.write_text(json.dumps(value), encoding="utf-8")
        self.connection.chmod(0o600)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = bridge.main(["--connection", str(self.connection), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_authenticated_chunk_transfer_preserves_unicode_names_and_hash(self):
        content = bytes(range(256)) * 8193 + "유니코드 입력".encode("utf-8")
        source = self.root / "입력 이미지 reference ü.bin"
        source.write_bytes(content)
        client = bridge.Client(self.connection)
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}):
            uploaded = client.upload(source)
        stored = self.worker.uploads[uploaded["upload_id"]]
        self.assertEqual(stored["name"], source.name)
        self.assertEqual(bytes(stored["data"]), content)
        self.assertEqual(uploaded["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual([r["offset"] for r in self.worker.requests], [0, bridge.CHUNK, bridge.CHUNK * 2])
        artifact = "D:\\fixture artifacts\\한글 작품\\final mesh.glb"
        self.worker.artifacts[artifact] = bytes(stored["data"])
        destination = self.root / "Linux 프로젝트" / "최종 작품 ü.glb"
        fetched = client.fetch("3d-assets", artifact, destination)
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(fetched["sha256"], uploaded["sha256"])
        self.assertEqual(fetched["size"], len(content))
        downloads = [r for r in self.worker.requests if r["op"] == "download"]
        self.assertEqual([r["offset"] for r in downloads], [0, bridge.CHUNK, bridge.CHUNK * 2])
        self.assertTrue(all(r["path"] == artifact for r in downloads))
        self.assertEqual(list(destination.parent.glob("*.part")), [])

    def test_empty_file_upload_and_download(self):
        source = self.root / "empty file.wav"
        source.write_bytes(b"")
        client = bridge.Client(self.connection)
        result = client.upload(source)
        self.assertEqual(result["sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(len(self.worker.requests), 1)
        self.worker.artifacts["empty.wav"] = b""
        destination = self.root / "empty result.wav"
        fetched = client.fetch("game-audio", "empty.wav", destination)
        self.assertEqual(fetched["size"], 0)
        self.assertEqual(destination.read_bytes(), b"")

    def test_upload_detects_source_shrink_without_sending_empty_chunks(self):
        source = self.root / 'changing input.bin'
        source.write_bytes(b'A' * (bridge.CHUNK * 2 + 17))
        client = bridge.Client(self.connection)
        original_call = client.call
        calls = []

        def shrink_after_first_chunk(op, **payload):
            calls.append(payload)
            if len(calls) > 1:
                self.fail('The client continued uploading after its source became shorter than the offset.')
            result = original_call(op, **payload)
            with source.open('r+b') as stream:
                stream.truncate(1)
            return result

        with patch.object(client, 'call', side_effect=shrink_after_first_chunk):
            with self.assertRaises(ValueError):
                client.upload(source)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]['final'])
        self.assertEqual(len(base64.b64decode(calls[0]['data'])), bridge.CHUNK)

    def test_publication_failure_leaves_no_partial_final_artifact(self):
        self.worker.artifacts['sound.wav'] = b'complete verified sound' * 100
        destination = self.root / 'published.wav'

        def fail_during_copy(source, output, *args):
            output.write(b'partial final file')
            raise OSError(errno.ENOSPC, 'Simulated publication failure')

        # Exercise failure at either old copying publication or atomic-link
        # publication: in both cases the public destination must stay absent.
        with patch('shutil.copyfileobj', side_effect=fail_during_copy), \
             patch.object(bridge.os, 'link', side_effect=OSError(errno.ENOSPC, 'Simulated publication failure')):
            with self.assertRaises(OSError):
                bridge.Client(self.connection).fetch('game-audio', 'sound.wav', destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_fetch_never_overwrites_existing_or_concurrently_created_destination(self):
        self.worker.artifacts["mesh.glb"] = b"new artifact"
        client = bridge.Client(self.connection)
        destination = self.root / "existing asset.glb"
        destination.write_bytes(b"keep existing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            client.fetch("3d-assets", "mesh.glb", destination)
        self.assertEqual(destination.read_bytes(), b"keep existing")
        self.assertFalse(any(r["op"] == "download" for r in self.worker.requests))
        raced = self.root / "racing asset.glb"
        self.worker.on_download = lambda request, result: raced.write_bytes(b"created by project")
        with self.assertRaises(FileExistsError):
            client.fetch("3d-assets", "mesh.glb", raced)
        self.assertEqual(raced.read_bytes(), b"created by project")
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_corrupted_download_rejects_artifact_and_cleans_partial_file(self):
        self.worker.artifacts["sound.wav"] = b"original sound"
        self.worker.on_download = lambda request, result: result.update(data=base64.b64encode(b"corrupt! sound").decode())
        destination = self.root / "output.wav"
        with self.assertRaisesRegex(ValueError, "changed while downloading"):
            bridge.Client(self.connection).fetch("game-audio", "sound.wav", destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_cli_separator_forwards_skill_arguments_without_parsing_them(self):
        request_id = str(uuid4())
        arguments = ["generate", "--prompt", "한글 prompt with spaces", "--wait", "17", "--", "literal tail"]
        code, stdout, stderr = self.invoke("run", "game-audio", "--request-id", request_id, "--wait", "0", "--", *arguments)
        self.assertEqual(code, 0)
        payload = self.worker.jobs[request_id]["payload"]
        self.assertEqual(payload["argv"], arguments)
        self.assertEqual(json.loads(stdout)["request_id"], request_id)
        self.assertIn(request_id, stderr)
        self.assertEqual(json.loads((self.connection.parent / "requests" / (request_id + ".json")).read_text()), payload)

    def test_lost_run_response_keeps_journal_and_same_id_retry_reuses_job(self):
        self.worker.lose_run_response = True
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaisesRegex(RuntimeError, "do not resubmit an uncertain job"):
                bridge.main(["--connection", str(self.connection), "run", "3d-assets", "--wait", "0", "--", "status", "--task-id", "existing task"])
        records = list((self.connection.parent / "requests").glob("*.json"))
        self.assertEqual(len(records), 1)
        payload = json.loads(records[0].read_text())
        request_id = payload["request_id"]
        self.assertIn(request_id, stderr.getvalue())
        first_job = self.worker.jobs[request_id]["result"]["job_id"]
        code, output, _ = self.invoke("run", "3d-assets", "--request-id", request_id, "--wait", "0", "--", *payload["argv"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["job_id"], first_job)
        self.assertEqual(self.worker.job_creations, 1)
        self.assertEqual(len([r for r in self.worker.requests if r["op"] == "run"]), 2)
        with self.assertRaisesRegex(ValueError, "already belongs to another command"):
            self.invoke("run", "3d-assets", "--request-id", request_id, "--wait", "0", "--", "different-command")
        self.assertEqual(self.worker.job_creations, 1)
        self.assertEqual(json.loads(records[0].read_text()), payload)

    def test_repeated_completed_request_returns_persisted_output_without_new_job(self):
        request_id = str(uuid4())
        arguments = ('run', 'game-audio', '--request-id', request_id, '--wait', '0', '--',
                     'import', 'uploaded specification.json')
        code, output, _ = self.invoke(*arguments)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'queued')
        entry = self.worker.jobs[request_id]
        entry['result']['status'] = 'complete'
        entry['detail'] = dict(entry['result'], stdout='완료된 음원: final.wav\n',
                               stderr='Stored review detail\n', exit_code=0)
        journal = self.connection.parent / 'requests' / (request_id + '.json')
        saved_journal = journal.read_bytes()

        for _ in range(2):
            code, stdout, stderr = self.invoke(*arguments)
            self.assertEqual(code, 0)
            self.assertEqual(stdout, entry['detail']['stdout'])
            self.assertIn(entry['detail']['stderr'], stderr)
            self.assertIn(request_id, stderr)
        self.assertEqual(self.worker.job_creations, 1)
        self.assertEqual(journal.read_bytes(), saved_journal)
        details = [request for request in self.worker.requests if request['op'] == 'job']
        self.assertEqual([request['job_id'] for request in details], [entry['result']['job_id']] * 2)

    def test_connection_rejects_nonloopback_and_malformed_descriptors(self):
        invalid = [
            {"version": 2}, {"token": "too-short"},
            {"endpoint": "http://example.invalid:8080/v1"},
            {"endpoint": "http://localhost:8080/v1"},
            {"endpoint": "http://0.0.0.0:8080/v1"},
            {"endpoint": "https://127.0.0.1:8080/v1"},
            {"endpoint": "http://127.0.0.1/v1"},
            {"endpoint": "http://127.0.0.1:8080/other"},
            {"endpoint": "http://user@127.0.0.1:8080/v1"},
            {"endpoint": "http://127.0.0.1:8080/v1?redirect=elsewhere"},
            {"endpoint": "http://127.0.0.1:8080/v1#fragment"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.write_connection(**changes)
                with self.assertRaises(ValueError):
                    bridge.Client(self.connection)
        self.connection.write_text("not-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            bridge.Client(self.connection)
        self.connection.write_text(" " * 32769, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid skill bridge connection"):
            bridge.Client(self.connection)
        self.assertEqual(self.worker.requests, [])

    def test_worker_authentication_error_is_reported_without_secret(self):
        self.write_connection(token="another-capability-" + uuid4().hex)
        with self.assertRaisesRegex(RuntimeError, "Fixture capability rejected") as caught:
            bridge.Client(self.connection).call("catalog")
        self.assertNotIn(self.worker.token, str(caught.exception))
        self.assertEqual(self.worker.requests, [])

    @unittest.skipIf(os.name == "nt", "POSIX ownership and mode are enforced on the SSH Linux side")
    def test_posix_connection_mode_owner_and_journal_permissions(self):
        self.connection.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "mode 600"):
            bridge.Client(self.connection)
        self.connection.chmod(0o600)
        with patch.object(bridge.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "owned by this user"):
                bridge.Client(self.connection)
        request_id = str(uuid4())
        self.invoke("run", "3d-assets", "--request-id", request_id, "--wait", "0", "--", "status")
        journal = self.connection.parent / "requests"
        self.assertEqual(journal.stat().st_mode & 0o777, 0o700)
        self.assertEqual((journal / (request_id + ".json")).stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "Symlink creation requires extra Windows privileges")
    def test_symlink_connection_is_rejected(self):
        linked = self.root / "linked-connection.json"
        linked.symlink_to(self.connection)
        with self.assertRaisesRegex(ValueError, "Invalid skill bridge connection"):
            bridge.Client(linked)


if __name__ == "__main__":
    unittest.main()
