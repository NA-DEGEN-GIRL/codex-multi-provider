"""Expose enabled shared skills to Claude without copying or changing their resources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .personal_skills import _enabled, _rules, inventory
from .store import identifier
from .common import _atomic_write


def _project_skills(cwd):
    project = Path(cwd).resolve()
    # Match project instructions within this checkout only, never arbitrary ancestors.
    ancestors = (project, *project.parents)
    boundary = next((p for p in ancestors if (p / '.git').exists()), project)
    for parent in ancestors:
        root = parent / '.agents' / 'skills'
        if root.is_dir():
            for entry in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
                path = entry / 'SKILL.md'
                if not entry.name.startswith('.') and path.is_file():
                    yield dict(name=entry.name, path=str(path), description='', enabled=True)
        if parent == boundary:
            break


def prepare_shared_skills(root, profile_id, cwd, *, config_home=None, output_root=None):
    """Return an immutable Claude plugin containing small shared-skill entry points.

    Relative resources are read at their original location. This avoids moving large
    models, scripts, credentials or platform-specific environments into a plugin.
    Claude's own .claude/skills discovery remains independent.
    """
    root = Path(root).resolve()
    profile_id = identifier(profile_id)
    home = Path(config_home) if config_home is not None else root / 'work/control-center/profiles' / profile_id / 'codex'
    rows = inventory(home)
    # Newly created profiles have not necessarily run shared-skill reconciliation.
    if not rows:
        rows = inventory(Path.home() / '.codex')
    rows += list(_project_skills(cwd))
    config = home / 'config.toml'
    rules = _rules(config.read_text(encoding='utf-8-sig')) if config.is_file() else []
    files, seen, names = {}, set(), set()
    for row in rows:
        if not row.get('enabled', True) or not _enabled(row, rules):
            continue
        source = Path(row['path']).resolve(strict=True)
        if source in seen:
            continue
        seen.add(source)
        if len(seen) > 512 or source.stat().st_size > 1024 * 1024:
            raise ValueError('Shared skill inventory exceeds the supported size.')
        content = source.read_text(encoding='utf-8-sig')
        description = row.get('description', '')
        if not description:
            match = re.search(r'^description:[ \t]*(.+)$', content, re.MULTILINE)
            description = match.group(1).strip().strip('\"\'') if match else ''
        if not description or description in ('|', '>', '|-', '>-'):
            description = 'Use the shared ' + row['name'] + ' skill when the user requests it.'
        slug = re.sub(r'[^a-z0-9-]+', '-', row['name'].lower()).strip('-')[:48] or 'skill'
        digest = hashlib.sha256(str(source).encode()).hexdigest()[:10]
        name = slug if slug not in names else slug + '-' + digest
        names.add(name)
        entry = ('---\nname: ' + json.dumps(name) + '\ndescription: ' + json.dumps(description[:1000], ensure_ascii=False)
                 + '\n---\n\nThis skill is shared with Codex. Before using it, read the complete original skill:\n\n'
                 + json.dumps(str(source), ensure_ascii=False) + '\n\n'
                 + 'Resolve every relative resource path against the original skill directory, not this entry point. '
                 + 'Follow the original skill instructions. Do not edit this generated entry point.\n')
        files[f'skills/{name}/SKILL.md'] = entry
        # Content changes must invalidate the runner cache even if the entry-point text is unchanged.
        files[f'skills/{name}/source-sha256.txt'] = hashlib.sha256(content.encode()).hexdigest() + '\n'
    files['.claude-plugin/plugin.json'] = json.dumps(dict(name='codex-shared-skills', version='1.0.0',
        description='Enabled personal and project skills shared with Codex.'), ensure_ascii=False) + '\n'
    revision = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    output_root = (Path(output_root) if output_root is not None
                   else root / 'work/control-center/claude-skill-plugins' / profile_id)
    destination = output_root / revision
    if destination.exists() and not destination.resolve().is_relative_to(output_root.resolve()):
        raise ValueError('Shared skill plugin path is outside its managed directory.')
    for relative, content in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError('Shared skill plugin contains a redirected file.')
        if not target.is_file() or target.read_text(encoding='utf-8') != content:
            _atomic_write(target, content)
    return destination
