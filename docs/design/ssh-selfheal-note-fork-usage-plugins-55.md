# SSH self-heal, shared fork memos, quota details and plugin long paths

The memo design below describes the earlier implementation. Native fork
discovery and independent copies now follow [Native fork note copies](native-fork-note-copies.md).

## SSH policy deadlock

Changing a profile's model policy (for example switching profile 02 to an
external-only subagent policy) marks every prepared host as
`pending_policy_hosts`. The shim refused those hosts with
"This host must apply the selected profile model settings before SSH
reconnects" and never entered the scoped auto-prepare path, which only ran for
hosts with no binding at all. A reboot or a failed maintenance pass therefore
left SSH permanently blocked, which is why a remote project and
its sessions disappeared from the profile list even though the catalog cache
and the remote threads still existed.

The shim now routes a stale host through the same auto-prepare path when the
alias is one of the saved managed connections, and the auto-prepare writer also
removes the alias from `pending_policy_hosts`. Hosts without a saved alias keep
the explicit pending-policy error because their remote launcher arguments
cannot be reconstructed automatically. No remote file is deleted by this
change; it only re-applies the current profile settings before the SSH
connection is used.

## Forked conversations share their memo

Task notes used to be stored per `(host, thread)` document, so a forked
conversation started with an empty memo. Notes now support a group document
under `work/control-center/note-groups/<group>.json`. The manager publishes
`thread_spawn_edges` as `work/control-center/note-forks.json`, and the note
service makes a new task adopt its parent's group, migrating the parent's own
notes into a group on first use. Both tasks then edit one document.

`notes.fork` copies the current notes into a fresh group with new note and item
ids and rebinds only the requesting task. The panel shows a `메모 fork` button
while the task shares a memo, and hides it afterwards. Splitting never modifies
the source task's memo.

## Weekly quota detail

`account/rateLimits/read` already returns `resetsAt` per window and
`rateLimitResetCredits.availableCount`. The normalizer keeps the weekly reset
timestamp and a sanitized credit count (never credit ids), and the profile card
now shows `주간 46% 남음 · 9/21 05:00 초기화 · 초기화권 2회` when those values
exist. External API profiles keep their existing label because the data is not
part of the Codex quota reply.

## Plugin mirrors and Windows path length

The shared plugin marketplace is mirrored into every managed home, including
external API profiles. Copying into
`<home>\plugins\cache\codex-manager-shared\<plugin>\.stage-<32 hex>` plus the
plugin's own nested files exceeded the classic Windows MAX_PATH limit for the
external profile home, so the copy raised `WinError 206` and aborted the whole
home's synchronization. Newly installed plugins (for example GitHub) therefore
never reached that profile while older mirrors stayed behind.

The copy, publish and removal helpers now use extended-length `\\?\` paths for
the concrete file operations while confinement checks keep the ordinary path,
so the deep tree stays inside the managed directory. A forced reconcile then
applied all seven shared plugins to the external profile with no errors.
