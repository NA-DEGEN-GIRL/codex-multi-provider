# Windows skills from SSH projects (revision 70)

## User behavior

In **Settings and management → Common personal skills**, enable **SSH use** for
`3d-assets` or `game-audio`. The workspace app prepares the connection in the
background for its registered SSH hosts. It does not delay profile launch or
change the host on which the coding task runs.

The remote task discovers the skill normally. Its wrapper delegates supported
commands to the existing Windows runtime. The agent uploads required inputs,
uses the returned Windows paths in requests, and fetches finished artifacts into
the remote project. Input JSON and paths are not rewritten automatically.
The skill reference documents explain these steps to the agent; the user does
not need to install the Windows model environments on Linux.

Both Windows and the workspace app must remain running. The settings panel
reports connection failures or a conflicting remote skill. Enabling the common
personal skill and enabling its SSH use are separate settings. Disabling either
revokes new worker commands and removes only the discovery links owned by this
bridge. An already running generation is not killed by toggling a setting.

## Components and data flow

1. `scripts/manager_core/skill_bridge.py` inventories the shared personal skills
   and registers the two supported, existing Windows runtime environments. A
   background worker reconciles settings and skill documents every 30 seconds.
2. `skill_bridge_server.py` starts one authenticated loopback HTTP worker. Each
   remote alias has its own capability and job/upload scope. Profiles using the
   same alias share a single dedicated reverse SSH tunnel.
3. `scripts/remote_helpers/skill_bridge_install.py` installs a standard-library
   Python client, a private connection descriptor and a lightweight,
   content-addressed projection of skill instructions. It preserves the source
   repository layout so relative documentation links resolve. No model weights,
   Python environment or provider credentials are copied to Linux.
4. The installer publishes owned symlinks under `~/.agents/skills/`. An existing
   native skill in that directory or `~/.codex/skills/` is preserved, including
   dangling links. A conflict is reported instead of replacing another install.
5. The wrapper supports explicit local/Windows execution. A separately registered
   native Linux runtime takes priority in auto mode; without that registration,
   the private descriptor preserves the original Windows bridge behavior.
   Native environments live outside the managed projection. See the skill's
   `references/execution-setup.md` for installation and job-host pinning.
6. The portable client supports `catalog`, `read`, `upload`, `run`, `job` and
   `fetch`. See the skill's `references/windows-bridge.md` and English companion
   for the exact commands and the maintainer contract.

This implementation uses the task's existing shell tool and a CLI client; it
does not register an MCP server or modify remote app-server configuration.

## Requests, artifacts and recovery

The client records a UUID and command before submission. Reusing that UUID with
the same payload returns the existing bridge job; a different payload is
rejected. A lost response must be reconciled through that record, not retried
with a new UUID. Queued jobs are durable. A job found running after an uncertain
worker shutdown is marked interrupted, and is not automatically executed again.

Bridge jobs and native runtime jobs have different IDs. In particular, a native
`--async` command may complete its bridge job while its generation continues.
Use the native job and provider receipts to inspect or recover that generation.
The bridge serializes command invocations, while detached work uses the runtime's
own resource locks and recovery rules. It does not introduce a universal GPU
scheduler or change provider limits, paid retry rules or review requirements.

Uploads and downloads are chunked. Fetch verifies SHA-256 and publishes the
destination atomically without overwriting an existing project file. Documentation
reads are limited to registered Markdown paths; artifact reads are limited to
the runtime's `.assets`, `.work`, and the caller's staged inputs. Symlinks and
hard links escaping those file contracts are rejected. Secrets, connection
descriptors and private logs must not be uploaded as skill inputs.

The SSH account is trusted to invoke the enabled runtime commands. This is an
execution bridge for that account, not a sandbox for untrusted skill programs.
Provider keys remain on the Windows runtime. The transport capability itself
is stored privately on the SSH account and must not appear in logs or commits.

## Integration points and extension

- Python manager: `skills.bridge.set`, common-skill changes and service lifecycle.
- Rust service: RPC allowlist entry; bridge work runs outside the UI launch path.
- WPF: per-skill SSH toggle and connection status in `PersonalSkillsWindow`.
- To support another skill, add its explicit runtime registration and allowed
  commands, a routing wrapper, maintained instructions, and end-to-end tests.
  A new CLI command in an existing runtime also needs an allowlist update here.
- Skill repositories own their routing tests and bilingual bridge references.
  Keep the `client`, `enabled_skills` and versioned connection contract aligned.

## Validation and limits

Automated checks cover real HTTP command execution and deduplication, access
scope, path escape rejection, artifact integrity, client failure recovery,
Linux installation conflicts, lifecycle revocation and the synthetic settings
UI. POSIX-only installer and file-mode checks run under Linux as well as the
Windows-compatible client and worker suites.

An opt-in live test is available as:

```text
python scripts/test_skill_bridge_ssh.py --host <registered-ssh-alias>
```

It creates an isolated remote temporary directory and leaves a private receipt
under ignored `work/`. It enables the two supported skills. Only the named host
is contacted by the fixture; no live profile is closed or navigated.

On 2026-09-20 the live test verified native Linux Codex skill discovery, both
remote wrappers calling Windows `doctor`, a canonical documentation read, WAV
upload and byte-identical return, and actual Windows audio import with the
processed WAV and manifest fetched back to the remote directory. No paid API,
model inference or listening review was performed. Generation quality and long
running generation recovery have not been validated by this transport smoke.

## Profile reopen fix included in this revision

Manual profile reopen previously requested a normal window close but did not
complete the verified idle exit used by automatic updates. When Electron kept
the process resident, the restart lease remained `native_close_requested` and
subsequent attempts could remain stuck waiting for maintenance locks to release.

Manual reopen now uses the same idle-exit completion. Recovery also finishes an
already requested close when the recorded generation, process creation times,
held maintenance gate, released writers, closed remote participants and empty
runtime activity still match. Identity and idle evidence are checked again just
before stopping the owned process. Another profile's active work is not a reason
to stop it or release its gate. Missing proof remains a reported failure.

Regression tests cover this recovery and profile isolation. The live profile
that reported the issue was inspected but was not restarted during development.
