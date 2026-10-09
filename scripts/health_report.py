"""Read-only health report for managed runtime releases and logins.

    python scripts/health_report.py [--root PATH]

Prints one JSON document, then a short Korean summary: the runtime pointers
(current, last-known-good, previous) and whether their files still verify,
staged candidates newer than the active runtime, whether the current runtime's
migrations fit the live stores (and store migrations it does not embed), the
SSH update backlog and SSH runtimes pinned to an older bundle, Claude login
expiry, the last runtime exit of each profile,
runtimes that recovered after process-start retries and the manager release. It writes nothing and starts or stops nothing; of a
Claude credential file it reads expiresAt only.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from activate_manager_runtime import RUNTIMES, known_bad, known_bad_entry, release_problem
from manager_core.claude_auth import config_dir
from manager_core.runtime_build import load_release
from manager_core.runtime_migrations import (STORE_DIRECTORIES, applied_migrations, incompatible_migrations,
                                             store_paths)

ROOT = Path(__file__).resolve().parents[1]
POINTERS = ('current', 'last-known-good', 'previous')
CONTROL = 'work/control-center'


def _json(path, limit=64 * 1024 * 1024):
    """A JSON document, or None when the file is missing."""
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_size > limit:
        raise ValueError('File is too large: ' + path.name)
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _section(function, *arguments):
    """One failing section never hides the others."""
    try:
        return function(*arguments)
    except Exception as error:
        return dict(error=type(error).__name__ + ': ' + str(error)[:300])


def _compact(value):
    return {key: item for key, item in value.items() if item is not None}


def _relative(root, path):
    path = Path(path)
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def _bad(root, release):
    """known_bad (the entry's reason, or True), or known_bad_unreadable when the list cannot be read."""
    try:
        entry = known_bad_entry(root, release)
    except ValueError:
        return dict(known_bad_unreadable=True)
    return dict(known_bad=None if entry is None else (entry.get('reason') or True))


def runtime_pointer(root, name):
    path = root / RUNTIMES / (name + '.json')
    raw = _json(path)
    if raw is None:
        return dict(state='missing')
    if not isinstance(raw, dict):
        return dict(state='invalid', problem='The pointer is not a JSON object.')
    result = dict(release=Path(raw.get('runtime') or '').parent.name or None, sha256=raw.get('sha256'),
                  version=raw.get('version'), build_profile=raw.get('build_profile'),
                  build_origin=raw.get('build_origin'), activated_at=raw.get('activated_at'),
                  marked_good_at=raw.get('marked_good_at'),
                  # False: activation kept the record of a replaced runtime whose files no longer verified.
                  verified=raw.get('verified'))
    try:
        load_release(root, path)
        result['state'] = 'valid'
    except FileNotFoundError:
        result.update(state='missing_on_disk', problem='The release files are no longer on disk.')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result.update(state='invalid', problem=str(error))
    result.update(activation_problem=release_problem(raw), **_bad(root, raw))
    return _compact(result)


def candidates(root, pointers):
    """Staged releases newer than the active one (release ids start with the UTC staging time)."""
    releases = root / RUNTIMES / 'releases'
    active = pointers['current'].get('release')
    named = {pointer.get('release') for pointer in pointers.values()}
    pending, incomplete, older = [], [], 0
    for folder in (sorted(releases.iterdir()) if releases.is_dir() else ()):
        if not folder.is_dir() or folder.name in named:
            continue
        if active and folder.name < active:
            older += 1
            continue
        try:
            raw = _json(folder / 'candidate.json')
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            incomplete.append(folder.name)
            continue
        pending.append(_compact(dict(
            release=folder.name, staged_at=raw.get('staged_at'), version=raw.get('version'),
            build_profile=raw.get('build_profile'), build_origin=raw.get('build_origin'),
            runtime_present=(folder / Path(raw.get('runtime') or 'codex.exe').name).is_file(),
            activation_problem=release_problem(raw), **_bad(root, raw))))
    return dict(pending=pending, incomplete=incomplete, older_count=older)


def migrations(root):
    """The activation gate's verdict for the current runtime, plus migrations it does not embed.

    sqlx ignores applied migrations a runtime does not know, so those do not
    stop it; they do show the stores were migrated by a newer runtime.
    """
    raw = _json(root / RUNTIMES / 'current.json')
    embedded = raw.get('migrations') if isinstance(raw, dict) else None
    stores = store_paths(root)
    if not isinstance(embedded, list) or not embedded:
        return dict(state='unknown', stores=len(stores),
                    reason='The current runtime records no embedded migrations.')
    by_directory = {(item['directory'], item['version']) for item in embedded}
    by_description = {(item['version'], item['description']) for item in embedded}
    incompatible, ahead, unreadable = [], [], []
    for path in stores:
        name = _relative(root, path)
        directory = next((item for prefix, item in STORE_DIRECTORIES if path.name.startswith(prefix)), None)
        try:
            rows = applied_migrations(path)
        except (OSError, sqlite3.Error):
            unreadable.append(name)
            continue
        # The same comparison activation runs, one store at a time.
        incompatible += [problem.replace(str(path), name, 1)
                         for problem in incompatible_migrations(embedded, [path])]
        for version, description, _ in rows:
            known = ((directory, version) in by_directory if directory is not None
                     else (version, description) in by_description)
            if not known:
                ahead.append('%s: %d %s' % (name, version, description))
    return dict(state='incompatible' if incompatible else 'compatible', stores=len(stores),
                incompatible=incompatible, ahead=ahead, unreadable=unreadable)


def _aliases(state):
    return {item.get('id'): item.get('alias') for item in (state or {}).get('profiles', []) if isinstance(item, dict)}


def remote_updates(state, root=None):
    """Profile/host pairs whose managed SSH runtime has an update waiting.

    stale lists prepared bindings, and runtimes last seen running, on another
    bundle than the one artifacts/remote now offers for that host's platform
    (needs root). A stopped one would start that older bundle again.
    """
    if state is None:
        return dict(state='missing')
    aliases, pending, states, stale = _aliases(state), [], {}, []
    removed = {item.get('id') for item in state.get('profiles', [])
               if isinstance(item, dict) and item.get('removed_at')}
    for value in (state.get('remote_updates') or {}).values():
        if not isinstance(value, dict) or value.get('profile_id') in removed:
            continue  # An entry the removal of its profile left behind.
        managed = value.get('managed') or {}
        status = managed.get('state') or 'unknown'
        states[status] = states.get(status, 0) + 1
        if 'update_available' in (managed.get('state'), managed.get('version_state')):
            pending.append(_compact(dict(
                profile_id=value.get('profile_id'), profile=aliases.get(value.get('profile_id')),
                host=value.get('alias'), state=status, active_bundle=managed.get('active_bundle'),
                available_bundle=managed.get('available_bundle'), prepared_bundle=managed.get('prepared_bundle'),
                job=(value.get('job') or {}).get('state'), auto_apply=value.get('auto_apply'),
                checked_at=value.get('checked_at'))))
    if root is not None:
        stale = _stale_bundles(Path(root), state, aliases)
    return dict(pending=pending, states=states, stale=stale)


def _stale_bundles(root, state, aliases):
    from manager_core.remote_updates import offered_for_host
    result = []
    observed = state.get('remote_updates') or {}
    for profile in state.get('profiles', []):
        if not isinstance(profile, dict) or profile.get('removed_at') or profile.get('view_only'):
            continue
        for binding in profile.get('remote_bindings') or []:
            if not isinstance(binding, dict) or binding.get('prepared') is not True:
                continue
            offered = offered_for_host(root, state, profile.get('id'), binding.get('alias'),
                                       binding.get('host_identity'))
            if offered is None:
                continue  # The host's platform was never observed, or nothing is offered for it.
            managed = (observed.get('%s:%s' % (profile.get('id'), binding.get('alias'))) or {}).get('managed') or {}
            running = managed.get('active_bundle') if managed.get('running') is True else None
            prepared = binding.get('runtime_bundle')
            if prepared == offered and running in (None, offered):
                continue
            result.append(_compact(dict(
                profile_id=profile.get('id'), profile=aliases.get(profile.get('id')), host=binding.get('alias'),
                offered_bundle=offered, prepared_bundle=prepared if prepared != offered else None,
                running_bundle=running if running not in (None, offered) else None,
                checked_at=(observed.get('%s:%s' % (profile.get('id'), binding.get('alias'))) or {}).get('checked_at'))))
    return result


def _expires_at(path):
    """claudeAiOauth.expiresAt in milliseconds; nothing else is kept."""
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('The credential file is too large.')
    oauth = json.loads(path.read_bytes()).get('claudeAiOauth')
    value = oauth.get('expiresAt') if isinstance(oauth, dict) else None
    return value if type(value) is int else None


def claude_logins(state, now, environ=None):
    result = []
    for profile in (state or {}).get('profiles', []):
        if not isinstance(profile, dict) or profile.get('auth_mode') != 'claude_code' or profile.get('removed_at'):
            continue
        item = dict(profile_id=profile.get('id'), profile=profile.get('alias'))
        try:
            source = config_dir(profile.get('id'), environ) / '.credentials.json'
            if not source.is_file():
                item['state'] = 'missing'
            elif (expires := _expires_at(source)) is None:
                item['state'] = 'no_expiry'
            else:
                hours = round((expires / 1000 - now.timestamp()) / 3600, 1)
                item.update(state='expired' if hours <= 0 else 'valid', hours_left=hours,
                            expires_at=datetime.fromtimestamp(expires / 1000, timezone.utc).isoformat())
        except (OSError, ValueError, TypeError, AttributeError, OverflowError, RuntimeError):
            # Never report the reason: a parse error could quote the file.
            item['state'] = 'unreadable'
        result.append(item)
    return result


def runtime_exits(root, state):
    """The last_exit runtime_proxy records when a profile's runtime ends."""
    aliases, exits = _aliases(state), []
    for path in sorted((root / CONTROL / 'instances').glob('*/runtime-state.json')):
        try:
            data = _json(path, 16 * 1024 * 1024)
        except (OSError, ValueError):
            continue
        last = data.get('last_exit') if isinstance(data, dict) else None
        if isinstance(last, dict):
            exits.append(_compact(dict(profile_id=path.parent.name, profile=aliases.get(path.parent.name),
                                       exit_code=last.get('exit_code'), uptime_ms=last.get('uptime_ms'),
                                       initialize_completed=last.get('initialize_completed'),
                                       exited_at=last.get('exited_at'), retries=last.get('retries'))))
    return exits


def runtime_start_retries(root, state):
    """Profiles whose runtime_proxy replaced runtimes that failed during process start.

    start_retries lists each failed start a replacement took over (while the
    proxy runs and after); last_exit.retries counts them once it ended.
    recovered: the last runtime completed initialize - by its exit record, or
    while it still runs, by the observer.
    """
    aliases, result = _aliases(state), []
    for path in sorted((root / CONTROL / 'instances').glob('*/runtime-state.json')):
        try:
            data = _json(path, 16 * 1024 * 1024)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        attempts = data.get('start_retries')
        attempts = [item for item in attempts if isinstance(item, dict)] if isinstance(attempts, list) else []
        last = data.get('last_exit') if isinstance(data.get('last_exit'), dict) else None
        retries = len(attempts) or (last or {}).get('retries')
        if type(retries) is not int or retries <= 0:
            continue
        initialized = last.get('initialize_completed') if last is not None else data.get('initialized')
        result.append(_compact(dict(profile_id=path.parent.name, profile=aliases.get(path.parent.name),
                                    retries=retries, recovered=initialized is True,
                                    exit_codes=[item['exit_code'] for item in attempts
                                                if type(item.get('exit_code')) is int] or None,
                                    last_retry_at=attempts[-1].get('exited_at') if attempts else None)))
    return result


def manager_release(root):
    pointer = _json(root / 'artifacts/manager/current.json')
    if pointer is None:
        return dict(state='missing')
    directory = pointer.get('directory') if isinstance(pointer, dict) else None
    if not isinstance(directory, str) or not directory:
        return dict(state='invalid')
    return _compact(dict(id=Path(directory).name, created_at=pointer.get('created_at'),
                         state='valid' if Path(directory).is_dir() else 'missing_on_disk'))


def report(root=ROOT, *, now=None, environ=None):
    root = Path(root).resolve()
    now = now or datetime.now(timezone.utc)
    state_error = None
    try:
        state = _json(root / CONTROL / 'state.json')
        if state is not None and not isinstance(state, dict):
            raise ValueError('state.json is not a JSON object.')
    except (OSError, ValueError) as error:
        state, state_error = None, type(error).__name__ + ': ' + str(error)[:300]
    pointers = {name: _section(runtime_pointer, root, name) for name in POINTERS}
    value = dict(generated_at=now.isoformat(), manager_release=_section(manager_release, root),
                 runtime_pointers=pointers,
                 known_bad=_section(lambda: [entry.get('release') or entry.get('sha256')
                                             for entry in known_bad(root)]),
                 candidates=_section(candidates, root, pointers),
                 migrations=_section(migrations, root),
                 remote_updates=_section(remote_updates, state, root),
                 claude_logins=_section(claude_logins, state, now, environ),
                 runtime_exits=_section(runtime_exits, root, state),
                 runtime_start_retries=_section(runtime_start_retries, root, state))
    if state_error is not None:
        value['state_error'] = state_error
    return value


POINTER_STATES = {'valid': '정상', 'missing': '없음', 'missing_on_disk': '파일 없음', 'invalid': '검증 실패'}


def _failed(section):
    return isinstance(section, dict) and 'error' in section


def summary(value):
    lines = ['[상태 점검] ' + value['generated_at']]
    manager = value['manager_release']
    # artifacts/manager/current.json selects the release for the next start, not the running one.
    label = '관리 앱 릴리스(다음 실행용): '
    if _failed(manager):
        lines.append(label + '확인 실패')
    elif manager.get('id'):
        lines.append(label + manager['id'] + ('' if manager['state'] == 'valid'
                                              else ' (' + POINTER_STATES.get(manager['state'], '?') + ')'))
    else:
        lines.append(label + POINTER_STATES.get(manager.get('state'), '?'))
    if value.get('state_error'):
        lines.append('관리 상태 파일(state.json)을 읽지 못했습니다.')
    parts = []
    for name in POINTERS:
        pointer = value['runtime_pointers'][name]
        if _failed(pointer):
            parts.append(name + ' 확인 실패')
            continue
        text = name + ' ' + (pointer.get('release') + ' ' if pointer.get('release') else '')
        text += POINTER_STATES.get(pointer.get('state'), '?')
        if pointer.get('known_bad_unreadable'):
            text += ', 불량 목록 읽기 실패'
        elif pointer.get('known_bad'):
            text += ', 불량 목록'
        if pointer.get('activation_problem') and name != 'previous':
            text += ', 활성화 조건 미충족'
        parts.append(text)
    lines.append('런타임: ' + ' / '.join(parts))
    staged = value['candidates']
    if _failed(staged):
        lines.append('활성화 대기 후보: 확인 실패')
    else:
        pending = staged['pending']
        lines.append('활성화 대기 후보: ' + ('%d개 (%s)' % (len(pending), ', '.join(item['release'] for item in pending))
                                      if pending else '없음'))
    stores = value['migrations']
    if _failed(stores):
        lines.append('저장소 마이그레이션: 확인 실패')
    elif stores['state'] == 'unknown':
        lines.append('저장소 마이그레이션: 확인 불가 (현재 런타임에 마이그레이션 기록 없음)')
    else:
        text = ('저장소 마이그레이션: 호환 (저장소 %d개)' % stores['stores'] if stores['state'] == 'compatible'
                else '저장소 마이그레이션: 불일치 %d건 - 현재 런타임이 시작하지 못합니다' % len(stores['incompatible']))
        if stores['ahead']:
            text += ', 런타임에 없는 적용 %d건' % len(stores['ahead'])
        if stores['unreadable']:
            text += ', 읽기 실패 %d개' % len(stores['unreadable'])
        lines.append(text)
    updates = value['remote_updates']
    if _failed(updates) or 'pending' not in updates:
        lines.append('SSH 업데이트 대기: 확인 불가')
    else:
        lines.append('SSH 업데이트 대기: ' + ('%d건 (%s)' % (len(updates['pending']), ', '.join(
            '%s@%s' % (item.get('profile') or item.get('profile_id'), item.get('host')) for item in updates['pending']))
            if updates['pending'] else '없음'))
        if updates.get('stale'):
            lines.append('이전 버전 SSH 런타임: %d건 (%s)' % (len(updates['stale']), ', '.join(
                '%s@%s%s' % (item.get('profile') or item.get('profile_id'), item.get('host'),
                             ' 실행 중' if item.get('running_bundle') else '')
                for item in updates['stale'])))
    logins = value['claude_logins']
    if _failed(logins):
        lines.append('Claude 로그인: 확인 실패')
    elif logins:
        words = {'missing': '자격 증명 없음', 'unreadable': '읽기 실패', 'no_expiry': '만료 정보 없음', 'expired': '만료됨'}
        lines.append('Claude 로그인: ' + ', '.join(
            '%s %s' % (item.get('profile'), '%.1f시간 남음' % item['hours_left'] if item['state'] == 'valid'
                       else words.get(item['state'], item['state'])) for item in logins))
    exits = value['runtime_exits']
    if not _failed(exits):
        # A clean exit (0) before initialize is a frontend that closed stdin, not a failed start.
        early = [item for item in exits
                 if item.get('initialize_completed') is False and item.get('exit_code') not in (0, None)]
        if early:
            lines.append('초기화 전에 종료된 런타임: %d개 프로필 (%s)' % (len(early), ', '.join(
                '%s 종료 코드 %s%s' % (item.get('profile') or item['profile_id'], item.get('exit_code'),
                                     ' · 시작 재시도 %s회' % item['retries'] if item.get('retries') else '')
                for item in early)))
    retried = value.get('runtime_start_retries')
    if isinstance(retried, list):
        # A failed last start is already listed above with its retry count.
        recovered = [item for item in retried if item.get('recovered')]
        if recovered:
            lines.append('시작 재시도 후 복구된 런타임: %d개 프로필 (%s)' % (len(recovered), ', '.join(
                '%s 시작 재시도 %s회%s' % (item.get('profile') or item['profile_id'], item['retries'],
                                       ' · 실패 종료 코드 %s' % '/'.join(str(code) for code in dict.fromkeys(
                                           item['exit_codes'])) if item.get('exit_codes') else '')
                for item in recovered)))
    return lines


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    value = report(args.root)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    print()
    print('\n'.join(summary(value)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
