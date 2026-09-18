"""Match a Windows account by identity and use llm-usage's supported rename API.

Run with the Python interpreter belonging to the installed llm-usage tool.
Only aliases and hashed identities cross SSH; no login tokens are transferred.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def fingerprint(profile):
    path = Path(profile.profile_dir) / 'auth.json'
    if path.is_symlink():
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024*1024 or info.st_uid != os.getuid():
                return None
            value = json.load(stream)
        account = (value.get('tokens') or {}).get('account_id')
        if not isinstance(account, str) or not account:
            return None
        return hashlib.sha256(b'codex-manager-account-v1\0' + account.encode()).hexdigest()
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def execute(request, store=None):
    from llm_usage.storage import StateStore
    from llm_usage.models import Provider
    from llm_usage.profiles import rename_profile, validate_alias
    store = store or StateStore()
    expected, alias = request['account_fingerprint'], validate_alias(request['alias'])
    registry = store.load_registry()
    matches = [p for p in registry.accounts if p.provider == Provider.CODEX and fingerprint(p) == expected]
    if len(matches) != 1:
        return dict(state='not_found' if not matches else 'ambiguous', matched_accounts=len(matches), changed=False)
    match = matches[0]
    if request.get('action') != 'sync':
        return dict(state='matched', account_id=match.id, remote_alias=match.alias, desired_alias=alias, changed=False)

    class CheckedStore:
        def mutate_registry(self, mutation):
            def checked(current):
                candidate = current.find(Provider.CODEX, match.alias)
                if candidate is None or candidate.id != match.id or fingerprint(candidate) != expected:
                    raise ValueError('Identity changed during alias synchronization.')
                return mutation(current)
            return store.mutate_registry(checked)

    result = rename_profile(CheckedStore(), Provider.CODEX, match.alias, alias)
    return dict(state='synced', account_id=result.id, remote_alias=result.alias,
                changed=match.alias != result.alias, identity_matched=True)


if __name__ == '__main__':
    try:
        request=json.loads(sys.stdin.buffer.read(8193))
        print(json.dumps(execute(request)))
    except Exception:
        print(json.dumps(dict(state='blocked', changed=False, message='llm-usage account identity or alias could not be verified.')))
        sys.exit(1)
