"""Personal skill inventory and shared enablement. Never edits plugin/system skills."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import tomllib
from uuid import uuid4

from .common import _atomic_write, config_lock, toml_value
from .store import atomic_json


def _read(path):
    return path.read_text(encoding='utf-8-sig') if path.exists() else ''


_pass = threading.local()


def _key(path):
    # resolve() is a filesystem walk. One reconcile pass compares every rule
    # with every skill in every profile while holding the state lock, so each
    # distinct path is resolved once per pass; the next pass re-resolves links.
    cache = getattr(_pass, 'keys', None)
    if cache is None:
        return os.path.normcase(str(Path(path).resolve()))
    text = str(path)
    if text not in cache:
        cache[text] = os.path.normcase(str(Path(path).resolve()))
    return cache[text]


@contextmanager
def _resolved_once():
    if getattr(_pass, 'keys', None) is not None:
        yield
        return
    _pass.keys = {}
    try:
        yield
    finally:
        _pass.keys = None


def _roots(source):
    return [source / 'skills', source.parent / '.agents/skills']


def _rules(text):
    skills = tomllib.loads(text).get('skills', {})
    if not isinstance(skills, dict):
        raise ValueError('개인 스킬 설정 형식을 확인하세요.')
    rules = skills.get('config', [])
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise ValueError('개인 스킬 설정 형식을 확인하세요.')
    return rules


def _enabled(row, rules):
    enabled = True
    for rule in rules:
        if rule.get('name') == row['name'] or (rule.get('path') and _key(rule['path']) == _key(row['path'])):
            enabled = rule.get('enabled', True) is not False
    return enabled


def inventory(source):
    """Only direct personal installations, including junctions, are manageable."""
    source = Path(source)
    rules = _rules(_read(source / 'config.toml'))
    rows, seen = [], set()
    for root in _roots(source):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            if entry.name.startswith('.') or not entry.is_dir() or not (entry / 'SKILL.md').is_file():
                continue
            path = entry / 'SKILL.md'
            if _key(path) in seen:
                continue
            seen.add(_key(path))
            # Read metadata as data, never execute skill instructions or scripts.
            text = path.read_text(encoding='utf-8-sig')[:65536]
            header = text.split('---', 2)[1] if text.startswith('---') and text.count('---') >= 2 else ''
            fields = dict(re.findall(r'^(name|description):[ \t]*(.*)$', header, re.MULTILINE))
            name = fields.get('name', entry.name).strip().strip('"\'') or entry.name
            description = fields.get('description', '').strip().strip('"\'')
            if description in ('|', '>', '|-', '>-'):
                description = ''
            row = dict(id=hashlib.sha256((_key(root) + '/' + entry.name).encode()).hexdigest()[:24],
                       name=name, directory=entry.name, path=str(path), root=str(root),
                       description=description[:500], linked=entry.is_symlink() or entry.is_junction())
            row['enabled'] = _enabled(row, rules)
            rows.append(row)
    return rows


def replace_rules(text, rules):
    """Replace only skills.config, preserving unrelated TOML semantically and textually."""
    original = tomllib.loads(text)
    if _rules(text) == rules:
        return text
    retained, statement, dropping, at_root = [], '', False, True
    for line in text.splitlines(keepends=True):
        if not statement and (not line.strip() or line.lstrip().startswith('#')):
            retained.append(line)
            continue
        statement += line
        try:
            parsed = tomllib.loads(statement)
        except tomllib.TOMLDecodeError:
            continue
        is_header = statement.lstrip().startswith('[')
        if is_header:
            at_root = False
            dropping = 'skills' in parsed
        if not dropping and not (at_root and 'skills' in parsed):
            retained.append(statement)
        statement = ''
    if statement:
        raise ValueError('설정 파일을 안전하게 분리하지 못해 변경하지 않았습니다.')
    skills = deepcopy(original.get('skills', {}))
    skills['config'] = rules
    newline = '\r\n' if '\r\n' in text else '\n'
    updated = ''.join(retained).rstrip() + newline + newline + '[skills]' + newline
    updated += newline.join(json.dumps(k) + ' = ' + toml_value(v) for k, v in skills.items()) + newline
    expected = deepcopy(original); expected['skills'] = skills
    if tomllib.loads(updated) != expected:
        raise ValueError('다른 설정이 바뀔 수 있어 개인 스킬 설정을 저장하지 않았습니다.')
    return updated


def _matches(rule, row, home):
    if rule.get('name') == row['name']:
        return True
    if not rule.get('path'):
        return False
    paths = {_key(row['path']), _key(home / 'skills' / row['directory'] / 'SKILL.md')}
    return _key(rule['path']) in paths


def sync_home(home, source, rows=None):
    home, source = Path(home), Path(source)
    rows = inventory(source) if rows is None else rows
    path = home / 'config.toml'
    if path.is_symlink() or (path.exists() and not path.resolve().is_relative_to(home.resolve())):
        raise ValueError('프로필 밖의 설정 파일은 수정하지 않습니다.')
    with config_lock(home):
        original = _read(path)
        rules = [r for r in _rules(original) if not any(_matches(r, row, home) for row in rows)]
        for row in rows:
            # Shared junctions canonicalize to the same file. Also cover a legacy
            # profile-local copy without taking ownership of unrelated local skills.
            paths = {str(Path(row['path']).resolve())}
            local = home / 'skills' / row['directory'] / 'SKILL.md'
            if local.is_file():
                paths.add(str(local.resolve()))
            rules.extend(dict(path=p, enabled=row['enabled']) for p in sorted(paths))
        updated = replace_rules(original, rules)
        if updated == original:
            return False
        home.mkdir(parents=True, exist_ok=True)
        if _read(path) != original:
            raise ValueError('설정이 동시에 변경되어 다음 확인 때 다시 적용합니다.')
        _atomic_write(path, updated)
        return True


class PersonalSkills:
    def __init__(self, store, source=None):
        self.store = store
        self.source = Path(source) if source is not None else Path.home() / '.codex'
        self.trash = self.source / '.manager-skill-trash'
        self.registry = store.directory / 'personal-skills.json'
        self.lock = threading.RLock()
        self.stamp = None
        self.result = dict(applied=0, errors=[])
        self.stopping = threading.Event()
        self.worker = None

    def _homes(self):
        homes = [('본앱', self.source)] + [(p['alias'], Path(p['home'])) for p in self.store.read()['profiles'] if not p.get('removed_at')]
        return list({ _key(home): (alias, home) for alias, home in homes }.values())

    def _load(self):
        if not self.registry.exists():
            return dict(version=1, skills={}, observations={})
        value = json.loads(_read(self.registry))
        if value.get('version') != 1 or not isinstance(value.get('skills'), dict) or not isinstance(value.get('observations'), dict):
            raise ValueError('공통 개인 스킬 설정 형식을 확인하세요.')
        return value

    @staticmethod
    def _observation(home, rows):
        path = home / 'config.toml'
        rules = _rules(_read(path))
        values = {}
        for row in rows:
            enabled = True
            for rule in rules:
                if _matches(rule, row, home):
                    enabled = rule.get('enabled', True) is not False
            values[row['id']] = enabled
        return dict(changed_at=path.stat().st_mtime_ns if path.exists() else 0, values=values)

    def start(self):
        def watch():
            while not self.stopping.is_set():
                try:
                    self.reconcile()
                except (OSError, ValueError) as error:
                    self.result = dict(applied=0, errors=[dict(profile='공통 스킬', message=str(error))])
                self.stopping.wait(2)
        self.worker = threading.Thread(target=watch, name='shared-personal-skills', daemon=True)
        self.worker.start()

    def shutdown(self):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=3)

    def reconcile(self, force=False):
        with self.lock, self.store.locked(), _resolved_once():
            homes = self._homes()
            paths = [self.registry, *_roots(self.source), *(h / 'config.toml' for _, h in homes)]
            stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p), None) for p in paths)
            if not force and stamp == self.stamp:
                return self.result
            rows = inventory(self.source)
            registry = self._load()
            previous = deepcopy(registry)
            for row in rows:
                # Bootstrap new installations once. Afterwards the manager's
                # registry owns the setting; no app/profile has priority.
                registry['skills'].setdefault(row['id'], dict(enabled=row['enabled'], name=row['name']))
            changes = []
            for alias, home in homes:
                try:
                    current = self._observation(home, rows)
                except (OSError, ValueError):
                    continue
                old = registry['observations'].get(_key(home))
                if old:
                    for sid, enabled in current['values'].items():
                        if sid in old['values'] and enabled != old['values'][sid]:
                            changes.append((current['changed_at'], _key(home), sid, enabled))
            # A real change in any client is promoted to the common registry.
            # New profiles' empty configs never reset existing shared choices.
            for _, _, sid, enabled in sorted(changes):
                registry['skills'][sid]['enabled'] = enabled
            for row in rows:
                row['enabled'] = registry['skills'][row['id']]['enabled']
            applied, errors = 0, []
            for alias, home in homes:
                try:
                    sync_home(home, self.source, rows)
                    registry['observations'][_key(home)] = self._observation(home, rows)
                    applied += 1
                except (OSError, ValueError) as error:
                    errors.append(dict(profile=alias, message=str(error)))
            self.result = dict(applied=applied, errors=errors)
            if registry != previous or not self.registry.exists():
                atomic_json(self.registry, registry)
            # Store the post-write signature to avoid rewriting every state poll.
            self.stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p), None) for p in paths)
            if errors:
                self.stamp = None
            return self.result

    def list(self):
        with self.lock:
            sync = self.reconcile()
            deleted = []
            if self.trash.is_dir():
                for meta in sorted(self.trash.glob('*/entry.json')):
                    try:
                        entry = json.loads(_read(meta))
                        if (meta.parent / 'skill').exists():
                            deleted.append(dict(id=meta.parent.name, name=entry['name']))
                    except (OSError, ValueError, KeyError):
                        continue
            rows = inventory(self.source)
            registry = self._load()
            for row in rows:
                row['enabled'] = registry['skills'].get(row['id'], {}).get('enabled', row['enabled'])
            return dict(skills=rows, deleted=deleted, sync=sync,
                        scope='이 PC의 본앱과 모든 프로필',
                        message='어느 프로필에서 변경해도 공통 설정에 반영됩니다. 열린 앱의 스킬 목록은 다시 열 때 반영될 수 있습니다.')

    def set(self, skill_id, enabled):
        if type(enabled) is not bool:
            raise ValueError('스킬 사용 여부가 올바르지 않습니다.')
        with self.lock, self.store.locked(), _resolved_once():
            self.reconcile(force=True)
            rows = inventory(self.source)
            row = next((r for r in rows if r['id'] == skill_id), None)
            if row is None:
                raise ValueError('개인 스킬이 변경되었습니다. 목록을 새로고침하세요.')
            registry = self._load()
            registry['skills'][skill_id]['enabled'] = enabled
            atomic_json(self.registry, registry)
            self.reconcile(force=True)
            return self.list()

    def _install_path(self, root, directory):
        roots = {_key(r): r for r in _roots(self.source)}
        if _key(root) not in roots or not isinstance(directory, str) or directory.startswith('.') or Path(directory).name != directory or any(c in directory for c in '/\\:'):
            raise ValueError('개인 스킬 폴더 범위를 벗어났습니다.')
        path = roots[_key(root)] / directory
        # Validate the parent, not the junction target: deletion moves only the
        # installation entry, never the source repository it points to.
        if _key(path.parent) != _key(root):
            raise ValueError('개인 스킬 폴더 범위를 벗어났습니다.')
        return path

    def delete(self, skill_id):
        with self.lock, self.store.locked(), _resolved_once():
            row = next((r for r in inventory(self.source) if r['id'] == skill_id), None)
            if row is None:
                raise ValueError('삭제할 개인 스킬을 찾지 못했습니다.')
            path = self._install_path(row['root'], row['directory'])
            self.set(skill_id, False)
            destination = self.trash / uuid4().hex
            if not destination.parent.resolve().is_relative_to(self.source.resolve()):
                raise ValueError('스킬 복구 폴더가 원본 홈 밖을 가리킵니다.')
            destination.mkdir(parents=True)
            atomic_json(destination / 'entry.json', row)
            os.rename(path, destination / 'skill')
            self.reconcile(force=True)
            return self.list()

    def restore(self, deleted_id):
        if not isinstance(deleted_id, str) or not re.fullmatch('[a-f0-9]{32}', deleted_id):
            raise ValueError('복구 항목이 올바르지 않습니다.')
        with self.lock, self.store.locked(), _resolved_once():
            entry = self.trash / deleted_id
            if not entry.resolve().is_relative_to(self.source.resolve()):
                raise ValueError('복구 경로가 올바르지 않습니다.')
            row = json.loads(_read(entry / 'entry.json'))
            target = self._install_path(row['root'], row['directory'])
            if os.path.lexists(target):
                raise ValueError('같은 이름의 스킬이 이미 있습니다. 기존 파일은 덮어쓰지 않았습니다.')
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(entry / 'skill', target)
            registry = self._load()
            if row['id'] in registry['skills']:
                registry['skills'][row['id']]['enabled'] = row['enabled']
                atomic_json(self.registry, registry)
            self.reconcile(force=True)
            return self.list()
