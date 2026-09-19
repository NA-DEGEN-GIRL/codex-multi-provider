# Native fork note sharing and explicit separation

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
error instead of assigning an unrelated empty document.

On first note access, a new fork joins the immediate parent's note group.
A standalone parent is promoted to a group without changing note IDs or
revisions; the child points to the same group. This also applies to empty
notes, so additions, edits, deletion and restoration are visible to both
tasks. Nested forks share their immediate parent's current group. Existing
standalone documents from older releases are preserved rather than merged.

The explicit `notes.fork` action, labelled `메모 분리`, copies all current notes
and checklist items to an independent document with fresh IDs. It detaches
only the selected task; already attached parents, children and siblings keep
their group. Splitting an already independent task is idempotent. Cycles and
corrupt source notes fail without creating an empty child document.

The notes panel finishes pending draft saves before requesting the destination
task's first note list or explicitly separating its memo. Waiting is
asynchronous, and a superseded selection cannot read or join the earlier
destination. During separation, editing is disabled until saved content has
been copied and reloaded. Save failures retain drafts and allow retry.

The panel labels shared and independent notes and allows separating empty
shared notes. An idle visible panel checks saved notes every two seconds;
active input, unsaved drafts and in-flight changes prevent display replacement.
Group membership is resolved at first successful note access, not at the
historical instant of conversation fork. A previously unopened child therefore
joins the parent's current group when first accessed. Remote host note identities
remain separate; local ancestry is never applied to an SSH task with the same ID.

Regression coverage includes native and managed homes, WAL-only inserts,
incomplete metadata, cache reuse, unrelated agent spawn edges, duplicate
refreshes and loads, nested forks, empty shared notes, independent edits after
explicit separation, existing child notes, legacy groups and corrupt ancestry.
The WPF fixture also delays an in-flight save while editing and changing tasks
to verify that the shared memo includes the latest draft and the correct task.
