"""Build the patched checkout on Linux and publish a verified remote bundle.

Run on a Linux build machine with this checkout and its Rust prerequisites:
    python3 scripts/remote_helpers/package_runtime.py --build
Pass --build-cache /path/to/remote-build to reuse its linux-<arch> and v8
directories while staging this checkout's package separately.
This is not a downloader and never installs over the host's stock Codex CLI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from manager_core.remote import ARCHES, REMOTE_CAPABILITY_MARKERS, _verify_elf

# The build server is shared: builds use 64 of its 128 hardware threads. The
# CPU set covers 32 physical cores (SMT siblings are N and N+64), leaving the
# other 32 cores entirely free; it also bounds linker and LLVM thread pools,
# which `-j` alone does not.
SERVER_BUILD_JOBS = 64
SERVER_BUILD_CPUS = "0-31,64-95"
# Generous: a codex that still carries its line tables is ~1.4 GB, and every
# SSH host would download it.
MAX_STRIPPED_CODEX_BYTES = 600 * 1024 * 1024


def parse_cpus(text):
    cpus = set()
    for part in text.split(","):
        first, separator, last = part.strip().partition("-")
        if not first.isdigit() or (separator and (not last.isdigit() or int(last) < int(first))):
            raise ValueError("Invalid CPU list: " + text)
        cpus.update(range(int(first), int(last or first) + 1))
    return cpus


def limit_server_build(cpus, jobs=None):
    """Linux-only process limits for a server build; call from __main__ only.

    Returns (jobs, lock). The manager imports this module on Windows, so the
    POSIX-only modules stay local. One lock per user keeps a Linux bundle build
    and a Windows cross build (any checkout or cache) from running together.
    """
    if platform.system() != "Linux":
        raise RuntimeError("Server build limits apply on the Linux build server only.")
    import fcntl
    import resource
    os.sched_setaffinity(0, parse_cpus(cpus))
    available = len(os.sched_getaffinity(0))
    jobs = min(SERVER_BUILD_JOBS, available) if jobs is None else jobs
    if not 1 <= jobs <= available:
        raise ValueError("--jobs must be between 1 and the %d CPUs in --cpus." % available)
    os.nice(10)
    # Parallel test and link steps exceed the default 1024 descriptors.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 65536 if hard == resource.RLIM_INFINITY else min(65536, hard)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    path = Path.home() / ".cache/codex-runtime-build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Waiting for another runtime build (" + str(path) + ")...", file=sys.stderr, flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    return jobs, lock


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contains_marker(path, marker):
    overlap = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            data = overlap + chunk
            if marker in data:
                return True
            overlap = data[-len(marker):]
    return False


def prepare_v8(root, arch, *, build_cache=None, target=None):
    """Follow the upstream .github/actions/setup-rusty-v8/action.yml contract."""
    lock = tomllib.loads((root / "runtime/codex-rs/Cargo.lock").read_text())
    versions = {package["version"] for package in lock["package"] if package["name"] == "v8"}
    if len(versions) != 1:
        raise RuntimeError("The V8 dependency version is ambiguous.")
    version = versions.pop()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("The V8 dependency version is unsupported.")
    target = target or arch + "-unknown-linux-gnu"
    if target.endswith("-pc-windows-msvc"):
        archive = "rusty_v8_ptrcomp_sandbox_release_" + target + ".lib.gz"
    else:
        archive = "librusty_v8_ptrcomp_sandbox_release_" + target + ".a.gz"
    binding = "src_binding_ptrcomp_sandbox_release_" + target + ".rs"
    checksums = "rusty_v8_ptrcomp_sandbox_release_" + target + ".sha256"
    url = "https://github.com/openai/codex/releases/download/rusty-v8-v" + version + "/"
    cache = Path(build_cache).expanduser().resolve() if build_cache is not None else root / "work/remote-build"
    directory = cache / "v8" / target / version
    directory.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url + checksums, timeout=60) as response:
        lines = response.read(8192).decode().splitlines()
    expected = {}
    for line in lines:
        checksum, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum) or name not in (archive, binding) or name in expected:
            raise RuntimeError("The official V8 checksum manifest is invalid.")
        expected[name] = checksum
    if len(expected) != 2:
        raise RuntimeError("The official V8 checksum manifest is incomplete.")
    for name in (archive, binding):
        path = directory / name
        if path.is_symlink():
            raise RuntimeError("Refusing a symlink in the V8 cache.")
        if not path.is_file() or digest(path) != expected[name]:
            with urllib.request.urlopen(url + name, timeout=120) as response, path.open("wb") as output:
                while data := response.read(1024 * 1024):
                    output.write(data)
        if digest(path) != expected[name]:
            raise RuntimeError("The V8 artifact checksum did not match.")
    return {"RUSTY_V8_ARCHIVE": str(directory / archive), "RUSTY_V8_SRC_BINDING_PATH": str(directory / binding)}


def build(root=ROOT, *, build_cache=None, jobs=SERVER_BUILD_JOBS):
    root = Path(root).resolve()
    arch = ARCHES.get(platform.machine().lower())
    if platform.system() != "Linux" or arch not in ("x86_64", "aarch64"):
        raise RuntimeError("Run this builder on Linux x86_64 or aarch64. Windows binaries cannot be used remotely.")
    source = root / "runtime/codex-rs"
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("The repository's Rust toolchain and Linux build prerequisites are required.")
    destination = root / "artifacts/remote" / ("linux-" + arch)
    cache = Path(build_cache).expanduser().resolve() if build_cache is not None else root / "work/remote-build"
    target = cache / ("linux-" + arch)
    environment = dict(os.environ)
    environment.update(prepare_v8(root, arch, build_cache=cache))
    # Match the upstream release build: finalize bubblewrap first, then pin its
    # exact digest in Codex. Never publish a Linux runtime without its sandbox.
    subprocess.run([cargo, "build", "--locked", "--release", "--target-dir", str(target), "-j", str(jobs),
                    "-p", "codex-bwrap", "--bin", "bwrap"], cwd=source, env=environment, check=True)
    bwrap = target / "release/bwrap"
    subprocess.run(["strip", "--strip-debug", "--strip-unneeded", str(bwrap)], check=True)
    bwrap_version = subprocess.run([str(bwrap), "--version"], capture_output=True, text=True, check=True, timeout=10)
    if "bubblewrap" not in bwrap_version.stdout or "not available" in bwrap_version.stdout:
        raise RuntimeError("The built bubblewrap companion is unavailable.")
    environment["CODEX_BWRAP_SHA256"] = digest(bwrap)
    # A controlled target directory prevents accidentally packaging an unrelated
    # CARGO_TARGET_DIR or a Windows cross-compilation result.
    subprocess.run([cargo, "build", "--locked", "--release", "--target-dir", str(target), "-j", str(jobs),
                    "-p", "codex-cli", "--bin", "codex", "-p", "codex-code-mode-host",
                    "--bin", "codex-code-mode-host"], cwd=source, env=environment, check=True)
    # The release profile keeps line tables (strip = false) so packaging can
    # archive symbols first. Shipping them made every SSH upload ~1.4 GB, so
    # strip copies here and keep the .debug sidecars beside them on the build
    # host; cargo's own outputs stay intact for the next incremental build.
    if build_cache is None:
        staged = target / "package"
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True)
    else:
        # The explicitly reused cache may hold another build's debug symbols.
        # Keep every candidate under this checkout and never clean cache/package.
        staging_root = root / "work/remote-build"
        staging_root.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix="package-linux-" + arch + "-", dir=staging_root))
    for name in ("codex", "codex-code-mode-host"):
        built, copy = target / "release" / name, staged / name
        shutil.copy2(built, copy)
        subprocess.run(["objcopy", "--only-keep-debug", str(built), str(staged / (name + ".debug"))], check=True)
        subprocess.run(["strip", "--strip-debug", "--strip-unneeded", str(copy)], check=True)
    binary = staged / "codex"
    companion = staged / "codex-code-mode-host"
    size = binary.stat().st_size
    if size > MAX_STRIPPED_CODEX_BYTES:
        raise RuntimeError("The stripped codex is %d MB, above the %d MB limit; strip probably kept its debug "
                           "sections. Nothing was published." % (size // 2**20, MAX_STRIPPED_CODEX_BYTES // 2**20))
    for file in (binary, companion, bwrap):
        _verify_elf(file, arch)
    if not contains_marker(binary, b"external_agents"):
        raise RuntimeError("The external-agent bridge marker was not found in the built runtime.")
    response = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True, timeout=30)
    match = re.fullmatch(r"codex-cli ([A-Za-z0-9][A-Za-z0-9_.-]{0,95})\s*", response.stdout)
    if not match:
        raise RuntimeError("The built runtime returned an unexpected version.")
    # Every patched build reports the upstream version; tag it with the recorded
    # patch so the SSH update screen can tell two managed builds apart.
    source_sha256 = json.loads((root / "patches/runtime-source.json").read_text(encoding="utf-8"))["patch_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise RuntimeError("The recorded runtime patch digest is invalid.")
    manifest = {"schema": 1, "platform": "linux", "architecture": arch,
                "version": match.group(1) + "-managed-" + source_sha256[:16],
                "build_source_sha256": source_sha256, "external_bridge_present": True,
                "managed_sources_present": contains_marker(binary, b"CODEX_MANAGER_MANAGED_SOURCES"),
                "source_catalog_present": (
                    contains_marker(binary, b"CODEX_MANAGER_SHARED_CATALOG")
                    and contains_marker(binary, b"invalid managed source catalog")),
                "mixed_source_catalog_present": contains_marker(binary, b"invalid mixed source catalog legacy identity"),
                "bwrap_sha256": environment["CODEX_BWRAP_SHA256"],
                "verification": "Built from the patched runtime checkout; ELF architecture and external_agents marker checked.",
                "native_gui_ssh_verified": False, "files": []}
    manifest.update({name: contains_marker(binary, marker) for name, marker in REMOTE_CAPABILITY_MARKERS.items()})
    destination.mkdir(parents=True, exist_ok=True)
    for source_file in (binary, companion, bwrap):
        if (destination / source_file.name).is_symlink():
            raise RuntimeError("Refusing a symlink in the artifact destination.")
        with tempfile.NamedTemporaryFile(dir=destination, prefix=".artifact-", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source_file, temporary)
            os.chmod(temporary, 0o700)
            manifest["files"].append({"path": source_file.name, "sha256": digest(temporary)})
            os.replace(temporary, destination / source_file.name)
        finally:
            temporary.unlink(missing_ok=True)
    # Publish the manifest last. A reader during an interrupted update fails the
    # hash gate rather than treating mismatched files as a valid old bundle.
    with tempfile.NamedTemporaryFile("w", dir=destination, prefix=".manifest-", encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, destination / "manifest.json")
    return {"status": "built", "bundle_directory": str(destination), "staging_directory": str(staged), "version": manifest["version"],
            "native_gui_ssh_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", required=True)
    parser.add_argument("--build-cache", type=Path, help="Explicit cache containing linux-<arch> and v8 directories.")
    parser.add_argument("--jobs", type=int, help="Parallel build jobs (default: 64, at most the --cpus size).")
    parser.add_argument("--cpus", default=SERVER_BUILD_CPUS, help="CPU set for the build (default 0-31,64-95).")
    args = parser.parse_args()
    try:
        jobs, lock = limit_server_build(args.cpus, args.jobs)
        print(json.dumps(build(build_cache=args.build_cache, jobs=jobs)))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
