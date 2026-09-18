"""Refresh shared declarations while preserving each profile's own settings.

Only manager-marked MCP blocks and an unchanged manager-generated AGENTS.md are
updated. Authentication files, plugin connection state and conversation stores
are never read or copied. MCP values never appear in returned status or metadata.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


_STATE = '.manager-common.json'
_BEGIN = '# BEGIN CODEX MANAGER COMMON MCP '
_END = '# END CODEX MANAGER COMMON MCP '
_BLOCK = re.compile(r'^' + re.escape(_BEGIN) + r'([0-9a-f]{24})\r?\n.*?^'
                    + re.escape(_END) + r'\1(?:\r?\n|$)', re.MULTILINE | re.DOTALL)


def toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + ' = ' + toml_value(v) for k, v in value.items()) + '}'
    raise ValueError('공통 설정에 지원하지 않는 값이 있습니다.')


def _read(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return handle.read()


def _hash(text):
    # An editor changing Windows line endings does not transfer block ownership.
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def _atomic_write(path, text):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _assert_owned_path(path, home):
    if not path.resolve().is_relative_to(home.resolve()):
        raise ValueError('프로필 밖을 가리키는 공통 설정 파일은 수정하지 않습니다.')


def _render_block(name, definition, newline):
    identity = _hash(name)[:24]
    lines = [_BEGIN + identity, '[mcp_servers.' + json.dumps(name, ensure_ascii=False) + ']']
    lines.extend(json.dumps(key, ensure_ascii=False) + ' = ' + toml_value(value)
                 for key, value in definition.items())
    lines.append(_END + identity)
    block = newline.join(lines) + newline
    tomllib.loads(block)
    return block


def _load_state(path, warnings):
    if not path.exists():
        return {'version': 1, 'mcp': {}}
    try:
        state = json.loads(_read(path))
        if (not isinstance(state, dict) or state.get('version') != 1
                or not isinstance(state.get('mcp'), dict)
                or not isinstance(state.get('mcp_overrides', []), list)
                or not all(isinstance(name, str) for name in state.get('mcp_overrides', []))):
            raise ValueError()
        return {key: state[key] for key in ('version', 'mcp', 'mcp_overrides', 'instructions_hash') if key in state}
    except (OSError, ValueError, TypeError):
        warnings.append('공통 설정의 관리 기록을 읽지 못해 기존 설정의 소유권을 추정하지 않습니다.')
        return {'version': 1, 'mcp': {}}


def _refresh_mcp(text, definitions, state, result):
    existing = tomllib.loads(text).get('mcp_servers', {})
    if not isinstance(existing, dict):
        raise ValueError('프로필 MCP 설정 형식이 올바르지 않습니다.')
    newline = '\r\n' if '\r\n' in text else '\n'
    records = dict(state['mcp'])
    overrides = set(state.get('mcp_overrides', []))
    preserved = set()
    blocks = {}
    for match in _BLOCK.finditer(text):
        blocks.setdefault(match.group(1), []).append(match.group(0))
    counts = dict(added=0, updated=0, removed=0, preserved=0)
    for name, record in list(records.items()):
        candidates = blocks.get(_hash(name)[:24], [])
        block = candidates[0] if len(candidates) == 1 else None
        if (not isinstance(record, dict) or block is None
                or _hash(block) != record.get('block_hash')):
            records.pop(name, None)
            overrides.add(name)
            preserved.add(name)
            continue
        try:
            if tomllib.loads(block)['mcp_servers'][name] != existing.get(name):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            records.pop(name, None)
            overrides.add(name)
            preserved.add(name)
            continue
        if name not in definitions:
            text = text.replace(block, '', 1)
            records.pop(name)
            counts['removed'] += 1
        else:
            try:
                replacement = _render_block(name, definitions[name], newline)
            except (ValueError, TypeError):
                result['warnings'].append('지원하지 않는 공통 MCP 정의가 있어 해당 기존 설정은 유지했습니다.')
                continue
            if replacement != block:
                text = text.replace(block, replacement, 1)
                records[name] = {'block_hash': _hash(replacement)}
                counts['updated'] += 1
    for name, definition in definitions.items():
        if name in records:
            continue
        if name in existing or name in overrides:
            overrides.add(name)
            preserved.add(name)
            continue
        try:
            block = _render_block(name, definition, newline)
        except (ValueError, TypeError):
            result['warnings'].append('지원하지 않는 공통 MCP 정의는 복사하지 않았습니다.')
            continue
        text += ('' if text.endswith(newline) else newline) + newline + block
        records[name] = {'block_hash': _hash(block)}
        counts['added'] += 1
    tomllib.loads(text)
    state['mcp'] = records
    state['mcp_overrides'] = sorted(overrides)
    counts['preserved'] = len(preserved)
    result['mcp_counts'] = counts
    result['mcp'] = 'updated' if any(counts[k] for k in ('added', 'updated', 'removed')) else 'unchanged'
    if counts['preserved']:
        result['warnings'].append('프로필에서 직접 설정하거나 수정한 MCP는 공통 설정으로 덮어쓰지 않았습니다.')
    return text


def _share_skills(home, source, result):
    src, dest = source / 'skills', home / 'skills'
    if not src.is_dir():
        return
    if os.path.lexists(dest):
        try:
            shared = dest.resolve(strict=True) == src.resolve(strict=True)
        except OSError:
            shared = False
        result['skills'] = 'shared_directory' if shared else 'profile_directory_preserved'
        if not shared:
            result['warnings'].append('프로필의 기존 skills 경로를 보존했습니다. 원본 스킬과 자동 연결된 상태는 아닙니다.')
        return
    try:
        if os.name == 'nt':
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            command = ('New-Item -ItemType Junction -Path ' + quote(dest)
                       + ' -Target ' + quote(src) + ' | Out-Null')
            process = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            if process.returncode:
                raise OSError()
        else:
            dest.symlink_to(src, target_is_directory=True)
        if dest.resolve(strict=True) != src.resolve(strict=True):
            raise OSError()
        result['skills'] = 'shared_directory'
    except (OSError, subprocess.SubprocessError):
        result['warnings'].append('공통 스킬 연결을 만들지 못했습니다.')


def _share_instructions(home, source, state, result):
    """Share explicit common instructions, including any plugin instructions there."""
    source_file, destination = source / 'AGENTS.md', home / 'AGENTS.md'
    if not source_file.is_file():
        return
    _assert_owned_path(destination, home)
    previous = _read(destination) if destination.exists() else None
    if previous is not None and _hash(previous) != state.get('instructions_hash'):
        result['instructions'] = 'profile_file_preserved'
        result['warnings'].append('프로필에서 직접 설정하거나 수정한 AGENTS.md는 보존했습니다.')
        return
    content = _read(source_file)
    if previous != content:
        _atomic_write(destination, content)
    state['instructions_hash'] = _hash(content)
    result['instructions'] = 'shared_snapshot'


def prepare_common(home, source):
    home, source = Path(home).resolve(), Path(source).resolve()
    if home == source:
        raise ValueError('원본 Codex 홈을 관리 프로필로 덮어쓸 수 없습니다.')
    home.mkdir(parents=True, exist_ok=True)
    result = dict(skills='unavailable', mcp='unchanged', instructions='unavailable',
                  plugins='native_profile_installation', warnings=[])
    config, state_path = home / 'config.toml', home / _STATE
    _assert_owned_path(config, home)
    _assert_owned_path(state_path, home)
    state = _load_state(state_path, result['warnings'])
    original = _read(config) if config.exists() else None
    text = original if original is not None else 'model = "gpt-6-astra"\ncli_auth_credentials_store = "file"\n'
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError('프로필 설정을 해석하지 못해 공통 설정을 변경하지 않았습니다.') from None
    donor = source / 'config.toml'
    if donor.is_file():
        try:
            definitions = tomllib.loads(_read(donor)).get('mcp_servers', {})
            if not isinstance(definitions, dict) or not all(isinstance(v, dict) for v in definitions.values()):
                raise ValueError()
        except (OSError, ValueError):
            result['warnings'].append('원본 MCP 설정을 읽지 못해 기존 공통 MCP 설정은 유지했습니다.')
        else:
            try:
                text = _refresh_mcp(text, definitions, state, result)
            except (ValueError, TypeError):
                result['warnings'].append('기존 TOML 구조와 공통 MCP 정의를 합칠 수 없어 프로필 설정은 유지했습니다.')
    else:
        result['warnings'].append('원본 설정 파일이 없어 기존 공통 MCP 설정은 유지했습니다.')
    if text != original:
        observed = _read(config) if config.exists() else None
        if observed != original:
            raise ValueError('프로필 설정이 준비 중에 변경되어 덮어쓰지 않았습니다. 다시 실행하세요.')
        _atomic_write(config, text)
    _share_skills(home, source, result)
    _share_instructions(home, source, state, result)
    metadata = json.dumps(state, ensure_ascii=False, indent=2) + '\n'
    if not state_path.exists() or _read(state_path) != metadata:
        _atomic_write(state_path, metadata)
    # Plugin bundles and connection state have their own installation/account
    # lifecycle. Sharing the entire directory would merge mutable state.
    return result
