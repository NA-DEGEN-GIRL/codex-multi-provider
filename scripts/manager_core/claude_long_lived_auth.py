"""Per-account long-lived Claude tokens (`claude setup-token`) for SSH turns.

Windows manager only: this module is never part of ``remote_helper_files()``.
The user pastes a token that the official CLI printed once; the manager keeps
it for SSH and delegated Claude turns of that account, which otherwise borrow
the PC login's access token (about 8 hours).

Storage
  The token is saved as a current-user DPAPI blob with purpose entropy bound to
  the profile, at work/control-center/credentials/claude/<profile>/<credential>.dpapi.
  The blob holds {v, profile_id, credential_id, token}; a blob is used only
  when both IDs match the metadata. Metadata lives on the profile record under
  METADATA_KEY and never holds the token or a hash of it.

Threat model
  DPAPI protects the saved token against other Windows users and offline copies
  of the manager folder. It does not protect against code running as this
  Windows user: such code can call CryptUnprotectData with the entropy below,
  which is public. The exposure equals the saved provider API keys and the CLI's
  own login file. Decrypting only in the lender is not a security boundary.

Locks
  The credential lock (one per profile) is always taken before the store lock,
  never the reverse. Rejection and unreadable marks take only the store lock.
  The lender takes neither: it reads the metadata, opens the blob and, when a
  concurrent replace removed that blob, reads the metadata once more.

Refusals
  A runtime reports a refused token once, in a report-only read; the broker
  records it with record_rejection(). Every later read of that turn only
  excludes the token (lend(excluded_credential_id=...)), so a turn that is still
  running never marks it rejected again after the user pressed 다시 시도.

Errors carry fixed text only. No message, log line or return value except
lend()'s result contains the token.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time as day_start
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from uuid import UUID, uuid4

from . import providers
from .store import Store, Unchanged, file_stamp, identifier

METADATA_KEY = 'claude_long_lived_token'
PRESENTATION_KEY = 'claude_long_lived'
SOURCE = 'longLivedToken'
# Set by SSH preparation on a binding whose runtime, helpers and authority accept
# credentialSource/credentialId; the settings panel counts it. The broker itself lends only
# when the running runtime declares the capability and the role's prepared authority has
# credential_sources == 1.
SSH_SUPPORT_KEY = 'claude_credential_sources'
DEFAULT_VALIDITY_DAYS = 365
MAX_VALIDITY_DAYS = 366
WARNING_DAYS = 30
# Lending stops a day before the computed expiry: the real expiry is known only
# to the server, and a turn that starts must not run into it.
STOP_MARGIN = 86400
# A 401 this close to the computed expiry is classified as expiry, not rejection.
EXPIRY_WINDOW = 2 * 86400
MIN_LENGTH, MAX_LENGTH = 40, 4096
_PASTE_LIMIT = 16384
_BLOB_LIMIT = 64 * 1024
_ENTROPY = b'codex-manager/claude-long-lived/v1\0'
_IDENTITY = re.compile(r'[0-9a-f]{64}')
_TOKEN = re.compile(r'sk-ant-oat[A-Za-z0-9_-]+')
_INVISIBLE = frozenset('​‌‍⁠﻿')
_KINDS = ('rejected', 'expired')
_GUARD = threading.Lock()
_PROFILE_LOCKS = {}
_PRESET_CACHE = {}

FORMAT_MESSAGE = '장기 토큰 형식이 아닙니다. claude setup-token이 출력한 sk-ant-oat… 토큰 전체를 붙여넣으세요.'
REFRESH_TOKEN_MESSAGE = '갱신 토큰(sk-ant-ort…)은 저장하지 않습니다. claude setup-token이 출력한 sk-ant-oat… 토큰을 붙여넣으세요.'
API_KEY_MESSAGE = 'API 키(sk-ant-api…)는 여기에 저장하지 않습니다. claude setup-token이 출력한 sk-ant-oat… 토큰을 붙여넣으세요.'
LENGTH_MESSAGE = '토큰 길이가 올바르지 않습니다. 일부만 복사되었을 수 있으니 터미널에서 토큰 전체를 다시 복사하세요.'
STORAGE_MESSAGE = '장기 토큰을 이 Windows 사용자 보호 저장소에 저장하지 못했습니다. 잠시 후 다시 시도하세요.'


class LongLivedTokenError(ValueError):
    """Fixed, user-facing text. Never built from the token or a decrypted value."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _Unreadable(Exception):
    pass


def _error(code, message):
    return LongLivedTokenError(code, message)


def normalize(value):
    """Return the pasted token without whitespace, or raise a fixed error.

    A console copy may wrap the token over several lines; every whitespace and
    zero-width character is removed. Only the CLI's OAuth prefix and character
    set are checked: an 8-hour access token has the same prefix and cannot be
    told apart locally.
    """
    try:
        if not isinstance(value, str) or len(value) > _PASTE_LIMIT:
            raise _error('claude_token_format', FORMAT_MESSAGE)
        token = ''.join(char for char in value if not char.isspace() and char not in _INVISIBLE)
        if token.startswith('sk-ant-ort'):
            raise _error('claude_token_format', REFRESH_TOKEN_MESSAGE)
        if token.startswith('sk-ant-api'):
            raise _error('claude_token_format', API_KEY_MESSAGE)
        if not token.startswith('sk-ant-oat') or not _TOKEN.fullmatch(token):
            raise _error('claude_token_format', FORMAT_MESSAGE)
        if not MIN_LENGTH <= len(token) <= MAX_LENGTH:
            raise _error('claude_token_format', LENGTH_MESSAGE)
        return token
    except LongLivedTokenError:
        raise
    except Exception:
        # A string operation on raw input must never surface its own text.
        raise _error('claude_token_format', FORMAT_MESSAGE) from None


def _canonical(value):
    """A canonical lowercase UUID string, else None. Never used to build a path unchecked."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None
    return parsed if parsed == value else None


def _profile_id(value):
    try:
        return identifier(value)
    except (ValueError, AttributeError, TypeError):
        raise _error('invalid_profile', 'Claude 프로필을 선택하세요.') from None


def _entropy(profile_id):
    return _ENTROPY + profile_id.encode('ascii')


def _protect(payload, profile_id):
    return providers._crypt_secret(payload, True, entropy=_entropy(profile_id))


def _unprotect(blob, profile_id):
    return providers._crypt_secret(blob, False, entropy=_entropy(profile_id))


def _directory(root, profile_id=None):
    root = Path(root).resolve()
    base = root / 'work' / 'control-center' / 'credentials' / 'claude'
    path = base if profile_id is None else base / profile_id
    item = path
    while item != root and root in item.parents:
        if item.is_symlink() or item.is_junction():
            raise OSError('linked credential path')
        item = item.parent
    if not path.resolve().is_relative_to(root):
        raise OSError('credential path outside manager storage')
    return path


def _blob_path(root, profile_id, credential_id):
    if _canonical(credential_id) is None or _canonical(profile_id) is None:
        raise OSError('invalid credential reference')
    return _directory(root, profile_id) / (credential_id + '.dpapi')


@contextmanager
def _credential_lock(root, profile_id):
    """Serialize save and delete of one profile, across threads and processes."""
    with _GUARD:
        lock = _PROFILE_LOCKS.setdefault((str(Path(root).resolve()), profile_id), threading.Lock())
    with lock:
        directory = _directory(root)
        with providers._lock(directory / (profile_id + '.lock')):
            yield


def _cleanup(root, profile_id, keep=None):
    """Best effort: Windows refuses to delete a blob a lender holds open.

    A file left behind is never used (its ID no longer matches the metadata)
    and is removed by the next save or delete of this profile.
    """
    try:
        directory = _directory(root, profile_id)
        entries = list(directory.iterdir()) if directory.is_dir() else []
    except OSError:
        return
    for entry in entries:
        if keep is not None and entry.name == keep + '.dpapi':
            continue
        try:
            if entry.is_symlink() or entry.is_junction() or not entry.is_dir():
                entry.unlink()
            else:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass
    if keep is None:
        try:
            directory.rmdir()
        except OSError:
            pass


def metadata(profile):
    """Validated copy of the saved metadata, or None when absent or malformed."""
    value = profile.get(METADATA_KEY) if isinstance(profile, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        if (value.get('version') != 1 or _canonical(value.get('credential_id')) is None
                or any(type(value.get(key)) is not int or value[key] <= 0
                       for key in ('saved_at', 'minted_at', 'expires_at'))
                or type(value.get('validity_days')) is not int
                or not 1 <= value['validity_days'] <= MAX_VALIDITY_DAYS
                or value.get('minted_source') not in ('paste_time', 'user_date')
                or not isinstance(value.get('account_identity'), str)
                or not _IDENTITY.fullmatch(value['account_identity'])
                or not (value.get('account_label') is None or isinstance(value['account_label'], str))
                or not (value.get('rejected_credential_id') is None
                        or _canonical(value['rejected_credential_id']) is not None)
                or not (value.get('rejection_kind') is None or value['rejection_kind'] in _KINDS)
                or any(not (value.get(key) is None or (type(value[key]) is int and value[key] > 0))
                       for key in ('last_rejected_at', 'unreadable_at'))):
            return None
    except (TypeError, AttributeError):
        return None
    result = dict(value)
    for key in ('account_label', 'rejected_credential_id', 'rejection_kind', 'last_rejected_at', 'unreadable_at'):
        result.setdefault(key, None)
    return result


def _claude_profile(store, profile_id, data=None):
    profile = store.profile(profile_id, data)
    if profile.get('auth_mode') != 'claude_code' or profile.get('view_only'):
        raise _error('invalid_profile', 'Claude 프로필을 선택하세요.')
    if profile.get('removed_at'):
        raise _error('invalid_profile', '제거한 프로필입니다. 계정을 복원한 뒤 다시 시도하세요.')
    return profile


def _validity(value):
    if value is None:
        return DEFAULT_VALIDITY_DAYS
    if type(value) is not int or not 1 <= value <= MAX_VALIDITY_DAYS:
        raise _error('claude_token_validity', '유효 기간은 발급 화면에 표시된 대로 1~366일로 입력하세요.')
    return value


def _minted(value, now):
    """Unix seconds of issue. Default: the paste time; a date means its local 00:00."""
    if value is None:
        return int(now), 'paste_time'
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        day = date.fromisoformat(value)
    except ValueError:
        raise _error('claude_token_minted', '발급일을 YYYY-MM-DD 형식으로 선택하세요.') from None
    today = datetime.fromtimestamp(now).date()
    if day > today:
        raise _error('claude_token_minted', '발급일은 오늘이나 그 이전이어야 합니다.')
    if day == today:
        return int(now), 'paste_time'
    return int(datetime.combine(day, day_start()).timestamp()), 'user_date'


def _encrypt(profile_id, credential_id, token):
    payload = bytearray(b'{"v":1,"profile_id":"' + profile_id.encode('ascii')
                        + b'","credential_id":"' + credential_id.encode('ascii') + b'","token":"')
    try:
        payload += token.encode('ascii')
        payload += b'"}'
        return _protect(payload, profile_id)
    except providers.ProviderError:
        raise _error('claude_token_storage', STORAGE_MESSAGE) from None
    finally:
        payload[:] = bytes(len(payload))


LOGIN_CHANGED_MESSAGE = 'Claude 로그인 계정이 바뀌었습니다. 로그인 상태를 확인한 뒤 다시 저장하세요.'
ATTESTED_ACCOUNT_MESSAGE = ('확인란에 표시된 계정과 지금 이 프로필에 로그인된 Claude 계정이 다릅니다. '
                            '로그인 상태를 확인하고 계정을 다시 확인한 뒤 저장하세요.')


def save(root, profile_id, token, *, attested, minted_on=None, validity_days=None,
         refresh=None, now=None, attested_account=None):
    """Save (or replace) the profile's token; return the presentation, never the token.

    Order (critique S4): validate, encrypt and drop the plaintext before the
    CLI status check, which can take 25 s. Then bind the identity, take the
    credential lock, write the generation file, update the metadata, and
    remove older generations best-effort. A crash before the metadata update
    keeps the previous token in use; the orphan file is ignored.

    ``attested_account`` is the masked account the user confirmed in the dialog
    (empty when none was shown). The token is bound to the login found at save
    time, so a different masked account there refuses the save: a token cannot
    be checked against its account locally, and the attestation is the only guard.
    """
    profile_id = _profile_id(profile_id)
    current = time.time() if now is None else now
    if attested is not True:
        raise _error('claude_token_attestation', '토큰을 발급한 계정이 이 프로필의 계정인지 확인하고 확인란을 선택하세요.')
    if attested_account is not None and (not isinstance(attested_account, str) or len(attested_account) > 320):
        raise _error('claude_login_changed', ATTESTED_ACCOUNT_MESSAGE)
    validity = _validity(validity_days)
    minted_at, minted_source = _minted(minted_on, current)
    expires_at = minted_at + validity * 86400
    if current >= expires_at - STOP_MARGIN:
        raise _error('claude_token_expired', '이 토큰은 이미 만료되었거나 하루 안에 만료됩니다. 새로 발급해 저장하세요.')
    store = Store(root)
    _claude_profile(store, profile_id)
    credential_id = str(uuid4())
    blob = _encrypt(profile_id, credential_id, normalize(token))
    token = None
    if refresh is None:
        from .claude_profiles import ClaudeProfiles
        refresh = ClaudeProfiles(store).refresh
    status = refresh(profile_id)
    profile = _claude_profile(store, profile_id)
    identity = profile.get('claude_account_identity')
    if (not isinstance(status, dict) or status.get('logged_in') is not True
            or not isinstance(identity, str) or not _IDENTITY.fullmatch(identity)):
        raise _error('claude_login_required',
                     '이 프로필의 Claude 로그인을 먼저 확인하세요. 로그인된 계정에만 장기 토큰을 저장할 수 있습니다.')
    label = (profile.get('claude_status') or {}).get('masked_email')
    label = label if isinstance(label, str) else None
    if attested_account is not None and attested_account != (label or ''):
        raise _error('claude_login_changed', ATTESTED_ACCOUNT_MESSAGE)
    value = dict(version=1, credential_id=credential_id, saved_at=int(current), minted_at=minted_at,
                 minted_source=minted_source, validity_days=validity, expires_at=expires_at,
                 account_identity=identity, account_label=label,
                 rejected_credential_id=None, rejection_kind=None, last_rejected_at=None, unreadable_at=None)

    def update(data):
        profile = _claude_profile(store, profile_id, data)
        if profile.get('claude_account_identity') != identity:
            raise _error('claude_login_changed', LOGIN_CHANGED_MESSAGE)
        replaced = METADATA_KEY in profile
        profile[METADATA_KEY] = value
        return dict(profile=profile, replaced=replaced)

    try:
        with _credential_lock(root, profile_id):
            path = _blob_path(root, profile_id, credential_id)
            providers._atomic_bytes(path, blob)
            try:
                result = store.mutate(update)
            except BaseException:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
            _cleanup(root, profile_id, keep=credential_id)
    except OSError:
        raise _error('claude_token_storage', STORAGE_MESSAGE) from None
    message = ('장기 토큰을 교체했습니다. 이전 토큰은 이 PC에서 삭제했습니다(Claude 계정에서 해지되지는 않습니다).'
               if result['replaced'] else
               '장기 토큰을 이 Windows 사용자용으로 암호화해 저장했습니다.')
    return dict(saved=True, replaced=result['replaced'],
                long_lived=presentation(root, profile_id, now=current), message=message)


def replace(root, profile_id, token, **options):
    """Save over an existing token; a new credential ID clears any rejection."""
    store = Store(root)
    if metadata(_claude_profile(store, _profile_id(profile_id))) is None:
        raise _error('claude_token_missing', '교체할 장기 토큰이 없습니다. 새로 저장하세요.')
    return save(root, profile_id, token, **options)


def delete(root, profile_id):
    """Remove the token from this PC only; idempotent. Claude-side revocation is separate."""
    profile_id = _profile_id(profile_id)
    store = Store(root)
    store.profile(profile_id)

    def update(data):
        profile = store.profile(profile_id, data)
        if METADATA_KEY not in profile:
            return Unchanged(False)
        profile.pop(METADATA_KEY)
        return True

    try:
        with _credential_lock(root, profile_id):
            existed = store.mutate(update)
            _cleanup(root, profile_id)
    except OSError:
        raise _error('claude_token_storage', '장기 토큰을 삭제하지 못했습니다. 잠시 후 다시 시도하세요.') from None
    return dict(removed=True, existed=existed, long_lived=presentation(root, profile_id),
                message=('이 PC에서 장기 토큰을 삭제했습니다. Claude 계정에서 토큰이 해지되지는 않으며, '
                         '진행 중인 SSH 작업은 끝날 때까지 이 토큰을 계속 사용합니다.') if existed else
                        '저장된 장기 토큰이 없습니다.')


def retry(root, profile_id, *, now=None):
    """Clear a recorded rejection (metadata only); a 401 may have been transient."""
    profile_id = _profile_id(profile_id)
    store = Store(root)

    def update(data):
        profile = store.profile(profile_id, data)
        value = metadata(profile)
        if value is None:
            raise _error('claude_token_missing', '저장된 장기 토큰이 없습니다.')
        if value['rejected_credential_id'] is None:
            return Unchanged(profile)
        profile[METADATA_KEY].update(rejected_credential_id=None, rejection_kind=None, last_rejected_at=None)
        return profile

    store.mutate(update)
    return dict(retried=True, long_lived=presentation(root, profile_id, now=now),
                message='거부 기록을 지웠습니다. 다음 SSH 작업부터 장기 토큰을 다시 사용합니다.')


def record_rejection(root, profile_id, credential_id, *, now=None):
    """Mark the current token rejected when ``credential_id`` is the current one.

    Called with remote input: the ID must be canonical, and it only ever
    selects metadata. A 401 within EXPIRY_WINDOW of the computed expiry is
    recorded as expiry. A newer saved token (another ID) is left untouched.
    Takes only the store lock.
    """
    credential_id = _canonical(credential_id)
    if credential_id is None:
        return False
    profile_id = _profile_id(profile_id)
    current = int(time.time() if now is None else now)
    store = Store(root)

    def update(data):
        profile = store.profile(profile_id, data)
        value = metadata(profile)
        if value is None or value['credential_id'] != credential_id:
            return Unchanged(False)
        kind = 'expired' if abs(current - value['expires_at']) <= EXPIRY_WINDOW else 'rejected'
        profile[METADATA_KEY].update(rejected_credential_id=credential_id, rejection_kind=kind,
                                     last_rejected_at=current)
        return True

    return store.mutate(update)


def _mark_unreadable(store, profile_id, credential_id, when):
    """Informational only: every read retries DPAPI, and a success clears it."""
    def update(data):
        profile = store.profile(profile_id, data)
        value = metadata(profile)
        if value is None or value['credential_id'] != credential_id or (
                (value['unreadable_at'] is not None) == (when is not None)):
            return Unchanged(False)
        profile[METADATA_KEY]['unreadable_at'] = when
        return True
    try:
        store.mutate(update)
    except (ValueError, RuntimeError, OSError):
        pass


def _read_blob(root, profile_id, credential_id):
    try:
        path = _blob_path(root, profile_id, credential_id)
        with path.open('rb') as stream:
            raw = stream.read(_BLOB_LIMIT + 1)
    except FileNotFoundError:
        raise
    except OSError:
        raise _Unreadable() from None
    if len(raw) > _BLOB_LIMIT:
        raise _Unreadable()
    try:
        value = json.loads(_unprotect(raw, profile_id))
        if (not isinstance(value, dict) or set(value) != {'v', 'profile_id', 'credential_id', 'token'}
                or value['v'] != 1 or value['profile_id'] != profile_id
                or value['credential_id'] != credential_id):
            raise ValueError()
        return normalize(value['token'])
    except Exception:
        # Any decrypt or parse failure, including an unexpected ctypes error, only makes this
        # generation unreadable; the broker then serves the turn with the PC login.
        raise _Unreadable() from None


def lend(root, profile_id, expected_identity, excluded_credential_id=None, *, now=None):
    """The broker's long-lived credential, or None to fall back to the PC login.

    The broker calls this only when the runtime declared the capability and the
    prepared authority allows it; every other condition is checked here:
    metadata valid, not marked rejected, the bound identity equal to both the
    role's expected identity and the profile's current login, before the stop
    margin, and a blob that decrypts with matching IDs. ``excluded_credential_id``
    names a token the turn's runner reported as refused: it is never returned,
    and nothing is recorded here (the report itself is, once).
    """
    current = int(time.time() if now is None else now)
    try:
        profile_id = _profile_id(profile_id)
    except LongLivedTokenError:
        return None
    if not isinstance(expected_identity, str) or not _IDENTITY.fullmatch(expected_identity):
        return None
    if excluded_credential_id is not None:
        excluded_credential_id = _canonical(excluded_credential_id)
        if excluded_credential_id is None:
            return None
    store = Store(root)
    previous = None
    for _attempt in range(2):
        try:
            profile = store.profile(profile_id)
        except (ValueError, RuntimeError, OSError):
            return None
        value = metadata(profile)
        if (value is None or profile.get('auth_mode') != 'claude_code'
                or profile.get('removed_at') or profile.get('view_only')
                or value['credential_id'] in (value['rejected_credential_id'], excluded_credential_id)
                or not (value['account_identity'] == expected_identity
                        == profile.get('claude_account_identity'))
                or current >= value['expires_at'] - STOP_MARGIN):
            return None
        credential_id = value['credential_id']
        try:
            token = _read_blob(root, profile_id, credential_id)
        except FileNotFoundError:
            # A concurrent replace or delete removed this generation (C5).
            if previous == credential_id:
                _mark_unreadable(store, profile_id, credential_id, current)
                return None
            previous = credential_id
            continue
        except _Unreadable:
            _mark_unreadable(store, profile_id, credential_id, current)
            return None
        if value['unreadable_at'] is not None:
            _mark_unreadable(store, profile_id, credential_id, None)
        return {'accessToken': token, 'expiresAt': value['expires_at'],
                'accountIdentity': expected_identity, 'credentialSource': SOURCE,
                'credentialId': credential_id}
    return None


# Reason codes the broker adds to a refused read when the saved token was refused or expired
# and the PC login could not replace it; the runtime explains them in English.
REJECTED_REASON, EXPIRED_REASON = 'claude_long_lived_rejected', 'claude_long_lived_expired'
# A token is saved for this account but the PC login is not confirmed, so the token is not
# lent (C10); older runtimes show their generic message for it.
PC_LOGIN_REASON = 'claude_pc_login_required'
# The answer to a report-only read: the refusal was recorded and no credential is lent.
RECORDED_REASON = 'claude_long_lived_recorded'


def refusal_reason(root, profile_id, *, now=None, excluded=None):
    """Why the saved token cannot be lent, when it was refused or has expired; else None.

    ``excluded`` is a token this turn's runner reported as refused; its report may still be on
    its way, so it already counts as refused here."""
    current = int(time.time() if now is None else now)
    try:
        value = metadata(Store(root).profile(_profile_id(profile_id)))
    except (ValueError, RuntimeError, OSError):
        return None
    if value is None:
        return None
    if value['rejected_credential_id'] == value['credential_id']:
        return EXPIRED_REASON if value['rejection_kind'] == 'expired' else REJECTED_REASON
    if current >= value['expires_at'] - STOP_MARGIN:
        return EXPIRED_REASON
    if excluded is not None and excluded == value['credential_id']:
        return EXPIRED_REASON if abs(current - value['expires_at']) <= EXPIRY_WINDOW else REJECTED_REASON
    return None


def waits_for_pc_login(profile, expected_identity):
    """Whether a token is saved for ``expected_identity`` while the profile's PC login is not
    confirmed, which is the only reason that token is not lent."""
    value = metadata(profile)
    return (value is not None and isinstance(profile, dict) and profile.get('auth_mode') == 'claude_code'
            and not profile.get('removed_at') and profile.get('claude_account_identity') is None
            and value['account_identity'] == expected_identity)


def presentation(root, profile_id, *, now=None):
    """The settings panel's view of one profile after an action, with its SSH usage."""
    store = Store(root)
    data = store.read()
    profile = store.profile(profile_id, data)
    return state(profile, now=now, ssh=ssh_usage(data.get('profiles') or [], profile['id'], preset_users(root)))


def preset_users(root):
    """Claude profile ID -> owner profile IDs whose saved presets use it as a role.

    Cached by file identity, so a state poll reads the preset file only after it
    was replaced.
    """
    path = Path(root).resolve() / 'work' / 'control-center' / 'execution-presets.json'
    stamp = file_stamp(path, replaced=True)
    if stamp == ():
        return {}
    cached = _PRESET_CACHE.get(str(path))
    if stamp and cached and cached[0] == stamp:
        return cached[1]
    users = {}
    try:
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        for item in data.get('presets', {}).values():
            if not isinstance(item, dict) or item.get('deleted'):
                continue
            for record in (item.get('revisions') or {}).values():
                for role in (record.get('roles') or []) if isinstance(record, dict) else []:
                    if isinstance(role, dict) and isinstance(role.get('profile_id'), str):
                        users.setdefault(role['profile_id'], set()).add(item.get('profile_id'))
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    if stamp:
        if len(_PRESET_CACHE) >= 8:
            _PRESET_CACHE.clear()
        _PRESET_CACHE[str(path)] = (stamp, users)
    return users


def ssh_usage(profiles, profile_id, users=None):
    """Prepared SSH bindings that run this Claude account, and how many can use the token."""
    owners = {profile_id} | set((users or {}).get(profile_id, ()))
    bindings = supported = 0
    for profile in profiles:
        if (not isinstance(profile, dict) or profile.get('id') not in owners
                or profile.get('removed_at') or profile.get('view_only')):
            continue
        for binding in profile.get('remote_bindings') or []:
            if not isinstance(binding, dict) or binding.get('prepared') is not True:
                continue
            if profile['id'] != profile_id and binding.get('execution_presets_version') != 1:
                continue
            bindings += 1
            supported += binding.get(SSH_SUPPORT_KEY) == 1
    return dict(bindings=bindings, supported=supported)


def _day(seconds):
    return datetime.fromtimestamp(seconds).strftime('%Y-%m-%d')


def state(profile, *, now=None, ssh=None):
    """Presentation for the shell. No secret, no hash; the email is masked."""
    current = int(time.time() if now is None else now)
    profile = profile if isinstance(profile, dict) else {}
    status = profile.get('claude_status') if isinstance(profile.get('claude_status'), dict) else {}
    logged_in = status.get('logged_in') is True
    ssh = ssh or dict(bindings=0, supported=0)
    if ssh['bindings'] == 0:
        ssh_text = '이 계정을 사용하는 SSH 연결이 아직 없습니다. SSH 준비 후 사용됩니다.'
    elif ssh['supported'] == 0:
        ssh_text = 'SSH 런타임 업데이트·재준비 후 사용됩니다.'
    else:
        ssh_text = f'SSH 연결 {ssh["supported"]}곳에서 사용합니다.' + (
            f' {ssh["bindings"] - ssh["supported"]}곳은 런타임 업데이트·재준비가 필요합니다.'
            if ssh['bindings'] > ssh['supported'] else '')
    raw = profile.get(METADATA_KEY)
    value = metadata(profile)
    result = dict(saved=raw is not None, state='none', label='장기 토큰 · 없음', tone='muted',
                  attention=False, attention_text='', card_text='', expires_on='', days_left=None,
                  last_rejected_on='', account_label=status.get('masked_email') if isinstance(
                      status.get('masked_email'), str) else '',
                  lendable=False, retry_available=False, fallback='', ssh=dict(ssh, text=ssh_text),
                  expanded=raw is not None or ssh['bindings'] > 0, logged_in=logged_in)
    if raw is None:
        return result
    # Where SSH turns of this account get their login while the saved token is not lent (U3).
    fallback = '지금은 Windows 로그인 토큰 사용' if logged_in else 'Windows 로그인도 필요'
    unreadable = dict(state='unreadable', label=f'읽을 수 없음 · 다시 저장 필요 · {fallback}', tone='warning',
                      attention=True, card_text='토큰 확인', fallback=fallback,
                      attention_text=f'Claude 장기 토큰을 읽을 수 없습니다 · {fallback} · Claude 로그인·설정에서 다시 저장하세요')
    if value is None:
        return dict(result, **unreadable)
    label = value['account_label'] or ''
    expires_on = _day(value['expires_at'])
    days = max(0, (value['expires_at'] - current) // 86400)
    rejected = value['rejected_credential_id'] == value['credential_id']
    identity = profile.get('claude_account_identity')
    result.update(expires_on=expires_on, days_left=days, account_label=label or result['account_label'],
                  last_rejected_on=_day(value['last_rejected_at']) if value['last_rejected_at'] else '')
    who = (' · ' + label) if label else ''
    if current >= value['expires_at'] or (rejected and value['rejection_kind'] == 'expired'):
        return dict(result, state='expired', label=f'만료됨 · 교체 필요 · {fallback}', tone='warning', attention=True,
                    card_text='토큰 확인', fallback=fallback,
                    attention_text=f'Claude 장기 토큰 만료 · {fallback} · Claude 로그인·설정에서 교체하세요')
    if rejected:
        when = f' ({result["last_rejected_on"]})' if result['last_rejected_on'] else ''
        return dict(result, state='rejected', label=f'거부됨{when} · 교체 필요 · {fallback}', tone='warning',
                    attention=True, card_text='토큰 확인', fallback=fallback, retry_available=True,
                    attention_text=f'Claude 장기 토큰 거부됨 · {fallback} · Claude 로그인·설정에서 교체하거나 다시 시도하세요')
    # The token's own problems come before the login it depends on: a new login does not fix them.
    if current >= value['expires_at'] - STOP_MARGIN:
        return dict(result, state='stopped', label=f'만료(사용 중지) · {expires_on} · 교체 필요 · {fallback}',
                    tone='warning', attention=True, card_text='토큰 확인', fallback=fallback,
                    attention_text=f'Claude 장기 토큰 만료(사용 중지) · {expires_on} · {fallback} · '
                                   'Claude 로그인·설정에서 교체하세요')
    if value['unreadable_at'] is not None:
        return dict(result, **unreadable)
    if identity is None or not logged_in:
        # The token is lent only while the PC login of the same account is confirmed (C10); SSH
        # turns of this account then fail, which matters only where an SSH binding runs it.
        attention = ssh['bindings'] > 0
        return dict(result, state='login_needed', tone='warning' if attention else 'muted',
                    label=f'저장됨{who} · 이 PC의 Claude 로그인이 확인되면 사용합니다',
                    attention=attention, card_text='토큰 확인' if attention else '',
                    attention_text=('Claude 장기 토큰을 쓰려면 이 PC의 Claude 로그인이 필요합니다 · '
                                    'Claude 로그인·설정에서 로그인 상태를 확인하세요') if attention else '')
    if identity != value['account_identity']:
        return dict(result, state='other_account', label='다른 계정용 · 교체 필요', tone='warning',
                    attention=True, card_text='토큰 확인',
                    attention_text='Claude 장기 토큰이 다른 계정용입니다 · Claude 로그인·설정에서 교체하세요')
    if value['expires_at'] - current <= WARNING_DAYS * 86400:
        return dict(result, state='expiring', label=f'만료 임박{who} · {expires_on} ({days}일 남음)', tone='warning',
                    attention=True, card_text='토큰 확인', lendable=True,
                    attention_text=f'Claude 장기 토큰 만료 임박 · {expires_on} · Claude 로그인·설정에서 교체하세요')
    return dict(result, state='active', label=f'설정됨{who} · 만료 {expires_on}', tone='ready', lendable=True)


def issue_command(cli, environment):
    """The console command line that keeps the window open after `claude setup-token` exits.

    CLI 2.1.282 exits half a second after it prints the token, and a console that belongs to
    the CLI alone closes with it (conhost at once; Windows Terminal on exit code 0), before the
    token can be copied. cmd.exe /k keeps the window until the user closes it; /d skips AutoRun
    commands and /s takes the quoted CLI path verbatim. Returns (cmd.exe path, command line).
    """
    comspec = Path(environment.get('SystemRoot') or environment.get('SYSTEMROOT') or r'C:\Windows') / 'System32' / 'cmd.exe'
    if not comspec.is_absolute() or not comspec.is_file():
        raise _error('configuration_path', '발급 창을 열 Windows 명령 프롬프트(cmd.exe)를 찾지 못했습니다.')
    cli = str(cli)
    # cmd.exe expands %NAME% even inside quotes; such a path would run something else.
    if any(char in cli for char in '%"\r\n') or not Path(cli).is_absolute():
        raise _error('configuration_path', 'Claude CLI 경로에 발급 창에서 쓸 수 없는 문자가 있습니다. 직접 실행하세요.')
    return str(comspec), f'"{comspec}" /d /s /k ""{cli}" setup-token"'


def launch_issue_console(profile_id, cli_path=None, environ=None):
    """Open the official `claude setup-token` in a new console the manager never reads.

    The environment is scrubbed of every credential variable, and the CLI runs
    with a new, empty, throwaway configuration directory, never the profile's
    own, so it cannot touch the profile's login. No pipes are attached. A
    command prompt stays open around the CLI until the user closes it.
    """
    if os.name != 'nt':
        raise _error('interactive_login_required', '장기 토큰 발급 창은 Windows에서만 열 수 있습니다.')
    from .claude_auth import cli_version, config_dir, discover_cli, scrub_environment
    profile_id = _profile_id(profile_id)
    base = config_dir(profile_id, environ).parent.parent / 'claude-setup-token'
    if base.resolve() != base:
        raise _error('configuration_path', '발급용 임시 폴더 경로를 확인하세요.')
    base.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 86400
    for old in base.iterdir():
        try:
            if old.is_dir() and not old.is_symlink() and not old.is_junction() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    directory = Path(tempfile.mkdtemp(prefix='issue-', dir=base))
    environment = scrub_environment(directory, environ)
    cli = discover_cli(cli_path, environ)
    cli_version(cli, environment)
    executable, command = issue_command(cli, environment)
    process = subprocess.Popen(command, executable=executable, env=environment, cwd=directory,
                               creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    return dict(pid=process.pid, status='issue_started',
                message='새 콘솔 창에서 claude setup-token을 시작했습니다. 브라우저 승인 화면의 계정이 이 프로필과 같은지 확인하세요. '
                        '토큰을 복사한 뒤 그 창은 직접 닫으세요. 이 관리 앱은 그 창의 내용을 읽지 않습니다.')
