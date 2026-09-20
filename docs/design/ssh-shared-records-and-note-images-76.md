# Revision 76: editable SSH records and images in task notes

## SSH record continuation

The SSH launcher enabled the shared catalog but omitted shared execution and its
writer identity. Consequently a legacy task could be listed and read, then reject
`turn/start` as a read-only catalog record. The desktop launcher already separated
shared execution from the older exclusive ownership/handoff manifest.

The SSH launcher now enables shared execution only when the pinned native binary
contains the execution, writer identity, and shared routes capabilities. A worker
uses its own profile's authentication/configuration and writer UUID. Record access
still requires the registered source catalog and native path validation. The older
ownership manifest is omitted in this mode. Explicit catalog viewers remain read
only; older binaries receive no shared write authority.

Legacy account setups can contain several physical copies with the same task ID.
Before selection these remain distinct projection links. An explicit resume or
turn on a saved link selects its validated source. Background reads do not choose
a source. A bounded, locked, atomically published host-local
`shared-record-routes.json` keeps the first selection stable across profiles and
restarts. Lists then expose that canonical record and suppress stale duplicates.
A missing selected source produces an error instead of silently substituting a
different copy. Existing copies are never merged, deleted, or overwritten.

Remote note documents created against an old projection ID remain usable after
the conversation becomes editable under its canonical ID. The manager derives the
alias from its existing SSH metadata cache and reuses the old note document,
including revisions and image references. It does not rewrite note bodies. An
existing canonical note document wins. Multiple old documents with notes produce
an explicit conflict rather than hiding one. Both catalog (`ssh:`) and desktop
(`remote-ssh-discovered:`) host identifiers are supported without crossing hosts.

## Notes image interaction

- Paste with Ctrl+V or **붙여넣기**; ordinary text paste remains unchanged.
- **+ 이미지** imports PNG, JPEG, BMP, or GIF (first frame).
- Small wrapping thumbnails sit below the text/checklist. Portrait and landscape
  images occupy the same compact tile area.
- Click a tile for a fit-to-window preview; **원본 크기** enables scrolling at full
  size. Escape closes it.
- **이미지 복사** in the preview or thumbnail context menu supplies both original
  PNG and native Bitmap clipboard formats, suitable for pasting into chat.
- Remove detaches the image only from that note.

Images are immutable content-addressed PNG files in the private, ignored
`work/control-center/note-images` store. Notes contain hash/dimension references,
not base64 data. Imports, decoding, and thumbnail work run off the UI thread.
Limits are 24 images per note, 16 MiB per encoded image, and 40 million pixels.
New references are validated for hash, PNG dimensions, and bounded file size.

Shared notes use the same references. Explicit note separation copies references;
removing an image from one independent note leaves other notes intact. Deleted
notes and unsaved recovery drafts retain their image references. Files are retained
for restore/recovery; automatic removal of unused image files is not included.

Image-bearing documents use schema 3 and shared groups use schema 2, so old
services fail closed instead of dropping unknown attachments. Older clients can
edit text through the new service without deleting omitted image fields. The
shell checks `image_attachments_version` before offering edits, and preserves
attachment recovery drafts if connected to an older service.

## Verification and activation

- Python launcher and note-identity tests cover capability checks, inherited
  authority removal, profile isolation, alias reuse/conflicts, and host isolation.
- Rust note tests cover persistence, revisions, old-client text edits, shared and
  independent notes, invalid references, deletion, and restore.
- WPF notes tests use the actual Rust named-pipe service and test thumbnail layout,
  full-resolution preview, PNG/Bitmap copy data, and durable recovery. Screenshots
  are generated from synthetic notes and images.
- App-server unit tests cover stable source selection, concurrent selection, and
  corrupt routing state alongside existing catalog tests. Tests run without the
  parent app's shared-record environment flags.
- `scripts/test_remote_shared_execution_live.py` uses temporary Linux homes,
  duplicate synthetic records, and a loopback model fixture. It reproduces the old
  rejection and verifies two profiles append to the selected original while
  credentials, configuration, and the other copy remain unchanged. It sends no
  real provider requests.

The change includes a new manager service and a matching Linux runtime artifact.
Existing work is not stopped during development or publication. Apply after work
finishes by fully exiting the workspace and reopening it; keeping the old service
alive does not activate the new notes capability or replace SSH workers.

A stock SSH CLI warning that its background service is older is a separate
installation issue. `codex app-server daemon version` reports those versions.
The stock CLI's `codex app-server daemon update` restarts that service and may
interrupt its work; it is not run automatically by this change.
