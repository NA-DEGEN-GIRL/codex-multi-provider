"""Managed Linux launcher with one writer per profile and no global changes.

The native desktop transport must explicitly invoke this launcher and revision;
uploading it alone does not integrate the original desktop's SSH connection.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


ROLE_NAME = re.compile(r"agents/cc_(?:gpt_(?:astra|luna|sol|terra)|external_[0-9a-f]{32}_r[0-9]+_[0-9a-f]{12})\.toml")


class ConfigurationConflict(ValueError):
    code = 'remote_configuration_changed'


def _read(path):
    if path.is_symlink():
        raise ValueError("symlink")
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic(path, value):
    if path.is_symlink():
        raise ValueError("symlink")
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".private-", encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def host_identity():
    identity = Path("/etc/machine-id").read_text().strip()
    return hashlib.sha256((identity + "\\0" + str(os.getuid()) + "\\0" + str(Path.home())).encode()).hexdigest()


def configure(profile, data, *, expected_host=None):
    revision = data.get("revision", "")
    if not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("revision")
    descriptor = _read(profile / "definitions" / (revision + ".json"))
    if descriptor["revision"] != revision:
        raise ValueError("revision mismatch")
    environment = data.get("environment", {})
    if not isinstance(environment, dict) or len(environment) > 64:
        raise ValueError("environment")
    # Accept generated provider key names only, never PATH/HOME/auth overrides.
    for key, value in environment.items():
        if (not re.fullmatch(r"CODEX_EXTERNAL_[A-Z0-9_]+_API_KEY", key) or
                not isinstance(value, str) or not value or len(value) > 65536 or "\x00" in value):
            raise ValueError("environment value")
    recorded_host = descriptor.get("host_identity")
    if not isinstance(recorded_host, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_host):
        raise ValueError("host binding missing")
    if recorded_host != (host_identity() if expected_host is None else expected_host):
        raise ValueError("SSH host or OS user changed")
    credentials = profile / "credentials"
    if credentials.is_symlink():
        raise ValueError("symlink")
    credentials.mkdir(mode=0o700, exist_ok=True)
    os.chmod(credentials, 0o700)
    _atomic(credentials / (revision + ".json"), environment)
    return {"status": "configured", "authenticated": (profile / "codex/auth.json").is_file()}


def validate_command(argv):
    # Commands can never kill unrelated remote Codex daemons. Native GUI bootstrap
    # rewriting must route only start/stdio here; app-server-control is not exposed.
    if argv in (["--version"], ["login"], ["login", "status"], ["login", "--device-auth"]):
        return
    if not argv or argv[0] != "app-server":
        raise ValueError("unsupported command")
    options = list(argv[1:])
    while options:
        option = options.pop(0)
        if option in ("--stdio", "--strict-config", "--analytics-default-enabled"):
            continue
        if option == "--listen" and options and options.pop(0) == "stdio://":
            continue
        if option == "--listen=stdio://":
            continue
        raise ValueError("only stdio app-server commands are allowed")


def protected_bwrap_directory(runtime):
    """Use an optional administrator-approved companion without changing global PATH."""
    bundled = runtime / "bwrap"
    if not bundled.is_file():
        return None
    with bundled.open("rb") as stream:
        expected = hashlib.file_digest(stream, "sha256").hexdigest()
    binary = Path("/usr/local/lib/codex-control-center/bwrap") / expected / "bwrap"
    if not binary.exists() and not binary.is_symlink():
        return None
    for path in (binary, *binary.parents):
        if path.is_symlink():
            raise ValueError("protected sandbox symlink")
        metadata = path.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError("protected sandbox ownership")
        if path == binary and (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o7000):
            raise ValueError("protected sandbox file type")
    if not os.access(binary, os.X_OK):
        raise ValueError("protected sandbox execute permission")
    with binary.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("protected sandbox hash")
    return binary.parent


def _proc_cmdline(pid):
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def managed_instances(profile_id):
    """Managed app-server processes for one profile as ``(pid, executable)``."""
    found = []
    root = Path("/proc")
    try:
        entries = list(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        command = _proc_cmdline(entry.name)
        if ("codex-control-center/runtime/" not in command or profile_id not in command
                or "app-server" not in command):
            continue
        try:
            executable = os.readlink(entry / "exe")
        except OSError:
            executable = ""
        found.append((int(entry.name), executable))
    return found


def stale_instances(instances, runtime):
    """Entries whose executable does not belong to the current runtime bundle."""
    # The launcher only ever runs on Linux. Using the raw string keeps the
    # comparison free of whatever separator the local platform would apply.
    prefix = str(runtime).rstrip("/") + "/"
    return [(pid, executable) for pid, executable in instances
            if not executable.startswith(prefix)]


def clear_stale_record(profile):
    """Drop an instance record whose process no longer exists."""
    path = profile / "native-instance.json"
    if not path.exists() or path.is_symlink():
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    pid = record.get("pid")
    if isinstance(pid, int) and pid > 0 and not (Path("/proc") / str(pid)).exists():
        path.unlink(missing_ok=True)


def recover_role_ownership(profile, home, previous):
    """Recover lost ownership only from exact bytes in validated old definitions.

    Older launches forgot removed files in generated-files.json while leaving
    them in agents/. When the role is selected again it must not look like a new
    user file. Unknown or edited files remain unowned and are never overwritten.
    """
    recovered = {}
    roles = home / 'agents'
    if not roles.exists() or roles.is_symlink():
        return recovered
    candidates = {}
    for path in roles.glob('cc_*.toml'):
        name = 'agents/' + path.name
        if (name in previous or not ROLE_NAME.fullmatch(name) or path.is_symlink()
                or not path.is_file() or path.stat().st_size > 1024 * 1024):
            continue
        candidates[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    definitions = profile / 'definitions'
    if not candidates or definitions.is_symlink():
        return recovered
    for index, descriptor_path in enumerate(sorted(definitions.glob('*.json'))):
        if index >= 512 or not candidates:
            break
        revision = descriptor_path.stem
        if (not re.fullmatch(r'[0-9a-f]{64}', revision) or descriptor_path.is_symlink()
                or descriptor_path.stat().st_size > 65536):
            continue
        try:
            descriptor = _read(descriptor_path)
            definition = definitions / revision
            if (descriptor.get('profile_id') != profile.name or descriptor.get('revision') != revision
                    or descriptor.get('definition') != str(definition) or definition.is_symlink()
                    or (definition / 'agents').is_symlink()):
                continue
            for name, digest in list(candidates.items()):
                source = definition / name
                if (not source.is_symlink() and source.is_file() and source.stat().st_size <= 1024 * 1024
                        and hashlib.sha256(source.read_bytes()).hexdigest() == digest):
                    recovered[name] = digest
                    del candidates[name]
        except (OSError, ValueError, TypeError):
            continue
    return recovered


def obsolete_role_plan(home, previous, generated):
    """Validate obsolete manager roles before changing any generated config."""
    plan = []
    for name, digest in previous.items():
        if name in generated or not ROLE_NAME.fullmatch(name):
            continue
        target = home / name
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise ValueError("symlink obsolete role")
        if not target.exists():
            continue
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ConfigurationConflict("obsolete managed role was edited; preserve it before applying settings")
        archive = home / "manager-retired-agents" / (target.stem + "." + digest[:16] + ".toml")
        if archive.is_symlink() or any(parent.is_symlink() for parent in archive.parents):
            raise ValueError("symlink obsolete role archive")
        if archive.exists() and archive.read_bytes() != content:
            raise ValueError("obsolete role archive conflict")
        plan.append((target, archive))
    return plan


def acquire_instance_lock(profile, lock, fcntl, runtime):
    """Take the single-writer lock, reaping instances left by an older bundle.

    A runtime update leaves the previous remote app-server running while the
    descriptor already points at the new bundle. Without this the next
    connection fails with a bare lock error and the profile looks broken.
    """
    import signal
    import time

    for _ in range(2):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        stale = stale_instances(managed_instances(profile.name), runtime)
        if not stale:
            raise ValueError("another Codex instance is already running for this profile")
        for pid, _ in stale:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any((Path("/proc") / str(pid)).exists() for pid, _ in stale):
                break
            time.sleep(0.2)
        for pid, _ in stale:
            if (Path("/proc") / str(pid)).exists():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    raise ValueError("a previous Codex instance still holds this profile")


def shared_execution_environment(runtime, profile_id):
    """Opt a supported worker into editing registered, host-local source records.

    Catalog support alone predates shared editing. Inspect the installed binary,
    already pinned by the descriptor, so old catalog runtimes stay read-only.
    Never inherit this authority or a writer identity from the SSH client.
    """
    from uuid import UUID
    if str(UUID(profile_id)) != profile_id:
        raise ValueError('canonical profile identifier required')
    with (runtime / 'codex').open('rb') as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            return {}
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as binary:
            if any(binary.find(marker) < 0 for marker in (
                    b'CODEX_MANAGER_SHARED_EXECUTION', b'CODEX_MANAGER_SHARED_WRITER_ID',
                    b'CODEX_MANAGER_SHARED_ROUTES')):
                return {}
    return {'CODEX_MANAGER_SHARED_EXECUTION': '1',
            'CODEX_MANAGER_SHARED_WRITER_ID': profile_id,
            'CODEX_RECORD_SHARED_APPEND': '1'}


def run(profile, revision, argv, *, managed_socket=None):
    import fcntl
    import common
    clear_stale_record(profile)
    if not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("revision")
    if managed_socket is None:
        validate_command(argv)
    else:
        from native_controller import socket_path
        if managed_socket != socket_path(profile) or argv != ["app-server", "--listen", "unix://" + str(managed_socket)]:
            raise ValueError("managed listener arguments")
    descriptor = _read(profile / "definitions" / (revision + ".json"))
    if descriptor["revision"] != revision or descriptor["profile_id"] != profile.name:
        raise ValueError("descriptor mismatch")
    base = profile.parent.parent.resolve()
    runtime = Path(descriptor["runtime"])
    definition = Path(descriptor["definition"])
    if (runtime.is_symlink() or definition.is_symlink() or
            runtime.parent.resolve() != (base / "runtime").resolve() or
            definition.parent.resolve() != (profile / "definitions").resolve()):
        raise ValueError("descriptor path")
    lock_path = profile / "instance.lock"
    if lock_path.is_symlink():
        raise ValueError("symlink lock")
    with lock_path.open("a+b") as lock:
        acquire_instance_lock(profile, lock, fcntl, runtime)
        codex_home = profile / "codex"
        if codex_home.is_symlink():
            raise ValueError("symlink home")
        codex_home.mkdir(mode=0o700, exist_ok=True)
        generated_path = profile / "generated-files.json"
        prior = _read(generated_path) if generated_path.exists() else {}
        recovered = recover_role_ownership(profile, codex_home, prior)
        prior = {**prior, **recovered}
        files = {}
        common_plan = None
        for source in definition.rglob("*"):
            if source.is_file():
                if source.is_symlink():
                    raise ValueError("symlink definition")
                name = source.relative_to(definition).as_posix()
                target = codex_home / name
                if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                    raise ValueError("symlink configuration")
                content = source.read_bytes()
                if name == "config.toml":
                    common_plan = common.prepare(codex_home, content.decode("utf-8"),
                                                 previous_generated_sha256=prior.get(name))
                    content = common_plan.config.encode("utf-8")
                new_digest = hashlib.sha256(content).hexdigest()
                if target.exists() and name != "config.toml":
                    current = hashlib.sha256(target.read_bytes()).hexdigest()
                    if current != new_digest and current != prior.get(name):
                        raise ConfigurationConflict("user configuration changed")
                files[name] = (content, new_digest)
        retired_roles = obsolete_role_plan(codex_home, prior, files)
        repaired_roles = obsolete_role_plan(codex_home, {
            name: digest for name, digest in recovered.items()
            if name in files and files[name][1] != digest}, {})
        if common_plan is not None:
            common.stage(codex_home, common_plan)
        # Preserve recovered historical bytes before replacing the selected role.
        for target, archive in repaired_roles:
            archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(target, archive)
        # Validate every destination before updating any file; never overwrite auth.
        for name, (content, digest) in files.items():
            if name in ("auth.json", "credentials.json"):
                raise ValueError("auth file forbidden")
            target = codex_home / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(content)
            os.replace(temporary, target)
        if common_plan is not None:
            common.commit(codex_home, common_plan)
        # Native role discovery scans agents/*.toml even after config references
        # disappear. Keep byte-exact backups outside that discovery directory.
        for target, archive in retired_roles:
            archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(target, archive)
        _atomic(generated_path, {name: digest for name, (_, digest) in files.items()})
        common.reconcile_skills(codex_home)
        inherited_auth = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"}
        env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_") and key not in inherited_auth}
        env.update(_read(profile / "credentials" / (revision + ".json")))
        env["CODEX_HOME"] = str(codex_home)
        if descriptor.get('managed_sources') is True:
            from managed_sources import generate
            shared_catalog = descriptor.get('source_catalog') is True
            mixed_catalog = descriptor.get('mixed_source_catalog') is True
            env['CODEX_MANAGER_MANAGED_SOURCES'] = str(generate(profile, atomic=_atomic,
                shared_catalog=shared_catalog, legacy_discovery=mixed_catalog))
            if shared_catalog:
                env['CODEX_MANAGER_SHARED_CATALOG'] = str(base / ('catalog-mixed-sources.json' if mixed_catalog else 'catalog-sources.json'))
                env.update(shared_execution_environment(runtime, profile.name))
                if env.get('CODEX_MANAGER_SHARED_EXECUTION') == '1':
                    env['CODEX_MANAGER_SHARED_ROUTES'] = str(base / 'shared-record-routes.json')
                    # Match the desktop shared-record worker: record routing is
                    # authorized by the source catalog, not the older exclusive
                    # ownership/handoff manifest. Mixing both modes rejects
                    # otherwise valid canonical resumes from another profile.
                    env.pop('CODEX_MANAGER_MANAGED_SOURCES', None)
        protected_sandbox = protected_bwrap_directory(runtime)
        if protected_sandbox is not None:
            env["PATH"] = str(protected_sandbox) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        # Transfer the lock into the actual runtime. Killing an intermediate SSH/
        # Python process cannot release ownership while a child runtime keeps running.
        # Companion discovery uses the executable's sibling directory in Codex.
        os.set_inheritable(lock.fileno(), True)
        if managed_socket is not None:
            from native_controller import process_record
            # The detached listener publishes its own ownership while it holds the
            # runtime lock. Losing the initiating SSH command cannot orphan an
            # otherwise healthy daemon without a reconnectable identity record.
            _atomic(profile / "native-instance.json", process_record(revision, managed_socket))
        executable = str(runtime / "codex")
        actual_args = [executable, *argv] if managed_socket is None else [executable, "-c", "features.code_mode_host=true", *argv]
        os.execve(executable, actual_args, env)


if __name__ == "__main__":
    profile = Path(os.environ.get("CODEX_MANAGER_PROFILE_DIR", str(Path(__file__).resolve().parent))).resolve()
    try:
        if sys.argv[-1:] == ["--configure"]:
            incoming = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
            if len(incoming) > 4 * 1024 * 1024:
                raise ValueError("payload too large")
            data = json.loads(incoming)
            if len(sys.argv) == 3 and data.get("revision") != sys.argv[1]:
                raise ValueError("configuration revision")
            print(json.dumps(configure(profile, data)))
        else:
            if len(sys.argv) == 3 and sys.argv[2].startswith("native-"):
                from native_controller import main
                sys.exit(main(profile, sys.argv[1], sys.argv[2]))
            sys.exit(run(profile, sys.argv[1], sys.argv[2:]))
    except Exception as error:
        if isinstance(error, ConfigurationConflict):
            print('Codex manager remote launcher error: remote_configuration_changed', file=sys.stderr)
        detail = str(error) or error.__class__.__name__
        print(f"Codex manager remote launcher failed: {detail}", file=sys.stderr)
        print("Codex manager remote launcher failed; inspect the profile binding.", file=sys.stderr)
        sys.exit(1)
