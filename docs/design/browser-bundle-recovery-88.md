# Revision 88: prepare the selected desktop's Browser plugin

## Observed failure

Browser initialization failed before website navigation with `Cannot find module`
for a versioned `scripts/browser-service.mjs` under a managed profile's plugin
cache. The desktop package contained Browser `26.915.31945`, including that file,
while the affected profile only contained Browser `26.911.61220`. Other active
profiles also lacked the version requested by their running desktop. This was
not evidence of a ChatGPT authentication failure.

The desktop and managed CLI have separate release lifecycles. Browser's trusted
service configuration can reference the desktop's current version even while
the profile's materialized plugin cache is older. The exact historical event
that left the old cache in place was not established. Account plugin sharing
intentionally excludes app-bundled marketplaces; widening that mirror to share
them across accounts would not guarantee a match with each running desktop.

## Change

`scripts/manager_core/browser_bundle.py::ensure(home, executable)` reads Browser
from the selected executable's `resources/plugins/openai-bundled/plugins/browser`.
It validates the manifest, entry points, and bounded file inventory, then prepares
the exact manifest version in `<home>/plugins/cache/openai-bundled/browser/<version>`.

- `Instances._show` prepares the chosen desktop before a new profile starts. For
  an existing window it schedules a background check of the actual executable
  (`ensure_later`, revision 119) and requests the window without waiting for it.
- `ControlCenter._open_profile_locally` schedules the same background check for an
  already-running profile's actual executable during selection, without launching
  a second Electron instance.
- `start_synced_original.launch` also prepares the selected original-sync desktop
  before launching it. Its read-only `--check` remains read-only.
- Versions come from the selected executable, including compatibility fallback,
  rather than from the newest installed package or another profile's cache.

New versions are copied to a uniquely named staging directory, verified by SHA-256
against both the source and the staged contents, and published with a directory
rename. Existing incomplete versions are repaired by atomic replacement of only
package-declared changed files; since revision 119 only those files are staged.
Staging uses `<home>/plugins/cache/.manager-stage-<id>`, outside the Browser
folder, where every folder is read as a version. A per-home OS lock coordinates
concurrent requests and is released on a crash. Links/junctions and escaping paths are rejected; Windows
extended paths support long profile and dependency paths. Existing versions and
unowned extra files are retained. A later attempt can resume an interrupted repair.

The change does not read/copy credentials, browser sessions, browser data,
account grants, or preferences, and does not modify permission policies. It does
not replace the official installed app or restart any profile. A desktop edition
without a Browser bundle remains launchable. A present but incomplete/invalid
bundle is reported rather than silently certified as ready.

## Revision 119: running profiles are checked in the background

A repair can rewrite hundreds of files, so selecting a running profile or opening
its task no longer waits for the check or fails on it.

- `browser_bundle.ensure_later` runs `ensure` on a daemon thread, one job per
  profile home. A request that arrives while the job runs queues one more pass
  with the newest arguments, so a change after the running pass's check is seen.
- A background pass is written to `profile-launch.performance.jsonl`
  (`browser_bundle_background`) only if it failed, changed files or took at least
  1 s. A failure is recorded by its exception class only, because the message can
  carry paths. A lasting failure is therefore no longer shown when a running
  profile is selected; the next launch of that profile still reports it.
- A launch keeps the synchronous `ensure`. It first calls `browser_bundle.settle`,
  which drops a queued pass and waits up to 60 s for the home's running job.
  Otherwise the launch would wait 10 s for the Browser lock and then fail.
- Under the Browser lock, `ensure` removes `.manager-stage-*` folders left in
  `plugins/cache` by a job the manager exit interrupted, and in the Browser folder
  by older managers. A folder that contains a link, or cannot be removed yet, is
  left for a later call. Nothing else stages there, and every Browser stage of the
  home is made under that lock.
- A repair is remembered with the stamps of its final check. Files and folders
  that changed less than 1 s before are proven by content and listing again on
  later calls until their stamps settle.

## Existing running profiles

For an old persistent backend that does not yet contain this fix:

```powershell
python -X utf8 scripts/repair_browser_plugins.py --root . --all-running
# Or restrict recovery to one managed profile:
python -X utf8 scripts/repair_browser_plugins.py --root . --profile <profile-id>
```

The utility checks the stored PID, creation time and executable against the live
process. It limits recovery to the matching managed profile home and the actual
managed desktop; a stopped process or reused PID is skipped. It performs no
restart, navigation, setting change, or remote operation.

An automation helper that already cached a failed module import may need its
browser tool connection reinitialized. That is distinct from restarting the
Codex profile or interrupting a task. Do not reset browser user data to fix this.

## Validation and limits

On 2026-09-23, the utility prepared the required 382 program files for all eight
verified running profiles, with zero profile restarts. Through the supported
in-app Browser tool, a fresh connection succeeded and navigation to
`https://example.com/` returned the expected `Example Domain` heading and page text.
The existing older browser client could use the repaired service. The first cold
connection timed out; the next initialization succeeded without further file changes.
Healthy bundle verification was measured at approximately 254 ms on this host.

Focused automated tests cover version selection, preservation of existing assets
and account files, missing/corrupted files, interrupted copies/repairs, source
changes, concurrent preparation, long paths, links, malformed manifests, and PID
reuse. These use temporary fixtures and do not contact accounts or start Codex.
All 98 focused Python checks passed (15 Browser bundle checks plus related launch,
login, original-sync and release checks). The Windows release build
`20260922-230711-894` succeeded and is selected for next launch (revision 88, IPC 27).
The source secret scan covered 534 files and reported no findings.

This validation does not establish ChatGPT website login, usage-limit retrieval,
all other accounts' browser navigation, or behavior on another computer. The
automatic launch/reconnect guard applies after the new manager backend is loaded;
the targeted file recovery already fixes the observed missing-module failure in
the existing profiles without replacing that backend.
