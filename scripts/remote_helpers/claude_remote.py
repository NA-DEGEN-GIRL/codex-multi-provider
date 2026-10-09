"""Host-native Claude entry point selected by the pinned SSH launcher revision."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import UUID

from manager_core.claude_auth import ClaudeError, borrowed_credential, cli_version, scrub_environment
from manager_core.claude_protocol import encode_message
from manager_core.claude_runner import AUTH_CHANNEL_ENV, StdinLines, receive_lent_credentials, serve
from manager_core.claude_skills import prepare_shared_skills


# Only runtimes without the stdin channel put the lent credential here.
AUTH_ENV = 'CODEX_MANAGER_CLAUDE_AUTH'


def _hide_process():
    """Keep same-user processes, the agent's own tools included, out of this process's /proc
    files and memory while it holds a lent token. Its CLI child is unaffected."""
    if not sys.platform.startswith('linux'):
        return
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):
        pass


def _read(path, limit=65536):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('redirected Claude binding')
    if hasattr(os, 'getuid') and path.stat().st_uid != os.getuid():
        raise ValueError('foreign Claude binding')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Claude binding size')
    return json.loads(raw)


def _private(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('redirected Claude state')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if hasattr(os, 'getuid') and path.stat().st_uid != os.getuid():
        raise ValueError('foreign Claude state')
    os.chmod(path, 0o700)
    return path


class RemoteExecution:
    def __init__(self, profile, revision, binding_path, auth, *, expected_host=None):
        profile = Path(profile).resolve()
        if not re.fullmatch(r'[0-9a-f]{64}', revision or ''):
            raise ValueError('Claude definition revision')
        owner = str(UUID(profile.name))
        descriptor = _read(profile / 'definitions' / (revision + '.json'))
        definition = profile / 'definitions' / revision
        if (descriptor.get('revision') != revision or descriptor.get('profile_id') != owner
                or descriptor.get('definition') != str(definition)):
            raise ValueError('Claude definition owner')
        if expected_host is None:
            machine = Path('/etc/machine-id').read_text().strip()
            expected_host = hashlib.sha256((machine + '\\0' + str(os.getuid())
                                           + '\\0' + str(Path.home())).encode()).hexdigest()
        target = str(UUID(Path(binding_path).stem))
        relative = Path('claude-bindings') / (target + '.json')
        if Path(binding_path) != profile / 'codex' / relative:
            raise ValueError('Claude binding path')
        binding = _read(definition / relative)
        if (binding.get('schema_version') != 1 or binding.get('owner_profile_id') != owner
                or binding.get('target_profile_id') != target
                or binding.get('host_identity') != expected_host
                or descriptor.get('host_identity') != expected_host):
            raise ValueError('Claude account or host binding')
        identity = binding.get('expected_account_identity')
        if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity):
            raise ValueError('Claude account identity')
        borrowed_credential(auth, target, identity, int(time.time()) + 30)
        self.profile, self.binding, self.auth = profile, binding, auth
        self.root = _private(profile.parent.parent / 'claude')
        self.ledger_directory = _private(self.root / 'state' / owner)
        self.configuration_directory = _private(self.root / 'accounts' / target)

    def prepare(self, cwd):
        if self.auth['expiresAt'] < int(time.time()) + 30:
            raise ClaudeError('claude_account_unavailable', 'The selected Claude access token expired before this turn.')
        path = Path(self.binding['cli_path'])
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ClaudeError('cli_missing', 'The prepared Linux Claude CLI is unavailable.')
        environment = scrub_environment(self.configuration_directory)
        version = cli_version(path, environment)
        if version != self.binding['cli_version']:
            raise ClaudeError('cli_changed', 'The Linux Claude CLI changed. Prepare this SSH profile again.')
        plugin = prepare_shared_skills(self.root, self.binding['target_profile_id'], cwd,
            config_home=self.profile / 'codex', output_root=self.ledger_directory / 'skill-plugins')
        # The runner keeps who lent the token and when it expires apart from the token, which
        # reaches each CLI launch by pipe and never enters an environment or the ledger.
        borrowed = {key: self.auth[key] for key in ('profileId', 'accountIdentity', 'expiresAt')}
        return dict(settings=self.binding['settings'], configuration_directory=self.configuration_directory,
                    environment=environment, cli=[str(path)], plugins=[plugin] if plugin else [],
                    status=dict(logged_in=True, method='oauth_token', cli_version=version,
                                account_identity=self.binding['expected_account_identity']),
                    borrowed_auth=borrowed, lent_token=self.auth['accessToken'])


def main(argv=None):
    _hide_process()
    # Pop before constructing the official CLI/MCP environments. Both values are set only
    # by the native task's selected-account resolver, never config. A current runtime asks
    # for the stdin channel instead, so the credential is never in /proc/<pid>/environ.
    raw_auth = os.environ.pop(AUTH_ENV, '')
    channel = os.environ.pop(AUTH_CHANNEL_ENV, '')
    incoming = StdinLines(sys.stdin.fileno())
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('command', choices=['claude-runner'])
        parser.add_argument('--binding', required=True)
        args = parser.parse_args(argv)
        if channel == 'stdin' and not raw_auth:
            auth = receive_lent_credentials(incoming, sys.stdout.buffer)
        else:
            if len(raw_auth.encode('utf-8')) > 70000:
                raise ValueError('Claude auth size')
            auth = json.loads(raw_auth)
        raw_auth = None
        context = RemoteExecution(Path(os.environ['CODEX_MANAGER_PROFILE_DIR']),
            os.environ.get('CODEX_MANAGER_DEFINITION_REVISION'), args.binding, auth)
        auth = None
        return serve(context.root, context.binding['target_profile_id'], incoming=incoming,
                     execution_context=context)
    except (OSError, ValueError, KeyError, TypeError):
        sys.stdout.buffer.write(encode_message(dict(type='refused', code='remote_claude_unavailable',
            message='The SSH Claude account or installed CLI binding is unavailable. Prepare this profile again.')))
        sys.stdout.buffer.flush()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
