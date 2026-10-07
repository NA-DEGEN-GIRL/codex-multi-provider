"""Record a working managed runtime, or return future launches to it.

    python scripts/rollback_manager_runtime.py --mark-good
        The active runtime (current.json) verifies and starts profiles:
        record it as artifacts/manager-runtime/last-known-good.json.
    python scripts/rollback_manager_runtime.py --rollback [--keep-replaced]
        Re-verify last-known-good and make it current again. The replaced
        runtime is added to known-bad.json unless --keep-replaced is given.
    python scripts/rollback_manager_runtime.py --mark-bad [current|previous|last-known-good|RELEASE_ID]
        Add a runtime to known-bad.json; activation and rollback refuse it.
        Profile launches do not read known-bad.json: marking the current
        runtime bad does not stop it being launched until --rollback (or
        another activation) replaces current.json.

Like activation, nothing here restarts a running profile; the next launch of
each profile uses the selected runtime.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from activate_manager_runtime import (RUNTIMES, known_bad, known_bad_entry, replace_current,
                                      require_activatable)
from manager_core.runtime_build import load_release
from manager_core.store import atomic_json

ROOT = Path(__file__).resolve().parents[1]
POINTERS = {'current': 'current.json', 'previous': 'previous.json', 'last-known-good': 'last-known-good.json'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _release_id(release):
    return Path(release.get('runtime') or '').parent.name


def _verified(root, pointer, name):
    try:
        return load_release(root, pointer)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise ValueError('The %s runtime no longer verifies (%s: %s).'
                         % (name, type(error).__name__, error)) from None


def mark_good(root=ROOT):
    """Record the active runtime, after the same checks a rollback will repeat."""
    root = Path(root).resolve()
    pointer = root / RUNTIMES / 'current.json'
    if not pointer.is_file():
        raise ValueError('No managed runtime is active; there is nothing to mark as good.')
    release = _verified(root, pointer, 'current')
    require_activatable(root, release)
    release['marked_good_at'] = _now()
    atomic_json(pointer.with_name('last-known-good.json'), release)
    return dict(last_known_good=_release_id(release), sha256=release['sha256'])


def mark_bad(root=ROOT, target='current', reason=None):
    """Add a pointer's runtime or a staged release to known-bad.json.

    Its files need not exist any more: the entry keeps the folder name and the
    codex.exe digest, so a re-staged copy of the same binary is refused too.
    """
    root = Path(root).resolve()
    if target in POINTERS:
        path = root / RUNTIMES / POINTERS[target]
    elif re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', target):
        path = root / RUNTIMES / 'releases' / target / 'candidate.json'
    else:
        raise ValueError('Name a pointer (current, previous, last-known-good) or a release folder.')
    if not path.is_file():
        raise ValueError('No runtime is recorded at ' + str(path.relative_to(root)) + '.')
    release = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(release, dict) or not _release_id(release) or not isinstance(release.get('sha256'), str):
        raise ValueError('The runtime record names no release folder or digest.')
    return _add_known_bad(root, release, reason or 'marked bad by hand')


def _add_known_bad(root, release, reason, *, by_folder=True):
    entries = known_bad(root)
    existing = known_bad_entry(root, release)
    if existing is not None:
        return dict(known_bad=existing, added=False)
    entry = dict(release=_release_id(release), sha256=release['sha256'], version=release.get('version'),
                 reason=reason, marked_at=_now())
    if not by_folder:
        entry.pop('release')
    atomic_json(root / RUNTIMES / 'known-bad.json', dict(version=1, releases=[*entries, entry]))
    return dict(known_bad=entry, added=True)


def rollback(root=ROOT, *, keep_replaced=False):
    """Make last-known-good current again once it still verifies and fits the stores."""
    root = Path(root).resolve()
    good = root / RUNTIMES / 'last-known-good.json'
    if not good.is_file():
        raise ValueError('No last-known-good runtime is recorded; mark a working runtime with --mark-good first.')
    # The release folder may have been deleted or changed since it was marked.
    release = _verified(root, good, 'last-known-good')
    require_activatable(root, release)
    current = root / RUNTIMES / 'current.json'
    try:
        replaced = json.loads(current.read_text(encoding='utf-8-sig')) if current.is_file() else None
    except (OSError, ValueError):
        replaced = None  # An unreadable pointer is replaced; it names nothing to record.
    if not isinstance(replaced, dict):
        replaced = None
    same_binary = replaced is not None and replaced.get('sha256') == release['sha256']
    same_folder = replaced is not None and _release_id(replaced) == _release_id(release)
    if same_binary and same_folder:
        return dict(runtime=release['runtime'], sha256=release['sha256'], changed=False,
                    running_profiles_restarted=False)
    release.pop('marked_good_at', None)
    release.update(activated_at=_now(), rolled_back_from=_release_id(replaced) if replaced else None)
    replace_current(root, release)
    result = dict(runtime=release['runtime'], sha256=release['sha256'], changed=True,
                  replaced=_release_id(replaced) if replaced else None, running_profiles_restarted=False)
    if replaced is not None and not keep_replaced and isinstance(replaced.get('sha256'), str):
        if same_binary:
            # Another folder with the very codex.exe being restored (a re-staged
            # copy): a known-bad entry would match the restored runtime by digest
            # and refuse it from the next activation or rollback on.
            result['known_bad_skipped'] = 'The replaced runtime has the same codex.exe as last-known-good.'
        else:
            # Same folder with another digest (a pointer edited by hand): list the
            # digest only, since the folder is the one being restored.
            result['known_bad'] = _add_known_bad(root, replaced, 'replaced by --rollback',
                                                 by_folder=not same_folder)['known_bad']
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--mark-good', action='store_true')
    action.add_argument('--rollback', action='store_true')
    action.add_argument('--mark-bad', nargs='?', const='current', metavar='TARGET',
                        help='Refuse this runtime in later activations and rollbacks. Launches still use '
                             'current.json until --rollback or another activation replaces it.')
    parser.add_argument('--keep-replaced', action='store_true',
                        help='With --rollback: do not add the replaced runtime to known-bad.json.')
    parser.add_argument('--reason', help='With --mark-bad: why the runtime is bad.')
    args = parser.parse_args()
    try:
        if args.mark_good:
            result = mark_good()
        elif args.rollback:
            result = rollback(keep_replaced=args.keep_replaced)
        else:
            result = mark_bad(target=args.mark_bad, reason=args.reason)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(json.dumps(result, ensure_ascii=False))
