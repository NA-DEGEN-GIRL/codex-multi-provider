# HANDOFF - Codex workspace manager

Update 2026-10-02: revision 106 adds one-shot navigation recovery in the WPF shell.
See [observations, implementation and application boundary](design/shortcut-navigation-recovery-106.md).
Candidate `20261001-171017-309` passed all ten staged UI groups and the separate
29-check shortcut fixture. A direct route to an existing live desktop repaired
the reported failure without restarting it; the user confirmed recovery. The new
automatic watcher has fixture validation; its live use awaits opening the new shell.
Only the manager release pointer was advanced after verifying 217 frozen-file hashes;
the previous pointer is preserved in `work/manager-current-before-106.json`.
Existing revision 99 processes remain running. Native Windows and default Linux
artifacts remain unchanged; revision 102–105 backend activation is still deferred.
Closing only the manager window and reopening loads the new shell against the
existing service while profiles are running. Do not full-exit or restart accounts
to apply this UI-only recovery. No commit or push is requested.

Update 2026-10-02: revision 105 removes leftover unconditional Claude-local-only
routing from ControlCenter, while requiring supported artifacts and the current
tracked SSH generation. See [cause, evidence and activation steps](design/claude-ssh-enrollment-105.md).
Manager candidate `20261001-162653-598` passed ten UI groups and all 217 frozen-file
hashes; focused Python tests passed 71 cases. Three enabled Linux hosts have the
selected Claude profile's definitions prepared and verified by actual binary
version execution and settings fingerprints. Two managed Claude CLI installations
were signature-verified; the third already existed. No paid model turn was sent.
The live manager/service is still revision 99; release pointers and live profile
SSH bindings are unchanged. The new nested Linux candidate is intentionally not
the default artifact. Service replacement awaits the user's other-work timing;
do not report the live Claude desktop as SSH-enabled yet. Preserve the preceding
revision 98–104 changes. No commit or push was made for this revision.

Update 2026-10-01: revision 104 adds default-hidden profile emails with an explicit
show/hide button. See [privacy boundaries and validation](design/profile-email-visibility-104.md).
Candidate `20261001-113150-043` is staged, not activated; all 217 frozen bundle
files match their hashes. The earlier revision 104 candidate passed all ten staged
UI groups; the final candidate additionally passed the email/Claude UI fixture after
the read-only command gate and old-service error handling were finalized. Python
email/dispatch (9), Claude profiles (16), and CLI adapter (42) tests passed. All seven
current Codex profiles had readable local email metadata; no address was printed,
persisted or included here. Live Claude status was not queried. The new service
must support `profile.email`; an old running service cannot provide this new UI
operation. Keep revision 103's separate native activation requirements. No running
profile, service, account login or release pointer was changed for revision 104.

Update 2026-10-01: revision 103 extends saved execution presets to SSH and adds
connection-bound account borrowing for remote Codex/Claude delegation. See
[source contracts, setup and verification status](design/ssh-execution-presets-103.md).
This entry supersedes revision 102's SSH limitation; it does not imply that a
production runtime or running profile was updated. Exact build/activation evidence
is recorded in the revision 103 document. Preserve unrelated revision 98–102 changes.
Revision 103's Windows native build, cross-harness tests, storage gates, generated
schemas and Bazel lock check supersede the older native-build blockers described
in the historical revision 102 entry below. The final manager candidate
`20261001-080642-677` passes all ten staged UI self-tests, including real service
and deep-path notes storage, plus the separate 13-check preset editor test. The
initial executable policy block does not describe this final candidate's service
execution. Separate manager-service unit tests remain blocked before execution by
Windows Application Control rejecting a dependency build script (OS 4551).
Activation remains unperformed; do not weaken policy or report the candidate as
installed. A discovered baseline long-path atomic-save defect was corrected in
`manager/service/src/windows.rs`; the layout fixture now supplies real current-task
selection state without weakening retention assertions. The cumulative patch restores to tree
`1b201dcd91d9cedf8515ac8427f7064292d04823`.
New source entry points include `execution_preset_render.py`,
`execution_preset_remote_service.py`, `execution_preset_reconnect.py`,
`execution_preset_replay.py`, `execution_preset_auth.py`,
`remote_helpers/execution_presets.py`, and
`remote_helpers/claude_remote.py`. The Linux CLI installer verifies an official
signed manifest and installs only a versioned manager tool, without logging in.

Update 2026-10-01: revision 102 implements Windows task execution presets and
Codex/Claude cross-harness subagent delegation, but is **not activated**. See
[design, validation and remaining work](design/execution-presets-cross-agents-102.md).
Manager candidate `20260930-172530-234` builds successfully and passes the hidden
preset editor fixture. Scoped Rust Clippy checking of core, protocol and app-server
protocol (including test code) passed, as did the separate protocol build. The
production native build remains blocked: Windows Application Control rejects an
`rmcp_macros` build DLL with OS error 4551. Bazel's Windows JNI DLL is independently
blocked (exit 37). Native behavioral tests and schema regeneration remain unverified.
Do not disable policy or substitute old binaries as feature validation. Use an
approved build environment, complete the native/schema/Bazel checks and offline
cross-harness tests, then the existing same-binary shared/canonical storage gates.
The current manager/native release pointers remain on their earlier validated
releases; production profiles and tasks were not restarted. Existing uncommitted
revision 98–101 work remains intact; no commit or push was made for revision 102.
New source entry points: `execution_presets.py`, `execution_preset_service.py`,
`claude_delegation.py`, `claude_delegation_mcp.py`, Shell `ExecutionPresets.cs`,
native core `config/execution_presets.rs`, `thread_manager/preset_auth.rs`, and
`tasks/claude_code/delegation.rs`. Source and registry tests explicitly cover
immutable version history and account authentication separation. SSH presets are
unsupported and fail closed; host-specific authentication/registry work is separate.
The cumulative source patch restores to tree
`90ffc4c5082cec81d1e5a2e4eaf045a000dfd844`; a new detached verification worktree
matched the manifest without changing the working runtime index. Regenerate both
stable and experimental API fixture sets before treating the patch as release-ready.

Update 2026-10-01: revision 101 fixes Claude per-task model selection and retains
the same CLI session across model/effort changes within the same identity boundary.
See [implementation and validation](design/claude-model-switching-101.md).
The [persistent worktree domain assessment](design/persistent-worktree-domains.md)
is a proposal grounded in current code; domain dispatch, execution admission and
automatic integration are not implemented. Preserve existing project grouping.
No existing worktree, live task, CLI installation or production process is changed
by the assessment. Earlier revision 98–100 uncommitted changes remain present.
Manager `20260930-154746-301` is published for the next launch, with 203 frozen
bundle files hash-checked. Validation passed 182 Python checks, 10 offline native
integration tests and 29 WPF checks. Native runtime is unchanged. No live model
calls or profile/service restarts were performed. Reopen the Claude profile after
applying the manager update to receive the new multi-model catalog and proxy.

Update 2026-09-29: revision 100 adds explicit local Responses-provider registration,
optional authentication, Windows-only/default versus explicit SSH execution scope,
and sourced presets for three Flash checkpoints. Full preset context is retained;
Qwen logical max maps to its actual xhigh enum. See
[revision 100](design/local-model-profiles-100.md) and its linked specification record.
Actual endpoint/model IDs, engine/version and Responses/thinking support are still
awaiting the local model operator. Do not claim live model integration is verified
or treat a Chat Completions endpoint as Responses. Desktop fallback notices are now
shown once in settings; the newer installed desktop remains unsupported, as below.
Release `20260929-043810-921` is published for the next manager launch. Its 203
bundle files were hash-checked; 95 Python checks, 29 WPF checks and 2 offline native
Responses integration tests passed. The native executable is unchanged from
revision 99. Running profiles and services were not restarted.

Update 2026-09-29: revision 99 adds native Claude context/auto-compaction defaults,
the genuine UltraCode CLI option, account-scoped five-hour/weekly usage display,
and an explicit remote project membership repair queue. Implementation/validation:
[revision 99](design/claude-defaults-usage-99.md). Existing revision 98 dirty work is
preserved. Pending membership files apply only during safe profile preparation;
their presence does not mean an already-running desktop has adopted the move.
Shortcut navigation now attaches before unrelated state refresh, avoids transient
viewport resizing and discarded legacy manifests, and validates the captured target
profile before opening it. CLI OAuth login alone may leave first-run onboarding
incomplete; the explicit setup action opens the official CLI, while quota probing
reports unknown rather than inventing a percentage.
Revision 99 Release `20260928-181339-023` and native `20260928-181038-780945`
passed the scoped validation recorded in the revision document. Real subscription
percentages remain unverified until CLI onboarding. Installed desktop 26.924 has
archive integrity enabled and remains unsupported by the customization path;
do not disable that protection. An audited immutable upgrade of the existing
26.917 managed cache provides the picker fixes and passes the current fallback
identity checks. See `desktop_managed_upgrade.py` and `work/desktop-upgrade-99.json`.

Update 2026-09-28: revision 98 is implemented from baseline `f554498`.
The change adds local Claude Code OAuth profiles, shared task history and skills,
isolated account-return sessions, and portable compaction checkpoints. Source
changes are not committed. Existing private migration notes and design drafts
must not be staged accidentally or overwritten. The older snapshots below are
historical; inspect the live process and release pointers before deployment.

Start with [the revision 98 implementation and validation record](design/claude-code-agent-profiles-98.md).
Python/C# manager integration passes. All five native executables build; the final
Claude bridge suite passes 8 tests, and shared-editing/canonical-storage proofs
pass 15/16 checks. Schema generation and 64 focused Rust checks are complete.
The scoped final `just fix` completed with exit code 0. Strict Clippy previously
failed at two unchanged revision-97 production sites; the final fix run retains
those diagnostics and an existing test's `needless_collect` diagnostic as warnings.
This is not a clean strict-Clippy pass. Only test expressions were auto-corrected.
No real Claude login/model request or active-profile restart has been performed.
The installed official CLI owns credentials outside the repository; never read
or copy its OAuth token into an HTTP provider. SSH Claude execution and translating
Codex-specific tools into Claude tools are outside the implemented local scope.

Local proof reports: `work/claude-98-native-e2e.json`,
`work/claude-98-python-regression.json`, and the same-binary headless reports
under `artifacts/results`. These ignored local reports are supporting evidence,
not credentials or a requirement for understanding the design. The final cumulative
patch was restored in a fresh worktree and matched tree
`edbbae73532603534316520fbc5702d528d5dd48`; the original runtime index was preserved.
`build-manager.ps1 -StageOnly` creates an immutable candidate without replacing
the active manager. A real user login and a small real-model smoke test remain separate.

Selected for next launch on 2026-09-28: manager `20260928-115405-935` (revision 98)
and native runtime `20260928-123915-e04fc1`. Both release pointers were published
after same-binary validation; no running profile or service was restarted. Finish
active work, use **Full exit**, and reopen before creating a Claude profile.
Closing only the manager window can leave the older service and runtime alive.
Next validation: official OAuth login, a small Claude → GPT → Claude exchange,
optional A → B → A with two accounts, and natural compaction during ordinary use.
See the implementation record for extra summary usage and unsupported tools/SSH.

## Earlier operational snapshots

Update 2026-09-23: revision 89 adds an explicit per-profile remote configuration
apply/drain command and remote stop verification to full exit on the new backend.
The root cause was a local full exit leaving detached SSH listeners alive;
shared-record listeners do not expose the older exclusive idle/shutdown proof.
Use the profile menu's SSH apply action only after loading the new backend. A new
shell on an old backend explains that its first full exit remains local-only.
Current assistant turns drain; independent background jobs/queued input are not
covered by an atomic all-client guarantee. Other profiles and stock terminal
daemons are not targeted. See [remote lifecycle](design/profile-remote-drain-89.md).

A separate local task lost its full-access selection after an elevated relaunch.
Revision 89 remembers future runtime-confirmed built-in full-access selections per profile/thread and
fills only omitted resume choices; explicit restrictions take priority. No
historical permission choice is inferred, and Windows elevation does not grant
SSH sudo or change Codex permissions. Select the wanted permission once after the
updated profile proxy starts. See [permission persistence](design/permission-selection-persistence-89.md).

SSH deferred warnings now retire only after every pending host has a verified
new start at the desired revision. Prepare-only, stop-only, partial application,
a changed generation/policy, and a newer restart notice preserve the warning.
Tests signalled only synthetic Linux children; no real profile runtime was stopped,
no live credentials/config/history/ACL was edited, and no stock daemon was updated.
The earlier revision 85-88 changes and all existing local work remain uncommitted.
Build `20260923-073019-732` (revision 89, IPC 27) is selected for next launch;
all 178 frozen bundle file hashes were checked. Windows release and 30 Rust
service tests passed. Full Python discovery ran 1,361 tests with 29 skips and no
failures; after the final deployed-schema compatibility correction, the 139-test
permission/proxy/regression batch passed. Linux drain fixtures passed 34 checks
on temporary child processes. Deployed app-server JSON schema was generated
locally (no API request), and the permission replay is restricted to its stable
sandbox/approval fields. The final source scan covered 541 files with no detected secrets. The already-running revision-88 backend was not replaced; active work
was preserved. After active work finishes, fully exit and reopen to load 89,
then use the selected profile's SSH apply action to verify production behavior.


Update 2026-09-23: Browser's configured service version was absent from managed
profile caches although it existed in the running desktop's package. Revision 88
prepares/verifies that exact bundle on launch or reconnect, and adds a PID-checked
repair utility for older persistent backends. All eight running profile caches
were repaired without profile restarts; the supported in-app Browser subsequently
opened and read a public test page. See [revision 88](design/browser-bundle-recovery-88.md)
for implementation, recovery commands, and validation limits. Earlier dirty work
below is retained; no new commit or push was performed for this change.
Build `20260922-230711-894` (revision 88, IPC 27) is selected for next launch.
All 98 focused Python checks passed; the Windows release build succeeded and
the 534-file source secret scan reported no findings. The persistent running
backend was not replaced, so its automatic checks require a later full exit
and restart after active work finishes; current file recovery is already applied.

Update 2026-09-23: diagnosed and fixed a missing SSH maintenance gauge being treated as endless busy.
Read-only remote identity checks restored the affected profile's previous bindings; its new remote
subagent/model settings remain explicitly deferred. No live runtime was stopped. Source, recovery
utility, invariants and remaining legacy-runtime limitation: [revision 87](design/ssh-settings-deferred-87.md).
This work follows the still-uncommitted revision 85/86 changes below; preserve all of them.
Build `20260922-154739-955` is selected for next launch (IPC 27). The affected profile's three
SSH connections subsequently reported `auth-state: ready`. Related Python checks: 387 passed,
one skipped; Windows release build passed. The already-running backend was not replaced.

Update 2026-09-22 17:17 KST: revision 86, build `20260922-081659-020`, is selected for
next launch. At inspection the live shell was revision 85 and the live service was revision 84,
both at normal authority. Neither was stopped. Revision 86 fixes UAC owner comparison and adds
an explicit full-exit/admin-restart action; see [the implementation and validation record](design/administrator-transition-86.md).
Shell build, 134 service checks, 25 launch-mode checks and 8 real UAC fixture checks passed.
The real UAC checks verified an elevated synthetic service; they did not stop actual profiles or
test Hyper-V. Migration remains untested. The inventory below preserves the earlier September 21
snapshot; re-read process paths and `current.json` before deciding what is actually running.

Snapshot generated 2026-09-21 (KST, late evening; 13:05 UTC at first draft) and
revised at 22:51 KST after the user separately authorized publishing the administrator-mode
update on this Windows machine. This document contains no credentials or secret values.
No migration, live service stop or live administrator launch was performed.

## 1. Repository snapshot

| Item | Value |
|---|---|
| Repository (source and target) | `<repo>` (the local checkout root) |
| Branch | `main` |
| HEAD | `6cf63da` - "fix: rebuild stale managed SSH sidebar caches once" (revision 84) |
| Baseline dirty | `tests/test_manager_local_first_remote.py` (+11/-2): a `reuse_equivalent` journal-drift strengthening that predates this handover. Preserve it; do not revert or fold it into unrelated commits. |
| Expected new dirty | `docs/HANDOFF.md`, `docs/RESTORE-CHECKLIST.md`, `docs/design/windows-execution-mode-85.md`, both READMEs, plus revision-85 administration work: new `manager/Shared/WindowsExecutionIdentity.cs`, `manager/Shell/WorkspaceExecutionMode.cs`, `manager/Shell/Dialogs.ExecutionMode.cs`, `manager/Supervisor.Tests/WorkspaceExecutionModeTests.cs`, `scripts/start-manager-admin.ps1`, `Open-Control-Center-Admin.cmd`; modified `manager/Shared/ManagerClient.cs`, `manager/Shell/App.xaml.cs`, `manager/Shell/MainWindow.cs`, `manager/Shell/WorkspaceBuild.cs`, `manager/Supervisor.Tests/*`, `manager/service/src/{main,windows}.rs`. Preserve this uncommitted source with the backup; a clone at HEAD does not contain it. |
| Untracked data | Everything under `work/` is local runtime data / ignored diagnostics. Do not treat it as source and do not delete it. |
| Live manager processes at snapshot | Exactly two observed (name + executable path, no arguments): one Shell and one Rust service process, both executables under `artifacts/manager/releases/20260921-063521-751` (revision 84). No fixture processes were running. PIDs change on restart. |

Working-tree status must be re-read before any handover claim
(`git status --porcelain`). A full-restore acceptance has **not** been executed.

## 2. What this repository is

A Windows manager for several Codex accounts/API providers that share tasks,
projects and personal skills:

* `manager/service/` - the **shipped** workspace service, a Rust crate (`Cargo.toml`,
  `src/{main,backend,protocol,processes,notes,note_aliases,windows,tests}.rs`). `scripts/build-manager.ps1`
  builds it with cargo and copies `codex-workspace-service.exe` into the release (the release
  `current.json` key for it is `supervisor`). This Rust service owns the manager named pipe.
* `manager/Supervisor/` - the legacy C# supervisor; it is not what `build-manager.ps1` ships.
* `manager/Supervisor.Tests/` - the .NET test project whose folder name is historical. Its harness
  drives the **current Rust service fixture** and is the active home of the execution-mode checks;
  it is not "legacy tests".
* `manager/Shell/` - WPF shell (`Codex.ControlCenter.Shell.csproj`): `App.xaml.cs`, `MainWindow.cs`.
* `manager/Shared/` - `Protocol.cs` (`ManagerProtocol.PipeName(root)`), `ManagerClient.cs`,
  `WindowsExecutionIdentity.cs` (`ExecutionAuthority`, pipe-token probing). The pipe name/protocol
  must match the Rust service (`manager/service/src/protocol.rs`).
* `scripts/manager_core/` - Python manager modules (store, instances, remote/SSH lifecycle,
  updates, catalog, notes, handoff).
* `scripts/remote_helpers/` - files pushed to managed SSH hosts (launcher, native controller,
  maintenance, ws/auth helpers).
* `artifacts/manager/` - built releases; `artifacts/manager/current.json` points at the active one.
* `artifacts/managed-desktop/<build>/` - managed desktop bundles (Electron host + Codex binaries).
* `runtime/` and `upstream/` - Codex source worktree and read-only comparison tree.

## 3. Source and target paths (exact)

| Role | Path |
|---|---|
| Source checkout (old Windows) | `<repo>` |
| Target checkout (new Windows) | `<repo>` preferred (same absolute path); any other location requires rewriting absolute paths in state, bindings and logs |
| Windows local data | `work\control-center\` (per profile: `profiles\<profile-id>\codex`, `profiles\<profile-id>\ui`) |
| Windows canonical Codex home | `%USERPROFILE%\.codex` on the current machine (rollouts, `sessions\`, `state_5.sqlite`); the target machine's actual user path must be confirmed before any path rewrite |
| Ubuntu stock CLI home | `/home/<ssh-user>/.codex` for the verified `<ssh-user>` account on `<ssh-host>` - canonical records for the SSH host (other users per actual host inventory) |
| Ubuntu manager service root | `/home/<ssh-user>/.local/share/codex-control-center/profiles/<profile-id>/` for the verified user - holds `launch.py`, the managed `codex/` home, and the private listener socket. Confirm other users against the actual host instead of guessing |

The stock Linux CLI and the manager's remote service are **distinct**: the stock
home is the record source; the manager's per-profile directory owns the launcher,
its own managed home and the private listener. Do not merge or overwrite one with
the other during a restore.

## 4. Goals and decisions

* Target restore: from the old Windows machine to a bare-metal Ubuntu GPU host;
  the new Windows app reaches it over SSH.
* The old `<ssh-host>` Hyper-V VM archive **files are meant to be restored** onto the new
  bare-metal Ubuntu host; only *booting* the old VM is not required. The restored data is the plan,
  the VM image itself is not a target.
* Scope is the Codex workspace app. Other project names/contents are out of scope.
* Cross-host handoff is not implemented: `scripts/manager_core/handoff.py` is an
  explicit **same-host** transfer of a canonical managed conversation subtree.
  There is no cross-host remap/reset, so a restored copy cannot be "repaired" into
  a live target by a normal `prepare` step.
* Shared records are shared through the common record catalog /
  `CODEX_RECORD_HOME`, not by copying raw databases.
* Credentials stay with the OS user: `scripts/settings.ps1` stores the model-lab key
  with `ProtectedData` `CurrentUser` scope in `profiles/deepseek.dpapi`; registered
  providers store per-provider blobs in
  `work/control-center/credentials/<provider-id>.dpapi` with metadata in
  `work/control-center/providers.json` (`scripts/manager_core/providers.py`,
  `CryptProtectData`). DPAPI blobs are bound to the Windows user, so a new user must
  re-enter keys - never copy them as usable credentials.
* Revision 84: on a cold profile launch the manager rebuilds the sidebar catalog
  metadata for every prepared managed SSH host once for the `ssh-canonical-v1`
  epoch (`scripts/manager_core/app_catalog_cache.py`, host ids from
  `scripts/manager_core/instances.py`). The reset is scoped to the four native
  catalog tables per host id, backs up the database first, and bumps the native
  catalog revision.
* Revision 85 (administrator launch mode) is **implemented, harness-verified and packaged for next
  launch; the live shell/service still run revision 84 and live UAC verification remains pending**:
  new `manager/Shared/WindowsExecutionIdentity.cs` (ExecutionAuthority, pipe-token probing),
  `manager/Shell/WorkspaceExecutionMode.cs`, `manager/Shell/Dialogs.ExecutionMode.cs`,
  `manager/Supervisor.Tests/WorkspaceExecutionModeTests.cs`, `scripts/start-manager-admin.ps1`,
  `Open-Control-Center-Admin.cmd`; modified `manager/Shared/ManagerClient.cs`,
  `manager/Shell/App.xaml.cs` (startup expected-user-SID guard), `manager/Shell/MainWindow.cs`
  (menu / current-token indicator), `manager/Shell/WorkspaceBuild.cs`, and the
  `manager/Supervisor.Tests/*` project. Rust `windows.rs` checks the client process token and sets the
  pipe's no-write-up integrity label; `main.rs::serve` rejects mismatches before reading any request.
  Design record: `docs/design/windows-execution-mode-85.md`.
  Verification reported so far: `dotnet build manager/Shell/Codex.ControlCenter.Shell.csproj --no-restore`
  passes with 0 warnings / 0 errors, the PowerShell launch/start scripts pass a syntax check, the
  Supervisor fixture harness prints **132 checks** (the OS-token checks, 20 of them, are a subset of
  those 132) and `WorkspaceExecutionModeTests` adds a **separate 19 checks**, with 3 further focused
  checks in the earlier standalone service fixture. After the server-side protection was added,
  all **30 Rust tests** and the **132 + 19 .NET checks** passed. The release pointer selects revision 85,
  build `20260921-135127-810`; the running shell and Rust service both remain
  in the revision-84 release directory. No UAC approval, live admin mode, Hyper-V access or live
  process/rights-switch test was executed.
  `ManagerClient` / `WindowsExecutionIdentity` must require the **actual named-pipe server OS token**
  to match (no trusted JSON or recorded PID) and must reject a privilege mismatch without retiring
  or stopping existing work. Switching Windows authority requires finishing work and using full exit,
  then starting `Open-Control-Center-Admin.cmd`; closing only the shell does not change service authority.

## 5. Live state that must NOT be reused after a restore

Restored copies of these are historical evidence only. They must be reset or
rebuilt before the target is treated as live:

| Area | Path / field |
|---|---|
| Manager live state | `work/control-center/state.json` fields `process_id`, `process_created`, `window_handle`, `status`, `started_at`, `login_observed_at`; maps `ssh_maintenance`, `profile_restarts`, `remote_updates`, `profile_maintenance` |
| Window/instance records | `work/control-center/instances/<uuid>/runtime-state.json`, `instances/<uuid>/runtime-admin/<generation>.json` (contains a DPAPI-protected admin key) |
| Journals | `work/control-center/updates/maintenance/<transaction>.json`, `work/control-center/restarts/*.lock`, `work/control-center/record-signals/*` |
| Remote listener records | `/home/<user>/.local/share/codex-control-center/profiles/<profile-id>/`: `native-instance.json`, `instance.lock`, `native-start.lock`, `native-shutdown.json`, `native-runtime.log` |
| IPC identity | supervisor named pipe (`ManagerProtocol.PipeName(root)`) and its OS token / generation; a restored pointer or PID is not authority |

Because no cross-host remap/reset is implemented, the first launch on the target
is **BLOCKED** until: the live-state files above are removed or rebuilt by the new
host, every SSH profile is re-prepared against the new Ubuntu host identity, and
the identity checks pass. A normal "prepare" is not a repair for restored stale
identity.

## 6. SSH identity and mapping model

* Binding id `ssh:<alias>` in `state.json` `remote_bindings`.
* Native host id `remote-ssh-discovered:<alias>` (revision-84 catalog epoch and native `host_id`).
* `migrationIdentity = <host id>:<remote CODEX_HOME>` - key for per-host metadata maps
  (`app-server-projects-migration-by-host`, project-id maps, catalog epochs).
* `host_identity = sha256(machine-id + uid + home)` - a new Ubuntu machine or user changes it, so the
  binding must be re-verified/re-prepared rather than silently reused. This is a **physical** guard.
  It does not by itself change the **logical** ids above (`ssh:<alias>`, `remote-ssh-discovered:<alias>`,
  thread UUIDs), which should be preserved whenever the alias and remote home stay the same.
* Per binding: `revision`, `runtime_bundle`, `remote_python`, `remote_launcher`,
  `remote_profile_home`, `blockers`.
* Rebind only when a **logical** id or path actually changes (alias rename, different Linux user/home,
  moved project root, changed thread id). Then migrate binding ids, native `host_id` rows, note task
  keys, project/assignment maps and catalog epochs together - never blindly replace UUIDs.
* Historical logs (`work/control-center/logs/*`, `ssh-routing.jsonl`, `work/*.log`) are evidence:
  leave their recorded paths untouched. Only live configuration/state is rewritten for a new root path.

## 7. Where user data actually lives

| Data | Actual store (verified) |
|---|---|
| Task notes | One JSON document per task: `work/control-center/notes/<sha256>.json` with `version`, `task:{host_id,thread_id}`, `group_id`, `notes`. Note bodies are **not** rows in the runtime DB. Logical-link changes are handled by `work/control-center/note-aliases.json` with the helpers `scripts/manager_core/note_aliases.py`, `manager/service/src/note_aliases.rs`, and `note_forks.py`. Keep the logical key stable and the document resolves; re-link only when the logical id changes. |
| Note drafts | `work/control-center/note-drafts/<sha256>.json` |
| Attachments | profile `codex/` home (attachments cache) and the remote managed home |
| Forks / handoffs | `work/control-center/handoffs`, `handoff-live`, task metadata |
| Skills / plugins | profile `codex/skills`, `codex/plugins`, `work/control-center/skill-bridge`, `shared-plugins` |
| API models | `config/deepseek-catalog.json`, provider registry, DPAPI-protected key |
| Runtime DB schema versions | documented in the private migration record (kept outside the published source); consult it instead of duplicating schema claims here |

Note documents are keyed by the **logical** `{host_id, thread_id}` pair (filename is its hash).
Preserving the logical ids is intended to keep those document keys stable; target restoration still
needs verification. A logical rename requires an explicit migration map. The existing `note_aliases.py`
helper repairs an old projection ID to a canonical ID using catalog evidence; it is not a general
cross-host note migration command. Preserve note groups, images, fork links and drafts with the documents.

## 8. Build, runtime and version state

| Item | Value |
|---|---|
| Active processes (live) | The running shell and Rust service still come from `artifacts/manager/releases/20260921-063521-751`, revision 84 |
| Selected release (next launch) | `artifacts/manager/current.json` selects `artifacts/manager/releases/20260921-135127-810`, revision 85 |
| Selected release manifest | `runtime-manifest.json`: `version 1`, `runtime_revision 54e0bbeb...`, `service_revision 5493e5ca...`, `shell_compatibility.revision 85`, `service_protocol 27`, per-file SHA-256. The service differs from the live revision-84 service (`29759794...`), so full exit is required to finish applying it. |
| Desktop app | `26.915.4065.0` |
| Remote managed runtime bundle | `0.153.4-managed-e29fcb2680f61520-c7f95e7` |

## 9. Verified evidence (source / test) and what is not verified

* Design record: `docs/design/ssh-sidebar-cache-84.md`.
* Python tests on `6cf63da` (already run twice; not re-run further): `tests/test_manager_app_catalog_cache.py`
  **19 tests OK** and `tests/test_manager_local_first_remote.py` **29 tests OK** (48 total).
* Revision-85 build and .NET evidence: Shell build
  (`dotnet build manager/Shell/Codex.ControlCenter.Shell.csproj --no-restore`) 0 warnings / 0 errors;
  PowerShell syntax check passed; **Supervisor fixture harness 132 checks passed** plus
  **WorkspaceExecutionModeTests 19 separate checks passed**, with 3 additional focused checks in the
  standalone service fixture. The 20 OS-token checks are a subset of the 132, not a separate count.
  After the Rust server-side authority check was added, all **30 Rust tests** and **132 + 19 .NET checks**
  passed again. `scripts/build-manager.ps1` published build `20260921-135127-810` without launching it.
* Privacy scan for revision 84: `work/privacy-84-report.json` = `[]`.
* Revision-85 source/documents: Gitleaks scanned 524 tracked/untracked candidate files and reported
  0 findings (`work/privacy-85-report.json`). This result is a secret scan; the published handoff and
  checklist replace local paths, user names and host aliases with placeholders, and the host inventory
  itself stays in the private migration record.
  README/companion-document local links were checked (6 documents, 0 broken links).
* Revision-84 live evidence recorded when revision 84 landed: one idle affected profile was
  cold-relaunched; its desktop repopulated 95 remote catalog rows matching the
  remote app-server list digest; the four stale projection ids were absent; three
  SSH connections reached ready with unchanged remote process identities.
* **Not verified**: any full restore on the new Windows/Ubuntu pair, a live administrator launch
  (UAC approval, admin mode, Hyper-V access, rights switch), and cross-host handoff. Do not claim
  restore testing.

## 10. Known failures and limits

* `tests/test_launchers.py::ExplorerLauncherTests::test_lab_entry_reaches_live_gui_and_python_child`
  timed out once in a full discover run (GUI `Open-Lab.cmd`, 120 s). Treat as an
  environment/live-launcher issue unrelated to revisions 82-85 until re-checked.
* `manager/NativeHost.LogicTests` fails to compile in a full run: pre-existing missing types
  (`NoteTask`, `ProfileCardData`, `NativeViewportRecovery`, `ResponsivenessMonitor`, and others).
  This predates the revision-85 work and that work did not touch those files (zero file diff there);
  it was not fixed. Do not report the native window/host tests as passing.
* The baseline dirty test file is uncommitted; label any full-suite result accordingly.
* One profile that was active during the revision-84 migration still needs its first
  cold launch with the updated backend (per the design record).
* Cross-host handoff unsupported by design; shared-storage unification requires no
  in-progress work (forced termination can lose unsaved input).
* The sidebar epoch deliberately clears a host's `local_thread_catalog_sync_state`
  row so that host rebuilds from a full `thread/list` on its next launch.

## 11. How to build and test

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-manager.ps1 -SelfTest
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-manager.ps1 -SkipBuild -Launch
dotnet build manager/Shell/Codex.ControlCenter.Shell.csproj --no-restore
dotnet run --project manager/Supervisor.Tests/Codex.ControlCenter.Supervisor.Tests.csproj -c Release
C:\Python313\python.exe -X utf8 -m unittest discover -s tests -p "test_manager_app_catalog_cache.py"
C:\Python313\python.exe -X utf8 -m unittest discover -s tests -p "test_manager_local_first_remote.py"
```

The `Supervisor.Tests` project is a console harness that copies and drives the current Rust service
binary (`manager/service/target/release/codex-workspace-service.exe`), so build that service first;
its printout is the 132-check harness result, and `WorkspaceExecutionModeTests` reports its 19 checks
separately. `manager/NativeHost.LogicTests` does not build today (see section 10).

`Stop-All-Codex.cmd` / `Stop-All-Codex.ps1` interrupt in-flight work; use them only
when that is intended.

## 12. Next concrete tasks (independent of chat context)

1. **Implement the missing offline restore planner/importer on disposable copies.** Start at
   `scripts/manager_core/store.py`, `instances.py::Instances.paths`, `remote.py`, and
   `scripts/remote_helpers/native_controller.py::_descriptor`. Inventory old/new roots, user homes and
   logical/physical host identities; generate a redacted dry-run plan. Preserve profile/source/thread IDs,
   account pins and durable metadata. Quarantine only copied execution descriptors; atomically update only
   validated target paths with per-file backups. Cover interrupted import, a changed drive/home, stale PID
   reuse, duplicate source IDs and changed SSH identity with fixtures. No existing tool proves this whole
   transition safe. This is the first blocking development task for cross-PC restoration.
2. **Finish live UI verification of the revision-86 administrator transition.** Source files are listed in
   section 4 and [the design record](design/windows-execution-mode-85.md). Fixture/build checks pass. Still
   test the full-exit/admin-restart button with disposable profiles, UAC cancel, different credentials,
   switching back to normal and `Get-VM` with the intended Windows rights. Real elevated service connection
   and preservation of a normal fixture service were verified on September 22. Revision 86 is available
   for next launch; existing production work was not stopped.
3. **Restore/rebind after step 1 passes.** Follow the private migration record on the new Windows/Ubuntu pair. Re-enter provider
   keys into `work/control-center/credentials/*.dpapi`, re-login accounts, verify the new SSH host key and
   prepare bindings. Keep logical aliases/IDs where possible. When a path/ID actually changes, apply the
   importer's explicit project/task/note map, including shared groups/images. Do not use `note_forks.py` as a
   generic cross-host converter.
4. **Run RESTORE-CHECKLIST end to end** after step 3. Compare the quiescent backup to the target, test local
   and SSH project paths, note sharing/detach/images, multi-profile catalogs, then reboot both target hosts.
   Record every executed/skipped check and keep the source/VM archives until acceptance.
5. **Repair the outdated native-window test harness** (`manager/NativeHost.LogicTests` fake boundaries and
   production links) before the next viewport/input refactor. Reproduce the missing-type failures in
   section 10 and test the real host lifecycle logic; do not weaken assertions just to compile.

## New-host operational update - 2026-09-23

The retained SSH servers were recovered on the new Windows PC: eight profiles
on each server, 16 live desktop transports verified. The user authorized remote
profile interruption after confirming that the work was finished. The old VM's
Windows metadata was then backed up outside Git and retired at the user's request.
The exact evidence, archive path, remaining connection-list refresh requirement
and limits of that backup are kept in the private migration record. The Linux/VM backup
disk has not arrived; original remote session restoration remains unverified.
No new application build, commit or push was made for these operational repairs.
Existing uncommitted revisions 85-89 and concurrent source edits were preserved.
