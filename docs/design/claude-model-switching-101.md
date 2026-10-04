# Revision 101 Claude model switching within a task

## Problem and intended behavior

The revision 100 Claude profile catalog contained only its default model. The
runtime proxy replaced every requested model with that default. The Claude
session ledger also included model and effort in its identity, so even an allowed
change would create a new CLI session and replay shared context unnecessarily.

Revision 101 lets the existing task composer choose among approved Claude models.
The profile setting is the default for a new/cross-provider task, not a forced
model for every message. An explicit model or effort change applies to the next
turn; an already running CLI process is not interrupted or changed mid-request.
Omitted turn settings preserve the task's current selection.

## Model and UltraCode contract

Supported choices are the `opus`, `sonnet` and `fable` aliases, plus the explicit
`claude-opus-5-5` ID. The aliases follow the version resolved by the official CLI;
the explicit ID selects Opus 5.5. Account/model access is still enforced by Claude.
The bridge publishes multiple `cc-*` catalog entries for the native picker and
removes that prefix before invoking Claude. It does not accept arbitrary model
IDs or cross-provider model overrides.

Checked 2026-10-01: installed Claude CLI is 2.1.282. Official documentation places
Opus 5.5 support at 2.1.280 and Sonnet 5.5 at 2.1.284. This patch does not update the
CLI or advertise the latter explicit model on the installed version.

UltraCode was already implemented in revision 99 as literal `--effort ultracode`.
It is distinct from `max`. On the installed 2.1.282 contract it enables xhigh
reasoning and workflows; availability also depends on model and policy. The
2.1.284 independent UltraCode toggle is not implemented by pretending that the
older installed CLI has that behavior. Existing saved effort choices are preserved.

Sources:

- [Claude model configuration](https://code.claude.com/docs/en/model-config)
- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude effort reference](https://platform.claude.com/docs/en/build-with-claude/effort)

## Boundaries and session continuity

- `scripts/manager_core/claude_profiles.py`: approved choices, multiple catalog
  entries and allowed per-model efforts. Existing context/compaction defaults
  remain the official CLI defaults when unset.
- `scripts/manager_core/external_profile.py`: honor approved Claude selections,
  including collaboration settings, while preserving sparse turn updates. The
  existing fixed-model routing for HTTP API profiles remains separate.
- `scripts/manager_core/claude_runner.py`: session identity excludes mutable model,
  effort and model-derived compaction tokens. Account, cwd, instructions, plugins,
  CLI version and explicit context/compaction policy remain identity boundaries.
- WPF Claude settings: distinguish profile defaults from task choices, name the
  explicit Opus 5.5 version, and explain UltraCode on the supported CLI.

The same account can resume the same Claude session UUID with a different
`--model` or `--effort`. This avoids unnecessary history replay; it does **not**
guarantee provider-side cache hits when changing models or accounts. Dirty,
rolled-back or mismatched canonical history still requires a fresh session.

Legacy model/effort-keyed ledgers require conservative migration. A candidate must
match the stable settings and its original filename hash for the actual cwd.
Select the newest compatible candidate, including dirty records; do not revive
an older clean session to bypass a newer uncertain run. Once the stable ledger
exists it is authoritative. Shared canonical history remains the recovery source.

The existing desktop resume hook carries the durable model/effort for the same
provider, and native Claude execution already transports exact selected values.
No native binary or Electron adapter changes are needed for this fix.
The hook reads task metadata before resume and passes those values explicitly.
A bare resume request that omits them still uses the profile default: forcing the
destination provider suppresses native automatic restoration. The integration
test reproduces the desktop contract, not a new guarantee for arbitrary RPC clients.

## Applying the update

The next-launch manager build includes frozen Python sources. Source edits alone
do not change a running manager service or existing profile process. Apply the
manager update after current work, then safely reopen the Claude profile so it
receives the new catalog and proxy. Use the model/effort picker in an existing
task to select another model. Changing a task selection does not rewrite the
profile's default settings.

## Validation and release

On 2026-10-01 the next-launch pointer was set to manager release
`20260930-154746-301` (revision 101), after validating its 203 frozen bundle files
against their hashes and current sources. The native runtime remains unchanged:
`20260928-181038-780945`. Existing profiles and services were not restarted.

- 69 focused Python tests: Claude catalog/configuration, routing, runner,
  account/cwd/instruction boundaries, legacy ledger migration and model settings.
- 113 related Python regressions: local-only Claude restrictions, usage, desktop
  reasoning/cached bundle upgrade, HTTP providers and local model presets.
  The first invocation missed the tests import path for the cached-upgrade module;
  its seven tests passed after retrying that module with `PYTHONPATH=tests;scripts`.
- 10 complete offline native integration tests: selected CLI arguments and the
  same session through nine turns, model/effort-only changes, desktop-style
  restart/resume, GPT/Claude context exchange, account return, compression,
  interruption and approval behavior. No skips or failures.
- 29 checks in the same compiled manager: five Claude settings/card checks and
  24 navigation/profile/local-model checks. Opus 5.5 and UltraCode round-trip while
  preserving saved settings.

The running managed desktop archive was also read without changing it; its
UltraCode picker, composer-selection and label patches were already present.
Independent review found malformed model input and ambiguous legacy timestamp
handling; both were fixed and included in the final focused tests before build.

Ignored proof files: `work/claude-101-native-e2e.json`,
`work/release-101-claude-profile.json`, `work/release-101-shortcut-navigation.json`
and `work/deployment-101.json`. The native proof records executable/source hashes;
the published bundle matches the tested Python sources.

No real model calls, account login, forced profile restart or CLI installation
were performed. These checks do not establish account entitlement, real provider
cache hits, or end-user visual behavior in an already-running old profile.

## Related worktree assessment

[Persistent development domains](persistent-worktree-domains.md) describes which
existing canonical task/worktree/notes capabilities can support long-lived owner
tasks and which dispatch, admission and integration features are still missing.
That document is a design assessment, not a shipped orchestrator.
