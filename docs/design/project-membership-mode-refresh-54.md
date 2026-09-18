# Project membership during mode refresh

Legacy desktop project assignments can exist before they are imported into native
`threads.project_id`. A native `null` therefore has two meanings: unimported
membership, or a deliberate move out of a project. A mode-triggered task refresh
previously treated both as removal and published that loss to other profiles.

The desktop adapter now guards the native assignment adoption path, including
both legacy and native-enabled observers. Cold profile preparation uses the same
rule. Existing local membership survives a null observation until either native
read migration completed or the task has confirmed native membership history.
Individual pending assignments remain protected even after a migration flag is
set. The source preference import applies this rule as well as final cold-profile
projection; otherwise importing an older source snapshot can lose the assignment
before the final projection runs.
Positive native assignments remain authoritative. Explicit project moves still
commit through the native metadata API before changing the desktop cache.

Confirmation is monotonic and shared through per-writer files in the private
record-signals directory. Only a successful native metadata write or a positive
native membership read confirms a task. Failed requests, UI refreshes and null
reads do not. Confirmation contains task IDs only, not assignment values or
credentials. Peers retry blocked observations after confirmation arrives, so a
real removal propagates even to an instance opened after the removal. Repeated
blocked null observations do not issue repeated reads. Evidence write failures
leave the conservative guard in place and do not prevent profile launch.

Recovery is separate from the fix: ten missing assignments were corroborated by
five local snapshots and restored through native metadata updates. Existing
non-null assignments, working directories and history paths were preserved.
Recovery evidence and backups remain in ignored local storage.

Validation includes mocked desktop observations, cold-profile preparation, and
an isolated native runtime with one original client and three profile clients.
The latter checks moves, removal, filtered listings and unchanged conversation
bodies without paid model calls or touching the user's running windows.

The native desktop fixture passed 26 checks. It exercises the real assignment
store and observes one protected null read; five repeated observations issue no
further reads. A pending assignment survives a completed read-migration flag
and an older peer proof. Removing the pending marker releases the deferred
observation, and subsequent explicit moves and peer removal appear without a
restart. Migration flags are seeded in this isolated test; it does not automate
the product's account-backed mode selection UI.
