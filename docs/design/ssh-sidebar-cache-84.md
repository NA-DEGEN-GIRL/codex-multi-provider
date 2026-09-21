# SSH project sidebar cache reconciliation (revision 84)

## Problem and evidence

Some profiles showed several old task titles under an SSH project while other
profiles showed only its current task. Project declarations and remote paths
were identical. Read-only requests to four independently running remote
app-servers returned the same complete, ordered list of 95 records. Four extra
IDs in the affected Windows sidebar caches were absent from that list; reading
each old ID resolved to the same current canonical task ID.

The native desktop catalog intentionally retains unseen SSH entries even after
a full scan. Its missing-candidate reconciliation applies to local and ChatGPT
hosts only. The manager previously invalidated local metadata when switching
record identity modes, leaving SSH projection caches from older releases intact.

## Change

On a cold profile launch, the manager rebuilds the sidebar metadata of each
prepared managed SSH host once for the `ssh-canonical-v1` epoch. Host identities
come from that profile's validated bindings. The reset is restricted to the
four native catalog and scan-state tables and increments the native catalog
revision. The desktop then reads the current list from its own remote connection.

Each database is backed up first. Local cache identity and SSH epochs are tracked
independently, so changing a local configuration does not repeatedly reset SSH
caches. Newly enrolled hosts receive their own first reset. The marker preserves
the old local-signature format for older running manager services.

No remote files, conversation histories, credentials, project declarations, pins,
or other hosts are removed. This is a metadata rebuild, not task deletion. Old
task links remain resolvable by the server. An unavailable host must reconnect
before its metadata can be populated again. Showing an already running profile
does not touch its database.

## Verification

Tests cover host scope, one-time behavior, later host enrollment, preservation of
local and unrelated host data, old marker compatibility, backups, malformed
schemas, and shared database rejection (19 tests). The adjacent local-first
remote launch suite also passed (29 tests).

An idle affected profile was cold-relaunched after the scoped metadata migration.
Its real desktop repopulated 95 remote catalog rows; the sorted ID digest matched
the remote app-server response exactly. All four stale projection IDs were
absent. Three SSH connections reached ready, their remote process identities
were unchanged, and other profiles' process IDs were unchanged. No local catalog
rows were removed. The active conversation's profile was left running and still
needs its first cold launch with the updated manager backend.

Release 84 was built; the source privacy scan covered 514 files with no findings.
An additional disposable SQLite simulation was used during investigation; it is
not an Electron UI test and is not counted as live desktop validation.
