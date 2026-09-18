"""Remote-user common settings for an isolated managed Codex home.

Run this helper on the SSH host, under that host's OS user. It never reads a
Windows config or any auth/credentials file. Only ``mcp_servers`` from the
remote user's config is imported. MCP environment values can contain secrets;
the returned plan stays on the remote host and must not be logged or uploaded.

``prepare`` is side-effect free. After all destinations validate, the launcher
calls ``stage`` to durably journal ownership, writes ``plan.config``, and calls
``commit``. A crash between these operations is recovered only against an exact
before/after config hash. ``apply`` performs this transaction for simple callers.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import tomllib


STATE_FILE = ".manager-common-settings.json"
PENDING_FILE = ".manager-common-settings.pending.json"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MISSING = object()


class CommonSettingsError(ValueError):
    """A value-free error; never include configuration content in diagnostics."""


class Plan:
    __slots__ = ("config", "summary", "_metadata", "_profile", "_before_config",
                 "_before_metadata", "_ownership_snapshot")

    def __init__(self, profile, config, metadata, summary, before_config, before_metadata, ownership_snapshot):
        self.config = config
        self.summary = summary
        self._metadata = metadata
        self._profile = profile
        self._before_config = before_config
        self._before_metadata = before_metadata
        self._ownership_snapshot = ownership_snapshot

    def __repr__(self):
        return "<RemoteCommonSettingsPlan>"


def _directory(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise CommonSettingsError("Managed settings directory contains a symlink.")
    if path.exists() and not path.is_dir():
        raise CommonSettingsError("Managed settings directory is invalid.")
    return path


def _read_text(path):
    if path.is_symlink():
        raise CommonSettingsError("Settings file is a symlink.")
    if not path.exists():
        return ""
    if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise CommonSettingsError("Settings file is invalid or too large.")
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise CommonSettingsError("Settings file cannot be read.") from None


def _parse(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise CommonSettingsError("Settings content is invalid or too large.")
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise CommonSettingsError("Settings TOML is invalid.") from None


def _quote(value):
    return json.dumps(value, ensure_ascii=False)


def _value(value):
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(_quote(key) + " = " + _value(value[key])
                                   for key in sorted(value)) + " }"
    raise CommonSettingsError("Settings contain an unsupported TOML value.")


def _dump(value):
    lines = []

    def table(data, path):
        if path:
            lines.extend(("", "[" + ".".join(_quote(part) for part in path) + "]"))
        for key, item in data.items():
            if not isinstance(item, dict):
                lines.append(_quote(key) + " = " + _value(item))
        for key, item in data.items():
            if isinstance(item, dict):
                table(item, (*path, key))

    table(value, ())
    return "\n".join(lines).lstrip("\n") + "\n"


def _digest(value):
    return hashlib.sha256(_value(value).encode("utf-8")).hexdigest()


def _path_key(path):
    return json.dumps(path, ensure_ascii=False, separators=(",", ":"))


def _flatten(value, path=()):
    result = {}
    for key, item in value.items():
        next_path = (*path, key)
        if isinstance(item, dict) and item:
            result.update(_flatten(item, next_path))
        else:
            result[_path_key(next_path)] = item
    return result


def _get(data, path):
    value = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _set(data, path, value):
    for key in path[:-1]:
        if key not in data:
            data[key] = {}
        if not isinstance(data[key], dict):
            raise CommonSettingsError("A manual setting conflicts with the managed runtime configuration.")
        data = data[key]
    data[path[-1]] = value


def _remove(data, path):
    if len(path) == 1:
        data.pop(path[0], None)
    elif isinstance(data.get(path[0]), dict):
        _remove(data[path[0]], path[1:])
        if not data[path[0]]:
            data.pop(path[0])


def _empty_state():
    return {"schema": 1, "base_owned": {}, "mcp_owned": {}, "mcp_preserved": []}


def _validate_state(state):
    try:
        if (state.get("schema") != 1 or not isinstance(state["base_owned"], dict)
                or not isinstance(state["mcp_owned"], dict)
                or not isinstance(state["mcp_preserved"], list)):
            raise ValueError()
        for entries in (state["base_owned"], state["mcp_owned"]):
            for key, digest in entries.items():
                if not isinstance(key, str) or not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError()
                int(digest, 16)
        for key in state["base_owned"]:
            path = json.loads(key)
            if (not isinstance(path, list) or not path or not all(isinstance(part, str) for part in path)
                    or path[0] == "mcp_servers"):
                raise ValueError()
        if not all(isinstance(name, str) for name in state["mcp_preserved"]):
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CommonSettingsError("Managed common settings ownership is invalid.") from None


def _state(profile):
    content = _read_text(profile / STATE_FILE)
    if not content:
        return _empty_state()
    try:
        return _validate_state(json.loads(content))
    except ValueError:
        raise CommonSettingsError("Managed common settings ownership is invalid.") from None


def _file_state(path):
    if path.is_symlink():
        raise CommonSettingsError("Settings file is a symlink.")
    if not path.exists():
        return {"exists": False, "sha256": hashlib.sha256(b"").hexdigest()}
    if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise CommonSettingsError("Settings file is invalid or too large.")
    return {"exists": True, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _ownership_snapshot(profile):
    return [_file_state(profile / STATE_FILE), _file_state(profile / PENDING_FILE)]


def _pending(profile):
    content = _read_text(profile / PENDING_FILE)
    if not content:
        raise CommonSettingsError("Common settings transaction journal is invalid.")
    try:
        pending = json.loads(content)
        if pending["schema"] != 1:
            raise ValueError()
        for key in ("before_config", "after_config"):
            value = pending[key]
            if (not isinstance(value, dict) or type(value["exists"]) is not bool
                    or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64):
                raise ValueError()
            int(value["sha256"], 16)
        if pending["after_config"]["exists"] is not True:
            raise ValueError()
        _validate_state(pending["before_metadata"])
        _validate_state(pending["after_metadata"])
        return pending
    except (ValueError, KeyError, TypeError):
        raise CommonSettingsError("Common settings transaction journal is invalid.") from None


def _active_state(profile, config_state):
    if not (profile / PENDING_FILE).exists():
        return _state(profile), False
    pending = _pending(profile)
    if config_state == pending["after_config"]:
        return pending["after_metadata"], True
    if config_state == pending["before_config"]:
        return pending["before_metadata"], True
    raise CommonSettingsError("An unfinished common settings update conflicts with the current config.")


def prepare(profile_home, generated_config, *, source_home=None, previous_generated_sha256=None):
    """Merge on the remote host without writing; preserve manual profile values.

    A previous whole-file hash permits one-time migration of older manager-owned
    configs. A generated runtime value that conflicts with a manual value is an
    error, so the launcher cannot falsely report a provider policy as applied.
    All unrelated manual fields and manually changed/deleted MCP declarations
    are preserved. Comments/formatting may be normalized, never config values.
    """
    profile = _directory(profile_home)
    source = Path(source_home) if source_home is not None else Path.home() / ".codex"
    if source.absolute() == profile:
        raise CommonSettingsError("Common and isolated settings homes must differ.")
    generated = _parse(generated_config)
    if "mcp_servers" in generated:
        raise CommonSettingsError("Remote MCP settings must originate on the SSH host.")
    before_config = _file_state(profile / "config.toml")
    ownership_snapshot = _ownership_snapshot(profile)
    current_text = _read_text(profile / "config.toml")
    current = _parse(current_text)
    state, recovering = _active_state(profile, before_config)
    if (before_config != _file_state(profile / "config.toml")
            or ownership_snapshot != _ownership_snapshot(profile)):
        raise CommonSettingsError("Common settings changed while preparing an update.")
    if not recovering and not (profile / STATE_FILE).exists() and previous_generated_sha256:
        if before_config["sha256"] == previous_generated_sha256:
            state["base_owned"] = {key: _digest(value) for key, value in
                                   _flatten({key: value for key, value in current.items()
                                             if key != "mcp_servers"}).items()}
    before_metadata = json.loads(json.dumps(state))

    desired = _flatten(generated)
    owned = state["base_owned"]
    # Remove obsolete generated leaves only if their value has not been edited.
    for key, digest in owned.items():
        if key not in desired:
            path = json.loads(key)
            value = _get(current, path)
            if value is not _MISSING and _digest(value) == digest:
                _remove(current, path)
    next_owned = {}
    for key, value in desired.items():
        path = json.loads(key)
        old = _get(current, path)
        if old is not _MISSING and _digest(old) != _digest(value):
            if key not in owned or _digest(old) != owned[key]:
                raise CommonSettingsError("A manual setting conflicts with the managed runtime configuration.")
        # A matching unowned value remains manual; do not claim ownership of it.
        if old is _MISSING or key in owned:
            _set(current, path, value)
            next_owned[key] = _digest(value)

    common = _parse(_read_text(source / "config.toml")).get("mcp_servers", {})
    existing = current.get("mcp_servers", {})
    if (not isinstance(common, dict) or not isinstance(existing, dict)
            or not all(isinstance(value, dict) for value in common.values())
            or not all(isinstance(value, dict) for value in existing.values())):
        raise CommonSettingsError("Remote MCP declarations must be server tables.")
    previous = state["mcp_owned"]
    preserved = set(state["mcp_preserved"])
    next_mcp = {}
    added, updated, removed = [], [], []
    for name, digest in previous.items():
        if name not in existing or _digest(existing[name]) != digest:
            preserved.add(name)
        elif name not in common:
            del existing[name]
            removed.append(name)
    for name, declaration in common.items():
        if name in preserved:
            continue
        if name in existing and name not in previous:
            preserved.add(name)
            continue
        if name not in existing:
            added.append(name)
        elif _digest(existing[name]) != _digest(declaration):
            updated.append(name)
        existing[name] = declaration
        next_mcp[name] = _digest(declaration)
    if existing:
        current["mcp_servers"] = existing
    else:
        current.pop("mcp_servers", None)
    output = _dump(current)
    _parse(output)
    next_state = {"schema": 1, "base_owned": next_owned, "mcp_owned": next_mcp,
                  "mcp_preserved": sorted(preserved)}
    summary = {"mcp_added": added, "mcp_updated": updated, "mcp_removed": removed,
               "mcp_preserved": sorted(preserved), "mcp_shared_count": len(next_mcp)}
    return Plan(profile, output, next_state, summary, before_config, before_metadata, ownership_snapshot)


def _sync_directory(path):
    if hasattr(os, "O_DIRECTORY"):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic(path, content):
    if path.is_symlink():
        raise CommonSettingsError("Settings destination is a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".private-common-", encoding="utf-8", newline="\n", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def stage(profile_home, plan):
    """Durably record ownership before changing config; journal contains no values."""
    profile = _directory(profile_home)
    if not isinstance(plan, Plan) or plan._profile != profile:
        raise CommonSettingsError("Common settings plan belongs to another profile.")
    if (plan._before_config != _file_state(profile / "config.toml")
            or plan._ownership_snapshot != _ownership_snapshot(profile)):
        raise CommonSettingsError("Common settings changed after preparing an update.")
    pending = {"schema": 1, "before_config": plan._before_config,
               "after_config": {"exists": True, "sha256": hashlib.sha256(plan.config.encode("utf-8")).hexdigest()},
               "before_metadata": plan._before_metadata, "after_metadata": plan._metadata}
    _atomic(profile / PENDING_FILE, json.dumps(pending, ensure_ascii=False, indent=2) + "\n")


def commit(profile_home, plan):
    """Publish ownership, then clear the staged journal, after exact config write."""
    profile = _directory(profile_home)
    if not isinstance(plan, Plan) or plan._profile != profile:
        raise CommonSettingsError("Common settings plan belongs to another profile.")
    if _read_text(profile / "config.toml") != plan.config:
        raise CommonSettingsError("Prepared common settings have not been installed.")
    if not (profile / PENDING_FILE).exists():
        raise CommonSettingsError("Common settings were not staged.")
    pending = _pending(profile)
    if (pending["after_config"] != _file_state(profile / "config.toml")
            or pending["after_metadata"] != plan._metadata):
        raise CommonSettingsError("Common settings transaction no longer matches the prepared update.")
    _atomic(profile / STATE_FILE, json.dumps(plan._metadata, ensure_ascii=False, indent=2) + "\n")
    (profile / PENDING_FILE).unlink()
    _sync_directory(profile)
    return dict(plan.summary)


def apply(profile_home, generated_config, *, source_home=None, previous_generated_sha256=None):
    """Convenience: write private merged config and ownership; return its text."""
    plan = prepare(profile_home, generated_config, source_home=source_home,
                   previous_generated_sha256=previous_generated_sha256)
    stage(profile_home, plan)
    _atomic(plan._profile / "config.toml", plan.config)
    commit(profile_home, plan)
    return plan.config


def reconcile_skills(profile_home, *, source_home=None):
    """Link to this remote user's skills; never replace a conflicting path."""
    profile = _directory(profile_home)
    source = Path(source_home) if source_home is not None else Path.home() / ".codex"
    origin = source.absolute() / "skills"
    target = profile / "skills"
    if target.is_symlink():
        return {"skills": "shared" if target.resolve() == origin.resolve() else "preserved"}
    if target.exists():
        return {"skills": "preserved"}
    if not origin.is_dir() or origin == target:
        return {"skills": "unavailable"}
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    try:
        target.symlink_to(origin, target_is_directory=True)
    except FileExistsError:
        return {"skills": "preserved"}
    return {"skills": "shared"}
