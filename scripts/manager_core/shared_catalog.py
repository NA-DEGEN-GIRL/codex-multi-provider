"""Enable a common record view in a normal, independently authenticated worker."""
from pathlib import Path
from .source_catalog import catalog_path


def selected_path(root, profile, capabilities):
    legacy = Path(root) / 'work/control-center/catalog/local-records.json'
    modern = catalog_path(root)
    saved = profile.get('shared_catalog_path')
    if saved:
        path = Path(saved)
        if path not in (legacy, modern):
            raise ValueError('공통 대화 목록의 저장된 경로가 올바르지 않습니다.')
        return path
    # Older running generations predate this field and use the V1 descriptor.
    if ('shared_catalog_path' not in profile and profile.get('generation')
            and profile.get('process_id')):
        return legacy
    return modern if capabilities.get('mixed_source_catalog') else legacy


def environment(store, profile, capabilities, refresh, source_refresh=None):
    if (profile.get('view_only') or profile.get('runtime_channel') == 'packaged'
            or not capabilities.get('shared_record_catalog')):
        return {}
    if capabilities.get('canonical_record_storage'):
        from .canonical_storage import migrate
        common = migrate(store.root)
        return {'CODEX_RECORD_HOME': common['home'], 'CODEX_SQLITE_HOME': common['home'], 'CODEX_RECORD_SHARED_APPEND': '1',
                'CODEX_MANAGER_SHARED_EXECUTION': '1'}
    expected = selected_path(store.root, profile, capabilities)
    if expected == catalog_path(store.root):
        if source_refresh is None:
            raise RuntimeError('공통 저장소 목록 갱신기가 준비되지 않았습니다.')
        refresh = source_refresh
    result = refresh.ensure(store.read()['sources'],
                            include_paginated=capabilities.get('paginated_record_catalog', False))
    path = Path(result['path'])
    if path != expected or path.is_symlink() or path.resolve(strict=True) != expected:
        raise RuntimeError('공통 대화 목록의 연결 경로가 변경되었습니다.')
    return {'CODEX_MANAGER_SHARED_CATALOG': str(path)}
