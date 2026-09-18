# SSH project declarations and explicit record deletion

Profile 02 stored example-game only in its private `remote-projects` desktop state.
Existing startup import included local projects and SSH connections, but omitted
SSH projects. Thread invalidations were also local-only and discarded the event
type: a deleted task was hydrated again, failed to read, and remained cached in
other windows. Desktop logs independently confirmed that SSH `thread/delete`
was rejected by the legacy record catalog's read-only guard.

SSH declarations now have a small shared event registry beside record signals.
The native desktop state API applies add/rename/remove operations and broadcasts
its normal renderer invalidations. Per-writer logical revisions and persistent
deletion tombstones prevent an old profile from resurrecting removed projects.
A first-run seed imports existing declarations from registered profile homes.
Selected project, account credentials, connection toggles and unsent drafts are
not part of this registry.

Record invalidations now carry host and event kind. Both main and renderer stores
evict a confirmed deletion through their native handler, rather than attempting
to hydrate it. Same-ID records on other hosts are unaffected. Late invalidations
and pending hydration cannot restore a deleted task. SSH managers participate in
this channel as well as local managers; hidden renderers drain on visibility.

An explicit SSH delete of an observed catalog projection is handled asynchronously
by the authenticated transport. It resolves the registered source and verifies the
projection ID again on the remote host, checks host identity and pinned runtime
hash, then invokes native `thread/delete` with that record/SQLite root and a fresh
credential-free private HOME. The source's config/auth is not loaded. Native
writer-lock, descendant and fork-reference checks are retained. No deletion SQL,
rollout unlinking, blanket read-only bypass, actor resume, or model call is added.
A compatible current runtime can be staged without restarting a running profile;
insufficient disk space is reported distinctly. Failed/uncertain deletion never
publishes a success notification and is not retried automatically.

Validation:
- 133 focused Python tests, one platform-specific skip.
- JavaScript multi-profile project add/rename/remove/relaunch test; shared local
  and SSH invalidation tests, host isolation, deletion without hydration, drafts,
  in-flight submission, hidden windows and late invalidation coverage.
- `remote-dev` native deletion with disposable records: selected JSONL removed,
  unrelated JSONL retained, no credentials created in the source home.
- `scripts/test_workspace_sync_desktop_live.py` uses a hidden real desktop with
  disposable homes and a loopback model fixture, including native project state
  updates and existing live-history/draft checks. No user window is manipulated.
- Evidence: `artifacts/results/ssh-project-delete-44/native-delete.json` and the
  corresponding `desktop-sync-*` reports.

The desktop adapters are immutable program assets, so running windows retain
their old implementation until the next normal app start. Existing example-game was
seeded into the new registry; user-requested deletion targets were not deleted
as part of verification.
