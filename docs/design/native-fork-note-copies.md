# Native fork note copies

Native task forks record their parent in the first rollout `session_meta`
record's `forked_from_id`. They do not create `thread_spawn_edges`, which
tracks spawned agents. The previous note inheritance implementation therefore
missed native forks, including forks opened in the managed desktop. Its SQLite
`immutable=1` connection also ignored uncheckpointed WAL changes.

The manager now reads verified metadata for the thread indexed by each local
state database. It reads no conversation messages, rejects mismatched thread
IDs and excludes subagents. Read-only SQLite connections include live WAL
changes. Complete metadata headers and database results are cached; incomplete
headers are retried. An unchanged database and WAL require no rollout scan.

Before a missing local note document is opened, the service requests a bounded
ancestry refresh from the Python backend. That lookup reads only the requested
thread and its ancestors, avoiding a dependency on the next manager state
poll. Missing or incomplete metadata fails the first load with a retryable
error instead of freezing an empty child snapshot.

The note service copies the immediate parent's saved notes into a private
child document, assigning new note and checklist item IDs. Parent documents
remain unchanged, including legacy shared group documents. Existing child
documents are never replaced. Nested forks materialize intermediate snapshots;
empty snapshots remain empty after later parent edits. Cycles and corrupt
source notes fail without creating an empty child document. Existing shared
groups remain compatible, and `notes.fork` detaches them into private notes.

The notes panel finishes pending draft saves before requesting the destination
task's first note list or explicitly splitting a shared memo. Waiting is
asynchronous, and a superseded selection cannot read or clone the earlier
destination. Save failures leave the destination load retryable.

The snapshot is taken at the child's first successful note access, using the
parent's saved notes at that time. This is not an exact snapshot of the instant
the conversation was forked: unsaved drafts and parent edits made before first
note access are not historical versions that this storage can recover. Remote
host note identities remain separate; local ancestry is never applied to an
SSH task with the same thread ID.

Regression coverage includes native and managed homes, WAL-only inserts,
incomplete metadata, cache reuse, unrelated agent spawn edges, duplicate
refreshes and loads, nested forks, empty source notes, independent child edits,
existing child notes, legacy shared groups and corrupt or cyclic ancestry.
The WPF fixture also delays an in-flight save while editing and changing tasks
to verify that the copied memo includes the latest draft and the correct task.
