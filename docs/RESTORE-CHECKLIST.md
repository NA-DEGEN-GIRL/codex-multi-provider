# RESTORE-CHECKLIST - Codex workspace manager

Acceptance checklist for restoring the manager on the new Windows machine and
pointing it at the new bare-metal Ubuntu GPU host over SSH.

Written at baseline HEAD `6cf63da`, with uncommitted revision-85 source and documentation.
No target restoration or checklist acceptance was executed while writing it. Every target item that needs network access,
cost, a process/service start, or any data mutation is marked **[NOT RUN]**.
Secret values never belong in this file.

Update 2026-09-22: revision 86 was built as `20260922-081659-020` and selected for next launch.
Real UAC/elevated synthetic-service checks passed; production full-exit/admin-restart, Hyper-V access
and new-host restoration are still unverified. See [the revision-86 record](design/administrator-transition-86.md).
The September 21 baseline below is historical; collect the current process paths and release pointer again.

Verified evidence available before any restore work (code health, not restore):
`docs/design/ssh-sidebar-cache-84.md`; `tests/test_manager_app_catalog_cache.py`
19 tests OK and `tests/test_manager_local_first_remote.py` 29 tests OK on this HEAD
(48 total); `work/privacy-84-report.json` = `[]`; release manifest
`artifacts/manager/releases/20260921-063521-751/runtime-manifest.json`.
A restore of this repository onto the new Windows + Ubuntu pair has **not** been
executed or tested. A full-restore acceptance is explicitly pending.

## Phase 0 - Paths, preconditions, safety (no side effects)

Absolute paths expected by this checklist:

| Role | Path |
|---|---|
| Source checkout (old Windows) | `<repo>` (the local checkout root) |
| Target checkout (new Windows) | `<repo>` (same path preferred; otherwise rewrite absolute paths) |
| Windows local data | `work\control-center\` |
| Windows canonical Codex home | `%USERPROFILE%\.codex` on the current machine; confirm the target machine's actual user path before rewriting anything |
| Ubuntu stock CLI home | `/home/<ssh-user>/.codex` for the verified `<ssh-user>` account on `<ssh-host>`; other users per the actual host inventory |
| Ubuntu manager service root | `/home/<ssh-user>/.local/share/codex-control-center/profiles/<profile-id>/` for the verified user; confirm other users instead of guessing |

- [ ] Windows x64 with .NET 10 SDK, Python 3.13 (`C:\Python313\python.exe`), Rust toolchain, Git.
- [ ] `git rev-parse HEAD` matches the agreed migration snapshot (baseline `6cf63da` plus the saved working
      tree, or a later commit containing it); do not discard revision 85 by checking out only the baseline.
- [ ] `git status --porcelain` shows only expected entries: the pre-existing dirty
      `tests/test_manager_local_first_remote.py`, the documentation files, and any in-flight
      revision-85 files. Do not revert or fold the pre-existing file into unrelated commits.
- [ ] Operator's own SSH key/agent is available; no keys, tokens or passwords are tracked by Git.
      Private credentials in the ignored `work/` tree belong only in the protected backup.
- [ ] Record the pre-restore live baseline: at the time of writing the source machine ran exactly two
      manager processes - one shell and one Rust service - both from
      `artifacts/manager/releases/20260921-063521-751` (revision 84). On 2026-09-21 22:51 KST,
      revision 85 was packaged separately as `20260921-135127-810` and selected for next launch.
      PIDs change on restart; record the executable paths as well as the selected release pointer.
- [ ] The stock Linux CLI (`/home/<ssh-user>/.codex`) and the manager's remote service root
      (`/home/<ssh-user>/.local/share/codex-control-center/`) are treated as separate stores.
- [ ] Restore the old `<ssh-host>` VM archive **files** onto the new bare-metal Ubuntu host, per user
      home; booting the old VM itself is optional and not part of acceptance.
- [ ] Leave historical logs untouched (`work/control-center/logs/*`, `ssh-routing.jsonl`, `work/*.log`):
      they are evidence and keep their original recorded paths.

## Phase 1 - Install / build acceptance (build only, local mutation)

- [ ] **Build + self test** **[NOT RUN - run manually]**:
      `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-manager.ps1 -SelfTest`
      Expected: success and a new `artifacts/manager/releases/<timestamp>/`.
- [ ] **Release manifest check** **[NOT RUN]**: `runtime-manifest.json` has `version 1`,
      the expected `shell_compatibility.revision`, a `service_protocol` matching the supervisor,
      and per-file SHA-256 entries (including `scripts/manager_core/app_catalog_cache.py`).
- [ ] **Active pointer check** **[NOT RUN]**: `artifacts/manager/current.json` points at that release
      and the `shell`, `supervisor`, `runtime_proxy`, `ssh_proxy` paths exist.
- [ ] **Shell build check** **[NOT RUN]**: `dotnet build manager/Shell/Codex.ControlCenter.Shell.csproj --no-restore`
      completes with 0 warnings and 0 errors.
- [ ] Do **not** launch from this phase. Launch happens only after the gates in Phases 2-4 pass.

## Phase 2 - BLOCKED gate: live state must not be reused

The restore **cannot** proceed to a live launch by copying these verbatim. They are
historical evidence; the **restored copy on the target** must rebuild them while the
preserved full archive stays untouched. There is no implemented cross-host remap/reset,
so a normal `prepare` is not a repair. This gate is a policy/verification requirement,
not a tested automated tool:

| Restored item | Required action on target |
|---|---|
| `state.json` live fields `process_id`, `process_created`, `window_handle`, `status`, `started_at`, `login_observed_at` | the target copy rebuilds these; never trust the copied values |
| `state.json` maps `ssh_maintenance`, `profile_restarts`, `remote_updates`, `profile_maintenance` | retire or rebuild by the new host's own lifecycle |
| `work\control-center\instances\<uuid>\runtime-state.json` and `runtime-admin\<generation>.json` | do not reuse (admin key is DPAPI-bound); regenerate |
| `work\control-center\updates\maintenance\*.json`, `restarts\*.lock`, `record-signals\*` | retire or rebuild |
| Remote `native-instance.json`, `instance.lock`, `native-start.lock`, `native-shutdown.json`, `native-runtime.log` in the profile dir | recreated by a fresh manager start on the new host; the archived originals stay untouched |
| Supervisor named pipe (`ManagerProtocol.PipeName(root)`) token/generation | new process, new authority; never trust a copied PID or JSON record |

- [ ] **[NOT RUN]** Confirm the first target launch is gated on the resets above:
      the manager must not adopt copied PIDs, locks, instance records, runtime-admin
      endpoints or maintenance journals as live proof.
- [ ] **[NOT RUN]** Re-prepare and re-verify each SSH profile against the new Ubuntu host identity
      (see Phase 4). The gate stays closed until every selected profile is rebound.

## Phase 3 - Data / schema acceptance (read-only unless stated)

- [ ] `work/control-center/state.json` parses, `version: 1`, with the expected `profiles`, `sources`,
      `ssh_inventory`, `ssh_maintenance`, `remote_updates`, `profile_restarts`.
- [ ] Do **not** treat `work/control-center/state.lock` presence/ownership as proof of a held or free
      lock: the file's existence is not lock state. Assert lock state only via the manager's own checks.
- [ ] Per profile: `profiles\<profile-id>\ssh-bindings.json` and `managed-sources.json` exist;
      `ssh-routing.jsonl` is the audit file (no secrets inside).
- [ ] Native catalog tables exist in each profile's `codex\sqlite\codex*.db`
      (`local_thread_catalog`, `_sync_state`, `_hosts`, `_metadata`, `_scan_checkpoints`, `_scan_entries`).
- [ ] `.manager-sidebar-cache.json` keeps the flat local signature per database plus the reserved
      `__ssh_canonical__` map for managed hosts; `.manager-cache-backups\` holds the pre-reset backups.
- [ ] Task notes match the actual store and are preserved with their media and grouping: one JSON
      document per task in `work\control-center\notes\<sha256>.json` with `version`,
      `task:{host_id,thread_id}`, `group_id`, `notes` (text, images, status); drafts in `note-drafts\`;
      groups via `group_id` / `note-groups\`; logical-link map `note-aliases.json`. Note bodies are not
      runtime-DB rows.
- [ ] Runtime DB schema versions: consult the private migration record kept outside the published
      source (it owns schema/migration detail; do not duplicate or assume its claims here).
- [ ] **Read-only SQL evidence** **[NOT RUN]**: for each managed SSH `host_id`
      (`remote-ssh-discovered:<alias>`), the stale projection ids are absent and the canonical id
      is present, checked without writing.

## Phase 4 - Identity, DPAPI and mapping (logical ids stay stable)

- [ ] **[NOT RUN]** Re-acquire credentials for the **new Windows user**: provider keys are
      DPAPI `CurrentUser`-scoped (`profiles\deepseek.dpapi` for the model lab,
      `work\control-center\credentials\<provider-id>.dpapi` for registered providers, registry at
      `work\control-center\providers.json`), so the restored blobs are unusable. Re-enter keys through
      settings and never print or copy secret values.
- [ ] **[NOT RUN]** Re-verify the SSH binding after the physical host change: `host_identity`
      (sha256 of machine-id/uid/home) is a guard, so a changed machine must be re-prepared and
      re-verified instead of silently reused. This does not by itself change the logical ids.
- [ ] **[NOT RUN]** Preserve the logical ids whenever the alias and remote home are unchanged:
      binding id `ssh:<alias>`, native host id `remote-ssh-discovered:<alias>`, thread UUIDs,
      note `task` keys, catalog `host_id` rows. Rebind only when a logical id or path actually
      changes (alias rename, different Linux user/home, `migrationIdentity = <host id>:<remote CODEX_HOME>`,
      moved project root), and then migrate binding rows, note keys, project-id maps and
      `thread-project-assignments` together - never by blind UUID replacement.
      Verify a sample note, fork and task before declaring the mapping whole.
- [ ] **[NOT RUN]** Re-check the supervisor/pipe authority path: `ManagerClient` must accept only an
      actual named-pipe server OS token match (`manager/Shared/WindowsExecutionIdentity.cs`), never a
      trusted JSON or recorded PID; a privilege mismatch must refuse without retiring or stopping
      existing work. The Rust service must also reject client token mismatches before its first RPC.
      Revision 85 passed 30 Rust tests and 132 + 19 .NET checks and was packaged for next launch;
      actual UAC approval/cancel and live administrator-mode verification remain pending.

## Phase 5 - First launch after the gates

- [ ] **Launch** **[NOT RUN - starts processes]** only after Phases 2-4 pass:
      `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-manager.ps1 -SkipBuild -Launch`
      (or `Open-Control-Center.cmd`). Expected: the workspace shell appears and connects to the service.
- [ ] **Clean termination is a UI path, not a kill** **[NOT RUN]**: acceptance uses the shell's own
      "complete shutdown" action. `Stop-All-Codex.cmd` / `Stop-All-Codex.ps1` force-stop processes and
      interrupt in-flight work; they are not clean-termination evidence and must not be used as a
      fallback proof.

## Phase 6 - Selected local + SSH path, new task

- [ ] **Local profile** **[NOT RUN]**: open a local-source profile, create a new task, confirm it
      appears under the expected project and in `work/control-center/logs`.
- [ ] **SSH profile** **[NOT RUN]**: open the profile bound to the new Ubuntu host; the binding must
      move from `prepared_not_connected` to connected and the sidebar must repopulate from a fresh
      `thread/list`. Confirm the canonical task id and that stale projection ids stay absent.
- [ ] **New task over SSH** **[NOT RUN]**: create one task, confirm title and cwd-based project
      grouping, and that reopening reconnects to the same task.
- [ ] **Failure behaviour** **[NOT RUN]**: with the host unreachable the UI defers instead of
      fabricating exit proof; no listener is stopped or restarted by these checks.

## Phase 7 - Preservation matrix (verify, do not mutate)

| Item | Actual store | Check |
|---|---|---|
| Attachments | profile `codex\` home + remote managed home | open a task with an attachment **[NOT RUN]** |
| Notes / drafts | `work\control-center\notes\<sha256>.json`, `note-drafts\` | counts plus a readable sample note **[NOT RUN]** |
| Forks / handoffs | `work\control-center\handoffs`, `handoff-live`, task metadata | existing fork resolves to its parent **[NOT RUN]** |
| Skills | profile `codex\skills`, `work\control-center\skill-bridge`, `shared-plugins` | listed in the UI **[NOT RUN]** |
| Plugins | profile `codex\plugins`, `work\control-center\shared-plugins`, `plugin-sync.json` | listed and loadable **[NOT RUN]** |
| API models | `config\deepseek-catalog.json`, `work\control-center\providers.json`, `work\control-center\credentials\<provider-id>.dpapi` | model list renders; key presence checked without printing **[NOT RUN]** |
| Record ids | `thread-project-assignments`, `local_thread_catalog`, remote rollouts | id lookup resolves; ids unchanged by the epoch **[NOT RUN]** |

## Phase 8 - Reboot / mount / startup, kept separate

- [ ] **Mount/restore the archived data** **[NOT RUN - mutates disks]**: restore the old `<ssh-host>`
      archive files into the new bare-metal Ubuntu user homes; booting the old VM is optional.
- [ ] **Boot the new Ubuntu host** **[NOT RUN]**: the Codex workspace app is CPU-side and does not
      require GPU drivers before use; GPU driver/rendering work belongs to a separate project and is
      not a Codex restore prerequisite.
- [ ] **SSH reachability** **[NOT RUN]**: connect once with the operator's own credentials; a changed
      host/user identity must block the binding rather than silently rebind.
- [ ] **Start the Windows manager** **[NOT RUN - starts processes]**: launch after the previous steps.
- [ ] **Reboot repeat** **[NOT RUN]**: repeat boot -> SSH -> launch once and record which steps ran.

## Phase 9 - Explicitly not run in this document

Every item below is **[NOT RUN]** here and must be marked when someone executes it:

* restore-target network calls (SSH connects, remote updates, model calls) and anything that costs money.
  Distinguish from source inventory: read-only SSH probes of the **source** hosts were executed earlier
  to inventory records; no restore-target check has been run;
* API/provider or account changes, credential writes, DPAPI re-encryption;
* database writes (catalog rebuild, state edits, SQL repairs);
* service installs, elevation, deploy or publish steps;
* deletion of files, profiles, records, caches, or archived Hyper-V images;
* remote stop/start/kill of any listener or daemon.

## Phase 10 - Evidence to leave behind

- [ ] New release path and `runtime-manifest.json` hashes.
- [ ] Focused test output for `test_manager_app_catalog_cache.py` and
      `test_manager_local_first_remote.py` (48 tests OK on `6cf63da` when this was written).
- [ ] Read-only SQL digest of the managed host catalog before/after the first cold launch.
- [ ] The list of checklist items executed versus skipped, with timestamps.
- [ ] Any deviation (host identity change, path rewrite, key re-entry) recorded in `docs/HANDOFF.md`.
      Do not state that a restore was tested unless Phases 1-7 actually ran.
