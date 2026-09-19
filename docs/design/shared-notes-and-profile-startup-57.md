# Shared fork notes and profile startup recovery

Conversation forks now share their parent's saved notes by default. The
`메모 분리` action copies the current notes into an independent document only
for the selected task. Empty groups are shared too; edits, new notes, checklist
changes, deletion and restoration use the same revision checks across members.
Existing standalone documents are preserved. See
[native note sharing](native-fork-note-copies.md) for first-access semantics.

The UI distinguishes shared and independent notes and permits separating empty
groups. It drains pending saves and prevents editing during separation;
deletion/restoration must finish before separation can start. Visible idle
panels refresh saved changes without replacing input or drafts. Late reads are
discarded if the selected task or content changed while the request ran.

Two separate profile startup failures were investigated:

- A valid configuration with a provider BEGIN comment but no END comment was
  rejected before process creation. Recovery now requires a recognized generated
  table and verifies that parsing the result preserves every other setting.
  Invalid TOML and other broken marker arrangements still fail without writes.
- Desktop preference merging could remove an unrelated END comment when a native
  editor placed a desktop table inside the generated block. Standalone comments
  and blank lines are now retained, except the desktop writer's own markers.

The reported configuration predates the latest startup change. The desktop
writer issue was reproduced independently; it is a confirmed corruption path,
not proof that it was the historical writer of that particular file.

Profile warmup and foreground launch also competed for a nonblocking global
launch fence. Acquisition now waits for at most ten seconds, then reports
profile-launch contention specifically. The existing global fence, maintenance
ownership checks and process identity checks remain in force. Launch guards
are evaluated after the fence is acquired, so queuing cannot bypass an update.

DeepSeek implemented the note storage and configuration repair changes. GPT
independently specified regression tests, reviewed the changes, and implemented
the UI, launch admission waiting and integration.

Validation: 977 manager Python tests (one environment-dependent skip), 23 Rust
service tests, and the packaged Windows self-tests passed. The real WPF notes
fixture covers 40 checks, including shared refresh, explicit separation,
pending saves/restoration and stale reads. Configuration recovery was also
verified in memory against the affected configuration without writing it.
Live profiles and SSH sessions were not restarted for validation.
