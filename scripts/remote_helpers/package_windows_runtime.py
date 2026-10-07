"""Cross-build the managed Windows runtime on the Linux build server.

Run on the Linux build host after the one-time setup (LLVM 20 clang-cl and
lld-link, an xwin MSVC CRT + Windows SDK splat under work/xwin, see
scripts/remote_helpers/README.md):
    python3 scripts/remote_helpers/package_windows_runtime.py --build
It builds the same five binaries as the Windows release build, checks every PE
file, and writes a package directory with windows-build.json. It never
publishes or activates anything: the Windows side fetches the package, stages
it with stage_manager_runtime.py --source and runs the headless validations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_runtime import (ROOT, SERVER_BUILD_CPUS, SERVER_BUILD_JOBS, contains_marker, digest,
                             limit_server_build, prepare_v8)
from manager_core.runtime_migrations import source_migrations

TARGET = "x86_64-pc-windows-msvc"
# Same list (and therefore the same feature unification) as the Windows build.
BINARIES = ("codex", "codex-code-mode-host", "codex-windows-sandbox-setup",
            "codex-command-runner", "codex-app-server")
LLVM = Path("/usr/lib/llvm-20/bin")
# Pinned to the toolset of the local Windows build (MSVC 14.44, SDK 10.0.26100).
XWIN_SPLAT = "msvc-14.44.17.14_sdk-10.0.26100"
XWIN_LIBRARIES = {
    "crt/lib/x86_64": ("libcmt.lib", "libcpmt.lib", "libvcruntime.lib", "oldnames.lib"),
    "sdk/lib/ucrt/x86_64": ("libucrt.lib",),
    "sdk/lib/um/x86_64": ("kernel32.lib", "advapi32.lib", "bcrypt.lib", "dbghelp.lib", "winmm.lib",
                          "uuid.lib", "ntdll.lib", "user32.lib", "shell32.lib", "ole32.lib",
                          "oleaut32.lib", "secur32.lib", "ws2_32.lib", "crypt32.lib"),
}
# Importing any of these means the static CRT (+crt-static in .cargo/config.toml)
# was lost. api-ms-win-core-* imports are legitimate and allowed.
CRT_IMPORT_PREFIXES = ("vcruntime140", "msvcp140", "ucrtbase", "api-ms-win-crt-", "msvcrt")
STACK_RESERVE = 8 * 1024 * 1024
# High-entropy ASLR, dynamic base and NX, as in the local build (0x8160).
REQUIRED_DLL_CHARACTERISTICS = 0x0160
MANIFEST_EXE = "codex-windows-sandbox-setup"
RT_MANIFEST = 24
# Windows applies only manifest resource ID 1 (CREATEPROCESS_MANIFEST_RESOURCE_ID) to an exe.
PROCESS_MANIFEST_ID = 1
ASM_V1 = "{urn:schemas-microsoft-com:asm.v1}"
TRUST_NAMESPACES = ("{urn:schemas-microsoft-com:asm.v2}", "{urn:schemas-microsoft-com:asm.v3}")


def source_tree(runtime):
    """Tree id of the checkout as it is on disk, with the patch-regeneration recipe."""
    with tempfile.TemporaryDirectory() as directory:
        environment = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))

        def git(*arguments):
            return subprocess.run(["git", "-C", str(runtime), *arguments], env=environment, check=True,
                                  capture_output=True, text=True).stdout.strip()

        git("read-tree", "HEAD")
        git("add", "-A", "--", ".", ":!*.snap.new", ":!*.pending-snap")
        return git("write-tree")


def windows_checkout(runtime, tree, destination):
    """Write `tree` exactly as a Windows checkout does (core.autocrlf=true).

    sqlx embeds each migration's SHA-384 over its bytes, so CRLF matters: the
    stores on the PC were migrated by CRLF-built runtimes and refuse an LF
    build ("failed to initialize sqlite state runtime"). include_str! prompts
    also keep the same bytes as the local build.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        environment = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        export = Path(directory) / "export"
        subprocess.run(["git", "-C", str(runtime), "read-tree", tree], env=environment, check=True)
        subprocess.run(["git", "-C", str(runtime), "-c", "core.autocrlf=true", "-c", "core.symlinks=false",
                        "checkout-index", "-a", "-f", "--prefix=" + str(export) + "/"],
                       env=environment, check=True)
        sample = export / "codex-rs/state/migrations/0001_threads.sql"
        if not sample.is_file() or b"\r\n" not in sample.read_bytes():
            raise RuntimeError("The Windows source checkout did not convert line endings.")
        _sync_tree(export, destination)


def _sync_tree(source, destination):
    """Make destination equal source, rewriting only changed files.

    Unchanged files keep their timestamps, so cargo rebuilds only the crates
    whose sources changed instead of the whole workspace.
    """
    wanted = set()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        wanted.add(relative)
        target = destination / relative
        if path.is_dir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                target.unlink()
            target.mkdir(exist_ok=True)
        elif target.is_symlink() or not target.is_file() or target.read_bytes() != path.read_bytes():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            shutil.copy2(path, target)
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.relative_to(destination) not in wanted:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


def _rva_reader(data, sections):
    def offset(rva):
        for virtual, size, raw, raw_size in sections:
            if virtual <= rva < virtual + max(size, raw_size):
                if rva - virtual >= raw_size:
                    raise ValueError("RVA in a section's zero-filled tail")
                return raw + rva - virtual
        raise ValueError("RVA outside every section")
    return offset


def inspect_pe(path):
    """Parse the facts the release checks need from one PE32+ file."""
    with open(path, "rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:2] != b"MZ":
            raise ValueError("not an MZ executable")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        machine, section_count = struct.unpack_from("<HH", data, pe + 4)
        optional_size = struct.unpack_from("<H", data, pe + 20)[0]
        optional = pe + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        if magic != 0x20B:
            raise ValueError("not PE32+")
        subsystem, characteristics = struct.unpack_from("<HH", data, optional + 68)
        stack_reserve = struct.unpack_from("<Q", data, optional + 72)[0]
        directory_count = struct.unpack_from("<I", data, optional + 108)[0]
        directories = [struct.unpack_from("<II", data, optional + 112 + 8 * index)
                       for index in range(min(directory_count, 16))]
        sections, names = [], []
        table = optional + optional_size
        for index in range(section_count):
            entry = table + 40 * index
            names.append(data[entry:entry + 8].rstrip(b"\0").decode("ascii", "replace"))
            virtual_size, virtual, raw_size, raw = struct.unpack_from("<IIII", data, entry + 8)
            sections.append((virtual, virtual_size, raw, raw_size))
        offset = _rva_reader(data, sections)

        def cstring(rva):
            start = offset(rva)
            return data[start:data.find(b"\0", start)].decode("ascii", "replace")

        imports = []
        if len(directories) > 1 and directories[1][0]:
            descriptor = offset(directories[1][0])
            while True:
                fields = struct.unpack_from("<IIIII", data, descriptor)
                if not any(fields):
                    break
                imports.append(cstring(fields[3]).lower())
                descriptor += 20
        manifests = []
        if len(directories) > 2 and directories[2][0]:
            base = offset(directories[2][0])

            def entries(directory):
                named, numbered = struct.unpack_from("<HH", data, directory + 12)
                for index in range(named + numbered):
                    name, target = struct.unpack_from("<II", data, directory + 16 + 8 * index)
                    yield name, target

            def leaves(target):
                if target & 0x80000000:
                    for _, child in entries(base + (target & 0x7FFFFFFF)):
                        yield from leaves(child)
                else:
                    rva, size = struct.unpack_from("<II", data, base + target)
                    start = offset(rva)
                    yield bytes(data[start:start + size])

            for name, target in entries(base):
                if name == RT_MANIFEST and target & 0x80000000:
                    # type -> resource ID -> language -> data; named IDs have the high bit.
                    for identifier, child in entries(base + (target & 0x7FFFFFFF)):
                        key = identifier if not identifier & 0x80000000 else None
                        manifests.extend((key, body) for body in leaves(child))
        return dict(machine=machine, subsystem=subsystem, dll_characteristics=characteristics,
                    stack_reserve=stack_reserve, sections=names, imports=imports, manifests=manifests)


def manifest_is_as_invoker(body):
    """True for exactly one trustInfo requesting asInvoker without UI access.

    Attributes must be unqualified: Windows rejects the manifest (error 14001)
    when they carry a namespace prefix, as LLVM's merge of lld's default UAC
    manifest with an asm.v2 trustInfo produces.
    """
    import xml.etree.ElementTree as ElementTree
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return False
    if root.tag != ASM_V1 + "assembly":
        return False
    trust = [element for element in root if element.tag in (ASM_V1 + "trustInfo",)
             + tuple(namespace + "trustInfo" for namespace in TRUST_NAMESPACES)]
    levels = [element for element in root.iter() if element.tag.endswith("}requestedExecutionLevel")]
    if len(trust) != 1 or len(levels) != 1:
        return False
    return dict(levels[0].attrib) == {"level": "asInvoker", "uiAccess": "false"}


def verify_pe(path, name):
    """Prove the release properties the local Windows build has."""
    facts = inspect_pe(path)
    problems = []
    if facts["machine"] != 0x8664:
        problems.append("machine is not x86_64")
    if facts["subsystem"] != 3:
        problems.append("subsystem is not console")
    if facts["dll_characteristics"] & REQUIRED_DLL_CHARACTERISTICS != REQUIRED_DLL_CHARACTERISTICS:
        problems.append("missing ASLR/NX characteristics 0x%x" % facts["dll_characteristics"])
    if facts["stack_reserve"] != STACK_RESERVE:
        problems.append("stack reserve %d (the .cargo/config.toml rustflags were lost)" % facts["stack_reserve"])
    crt = [item for item in facts["imports"] if item.startswith(CRT_IMPORT_PREFIXES)]
    if crt:
        problems.append("dynamic CRT imports " + ", ".join(crt))
    if name == MANIFEST_EXE:
        if [identifier for identifier, _ in facts["manifests"]] != [PROCESS_MANIFEST_ID]:
            problems.append("expected one embedded manifest with ID 1, found %d" % len(facts["manifests"]))
        elif not manifest_is_as_invoker(facts["manifests"][0][1]):
            problems.append("unexpected embedded manifest")
    elif facts["manifests"]:
        problems.append("unexpected embedded manifest")
    if problems:
        raise RuntimeError(name + ".exe: " + "; ".join(problems))
    return dict(stack_reserve=facts["stack_reserve"], imports=facts["imports"],
                manifest=bool(facts["manifests"]))


def cross_environment(xwin, v8):
    """A fresh environment for the target only; host builds keep their defaults."""
    for part in (xwin, *v8.values()):
        if any(character.isspace() for character in str(part)):
            # aws-lc-sys re-splits CFLAGS on whitespace.
            raise RuntimeError("Cross-build paths must not contain whitespace: " + str(part))
    # RUSTFLAGS would replace the /STACK and +crt-static flags from
    # runtime/codex-rs/.cargo/config.toml; generic CC/AR would leak into the
    # Linux host builds of build scripts and proc macros; profile overrides and
    # other compiler variables would make the package differ from the recorded
    # release profile.
    dropped = {"RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_RUSTFLAGS", "CARGO_BUILD_TARGET",
               "CARGO_TARGET_DIR", "CODEX_BWRAP_SHA256", "CC", "CXX", "AR", "CFLAGS", "CXXFLAGS",
               "RUSTY_V8_MIRROR", "V8_FROM_SOURCE"}
    environment = {name: value for name, value in os.environ.items()
                   if name not in dropped and not name.startswith(("CARGO_PROFILE_", "TARGET_"))
                   and not name.endswith(("-pc-windows-msvc", "_pc_windows_msvc", "_PC_WINDOWS_MSVC_RUSTFLAGS"))}
    include = [xwin / "crt/include", xwin / "sdk/include/ucrt", xwin / "sdk/include/um",
               xwin / "sdk/include/shared"]
    flags = " ".join(["--target=" + TARGET, "-Wno-unused-command-line-argument"]
                     + ["/imsvc" + str(path) for path in include])
    environment.update({
        "PATH": str(LLVM) + os.pathsep + environment.get("PATH", ""),
        # blake3 picks MASM (ml64) unless this names a non-cl compiler.
        "CC_x86_64_pc_windows_msvc": str(LLVM / "clang-cl"),
        "CXX_x86_64_pc_windows_msvc": str(LLVM / "clang-cl"),
        "AR_x86_64_pc_windows_msvc": str(LLVM / "llvm-lib"),
        "CFLAGS_x86_64_pc_windows_msvc": flags,
        "CXXFLAGS_x86_64_pc_windows_msvc": flags + " /EHsc",
        # Ubuntu's lld-link merges /MANIFESTINPUT in-process (libxml2); rust-lld
        # would need mt.exe.
        "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER": str(LLVM / "lld-link"),
        # Joined with the cfg(windows, msvc) flags of .cargo/config.toml. lld's
        # default UAC manifest merged with the sandbox setup's asm.v2 trustInfo
        # gives namespace-prefixed attributes that Windows rejects (14001); the
        # input manifest already requests asInvoker, so skip the default.
        "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS": "-Clink-arg=/MANIFESTUAC:NO",
        "LIB": ";".join(str(xwin / part) for part in XWIN_LIBRARIES),
        "LIBSQLITE3_FLAGS": "SQLITE_DISABLE_INTRINSIC",
        **v8,
    })
    return environment


def _version(command, cwd=None):
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              timeout=30).stdout.strip().splitlines()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def splat_digest(xwin):
    """Identity of the CRT/SDK libraries every build links (content hashes)."""
    hasher = hashlib.sha256()
    for part, names in XWIN_LIBRARIES.items():
        for name in names:
            hasher.update(("%s/%s:%s\n" % (part, name, digest(xwin / part / name))).encode())
    return hasher.hexdigest()


def build(root=ROOT, *, build_cache=None, xwin=None, jobs=SERVER_BUILD_JOBS):
    started = time.monotonic()
    root = Path(root).resolve()
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeError("Run the Windows cross build on the Linux x86_64 build server.")
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("The repository's Rust toolchain is required.")
    for tool in ("clang-cl", "lld-link", "llvm-lib"):
        if not (LLVM / tool).exists():
            raise RuntimeError("Missing " + str(LLVM / tool) + ". Install clang-20 clang-tools-20 lld-20 llvm-20.")
    cache = Path(build_cache).expanduser().resolve() if build_cache is not None else root / "work/remote-build"
    xwin = Path(xwin).expanduser().resolve() if xwin is not None else root / "work/xwin" / XWIN_SPLAT
    for part, names in XWIN_LIBRARIES.items():
        for name in names:
            if not (xwin / part / name).exists():
                raise RuntimeError("The xwin splat lacks " + part + "/" + name + ". Run the one-time setup.")
    source = json.loads((root / "patches/runtime-source.json").read_text(encoding="utf-8"))
    tree = source_tree(root / "runtime")
    if tree != source["result_tree"]:
        raise RuntimeError("runtime/ is not the recorded patch tree (" + tree + "); run restore_runtime.py first.")
    v8 = prepare_v8(root, "x86_64", build_cache=cache, target=TARGET)
    environment = cross_environment(xwin, v8)
    target_dir = cache / "windows-x86_64"
    # A stable path keeps cargo's incremental state; refreshed for every build.
    checkout = cache / "windows-source"
    windows_checkout(root / "runtime", tree, checkout)
    command = [cargo, "build", "--locked", "--release", "--target", TARGET, "--target-dir", str(target_dir),
               "-j", str(jobs)]
    for name in BINARIES:
        command += ["--bin", name]
    subprocess.run(command, cwd=checkout / "codex-rs", env=environment, check=True)
    if source_tree(root / "runtime") != tree:
        raise RuntimeError("runtime/ changed during the build; the package would not match its recorded tree.")
    output = target_dir / TARGET / "release"
    package_root = target_dir / "package"
    package_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    package = Path(tempfile.mkdtemp(prefix=stamp + "-" + source["patch_sha256"][:16] + "-", dir=package_root))
    try:
        return _package(root, package, output, source, xwin, v8, environment, cargo, jobs, started,
                        source_migrations(checkout / "codex-rs"))
    except BaseException:
        # A package without windows-build.json is useless; do not leave ~650 MB behind.
        shutil.rmtree(package, ignore_errors=True)
        raise


def _package(root, package, output, source, xwin, v8, environment, cargo, jobs, started, migrations):
    """Copy, then verify the copies that ship, and write windows-build.json last."""
    (package / "symbols").mkdir()
    files, symbols, checks = {}, {}, {}
    for name in BINARIES:
        copy = package / (name + ".exe")
        shutil.copyfile(output / copy.name, copy)
        checks[name] = verify_pe(copy, name)
        files[copy.name] = dict(sha256=digest(copy), size=copy.stat().st_size)
        pdb = output / (name.replace("-", "_") + ".pdb")
        if pdb.is_file():
            shutil.copyfile(pdb, package / "symbols" / pdb.name)
            symbols[pdb.name] = digest(pdb)
    if not contains_marker(package / "codex.exe", b"external_agents"):
        raise RuntimeError("The external-agent bridge marker was not found in codex.exe.")
    manifest = dict(
        schema=1, platform="windows", target=TARGET, build_kind="linux-cross-xwin", profile="release",
        base_commit=source["base_commit"], result_tree=source["result_tree"],
        build_source_sha256=source["patch_sha256"],
        # Built from a core.autocrlf=true checkout, like the local Windows build;
        # staging compares these migration digests with the live stores.
        line_endings="crlf", migrations=migrations,
        # rust-toolchain.toml in runtime/codex-rs selects the pinned toolchain.
        toolchain=dict(rustc=_version(["rustc", "-V"], cwd=root / "runtime/codex-rs"),
                       cargo=_version([cargo, "-V"], cwd=root / "runtime/codex-rs"),
                       clang_cl=_version([str(LLVM / "clang-cl"), "--version"]),
                       lld_link=_version([str(LLVM / "lld-link"), "--version"])),
        xwin=dict(splat=xwin.name, libraries_sha256=splat_digest(xwin)),
        v8=dict(archive_sha256=digest(Path(v8["RUSTY_V8_ARCHIVE"])),
                binding_sha256=digest(Path(v8["RUSTY_V8_SRC_BINDING_PATH"]))),
        environment=dict(LIBSQLITE3_FLAGS=environment["LIBSQLITE3_FLAGS"], jobs=jobs,
                         cpus=len(os.sched_getaffinity(0))),
        files=files, symbols=symbols, pe_checks=checks,
        built_at=datetime.now(timezone.utc).isoformat(), build_seconds=int(time.monotonic() - started))
    # Written last: a package without windows-build.json is incomplete.
    with tempfile.NamedTemporaryFile("w", dir=package, prefix=".manifest-", encoding="utf-8", delete=False) as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    os.replace(stream.name, package / "windows-build.json")
    return dict(status="built", package_directory=str(package), files=files,
                build_seconds=manifest["build_seconds"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", required=True)
    parser.add_argument("--build-cache", type=Path, help="Cache holding windows-x86_64 and v8 directories.")
    parser.add_argument("--xwin", type=Path, help="xwin splat directory (default work/xwin/" + XWIN_SPLAT + ").")
    parser.add_argument("--jobs", type=int, help="Parallel build jobs (default: 64, at most the --cpus size).")
    parser.add_argument("--cpus", default=SERVER_BUILD_CPUS, help="CPU set for the build (default 0-31,64-95).")
    args = parser.parse_args()
    try:
        jobs, lock = limit_server_build(args.cpus, args.jobs)
        print(json.dumps(build(build_cache=args.build_cache, xwin=args.xwin, jobs=jobs)))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
