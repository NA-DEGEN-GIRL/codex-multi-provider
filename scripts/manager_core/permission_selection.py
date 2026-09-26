"""Persist one runtime-validated permission selection for a local thread.

The scope is deliberately narrow: only a bare built-in ``dangerFullAccess``
policy is remembered, and it is replayed through the stable resume parameters
of the deployed app-server schema (``sandbox``, ``approvalPolicy``,
``approvalsReviewer``). The experimental ``permissions`` and
``runtimeWorkspaceRoots`` request parameters are never emitted, so a client
that does not support them is never handed one.

Evidence comes from the runtime only:

* successful ``thread/start`` / ``thread/resume`` / ``thread/fork`` responses,
  whose top level carries ``approvalPolicy``, ``approvalsReviewer``,
  ``sandbox`` (a *policy* object) and ``activePermissionProfile``;
* ``thread/settings/updated`` notifications, whose ``threadSettings`` carry
  ``approvalPolicy``, ``approvalsReviewer``, ``sandboxPolicy`` and
  ``activePermissionProfile``.

Only the built-in full profile (absent/null ``activePermissionProfile`` or
``:danger-full-access``) is convertible into the built-in sandbox mode. A
named/custom profile, a restricted workspace policy, or an approval value this
module cannot represent as a supported string retires any older selection for
the thread instead of being partially replayed, so a later resume cannot
restore a broader right the runtime no longer confirms. A resume that already
carries any permission key is never modified, which keeps a fresh choice -
including a narrower one chosen in the desktop UI - intact.
"""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from .store import atomic_json, identifier
from .updates import UpdateError, _lock_file, _unlock_file


SANDBOX_KINDS = {'readOnly': 'read-only', 'workspaceWrite': 'workspace-write',
                 'dangerFullAccess': 'danger-full-access'}
SANDBOX_PROFILES = {'read-only': ':read-only', 'workspace-write': ':workspace',
                    'danger-full-access': ':danger-full-access'}
BUILTIN_FULL_PROFILE = ':danger-full-access'
REQUEST_KEYS = ('permissions', 'sandbox', 'sandboxPolicy', 'approvalPolicy',
                'approvalsReviewer', 'runtimeWorkspaceRoots')
# ConfigToml keys (config_toml.rs) that decide permissions. A `config` HashMap
# entry may use the dotted form (`sandbox_workspace_write.network_access`), so
# the check is by prefix: any of these is a fresh authority that must never be
# replaced by a remembered selection.
CONFIG_PREFIXES = ('sandbox_mode', 'sandbox_workspace_write', 'approval_policy',
                   'approvals_reviewer', 'permissions', 'default_permissions',
                   'profile', 'profiles')
# Explicit runtime policy evidence. A confirmed policy this module cannot
# reproduce exactly still retires an older remembered selection for the thread;
# an error or a message without these fields must not.
EVIDENCE_FIELDS = ('sandboxPolicy', 'sandbox', 'permissions', 'activePermissionProfile',
                   'approvalPolicy', 'approvalsReviewer')
STORED_KEYS = ('sandbox', 'approval_policy', 'approvals_reviewer')
RESUME_METHODS = ('thread/resume', 'thread/fork')
RESPONSE_METHODS = ('thread/start', 'thread/resume', 'thread/fork')
MAX_THREADS = 128
PENDING_REQUESTS = 64
SCHEMA = 1
LOCK_TIMEOUT = 0.1
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class Busy(RuntimeError):
    """Another writer holds the profile document; callers fail open."""


def _text(value, limit=128):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _sandbox(value):
    """Kebab-case mode from either wire form (request string or policy object)."""
    if isinstance(value, dict):
        kind = value.get('type')
        return SANDBOX_KINDS.get(kind) if isinstance(kind, str) else None
    return value if value in SANDBOX_PROFILES else None


def _bare(policy):
    return isinstance(policy, dict) and all(
        key == 'type' or policy[key] is None for key in policy)


def selection(value):
    """Canonical selection from a runtime result or thread settings, else None.

    Only one selection is reproducible against the deployed app-server resume
    schema: a bare ``dangerFullAccess`` policy, which maps one-to-one onto
    ``sandbox: "danger-full-access"``. Named profiles need the experimental
    ``permissions`` parameter and workspace-write policies carry
    network/writable-root restrictions that a mode string cannot express, so
    both are skipped - and therefore retire an older value - instead of being
    replayed through a parameter this app-server does not accept.
    """
    if not isinstance(value, dict):
        return None
    # A custom profile's provenance and restrictions cannot be reproduced
    # through the built-in sandbox mode, so only the built-in full profile (or
    # no profile at all) may be converted into ``sandbox: danger-full-access``.
    profile = value.get('activePermissionProfile')
    if profile is not None:
        profile_id = profile.get('id') if isinstance(profile, dict) else None
        if profile_id != BUILTIN_FULL_PROFILE:
            return None
    policy = value.get('sandboxPolicy')
    if not isinstance(policy, dict):
        policy = value.get('sandbox')
    if _sandbox(policy) != 'danger-full-access' or not _bare(policy):
        return None
    found = {'sandbox': 'danger-full-access'}
    # AskForApproval also has object forms (reject/custom). A value we cannot
    # replay as a supported string means the whole selection is not
    # representable: retiring the older value is safe, silently dropping the
    # restriction is not.
    approval = value.get('approvalPolicy')
    if approval is not None:
        approval = _text(approval)
        if approval is None:
            return None
        found['approval_policy'] = approval
    reviewer = value.get('approvalsReviewer')
    if reviewer is not None:
        reviewer = _text(reviewer)
        if reviewer is None:
            return None
        found['approvals_reviewer'] = reviewer
    return found or None


def carries_selection(params):
    """True when a request already states its own permission selection."""
    if not isinstance(params, dict):
        return False
    if any(params.get(key) is not None for key in REQUEST_KEYS):
        return True
    config = params.get('config')
    if not isinstance(config, dict):
        return False
    for key, value in config.items():
        if value is None or not isinstance(key, str):
            continue
        if any(key == prefix or key.startswith(prefix + '.') for prefix in CONFIG_PREFIXES):
            return True
    return False


def stored(value):
    """Validate a selection this module persisted earlier.

    The stored form is canonical and deliberately narrower than a runtime
    message: a bare ``danger-full-access`` sandbox plus optional approval
    fields. Unknown fields, named profiles, client-style sandbox strings or
    policy objects are rejected so malformed or outdated state is ignored
    instead of being re-emitted.
    """
    if not isinstance(value, dict) or not value:
        return None
    if set(value) - set(STORED_KEYS):
        return None
    if value.get('sandbox') != 'danger-full-access':
        return None
    found = {'sandbox': 'danger-full-access'}
    for key in ('approval_policy', 'approvals_reviewer'):
        item = value.get(key)
        if item is not None:
            item = _text(item)
            if item is None:
                return None
            found[key] = item
    return found


def has_evidence(value):
    """True when the runtime stated a policy, representable or not."""
    return isinstance(value, dict) and any(
        value.get(key) is not None for key in EVIDENCE_FIELDS)


def attach(params, value):
    """Copy of the resume params with the remembered selection added.

    Only deployed, non-experimental keys are written. A request that already
    states any selection is returned unchanged.
    """
    updated = deepcopy(params) if isinstance(params, dict) else {}
    if carries_selection(updated):
        return updated
    def fill(key, remembered):
        if updated.get(key) is None:
            updated[key] = remembered

    if value.get('sandbox'):
        fill('sandbox', value['sandbox'])
    if value.get('approval_policy'):
        fill('approvalPolicy', value['approval_policy'])
    if value.get('approvals_reviewer'):
        fill('approvalsReviewer', value['approvals_reviewer'])
    return updated


def requirements(payload):
    """Managed requirement fields from a configRequirements response, if any.

    The deployed schema types ``allowedPermissionProfiles`` as
    ``dict[str, bool]`` and ``allowedSandboxModes`` as a mode list; older
    shapes are accepted too. An empty list or an all-false map is a real,
    restrictive answer and is kept so the caller can fail closed on it.
    """
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for key in ('requirements', 'configRequirements', 'config'):
        if isinstance(payload.get(key), dict):
            candidates.append(payload[key])
    found = {}
    for candidate in candidates:
        profiles = candidate.get('allowedPermissionProfiles')
        if (isinstance(profiles, dict) and len(profiles) <= 64
                and all(isinstance(key, str) and type(item) is bool
                        for key, item in profiles.items())):
            found['allowedPermissionProfiles'] = dict(profiles)
        elif (isinstance(profiles, list) and len(profiles) <= 64
                and all(isinstance(item, str) for item in profiles)):
            found['allowedPermissionProfiles'] = list(profiles)
        modes = candidate.get('allowedSandboxModes')
        if (isinstance(modes, list) and len(modes) <= 16
                and all(item in SANDBOX_PROFILES for item in modes)):
            found['allowedSandboxModes'] = list(modes)
        for field in ('allowedApprovalPolicies', 'allowedApprovalsReviewers'):
            value = candidate.get(field)
            if (isinstance(value, list) and len(value) <= 64
                    and all(isinstance(item, str) for item in value)):
                found[field] = value
    return found or None


def within_requirements(limits, value):
    """True only when an observed managed ceiling permits this selection."""
    if not limits:
        return True
    sandbox = value.get('sandbox')
    if sandbox is not None:
        profiles = limits.get('allowedPermissionProfiles')
        if profiles is not None:
            named = SANDBOX_PROFILES.get(sandbox)
            allowed = (profiles.get(named) if isinstance(profiles, dict)
                       else named in profiles)
            if allowed is not True:
                return False
        modes = limits.get('allowedSandboxModes')
        if isinstance(modes, list) and (not modes or sandbox not in modes):
            return False
    approval = value.get('approval_policy')
    if limits.get('allowedApprovalPolicies') is not None and approval:
        if approval not in limits['allowedApprovalPolicies']:
            return False
    reviewer = value.get('approvals_reviewer')
    if limits.get('allowedApprovalsReviewers') is not None and reviewer:
        if reviewer not in limits['allowedApprovalsReviewers']:
            return False
    return True


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _path(directory, profile_id):
    return Path(directory) / 'profiles' / identifier(profile_id) / 'permission-selection.json'


@contextmanager
def _exclusive(directory, profile_id):
    """Serialise the read-modify-write across both proxy pumps and processes.

    Bounded to ``LOCK_TIMEOUT``: a writer that cannot take the lock raises
    ``Busy`` so the caller can leave the document and the RPC stream untouched
    instead of stalling a runtime pump.
    """
    path = _path(directory, profile_id).with_suffix('.lock')
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(path), threading.Lock())
    if not lock.acquire(timeout=LOCK_TIMEOUT):
        raise Busy()
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                stream = _lock_file(path)
                break
            except UpdateError:
                if time.monotonic() >= deadline:
                    raise Busy() from None
                time.sleep(.01)
        try:
            yield
        finally:
            _unlock_file(stream)
    finally:
        lock.release()


def _document(directory, profile_id):
    path = _path(directory, profile_id)
    document = {'schema': SCHEMA, 'profile_id': identifier(profile_id),
                'updated_at': _stamp(), 'threads': {}}
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 262144:
            return path, document
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return path, document
    if (isinstance(value, dict) and value.get('schema') == SCHEMA
            and value.get('profile_id') == identifier(profile_id)):
        threads = value.get('threads')
        if isinstance(threads, dict):
            document['threads'] = threads
        limits = requirements(value.get('requirements'))
        if limits is not None:
            document['requirements'] = limits
    return path, document


def remember(directory, profile_id, thread, value, *, source='runtime'):
    """Record - or retire - a runtime-validated selection for one thread.

    A confirmed policy that cannot be reproduced exactly (for example a
    workspace-write policy with network or writable-root restrictions and no
    profile id) clears any older selection for this thread, so a later resume
    without a selection cannot restore a broader value the runtime no longer
    confirmed. Errors and messages without policy evidence never clear.
    """
    thread = _thread_id(thread)
    found = selection(value)
    if thread is None or (found is None and not has_evidence(value)):
        return False
    try:
        with _exclusive(directory, profile_id):
            path, document = _document(directory, profile_id)
            threads = document['threads']
            threads.pop(thread, None)
            if found is not None:
                threads[thread] = {'selection': found, 'source': source,
                                   'observed_at': _stamp()}
                while len(threads) > MAX_THREADS:
                    threads.pop(next(iter(threads)))
            document['updated_at'] = _stamp()
            atomic_json(path, document)
    except Busy:
        return False
    return True


def remember_requirements(directory, profile_id, payload):
    """Persist an observed managed ceiling for this profile, if any."""
    limits = requirements(payload)
    if limits is None:
        return False
    try:
        with _exclusive(directory, profile_id):
            path, document = _document(directory, profile_id)
            document['requirements'] = limits
            document['updated_at'] = _stamp()
            atomic_json(path, document)
    except Busy:
        return False
    return True


def recall(directory, profile_id, thread):
    """Last runtime-validated selection for this thread, or None.

    A selection excluded by an observed managed ceiling is withheld here, so
    every caller fails closed without duplicating the rule.
    """
    thread = _thread_id(thread)
    if thread is None:
        return None
    try:
        with _exclusive(directory, profile_id):
            _, document = _document(directory, profile_id)
            limits = document.get('requirements')
            entry = document['threads'].get(thread)
    except Busy:
        return None
    if not isinstance(entry, dict) or stored(entry.get('selection')) is None:
        return None
    canonical = stored(entry['selection'])
    if not within_requirements(limits, canonical):
        return None
    return deepcopy(canonical)


class PermissionSelectionProxy:
    """Per-profile decoration for the managed local runtime proxy.

    ``to_runtime`` only fills a resume that states no permission selection of
    its own. ``from_runtime`` records successful responses and settings
    notifications. Failures never escape into the RPC stream.
    """

    def __init__(self, root, profile_id):
        self.directory = Path(root) / 'work/control-center'
        self.profile_id = identifier(profile_id)
        self.pending = OrderedDict()
        self.pending_lock = threading.Lock()

    def to_runtime(self, message):
        try:
            return self._to_runtime(message)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            return message

    def from_runtime(self, message):
        try:
            self._from_runtime(message)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            pass

    def _to_runtime(self, message):
        if not isinstance(message, dict):
            return message
        method = message.get('method')
        params = message.get('params')
        if (method in RESUME_METHODS and isinstance(params, dict)
                and not carries_selection(params)):
            thread = _thread_id(params.get('threadId'))
            if thread:
                value = recall(self.directory, self.profile_id, thread)
                if value is not None:
                    message = {**message, 'params': attach(params, value)}
        if 'id' in message and isinstance(method, str):
            key = json.dumps(message['id'], sort_keys=True)
            with self.pending_lock:
                self.pending[key] = method
                self.pending.move_to_end(key)
                while len(self.pending) > PENDING_REQUESTS:
                    self.pending.popitem(last=False)
        return message

    def _from_runtime(self, message):
        if not isinstance(message, dict) or 'error' in message:
            return
        method = message.get('method')
        if method == 'thread/settings/updated':
            params = message.get('params') or {}
            if not isinstance(params, dict):
                return
            thread = _thread_id(params.get('threadId'))
            settings = params.get('threadSettings')
            if thread and isinstance(settings, dict):
                remember(self.directory, self.profile_id, thread, settings)
            return
        if 'id' not in message or method is not None:
            return
        with self.pending_lock:
            requested = self.pending.pop(json.dumps(message['id'], sort_keys=True), None)
        if requested is None:
            return
        result = message.get('result')
        if requested == 'configRequirements/read':
            remember_requirements(self.directory, self.profile_id, result)
            return
        if requested in RESPONSE_METHODS and isinstance(result, dict):
            thread = _thread_id(result.get('id'))
            if thread is None and isinstance(result.get('thread'), dict):
                thread = _thread_id(result['thread'].get('id'))
            if thread:
                remember(self.directory, self.profile_id, thread, result)


def _thread_id(value):
    try:
        return identifier(value)
    except (ValueError, TypeError):
        return None
