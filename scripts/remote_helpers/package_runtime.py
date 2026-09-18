"""Build the patched checkout on Linux and publish a verified remote bundle.

Run on a Linux build machine with this checkout and its Rust prerequisites:
    python3 scripts/remote_helpers/package_runtime.py --build
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
from manager_core.remote import ARCHES, _verify_elf


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


def prepare_v8(root, arch):
    """Follow the upstream .github/actions/setup-rusty-v8/action.yml contract."""
    lock = tomllib.loads((root / "runtime/codex-rs/Cargo.lock").read_text())
    versions = {package["version"] for package in lock["package"] if package["name"] == "v8"}
    if len(versions) != 1:
        raise RuntimeError("The V8 dependency version is ambiguous.")
    version = versions.pop()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("The V8 dependency version is unsupported.")
    target = arch + "-unknown-linux-gnu"
    archive = "librusty_v8_ptrcomp_sandbox_release_" + target + ".a.gz"
    binding = "src_binding_ptrcomp_sandbox_release_" + target + ".rs"
    checksums = "rusty_v8_ptrcomp_sandbox_release_" + target + ".sha256"
    url = "https://github.com/openai/codex/releases/download/rusty-v8-v" + version + "/"
    directory = root / "work/remote-build/v8" / target / version
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


def build(root=ROOT):
    root = Path(root).resolve()
    arch = ARCHES.get(platform.machine().lower())
    if platform.system() != "Linux" or arch not in ("x86_64", "aarch64"):
        raise RuntimeError("Run this builder on Linux x86_64 or aarch64. Windows binaries cannot be used remotely.")
    source = root / "runtime/codex-rs"
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("The repository's Rust toolchain and Linux build prerequisites are required.")
    destination = root / "artifacts/remote" / ("linux-" + arch)
    target = root / "work/remote-build" / ("linux-" + arch)
    environment = dict(os.environ)
    environment.update(prepare_v8(root, arch))
    # Match the upstream release build: finalize bubblewrap first, then pin its
    # exact digest in Codex. Never publish a Linux runtime without its sandbox.
    subprocess.run([cargo, "build", "--locked", "--release", "--target-dir", str(target),
                    "-p", "codex-bwrap", "--bin", "bwrap"], cwd=source, env=environment, check=True)
    bwrap = target / "release/bwrap"
    subprocess.run(["strip", "--strip-debug", "--strip-unneeded", str(bwrap)], check=True)
    bwrap_version = subprocess.run([str(bwrap), "--version"], capture_output=True, text=True, check=True, timeout=10)
    if "bubblewrap" not in bwrap_version.stdout or "not available" in bwrap_version.stdout:
        raise RuntimeError("The built bubblewrap companion is unavailable.")
    environment["CODEX_BWRAP_SHA256"] = digest(bwrap)
    # A controlled target directory prevents accidentally packaging an unrelated
    # CARGO_TARGET_DIR or a Windows cross-compilation result.
    subprocess.run([cargo, "build", "--locked", "--release", "--target-dir", str(target),
                    "-p", "codex-cli", "--bin", "codex", "-p", "codex-code-mode-host",
                    "--bin", "codex-code-mode-host"], cwd=source, env=environment, check=True)
    binary = target / "release/codex"
    companion = target / "release/codex-code-mode-host"
    for file in (binary, companion, bwrap):
        _verify_elf(file, arch)
    if not contains_marker(binary, b"external_agents"):
        raise RuntimeError("The external-agent bridge marker was not found in the built runtime.")
    response = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True, timeout=30)
    match = re.fullmatch(r"codex-cli ([A-Za-z0-9][A-Za-z0-9_.-]{0,95})\s*", response.stdout)
    if not match:
        raise RuntimeError("The built runtime returned an unexpected version.")
    manifest = {"schema": 1, "platform": "linux", "architecture": arch,
                "version": match.group(1), "external_bridge_present": True,
                "managed_sources_present": contains_marker(binary, b"CODEX_MANAGER_MANAGED_SOURCES"),
                "source_catalog_present": (
                    contains_marker(binary, b"CODEX_MANAGER_SHARED_CATALOG")
                    and contains_marker(binary, b"invalid managed source catalog")),
                "mixed_source_catalog_present": contains_marker(binary, b"invalid mixed source catalog legacy identity"),
                "bwrap_sha256": environment["CODEX_BWRAP_SHA256"],
                "verification": "Built from the patched runtime checkout; ELF architecture and external_agents marker checked.",
                "native_gui_ssh_verified": False, "files": []}
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
    return {"status": "built", "bundle_directory": str(destination), "version": manifest["version"],
            "native_gui_ssh_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", required=True)
    parser.parse_args()
    try:
        print(json.dumps(build()))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
