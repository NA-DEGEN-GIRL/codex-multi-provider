"""Scoped, loopback-only execution of explicitly registered Windows skill CLIs.

This service is intentionally independent of SSH and account configuration. Its
owner supplies bearer-token scopes and a small CLI registration list. Job state
is durable; a process lost during execution is never automatically resubmitted.
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import secrets
import subprocess
import threading
import time
from uuid import UUID, uuid4


CHUNK_BYTES = 1024 * 1024
OUTPUT_BYTES = 512 * 1024  # Per stream; complete output remains in the job directory.
REQUEST_BYTES = 2 * CHUNK_BYTES
UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_MODULE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z')


class BridgeError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _error(code, message, status=400):
    raise BridgeError(code, message, status)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        _error('invalid_id', 'A canonical UUID is required.')
    return value


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _root(value):
    path = Path(value).absolute()
    for ancestor in (path, *path.parents):
        if _linked(ancestor):
            _error('unsafe_path', 'Linked directories are not allowed.')
    if not path.is_dir():
        _error('invalid_registration', 'A registered directory is unavailable.')
    return path.resolve()


def _parts(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        _error('unsafe_path', 'A relative path is required.')
    parts = value.split('/')
    if any(p in ('', '.', '..') or ':' in p or p.endswith((' ', '.'))
           or any(ord(c) < 32 for c in p) for p in parts):
        _error('unsafe_path', 'Traversal and ambiguous paths are not allowed.')
    if PurePosixPath(value).is_absolute():
        _error('unsafe_path', 'A relative path is required.')
    return parts


def _confined(root, relative, *, exists=True):
    for ancestor in (root, *root.parents):
        if _linked(ancestor):
            _error('unsafe_path', 'Linked files and directories are not allowed.')
    path = root
    for part in _parts(relative):
        path = path / part
        if _linked(path):
            _error('unsafe_path', 'Linked files and directories are not allowed.')
    if not path.resolve().is_relative_to(root.resolve()):
        _error('unsafe_path', 'The path is outside its allowed directory.')
    if exists and (not path.is_file() or path.stat().st_nlink != 1):
        _error('not_found', 'The requested file is unavailable.', 404)
    return path


def _atomic(path, value):
    if _linked(path):
        _error('unsafe_path', 'Linked state files are not allowed.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
            digest.update(chunk)
    return digest.hexdigest()


class BridgeServer:
    def __init__(self, state_dir, registrations, tokens, enabled_callback=None):
        self.state_dir = Path(state_dir).absolute()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir = _root(self.state_dir)
        self.lock = threading.RLock()
        self.enabled_callback = enabled_callback
        self.tokens = {}
        self.update_tokens(tokens)
        self.registrations = {}
        self.update_registrations(registrations)
        self.jobs, self.requests = {}, {}
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.httpd = self.thread = self.worker = None
        self._lease = None
        for name in ('jobs', 'uploads', 'incoming'):
            path = self.state_dir / name
            path.mkdir(exist_ok=True, mode=0o700)
            _root(path)
        self._acquire_lease()
        try:
            self._load_jobs()
        except BaseException:
            self._release_lease()
            raise

    def _acquire_lease(self):
        path = self.state_dir / 'server.lock'
        if _linked(path):
            _error('unsafe_path', 'Linked state files are not allowed.')
        stream = path.open('a+b')
        try:
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            _error('state_busy', 'This bridge state is already in use.', 409)
        self._lease = stream

    def _release_lease(self):
        with self.lock:
            stream, self._lease = self._lease, None
            if stream is not None:
                stream.close()

    def update_registrations(self, registrations):
        updated = {}
        for item in registrations:
            row = dict(item)
            name = row.get('name')
            commands = row.get('commands')
            if (not isinstance(name, str) or not _NAME.fullmatch(name) or name in updated
                    or not isinstance(row.get('module'), str) or not _MODULE.fullmatch(row['module'])
                    or not isinstance(commands, (list, tuple, set)) or not commands
                    or any(not isinstance(c, str) or not _NAME.fullmatch(c) for c in commands)):
                _error('invalid_registration', 'The skill registration is invalid.')
            row['root'], row['skill_dir'] = _root(row['root']), _root(row['skill_dir'])
            python = Path(row['python']).absolute()
            if not python.is_file() or _linked(python):
                _error('invalid_registration', 'The registered Python executable is unavailable.')
            row['python'], row['commands'] = str(python), frozenset(commands)
            row['description'] = str(row.get('description', ''))[:2000]
            updated[name] = row
        with self.lock:
            self.registrations = updated

    def update_tokens(self, tokens):
        """Replace bearer bindings without stopping active commands or the listener."""
        updated = dict(tokens)
        if any(not isinstance(k, str) or len(k) < 16 or not isinstance(v, str)
               or not v or len(v) > 512 for k, v in updated.items()):
            raise ValueError('Tokens must map bearer secrets (at least 16 characters) to nonempty scopes.')
        with self.lock:
            self.tokens = updated

    def _registration(self, name):
        row = self.registrations.get(name) if isinstance(name, str) else None
        if row is None:
            _error('skill_unavailable', 'The requested skill is not registered.', 404)
        if self.enabled_callback is not None:
            try:
                enabled = self.enabled_callback(name)
            except Exception:
                enabled = False
            if not enabled:
                _error('skill_disabled', 'The requested skill is disabled.', 403)
        return row

    def _job_file(self, job_id):
        return _confined(self.state_dir / 'jobs', _uuid(job_id) + '/job.json', exists=False)

    def _save_job(self, job):
        _atomic(self._job_file(job['job_id']), job)

    def _load_jobs(self):
        for path in (self.state_dir / 'jobs').glob('*/job.json'):
            path = self._job_file(path.parent.name)
            if not path.is_file() or path.stat().st_size > REQUEST_BYTES:
                raise ValueError('Invalid persisted job state.')
            job = json.loads(path.read_text(encoding='utf-8'))
            if (job.get('job_id') != path.parent.name or not isinstance(job.get('scope'), str)
                    or job.get('status') not in ('queued', 'running', 'complete', 'failed', 'interrupted')):
                raise ValueError('Invalid persisted job state.')
            _uuid(job.get('request_id'))
            key = (job['scope'], job['request_id'])
            if key in self.requests:
                raise ValueError('Duplicate persisted request identity.')
            if job['status'] == 'running':
                job.update(status='interrupted', exit_code=None, finished_at=time.time())
                self._save_job(job)
            self.jobs[job['job_id']] = job
            self.requests[key] = job['job_id']
            if job['status'] == 'queued':
                self.pending.put(job['job_id'])

    def start(self):
        with self.lock:
            if self.stopping.is_set():
                _error('closed', 'The bridge is closed.', 503)
            if self.httpd is not None:
                return {'port': self.httpd.server_port}
            owner = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.0'

                def log_message(self, *args):
                    pass  # Never log bearer headers or user payloads.

                def do_POST(self):
                    try:
                        self.connection.settimeout(30)
                        if self.path != '/v1':
                            _error('not_found', 'Unknown endpoint.', 404)
                        authorization = self.headers.get('Authorization', '')
                        token = authorization[7:] if authorization.startswith('Bearer ') else ''
                        scope = next((v for k, v in owner.tokens.items()
                                      if secrets.compare_digest(k.encode(), token.encode())), None)
                        if scope is None:
                            _error('unauthorized', 'A valid bearer token is required.', 401)
                        if self.headers.get('Transfer-Encoding'):
                            _error('invalid_request', 'Transfer encoding is not supported.')
                        length = int(self.headers.get('Content-Length', '0'))
                        if length < 1 or length > REQUEST_BYTES:
                            _error('request_too_large', 'The request size is invalid.', 413)
                        raw = self.rfile.read(length)
                        if len(raw) != length:
                            _error('invalid_request', 'The request body is incomplete.')
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            _error('invalid_request', 'A JSON object is required.')
                        result = owner.dispatch(scope, payload)
                        self.reply(200, result)
                    except BridgeError as error:
                        self.reply(error.status, {'error': error.code, 'message': str(error)})
                    except (ValueError, TypeError, KeyError, UnicodeError):
                        self.reply(400, {'error': 'invalid_request', 'message': 'The request is invalid.'})
                    except Exception:
                        self.reply(500, {'error': 'internal_error', 'message': 'The bridge could not complete the request.'})

                def reply(self, status, value):
                    body = json.dumps(value, ensure_ascii=False).encode('utf-8')
                    try:
                        self.send_response(status)
                        self.send_header('Content-Type', 'application/json; charset=utf-8')
                        self.send_header('Content-Length', str(len(body)))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        self.wfile.write(body)
                    except (OSError, ValueError):
                        pass

            self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            self.httpd.daemon_threads = True
            self.worker = threading.Thread(target=self._work, name='windows-skill-worker', daemon=True)
            self.thread = threading.Thread(target=self.httpd.serve_forever, name='skill-bridge-http', daemon=True)
            self.worker.start()
            self.thread.start()
            return {'port': self.httpd.server_port}

    def close(self):
        self.stopping.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            if self.thread is not None:
                self.thread.join(timeout=2)
        # The worker retains the state lease until its current command finishes.
        # Queued jobs stay durable for the next service start.
        if self.worker is None or not self.worker.is_alive():
            self._release_lease()

    def dispatch(self, scope, payload):
        if self.stopping.is_set():
            _error('closed', 'The bridge is closed.', 503)
        operation = payload.get('op')
        with self.lock:
            if operation == 'catalog':
                rows = []
                for name in self.registrations:
                    try:
                        row = self._registration(name)
                    except BridgeError:
                        continue
                    rows.append({k: str(row[k]) for k in ('name', 'root', 'description')})
                return {'skills': rows}
            if operation == 'read':
                return self._read(payload)
            if operation == 'upload':
                return self._upload(scope, payload)
            if operation in ('stat', 'download'):
                return self._artifact(scope, payload, operation)
            if operation == 'run':
                return self._run(scope, payload)
            if operation == 'job':
                return self._job(scope, payload)
        _error('unknown_operation', 'Unknown bridge operation.')

    def _read(self, payload):
        row = self._registration(payload.get('skill'))
        path = payload.get('path')
        if not isinstance(path, str):
            _error('unsafe_path', 'A documented skill path is required.')
        if path.startswith('skill:/'):
            relative, root = path[7:], row['skill_dir']
            parts = _parts(relative)
            allowed = relative == 'SKILL.md' or (parts[0] == 'references' and relative.endswith('.md'))
        elif path.startswith('runtime:/'):
            relative, root = path[9:], row['root']
            parts = _parts(relative)
            allowed = parts[0] == 'docs' and relative.endswith('.md')
        else:
            allowed = False
        if not allowed:
            _error('path_forbidden', 'Only skill and runtime Markdown documentation may be read.', 403)
        file = _confined(root, relative)
        if file.stat().st_size > CHUNK_BYTES:
            _error('file_too_large', 'The document exceeds the read limit.', 413)
        return {'path': path, 'text': file.read_text(encoding='utf-8-sig')}

    def _scope_root(self, scope, category):
        root = self.state_dir / category / hashlib.sha256(scope.encode()).hexdigest()
        root.mkdir(exist_ok=True, mode=0o700)
        return _root(root)

    def _upload(self, scope, payload):
        name = payload.get('name')
        if (not isinstance(name, str) or len(name) > 180 or name.startswith('.')
                or any(c in name for c in '\\/:\x00<>"|?*') or len(_parts(name)) != 1
                or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                    *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
            _error('unsafe_path', 'A plain artifact filename is required.')
        offset, final = payload.get('offset', 0), payload.get('final')
        if type(offset) is not int or offset < 0 or type(final) is not bool:
            _error('invalid_upload', 'A valid offset and final flag are required.')
        encoded = payload.get('data')
        if not isinstance(encoded, str) or len(encoded) > ((CHUNK_BYTES + 2) // 3) * 4:
            _error('chunk_too_large', 'The upload chunk exceeds one MiB.', 413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            _error('invalid_upload', 'Upload data must be valid base64.')
        if len(data) > CHUNK_BYTES or offset + len(data) > UPLOAD_BYTES:
            _error('chunk_too_large', 'The upload size limit was exceeded.', 413)
        expected = payload.get('sha256')
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)):
            _error('invalid_upload', 'The SHA-256 digest is invalid.')
        upload_id = _uuid(payload['upload_id']) if payload.get('upload_id') is not None else str(uuid4())
        incoming = self._scope_root(scope, 'incoming')
        meta = _confined(incoming, upload_id + '.json', exists=False)
        part = _confined(incoming, upload_id + '.part', exists=False)
        destination = _confined(self._scope_root(scope, 'uploads'), upload_id + '/' + name, exists=False)
        if meta.exists():
            record = json.loads(meta.read_text(encoding='utf-8'))
            if record['name'] != name or record.get('final'):
                _error('upload_conflict', 'This upload cannot accept more chunks.', 409)
            if not part.is_file() or part.stat().st_nlink != 1:
                _error('upload_conflict', 'The upload state is unavailable.', 409)
        else:
            if payload.get('upload_id') is not None:
                _error('not_found', 'The upload is unavailable in this scope.', 404)
            if offset != 0:
                _error('offset_mismatch', 'The first upload offset must be zero.', 409)
            record = {'name': name, 'size': 0, 'final': False}
        if offset != record['size'] or (part.exists() and part.stat().st_size != offset):
            _error('offset_mismatch', 'The upload offset does not match its current size.', 409)
        with part.open('ab' if meta.exists() else 'xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        record['size'] += len(data)
        _atomic(meta, record)
        if final:
            digest = _sha(part)
            if expected is not None and not secrets.compare_digest(expected, digest):
                _error('hash_mismatch', 'The finalized upload does not match its SHA-256 digest.', 409)
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            _confined(self._scope_root(scope, 'uploads'), upload_id + '/' + name, exists=False)
            if destination.exists():
                _error('upload_conflict', 'The artifact already exists.', 409)
            os.replace(part, destination)
            record.update(final=True, sha256=digest)
            _atomic(meta, record)
        return {'upload_id': upload_id, 'path': str(destination), 'size': record['size'],
                'final': record['final'], **({'sha256': record['sha256']} if record.get('final') else {})}

    def _artifact_path(self, scope, payload):
        value = payload.get('path')
        if not isinstance(value, str):
            _error('unsafe_path', 'An artifact path is required.')
        uploads = self._scope_root(scope, 'uploads')
        if value.startswith('upload:/'):
            return _confined(uploads, value[8:])
        row = self._registration(payload.get('skill'))
        if value.startswith('runtime:/'):
            relative = value[9:]
            parts = _parts(relative)
            if parts[0] not in ('.assets', '.work') or len(parts) < 2:
                _error('path_forbidden', 'Only runtime artifact directories may be read.', 403)
            return _confined(row['root'], relative)
        candidate = Path(value)
        if not candidate.is_absolute() or '..' in candidate.parts:
            _error('unsafe_path', 'An absolute or virtual artifact path is required.')
        for base in (uploads, row['root'] / '.assets', row['root'] / '.work'):
            try:
                relative = candidate.relative_to(base)
            except ValueError:
                continue
            # Check the runtime artifact directory itself as well as children.
            if _linked(base):
                _error('unsafe_path', 'Linked artifact directories are not allowed.')
            return _confined(base, relative.as_posix())
        _error('path_forbidden', 'The artifact is outside the permitted scope.', 403)

    def _artifact(self, scope, payload, operation):
        path = self._artifact_path(scope, payload)
        size = path.stat().st_size
        if operation == 'stat':
            return {'name': path.name, 'size': size, 'sha256': _sha(path)}
        offset, length = payload.get('offset', 0), payload.get('length', CHUNK_BYTES)
        if type(offset) is not int or type(length) is not int or offset < 0 or offset > size or not 1 <= length <= CHUNK_BYTES:
            _error('invalid_range', 'A valid download range of at most one MiB is required.')
        with path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(length)
        return {'data': base64.b64encode(data).decode('ascii'), 'offset': offset, 'size': size}

    def _run(self, scope, payload):
        request_id = _uuid(payload.get('request_id'))
        name, argv = payload.get('skill'), payload.get('argv')
        if (not isinstance(name, str) or not isinstance(argv, list) or not argv or len(argv) > 256
                or any(not isinstance(v, str) or '\x00' in v or len(v) > 32768 for v in argv)):
            _error('invalid_command', 'A skill command argument list is required.')
        fingerprint = hashlib.sha256(json.dumps({'skill': name, 'argv': argv}, sort_keys=True,
                                                   ensure_ascii=False).encode()).hexdigest()
        existing = self.requests.get((scope, request_id))
        if existing is not None:
            job = self.jobs[existing]
            if job['fingerprint'] != fingerprint:
                _error('request_conflict', 'This request ID was already used for a different command.', 409)
            return {'job_id': existing, 'status': job['status']}
        row = self._registration(name)
        if argv[0] not in row['commands'] or any(v == '--root' or v.startswith('--root=') for v in argv):
            _error('command_forbidden', 'The command or root override is not allowed.', 403)
        if self.pending.qsize() >= 256:
            _error('queue_full', 'The skill execution queue is full.', 503)
        job_id = str(uuid4())
        job = dict(job_id=job_id, request_id=request_id, scope=scope, skill=name, argv=argv,
                   fingerprint=fingerprint, status='queued', created_at=time.time())
        self._save_job(job)
        self.jobs[job_id] = job
        self.requests[(scope, request_id)] = job_id
        self.pending.put(job_id)
        return {'job_id': job_id, 'status': 'queued'}

    def _job(self, scope, payload):
        job = self.jobs.get(_uuid(payload.get('job_id')))
        if job is None or job['scope'] != scope:
            _error('not_found', 'The job is unavailable in this scope.', 404)
        result = {key: job[key] for key in ('job_id', 'status', 'created_at', 'started_at', 'finished_at', 'exit_code', 'error') if key in job}
        if job['status'] in ('complete', 'failed', 'interrupted'):
            for name in ('stdout', 'stderr'):
                path = _confined(self.state_dir / 'jobs', job['job_id'] + '/' + name + '.log', exists=False)
                size = path.stat().st_size if path.exists() else 0
                with path.open('rb') if path.exists() else io.BytesIO() as stream:
                    result[name] = stream.read(OUTPUT_BYTES).decode('utf-8', errors='replace')
                result[name + '_truncated'] = size > OUTPUT_BYTES
                result[name + '_size'] = size
        return result

    def _work(self):
        try:
            while not self.stopping.is_set():
                try:
                    job_id = self.pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                with self.lock:
                    if self.stopping.is_set():
                        break
                    job = self.jobs[job_id]
                    try:
                        row = self._registration(job['skill'])
                        if job['argv'][0] not in row['commands']:
                            _error('command_forbidden', 'The queued command is no longer permitted.', 403)
                        command = [row['python'], '-m', row['module'], '--root', str(row['root']), *job['argv']]
                        root = str(row['root'])
                        job.update(status='running', started_at=time.time())
                        self._save_job(job)
                    except Exception:
                        job.update(status='failed', error='skill_unavailable', exit_code=None, finished_at=time.time())
                        self._save_job(job)
                        continue
                try:
                    stdout = _confined(self.state_dir / 'jobs', job_id + '/stdout.log', exists=False)
                    stderr = _confined(self.state_dir / 'jobs', job_id + '/stderr.log', exists=False)
                    with stdout.open('xb') as out, stderr.open('xb') as err:
                        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                                   stdout=out, stderr=err, shell=False,
                                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        exit_code = process.wait()
                    with self.lock:
                        job.update(status='complete' if exit_code == 0 else 'failed',
                                   exit_code=exit_code, finished_at=time.time())
                        self._save_job(job)
                except Exception:
                    with self.lock:
                        job.update(status='failed', error='execution_failed', exit_code=None, finished_at=time.time())
                        self._save_job(job)
        finally:
            self._release_lease()
