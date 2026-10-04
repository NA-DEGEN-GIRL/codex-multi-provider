"""Install an explicitly selected, signed Claude CLI into the manager's tools.

Run on the Linux SSH host. This does not modify PATH, log in, start a model,
or replace a system CLI. Release verification follows Anthropic's setup guide.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import urllib.request

FINGERPRINT = '31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE'
RELEASES = 'https://downloads.claude.ai/claude-code-releases/'
PUBLIC_KEY = 'https://downloads.claude.ai/keys/claude-code.asc'


def download(url, path, limit):
    total = 0
    with urllib.request.urlopen(url, timeout=120) as response, path.open('xb') as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise RuntimeError('Release download exceeded its size limit.')
            output.write(chunk)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('An exact CLI version is required.')
    architectures = {'x86_64': 'linux-x64', 'aarch64': 'linux-arm64'}
    target = architectures.get(platform.machine())
    if platform.system() != 'Linux' or target is None:
        raise RuntimeError('Run this installer on Linux x86_64 or aarch64.')
    base = Path.home() / '.local/share/codex-control-center/tools/claude'
    if any(path.is_symlink() for path in (base, *base.parents)):
        raise RuntimeError('The managed tools directory must not be a symlink.')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = base / version / 'claude'
    if destination.is_symlink() or destination.parent.is_symlink():
        raise RuntimeError('The version directory must not redirect elsewhere.')
    with tempfile.TemporaryDirectory(prefix='.install-', dir=base) as temporary:
        scratch = Path(temporary)
        key, manifest, signature = (scratch / name for name in ('release.asc', 'manifest.json', 'manifest.json.sig'))
        prefix = RELEASES + version + '/'
        download(PUBLIC_KEY, key, 65536)
        download(prefix + 'manifest.json', manifest, 8 * 1024 * 1024)
        download(prefix + 'manifest.json.sig', signature, 65536)
        keyring = scratch / 'gnupg'
        keyring.mkdir(mode=0o700)
        gpg = ['gpg', '--batch', '--homedir', str(keyring)]
        subprocess.run([*gpg, '--import', str(key)], check=True, capture_output=True)
        fingerprints = subprocess.run([*gpg, '--with-colons', '--fingerprint'],
                                      check=True, capture_output=True, text=True).stdout
        if FINGERPRINT not in [line.split(':')[9] for line in fingerprints.splitlines() if line.startswith('fpr:')]:
            raise RuntimeError('The release signing key fingerprint did not match.')
        verified = subprocess.run([*gpg, '--status-fd', '1', '--verify', str(signature), str(manifest)],
                                  check=True, capture_output=True, text=True).stdout
        if not any(line.startswith('[GNUPG:] VALIDSIG ') and FINGERPRINT in line.split()
                   for line in verified.splitlines()):
            raise RuntimeError('The release manifest signature could not be verified.')
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        checksum = metadata['platforms'][target]['checksum']
        if not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise RuntimeError('The signed checksum is invalid.')
        if destination.exists():
            if not destination.is_file() or digest(destination) != checksum:
                raise RuntimeError('An existing version differs; it was left untouched.')
        else:
            binary = scratch / 'claude'
            download(prefix + target + '/claude', binary, 1024 * 1024 * 1024)
            if digest(binary) != checksum:
                raise RuntimeError('The binary checksum did not match the signed manifest.')
            binary.chmod(0o700)
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            os.replace(binary, destination)
        reported = subprocess.run([str(destination), '--version'], check=True,
                                  capture_output=True, text=True, timeout=30).stdout.strip()
        if not reported.startswith(version + ' '):
            raise RuntimeError('The installed CLI reported an unexpected version.')
        link = base / 'claude'
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or not link.resolve().is_relative_to(base):
                raise RuntimeError('The existing launcher is not managed; it was left untouched.')
        temporary_link = scratch / 'launcher'
        temporary_link.symlink_to(destination)
        os.replace(temporary_link, link)
        proof = dict(version=version, platform=target, executable=str(destination), sha256=checksum,
                     signing_fingerprint=FINGERPRINT, signature_verified=True,
                     account_login_performed=False, model_calls=0)
        (destination.parent / 'installation.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
        return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    print(json.dumps(install(parser.parse_args().version)))
