"""Apply common desktop preferences only before a managed profile is launched.

This is an allowlist, not a copy of browser data, authentication, drafts or
permission grants. The onboarding keys were checked against app 26.908.4834.0.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import tomllib

from .common import _atomic_write, _assert_owned_path, config_lock, toml_value
from .store import atomic_json

_DESKTOP_KEYS = ('appearanceTheme', 'appearanceDarkCodeThemeId', 'appearanceLightCodeThemeId',
                 'appearanceDarkChromeTheme', 'appearanceLightChromeTheme', 'sansFontSize',
                 'codeFontSize', 'usePointerCursors', 'followUpQueueMode', 'conversationDetailMode')
_MARKER = re.compile(r'^# (?:BEGIN|END) MANAGER DESKTOP\s*$')


def merge_desktop(text, values):
    """Apply shared presentation settings, retaining all other TOML values.

    A native settings write can create [desktop] before the first import and
    removes our old block comments. Neither means the entire table is private.
    Parse complete statements so a table-looking line inside a multiline string
    cannot be mistaken for a section. Only the desktop table is reserialized.
    """
    original = tomllib.loads(text)
    expected = deepcopy(original)
    expected.setdefault('desktop', {}).update(values)
    if original == expected:
        return text
    retained, statement = [], ''
    in_desktop, in_table = False, False
    for line in text.splitlines(keepends=True):
        # Standalone comments and blank lines are not part of the following
        # statement. Retain them verbatim so native writes cannot lose markers
        # or notes inserted around [desktop]; only our own block markers go.
        if not statement and (not line.strip() or line.lstrip().startswith('#')):
            if not _MARKER.fullmatch(line.strip()):
                retained.append(line)
            continue
        statement += line
        try:
            parsed = tomllib.loads(statement)
        except tomllib.TOMLDecodeError:
            continue
        if statement.lstrip().startswith('['):
            in_table = True
            in_desktop = 'desktop' in parsed
        if not in_desktop and not (not in_table and 'desktop' in parsed):
            retained.append(statement)
        statement = ''
    if statement:
        raise ValueError('공통 화면 설정의 TOML 구문을 확인하지 못했습니다.')
    block = '# BEGIN MANAGER DESKTOP\n[desktop]\n' + ''.join(
        json.dumps(key) + ' = ' + toml_value(value) + '\n'
        for key, value in expected['desktop'].items()) + '# END MANAGER DESKTOP\n'
    updated = ''.join(retained).rstrip() + '\n\n' + block
    if tomllib.loads(updated) != expected:
        raise ValueError('다른 설정이 변경될 수 있어 공통 화면 설정을 적용하지 않았습니다.')
    return updated


def prepare(home, source, *, account_id=None, ssh_ready_aliases=None, canonical=False, signals=None):
    home, source = Path(home).resolve(), Path(source).resolve()
    if home == source:
        raise ValueError('원본 앱의 설정은 덮어쓰지 않습니다.')
    home.mkdir(parents=True, exist_ok=True)
    config, metadata, state, aliases = (home / name for name in
        ('config.toml', '.manager-app-preferences.json', '.codex-global-state.json', '.manager-project-aliases.json'))
    for path in (config, metadata, state, aliases):
        _assert_owned_path(path, home)
        if path.exists() and (path.is_symlink() or path.stat().st_nlink != 1):
            raise ValueError('공통 앱 설정의 대상 파일이 다른 파일과 연결되어 있습니다.')
    result = dict(desktop='unchanged', onboarding='source_not_completed')
    source_config = source / 'config.toml'
    donor = tomllib.loads(source_config.read_text(encoding='utf-8-sig')) if source_config.is_file() else {}
    values = {k: v for k, v in donor.get('desktop', {}).items() if k in _DESKTOP_KEYS}
    with config_lock(home):
        text = config.read_text(encoding='utf-8-sig') if config.exists() else ''
        owned = json.loads(metadata.read_text(encoding='utf-8')) if metadata.exists() else {}
        if values:
            updated = merge_desktop(text, values)
            if text != updated:
                if (config.read_text(encoding='utf-8-sig') if config.exists() else '') != text:
                    raise ValueError('설정이 변경 중입니다. 이 프로필을 종료한 뒤 다시 열어주세요.')
                _atomic_write(config, updated)
                result['desktop'] = 'updated'
            owned.pop('desktop_hash', None)
            owned['desktop_keys'] = sorted(values)
            result['theme'] = values.get('appearanceTheme', 'system')
    donor_state = source / '.codex-global-state.json'
    original = json.loads(donor_state.read_text(encoding='utf-8-sig')) if donor_state.is_file() else {}
    current = json.loads(state.read_text(encoding='utf-8-sig')) if state.exists() else {}
    from .ssh_connection_recovery import apply_pending, finish as finish_ssh_recovery
    ssh_recovery = apply_pending(home, current)
    from .app_workspace import merge_workspace, remove_imported_projects
    project_aliases, result['workspace'] = merge_workspace(current, original, owned, source,
        ssh_ready_aliases=ssh_ready_aliases, signals=signals)
    if canonical:
        # Project IDs now belong to the shared store. Never run each profile's
        # legacy importer against it or resurrect removed project declarations.
        from .app_workspace import current_workspace
        workspace = current_workspace(source, original, signals=signals)
        host = 'local:' + str(home)
        donor_host = 'local:' + str(source)
        current.setdefault('app-server-project-id-by-legacy-project-id-by-host', {})[host] = deepcopy(
            workspace.get('app-server-project-id-by-legacy-project-id-by-host', {}).get(donor_host, {}))
        # Shared native IDs are not the old synthetic catalog IDs. Preserve
        # explicit moves, including moves out of a project, while the original
        # desktop is still migrating its legacy membership. Do not certify that
        # migration as complete on another profile's behalf.
        source_assignments = {key: value for key, value in workspace.get('thread-project-assignments', {}).items()
                              if value.get('projectKind') == 'local'}
        remote_assignments = {key: value for key, value in current.get('thread-project-assignments', {}).items()
                              if value.get('projectKind') != 'local'}
        current['thread-project-assignments'] = {**remote_assignments, **deepcopy(source_assignments)}
        current['projectless-thread-ids'] = deepcopy(workspace.get('projectless-thread-ids', []))
        migration = deepcopy(workspace.get('app-server-projects-migration-by-host', {}).get(donor_host, {}))
        migration['projectsMigrated'] = True
        migration.setdefault('threadAssignmentsMigrated', False)
        current.setdefault('app-server-projects-migration-by-host', {})[host] = migration
        result['workspace']['storage'] = 'canonical'
    removed = remove_imported_projects(home, current, owned, tomllib.loads(text).get('sqlite_home'))
    if removed:
        result['workspace']['removed_projects'] = removed
    atomic_json(aliases, project_aliases)
    atoms = original.get('electron-persisted-atom-state', {})
    completed = atoms.get('last_completed_onboarding')
    if isinstance(completed, (int, float)) and not isinstance(completed, bool) and completed > 0:
        target = current.setdefault('electron-persisted-atom-state', {})
        target.update({'last_completed_onboarding': completed,
                       'electron:onboarding-projectless-completed': True,
                       'electron:onboarding-welcome-pending': False})
        # The native app records this as a per-account boolean. Do not import the
        # source account's map or its onboarding conversation into a different login.
        if account_id:
            target.setdefault('electron:onboarding-conversational-completed-by-account-id', {})[account_id] = True
        result['onboarding'] = 'common_setup_completed'
    atomic_json(state, current)
    atomic_json(metadata, owned)
    finish_ssh_recovery(home, ssh_recovery)
    return result
