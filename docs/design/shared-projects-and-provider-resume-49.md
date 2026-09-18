# Shared project declarations and profile model selection (49)

External API profiles and ChatGPT profiles share the same local records and
project declarations. The selected profile supplies execution credentials and
model configuration. The original desktop is another reader/writer, not a
permanent authority for project membership.

## Failures addressed

- The previous workspace event registry covered SSH projects only. Creating a
  local project in an API profile wrote the common native project database but
  left other desktops' declaration and native-ID caches unchanged.
- Cold preparation filtered SSH IDs out of project order. The SSH adapter only
  repaired that order when project content changed and held onto the first state
  object across login/replacement. New profiles could have example-game on disk but
  omit it in their visible catalog. Preparation now hydrates both registries
  before launching, order repair is independent, and replacement stores rebind.
- Provider selection was corrected only in the main-process conversation manager.
  The visible renderer has its own direct runtime client. Opening an API-created
  task there could send its unregistered provider ID to a ChatGPT profile.
- Archive/unarchive signals were collapsed into generic reads. Native archive
  suppression is separate from transcript hydration and now propagates explicitly.

## Implementation

`desktop_local_workspace_sync.cjs` merges bounded per-writer project events with
logical clocks and removal tombstones, using the existing SSH registry pattern.
It shares local names, roots and native project IDs. A read-only native project
cache refresh permits a peer to rename/remove the imported declaration without
re-importing records. Explicit undo publishes a new event. A stale cold profile
cannot republish a tombstoned declaration. No live selections, prompts, login
files or permission grants are shared.

`shared_workspaces.py` seeds the local registry once from the common SQLite store,
retains existing aliases found across profiles, and applies local/SSH registry
snapshots only to inactive homes before launch. Running homes write through
their own desktop state API. Registry seeding never rewrites a conversation.

`desktop_profile_resume.cjs` is installed in both the original companion and
managed desktop, in both main and renderer managers. On resume/fork it reads the
destination config and binds its provider. When the saved provider differs, it
also replaces the old provider's model and effort defaults. Same-provider user
choices survive. No foreign provider keys are copied into ChatGPT profiles.

Archive, restore and deletion notifications call the native catalog handlers in
both processes. A late generic hydration cannot resurrect a removed/archived task.
The proxy retains visibility events when later notifications are coalesced.

## Validation

- JavaScript tests: local/SSH add, rename, remove, undo, cold tombstones, late order
  reset, login state replacement and native project cache mapping.
- Python tests: API-created native project bootstrap, fresh profile local/SSH
  declarations, delayed deletion, original-app alias deduplication and preserved
  SSH order. Existing workspace, preferences and archive bundle suites also pass.
- Actual hidden Codex desktop + two disposable app-server homes + loopback model:
  API-created task opens through the visible renderer without the source provider
  table; own profile model is selected; history refresh and unsent draft protection
  pass; peer local project appears in the sidebar and supports native rename and
  deletion; archive/restore reaches the renderer; SSH registry add/remove passes.
  Evidence: `artifacts/results/desktop-sync-c3699167/report.json`.
- The tests send no paid model requests, use no account credentials, and do not
  navigate/restart the user's desktops. Existing running apps load the new adapter
  on their next normal launch; no runtime injection is performed.
