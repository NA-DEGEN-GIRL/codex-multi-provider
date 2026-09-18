# New profile SSH provisioning reuses verified host runtimes

Profile 01 could authenticate and prepare hp/remote-dev, but remote-linux failed
before it obtained a remote binding. That server had about 300 MB free while the
current Linux runtime required about 475 MB. Profiles 02/03/04 already had a
working, older managed runtime installed. New-profile provisioning always uploaded
the current runtime, including when a compatible verified host runtime existed.
Remote installation errors also became the generic shim_start_failed message.

Automatic first connection now checks the remote runtime cache before uploading.
It can reuse a release registered for the same SSH alias and host identity, with
the required capabilities, a retained local manifest, verified local executable
hashes/ELF headers, and matching remote file inventory, sizes and SHA-256 hashes.
The installer rechecks the referenced runtime before accepting a small profile
definition/helper archive. Each account retains its separate remote profile and
authentication. Explicit runtime updates still select the current release.

When an upload is necessary, a read-only preflight checks available disk space
before building/transferring the archive. ENOSPC/EDQUOT during installation also
produce a specific static error. SSH audits include the preparation stage and
host alias without printing raw remote output or credentials. This does not
depend on llm-usage and does not delete remote files to make room.

Validation:
- 76 focused Python tests, including a new account with effectively no free
  runtime space, verified cache reuse with no runtime archive members, independent
  profiles, explicit update refusal, cache tampering, and error redaction.
- One POSIX-only inventory locking test is skipped on Windows.
- Live profile 01 preparation on remote-linux completed in about 9 seconds using
  the same pinned runtime as the existing profiles; the native version route
  returned 0.153.4. Its already-running desktop then automatically completed
  native probe, version, start and proxy authentication with auth-state=ready.
- Evidence: artifacts/results/new-profile-ssh-43/live-repair.json.

The server still has little free disk space. Reuse solves adding an account;
installing a genuinely new runtime will still require enough free space.
