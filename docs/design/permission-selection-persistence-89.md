# Local permission selection across manager restart (revision 89)

## Failure

A local task was opened with `danger-full-access` / `approval_policy=never`
in one manager generation. After a full exit and an administrator relaunch,
the same task resumed with `workspace-write` and `network_access=false`, and a
later command failed with `PermissionError [WinError 5]` on a local file the
first turn could write. The failure is a consequence of the narrowed
permission, not of SSH, a remote Unix user or an ACL change.

## Cause

The profile's `config.toml` carries no `sandbox_mode`, so a resume that states
no permission selection falls back to the runtime default:
`ConfigToml::derive_permission_profile` (codex-rs, `config/src/config_toml.rs`)
resolves a trusted project without `sandbox_mode` to `workspace-write`. The
original home keeps `sandbox_mode = "danger-full-access"`, so the two homes
have different defaults. That confirmed discrepancy explains a possible
omitted-selection fallback; it does not prove that the resume overlay was
actually omitted.

Whether the desktop resume request itself carried an explicit `sandbox` /
`permissions` overlay could not be proven from stored evidence: the manager
records no request parameters, and the managed profile state keeps no
permission selection. The fix therefore never disputes a selection the
request states; it only fills a request that states none.

## Change

`scripts/manager_core/permission_selection.py` follows the deployed app-server
JSON schema for this build: a resume accepts `sandbox` (kebab-case mode
string), `approvalPolicy` and `approvalsReviewer` only -
`permissions`/`runtimeWorkspaceRoots` are experimental and are never emitted.
Results and `thread/settings/updated` still expose `sandboxPolicy` and
`activePermissionProfile`, which are used as evidence.

Only runtime evidence is stored: successful `thread/start|resume|fork`
responses and `thread/settings/updated` notifications. Error responses and
client requests never become evidence, so a rejected choice cannot return as
a later widening.

`scripts/manager_core/runtime_proxy.py` decorates the managed local proxy:
`managed_client_message()` keeps the profile model binding and then calls
`PermissionSelectionProxy.to_runtime()`, which fills a resume only when the
request states no `permissions`, `sandbox`, `sandboxPolicy`, `approvalPolicy`
or `approvalsReviewer`, and only with a value the runtime itself reported
earlier. `from_runtime()` records the notifications and successful responses.

## Adoption

Nothing is backfilled. The proxy captures only choices the runtime confirms
after the updated proxy is installed, so an existing thread that ran with full
access before the update may need `Full access` selected once more in the UI
for the choice to be captured. Until then, a resume that states no selection
keeps the runtime default and is never widened.

## Rules

* A request that states any permission key is never rewritten, even when it is
  narrower than the remembered value. A `config` HashMap override
  (`sandbox_mode`, `approval_policy`, `approvals_reviewer`, `permissions`,
  `default_permissions`, `sandbox_workspace_write`, including the dotted
  `sandbox_workspace_write.network_access` form) is the same kind of fresh
  authority, and so is a supplied `runtimeWorkspaceRoots` scope. A
  present-but-null key is an omission and is filled.
* Only one selection is replayed: a bare `dangerFullAccess` policy with the
  built-in full profile (`activePermissionProfile` absent/null or
  `:danger-full-access`), which maps exactly onto
  `sandbox: "danger-full-access"`. A named or custom profile is never converted
  into the built-in mode, and a workspace-write policy carries
  network/writable-root restrictions a mode string cannot express; both retire
  any older selection for that thread, so a later resume cannot restore a
  broader right the runtime no longer confirms. A message with no policy
  field, or a rejected request, never retires or restores anything.
  Writable-root and network detail is never claimed to survive a restore.
* What is read back is validated as the canonical stored form (exactly one
  mode, no unknown fields, no client-style sandbox strings); malformed state is
  ignored instead of being re-emitted.
* An observed `configRequirements/read` ceiling is applied on read. The
  deployed schema types `allowedPermissionProfiles` as `dict[str, bool]`
  (a `false` value denies the profile) and `allowedSandboxModes` as a mode
  list; an empty list or an all-false map excludes everything (fail closed).
* Storage is a bounded JSON document under
  `<root>/work/control-center/profiles/<id>/permission-selection.json`
  (128 threads); a document whose `profile_id` does not match is ignored. Both
  proxy pump threads are serialised by a per-profile lock that gives up after
  0.1 s and leaves the document and the RPC stream untouched rather than
  stalling a pump. No config, auth, history or ACL file is touched, and
  elevation alone never widens a selection.

## Verification

`tests/test_manager_permission_selection.py` covers the request/result/thread
settings vocabulary, exclusive `permissions` vs `sandbox`, fresh-choice
precedence (including config overrides and present-but-null keys),
error-response rejection, retirement of a stale selection by a confirmed but
unrepresentable custom policy, dotted config narrowing, empty requirement
lists, cross-profile document isolation, corruption recovery, the 128-thread
bound, concurrent writers, resume filling and malformed traffic.
`tests/test_manager_runtime_proxy.py` passes unchanged.
`tests/fixtures/app-server-permission-schema-89.json` is a small sanitized
excerpt of the deployed schema; one test asserts that every key this module
replays exists in that schema and that the requirement shapes drive the
ceiling exactly. The simplified scope means a custom full policy retires a
stale built-in full selection instead of being converted into it.

## Limits

Only managed **local** runtimes pass through this proxy. SSH windows speak to
the remote app-server through the SSH shim, so remote threads keep their own
runtime-side behaviour; that path is owned by the remote lifecycle work.
