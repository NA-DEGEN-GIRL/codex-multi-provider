# Project drag/drop membership (51)

The reported task (`00000000-0000-7000-8000-000000000002`) had an explicit assignment to project `00000000-0000-7000-8000-000000000003` only in the external profile's legacy desktop state. Its common SQLite `project_id` was null. Other profiles retained `projectless-thread-ids` and therefore displayed it outside the project.

The desktop backend can have `threadAssignmentsEnabled` disabled while project creation/import remains enabled. In that mode `writeThreadAssignment` invokes only the legacy state callback and records a pending migration. Sharing project declarations alone cannot share these task moves.

For a persisted local task, the manager adapter now commits an explicit legacy move through `thread/metadata/update` before the desktop's cache callback. Failed writes do not pretend the move succeeded. Native membership-enabled, remote and new/prewarmed task paths retain their existing behavior. No rollout flags are changed.

Peer thread observations refresh current native metadata and adopt it through the desktop's own assignment store and publication callback. Reads racing with a local move are deferred or discarded. Pending work is bounded and failures retry without blocking input. Projectless moves use the same native API. No navigation, focus, model, credentials, or history changes are performed by this adapter.

Before cold launch, shared project declarations are followed by membership projection from common metadata. An uncommitted legacy pending assignment is preserved; a matching committed assignment is acknowledged. Remote and ChatGPT memberships are retained.

The reported move was repaired using an isolated native app-server and the user's explicit destination; the working directory was verified unchanged. The repair did not call a model or alter any running UI. Backup/evidence: `artifacts/results/project-membership-repair-a5919593`.

Validation includes native desktop regression `artifacts/results/desktop-sync-56ceceba`: legacy-mode drag persists to the common database, a different provider reads the destination, and its subsequent removal from the project appears in the desktop cache without reopening. Existing live history/draft preservation and archive/restore checks also passed. Unit tests cover failed writes, transient read retry, native remote/new-task paths, and cold membership projection.
