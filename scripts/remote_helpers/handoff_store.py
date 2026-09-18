"""Bounded canonical storage operations, executed as the existing SSH user.

The caller sends this helper and the portable authority implementation over
stdin. No daemon, login file, model request, or general filesystem RPC is used.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID

import authority
import managed_sources


def uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('canonical UUID required')
    return value


def read_json(path, limit=256 * 1024):
    if path.is_symlink():
        raise ValueError('metadata symlink')
    info = path.stat()
    if info.st_nlink != 1 or info.st_size > limit:
        raise ValueError('metadata bounds')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('metadata object required')
    return value


def atomic(path, value):
    if path.is_symlink() or path.parent.resolve(strict=True) != path.parent:
        raise ValueError('metadata path changed')
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)


class Storage:
    def __init__(self, directory=None):
        self.directory = directory or Path.home() / '.local/share/codex-control-center'
        if self.directory.resolve(strict=True) != self.directory:
            raise ValueError('managed directory path changed')

    def profile(self, profile_id):
        profile = self.directory / 'profiles' / uuid(profile_id)
        home = profile / 'codex'
        if profile.resolve(strict=True) != profile or home.resolve(strict=True) != home:
            raise ValueError('profile path changed')
        if read_json(home / 'managed-source.json', 4096) != {'host_id': 'local', 'store_id': 'manager:' + profile_id}:
            raise ValueError('source identity changed')
        return profile

    def inspect(self, request):
        profile = self.profile(request['profile_id'])
        revision = request['revision']
        if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision):
            raise ValueError('revision')
        definition = read_json(profile / 'definitions' / (revision + '.json'))
        runtime = Path(definition['runtime'])
        machine = Path('/etc/machine-id').read_text().strip()
        host_identity = hashlib.sha256((machine + '\\0' + str(os.getuid()) + '\\0' + str(Path.home())).encode()).hexdigest()
        if (definition.get('profile_id') != profile.name or definition.get('revision') != revision
                or definition.get('host_identity') != host_identity or request['host_identity'] != host_identity
                or definition.get('managed_sources') is not True
                or runtime != self.directory / 'runtime' / request['runtime_bundle']
                or runtime.resolve(strict=True) != runtime):
            raise ValueError('remote definition mismatch')
        result = dict(profile_id=profile.name, home=str(profile / 'codex'), revision=revision,
                      runtime=str(runtime), host_identity=host_identity)
        if request.get('require_running'):
            process = read_json(profile / 'native-instance.json', 8192)
            pid = process['pid']
            if type(pid) is not int or pid <= 0: raise ValueError('runtime PID')
            stat = Path('/proc') / str(pid) / 'stat'
            text = stat.read_text()
            fields = text[text.rfind(')') + 2:].split()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            executable = (Path('/proc') / str(pid) / 'exe').resolve(strict=True)
            if (fields[0] == 'Z' or fields[19] != process.get('process_start') or boot != process.get('boot_id')
                    or process.get('revision') != revision or executable != runtime / 'codex'):
                raise ValueError('remote process changed')
            # Compare only these exact entries; never return process environment
            # contents, which can include provider credentials.
            env = (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0')
            if (os.fsencode('CODEX_HOME=' + str(profile / 'codex')) not in env
                    or os.fsencode('CODEX_MANAGER_MANAGED_SOURCES=' + str(profile / 'managed-sources.json')) not in env):
                raise ValueError('runtime managed source binding absent')
            result['process'] = dict(pid=pid, process_start=process['process_start'], boot_id=boot)
        return result

    def dispatch(self, request):
        if not isinstance(request, dict): raise ValueError('request object')
        operation = request.get('operation')
        if operation == 'inspect': return self.inspect(request)
        profile = self.profile(request['profile_id'])
        home = profile / 'codex'
        if operation == 'read':
            return authority.read(home, uuid(request['thread_id']))
        if operation == 'manifest':
            grants = request['required']
            if not isinstance(grants, list) or len(grants) > 128: raise ValueError('grant bounds')
            with authority._authority_guard(profile / 'managed-sources.guard'):
                for grant in grants:
                    authority.validate(grant)
                    source = self.profile(grant['store_id'][8:]) / 'codex'
                    if authority.read(source, grant['thread_id']) != grant: raise ValueError('grant changed')
                path = managed_sources.generate(profile, atomic=atomic)
            return dict(path=str(path))
        if operation != 'transfer': raise ValueError('unsupported storage operation')
        transaction = uuid(request['transaction_id'])
        target = uuid(request['target_profile_id'])
        self.profile(target)
        grants = request['expected_grants']
        if not isinstance(grants, list) or not 1 <= len(grants) <= 128: raise ValueError('grant bounds')
        for grant in grants:
            authority.validate(grant)
            if grant['store_id'] != 'manager:' + profile.name: raise ValueError('source mismatch')
        proof = request.get('close_proof', {})
        if (proof.get('writerReleaseVerified') is not True
                or not isinstance(proof.get('closedThreadIds'), list)
                or len(proof['closedThreadIds']) != len(grants)
                or set(proof['closedThreadIds']) != {g['thread_id'] for g in grants}
                or proof.get('threadId') not in proof['closedThreadIds']):
            raise ValueError('whole subtree release proof required')
        journal_dir = self.directory / 'handoffs'
        journal_dir.mkdir(mode=0o700, exist_ok=True)
        if journal_dir.resolve(strict=True) != journal_dir: raise ValueError('journal symlink')
        file = journal_dir / (transaction + '.json')
        intent = dict(source_profile_id=profile.name, target_profile_id=target, expected_grants=grants, close_proof=proof)
        with authority._authority_guard(journal_dir / (transaction + '.guard')):
            if file.exists():
                previous = read_json(file)
                if previous.get('intent') == intent and previous.get('status') == 'complete':
                    # Return a known result without reapplying a CAS. The caller
                    # still verifies each current grant before activating.
                    return dict(grants=previous['changed_grants'], replayed=False, already_committed=True)
                raise RuntimeError('remote transaction needs recovery')
            journal = dict(version=1, transaction_id=transaction, status='commit_requested', intent=intent,
                           created_at=datetime.now(timezone.utc).isoformat(), changed_grants=[])
            atomic(file, journal)
            try:
                changed = authority.transfer_many(home, grants, target, release_verified=True)
                journal.update(status='complete', changed_grants=changed)
                atomic(file, journal)
                return dict(grants=changed, replayed=False, already_committed=False)
            except (OSError, ValueError, RuntimeError) as error:
                journal.update(status='recovery_required', changed_grants=getattr(error, 'changed', []))
                atomic(file, journal)
                raise
