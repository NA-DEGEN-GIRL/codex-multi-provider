"""Portable SSH-side client for the workspace's Windows skill worker (stdlib only)."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

CHUNK = 1024 * 1024


def connection_path():
    explicit = os.environ.get('CODEX_WORKSPACE_SKILL_BRIDGE')
    if explicit:
        return Path(explicit)
    homes = [Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))), Path.home() / '.codex']
    return next((h / 'workspace-skill-bridge/connection.json' for h in homes
                 if (h / 'workspace-skill-bridge/connection.json').is_file()),
                homes[-1] / 'workspace-skill-bridge/connection.json')


class Client:
    def __init__(self, path=None):
        self.path = Path(path) if path else connection_path()
        if self.path.is_symlink() or self.path.stat().st_size > 32768:
            raise ValueError('Invalid skill bridge connection file.')
        if os.name != 'nt' and (self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077):
            raise ValueError('The skill bridge connection must be owned by this user with mode 600.')
        self.config = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(self.config, dict) or not isinstance(self.config.get('endpoint'), str):
            raise ValueError('Invalid skill bridge connection data.')
        address = urllib.parse.urlsplit(self.config['endpoint'])
        if (self.config.get('version') != 1 or address.scheme != 'http' or address.hostname != '127.0.0.1'
                or not address.port or address.path != '/v1' or address.query or address.fragment or address.username):
            raise ValueError('The skill bridge requires its authenticated SSH loopback endpoint.')
        if not isinstance(self.config.get('token'), str) or len(self.config['token']) < 32:
            raise ValueError('Invalid skill bridge credential.')
        # Never send a loopback capability through a user's HTTP proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, op, **params):
        raw = json.dumps(dict(op=op, **params), ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(self.config['endpoint'], raw,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.config['token']})
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.loads(response.read(4 * CHUNK))
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read(8192))
                message = body.get('message', 'Skill bridge request rejected.')
            except (ValueError, UnicodeError):
                message = 'Skill bridge request rejected.'
            raise RuntimeError(message) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise RuntimeError('Windows skill worker is unavailable. Open the workspace app and check SSH skill status; do not resubmit an uncertain job.') from None

    def upload(self, path):
        source = Path(path).resolve(strict=True)
        if not source.is_file():
            raise ValueError('Upload requires a regular input file.')
        original = source.stat()
        digest = hashlib.sha256()
        with source.open('rb') as stream:
            for block in iter(lambda: stream.read(CHUNK), b''):
                digest.update(block)
        digest = digest.hexdigest()
        offset, upload_id = 0, None
        with source.open('rb') as stream:
            while True:
                block = stream.read(CHUNK)
                observed = source.stat()
                if ((observed.st_size, observed.st_mtime_ns) != (original.st_size, original.st_mtime_ns)
                        or (not block and offset != original.st_size)):
                    raise ValueError('Input file changed while uploading; prepare a stable input before retrying.')
                final = offset + len(block) == original.st_size
                payload = dict(name=source.name, data=base64.b64encode(block).decode(), offset=offset, final=final)
                if upload_id:
                    payload['upload_id'] = upload_id
                if final:
                    payload['sha256'] = digest
                result = self.call('upload', **payload)
                upload_id = result['upload_id']
                offset += len(block)
                if final:
                    return result

    def fetch(self, skill, source, destination):
        info = self.call('stat', skill=skill, path=source)
        target = Path(destination).absolute()
        if target.exists() or target.is_symlink():
            raise ValueError('Destination already exists; choose a new file to preserve current project assets.')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.bridge-' + uuid4().hex + '.part')
        offset, digest = 0, hashlib.sha256()
        try:
            with temporary.open('xb') as stream:
                while offset < info['size']:
                    part = self.call('download', skill=skill, path=source, offset=offset,
                                     length=min(CHUNK, info['size'] - offset))
                    data = base64.b64decode(part['data'], validate=True)
                    if not data or len(data) > min(CHUNK, info['size'] - offset) or part['offset'] != offset:
                        raise ValueError('Artifact download offset or size changed.')
                    stream.write(data); digest.update(data); offset += len(data)
            if digest.hexdigest() != info['sha256']:
                raise ValueError('Artifact changed while downloading. The destination was not replaced.')
            # Same-directory exclusive atomic publication: never expose a partial
            # artifact or overwrite a file created while the transfer was running.
            os.link(temporary, target)
            return dict(path=str(target), size=offset, sha256=digest.hexdigest())
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connection', type=Path)
    commands = parser.add_subparsers(dest='op', required=True)
    commands.add_parser('catalog')
    read = commands.add_parser('read'); read.add_argument('skill'); read.add_argument('path')
    upload = commands.add_parser('upload'); upload.add_argument('path')
    fetch = commands.add_parser('fetch'); fetch.add_argument('skill'); fetch.add_argument('path'); fetch.add_argument('destination')
    job = commands.add_parser('job'); job.add_argument('job_id')
    run = commands.add_parser('run'); run.add_argument('skill'); run.add_argument('--request-id'); run.add_argument('--wait', type=float, default=30)
    # Parse the bridge options separately from the skill's unchanged CLI arguments.
    actual = list(sys.argv[1:] if argv is None else argv)
    separator = actual.index('--') if '--' in actual else len(actual)
    trailing = actual[separator + 1:]
    args = parser.parse_args(actual[:separator])
    client = Client(args.connection)
    if args.op == 'catalog': result = client.call('catalog')
    elif args.op == 'read': result = client.call('read', skill=args.skill, path=args.path)
    elif args.op == 'upload': result = client.upload(args.path)
    elif args.op == 'fetch': result = client.fetch(args.skill, args.path, args.destination)
    elif args.op == 'job': result = client.call('job', job_id=args.job_id)
    else:
        if not trailing: parser.error('run requires -- followed by the skill command and arguments')
        request_id = args.request_id or str(uuid4())
        # Record the request before the network call so lost responses remain reconcilable.
        journal = client.path.parent / 'requests'
        if any(p.is_symlink() for p in (journal, *journal.parents)):
            raise ValueError('The bridge request journal must not use symbolic links.')
        journal.mkdir(mode=0o700, exist_ok=True)
        from uuid import UUID
        request_id = str(UUID(request_id))
        record = journal / (request_id + '.json')
        payload = dict(skill=args.skill, argv=trailing, request_id=request_id)
        if record.is_symlink():
            raise ValueError('The bridge request record must not be a symbolic link.')
        try:
            with record.open('x', encoding='utf-8') as stream:
                record.chmod(0o600); json.dump(payload, stream)
        except FileExistsError:
            if json.loads(record.read_text(encoding='utf-8')) != payload:
                raise ValueError('This request ID already belongs to another command.')
        print('Skill bridge request: ' + request_id, file=sys.stderr, flush=True)
        result = client.call('run', **payload)
        if result['status'] not in ('queued', 'running'):
            result = client.call('job', job_id=result['job_id'])
        deadline = time.monotonic() + max(0, min(args.wait, 300))
        while result['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.25)
            result = client.call('job', job_id=result['job_id'])
        result['request_id'] = request_id
        if result['status'] == 'complete':
            print(result.get('stdout', ''), end='')
            if result.get('stderr'): print(result['stderr'], file=sys.stderr, end='')
            return int(result.get('exit_code', 0))
        if result['status'] in ('failed', 'interrupted'):
            print(json.dumps(result, ensure_ascii=False)); return 1
    if args.op == 'read' and 'text' in result:
        print(result['text'], end='')
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print('Skill bridge: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
