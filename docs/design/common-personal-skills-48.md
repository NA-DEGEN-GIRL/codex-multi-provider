# Common personal skills — revision 48

## Cause and authority

The shared `skills` directory was already linked into every managed profile.
The original app had disabled seven personal skills with `skills.config`, but
the profile configs had no corresponding entries. Sharing files did not share
enablement. No personal skill files needed to be recovered or recopied.

Following the user's clarification, the original app is not authoritative.
`work/control-center/personal-skills.json` is the common registry. Initial
settings are imported once; subsequent changes in any existing profile or the
original app are detected against each client's last observed effective values.
Changed values are promoted to the registry (latest config timestamp wins on
concurrent conflicts), then applied to all clients. A newly created profile's
empty configuration cannot reset established choices. Polling is every two
seconds while the manager backend is running, and idle polls do not rewrite
files. Changes made while it is stopped are reconciled at its next start.

All five current profiles use the same personal installation directory. Actual
deletion through any of those shared paths therefore removes the installation
for all of them; reconciliation does not resurrect files. New installations are
discovered from the personal `.codex/skills` and `.agents/skills` locations.

## UI and file boundaries

Settings and management → **공통 개인 스킬** provides search, enable/disable,
delete, and restore. Scope is the original Windows app and all local managed
profiles. Built-in `.system`, plugin bundles, repository skills, and remote
machines' separate skill installations are excluded.

Deleting through this UI moves the installation entry into
`~/.codex/.manager-skill-trash/<id>/skill` and retains restoration metadata.
Directory junctions are moved as links; their external repository targets are
never recursively deleted or moved. Restoring refuses to overwrite a replacement
installation. Enablement is applied through native `skills.config` rules, with
semantic checks protecting unrelated TOML settings. The UI reports per-profile
failures instead of claiming all clients were updated.

Fresh runtimes load the settings immediately. Already running apps can retain
their skill list until reopened, and past prompt contents are not rewritten.
No active work, windows, selections, login files, or conversations were restarted
or changed while applying this update.

## Applied default and validation

Enabled: `codex-handoff`, `3d-assets`, `game-audio`.

Disabled: `codex-init-gate`, `design-repo-subagents`, `distill-ramble`,
`orient-repo`, `redact-sensitive-info`, `shape-idea`, `write-agents-md`.

- Applied successfully to the original app plus profiles 01–05, six configs.
- Verified with the actual Codex app-server `skills/list` in an isolated,
  credential-free probe using the shared skill directory and common rules:
  exactly those three personal skills enabled, seven disabled, no skill errors.
- 11 Python tests cover propagation from each client in both directions, new
  profiles, persisted registry, idle writes, deletion, restoration, junction
  target preservation, scope/path checks, partial errors, and multiline TOML.
- Existing 12 common-settings and 22 control-center tests pass.
- 14 isolated WPF checks cover filtering, toggles, delete/restore and narrow-window
  button bounds. Rendered `work/control-center-personal-skills-test.png` inspected.
- Full manager build/self-test passes. Frozen release scripts match source.

Release: `artifacts/manager/releases/20260918-092454-212`.

Native skill enablement format and local discovery behavior:
[Official OpenAI skill documentation](https://developers.openai.com/codex/skills/).
