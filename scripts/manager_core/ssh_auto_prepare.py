"""Prepare an inherited SSH connection in its own launcher, never on the UI thread."""
import json
from pathlib import Path
import time

from .ssh_shim import ALIAS, ShimError, decode_native, parse_invocation, validate_binding
from .ssh_inventory import SshInventory
from .store import Store, atomic_json
from .updates import UpdateError, _lock_file, _unlock_file
from .model_settings import render_options


def wait_lock(path, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _lock_file(path)
        except UpdateError:
            if time.monotonic() >= deadline:
                raise ShimError('ssh_prepare_busy', 'SSH 자동 준비가 진행 중입니다. 잠시 뒤 다시 연결됩니다.') from None
            time.sleep(.1)


def ensure_binding(arguments, manifest, path, *, remote=None):
    invocation = parse_invocation(arguments)
    alias = invocation.destination
    if (not invocation.understood or invocation.configuration_only or not isinstance(alias, str)
            or not ALIAS.fullmatch(alias) or alias not in manifest.get('auto_prepare_aliases', [])
            or invocation.command_index != len(arguments) - 1
            or not (manifest.get('native_compatible') is True or manifest.get('native_command_validation') == 1)):
        raise ShimError('host_binding_required', '이 SSH 연결의 저장된 프로필 설정을 확인하세요.')
    operation, _, _ = decode_native(arguments[-1], manifest.get('native_cli', 'codex'))
    if operation not in ('native-probe', 'native-version', 'native-start', 'native-proxy', 'native-platform'):
        raise ShimError('host_binding_required', '연결되지 않은 SSH 프로필은 종료할 수 없습니다.')
    root = Path(manifest['inventory_root']).resolve()
    store = Store(root)
    profile_id, generation = manifest['profile_id'], manifest['generation']
    expected = store.directory / 'profiles' / profile_id / 'ssh-bindings.json'
    if Path(path).resolve() != expected.resolve():
        raise ShimError('manifest_invalid', 'SSH 프로필 경로가 일치하지 않습니다.')
    models = manifest.get('selected_model_ids', [])
    primary = manifest.get('primary_model_id')

    def check_profile():
        profile = store.profile(profile_id)
        current_models = profile['policy']['model_ids'] if profile['policy']['enabled'] else []
        if (profile.get('generation') != generation or sorted(current_models) != models or profile.get('external_model_id') != primary
                or ('model_options' in manifest and render_options(profile) != manifest['model_options'])):
            raise ShimError('ssh_generation_changed', 'SSH 준비 중 프로필이 변경되었습니다. 다시 연결하세요.')
        return profile

    # One provisioner per profile/host. Inventory enrollment also prevents an
    # automatic profile restart from interrupting an in-progress installation.
    with SshInventory(root).execution(profile_id, generation, dict(operation='native-prepare', alias=alias)):
        lock = wait_lock(expected.parent / ('ssh-prepare-' + alias + '.lock'), 600)
        try:
            current = json.loads(expected.read_text(encoding='utf-8'))
            if current.get('generation') != generation:
                raise ShimError('ssh_generation_changed', 'SSH 프로필 실행이 변경되었습니다.')
            profile = check_profile()
            if any(item.get('alias') == alias for item in current.get('bindings', [])):
                return current
            if remote is None:
                from .remote import RemoteManager
                remote = RemoteManager(root, ssh_executable=manifest['real_ssh'])
            result = remote.prepare(alias, profile_id, profile['home'], models,
                reuse_host_runtime=True, **render_options(profile))
            if not result.get('prepared'):
                raise ShimError('ssh_auto_prepare_failed', 'SSH 자동 준비를 완료하지 못했습니다. SSH 연결 준비에서 상태를 확인하세요.')
            binding = validate_binding(result, profile_id)
            check_profile()
            def save(data):
                item = store.profile(profile_id, data)
                current_models = item['policy']['model_ids'] if item['policy']['enabled'] else []
                if (item.get('generation') != generation or sorted(current_models) != models or item.get('external_model_id') != primary
                        or ('model_options' in manifest and render_options(item) != manifest['model_options'])):
                    raise ShimError('ssh_generation_changed', 'SSH 준비 중 프로필 실행이 변경되었습니다.')
                item['remote_bindings'] = [b for b in item.get('remote_bindings', []) if b.get('alias') != alias] + [result]
                store.remote_source(data, result, item['alias'])
            store.mutate(save)
            # Different hosts can finish concurrently; serialize only this short
            # manifest merge, not network preparation for unrelated hosts.
            write_lock = wait_lock(expected.parent / 'ssh-binding-write.lock', 5)
            try:
                current = json.loads(expected.read_text(encoding='utf-8'))
                if current.get('generation') != generation:
                    raise ShimError('ssh_generation_changed', 'SSH 프로필 실행이 변경되었습니다.')
                current['bindings'] = [b for b in current.get('bindings', []) if b.get('alias') != alias] + [binding]
                atomic_json(expected, current)
            finally:
                _unlock_file(write_lock)
            return current
        finally:
            _unlock_file(lock)
