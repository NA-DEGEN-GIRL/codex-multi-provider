# Profile launch latency, revision 66

Revision 65 reduced synchronization overhead, but did not establish faster
interactive startup. A subsequent real startup trace identified a different
critical path: a selected profile waited about 18 seconds to appear, including
13.2 seconds waiting for the preparation gate. Each profile spent 2.6–3.6 seconds
in desktop package discovery and validation. Prepared-profile switch handlers
were generally 0–31 ms; that metric does not measure the final rendered frame.

## Changes

- Desktop validation still checks every listed program file's metadata on each
  use. Parent containment is resolved once per distinct parent instead of twice
  per file. Nonregular files, reparse-point files, escaping paths and hashes for
  unchecked files are rejected.
- Program digests are reused only within the service process, using the open
  file's identity, size, last-write time and content-change time. Windows uses
  `FileBasicInfo.ChangeTime`, because Python's Windows ctime denotes creation.
  Restoring mtime after an edit therefore does not reuse the old digest. An
  unavailable change stamp disables reuse; a stamp change while hashing rejects
  that validation. The cache holds at most 64 small digest entries, not file data.
- Installed-package discovery is reused for at most five seconds per Instances
  service, across profile preparation and SSH environment construction. Missing
  executables invalidate it immediately. Each caller receives its own dictionary.
  Update checks still use the original uncached package discovery API.
- The preparation queue now prioritizes the clicked profile even when its
  warmup worker is already waiting for launch admission. Running preparation is
  not interrupted. Other requests preserve FIFO order, queued maintenance remains
  a fence, and cross-process update locks and lifetime checks remain in place.
- The shell logs profile-list, shortcut-list, task-context, diagnostic-processing,
  selected-window and background-window work exceeding 25 ms. Periodic shell
  render operations of roughly 150–250 ms still need attribution from this finer
  trace; this revision does not claim to fix them.

## Read-only measurement on installed program assets

The previous and current validation functions checked the same isolated desktop
copy in separate module scopes without launching or manipulating a user app:

| Measurement | Previous | Revision 66 |
| --- | ---: | ---: |
| First check in that module | 1,299 ms | 715 ms |
| Three subsequent checks | 1,210 / 1,493 / 1,498 ms | 228 / 252 / 218 ms |

The package contained 3,110 listed files. Its three content-hashed files totalled
about 693 MB. These numbers measure validation only; file-cache state and host
load affect them. They are not end-to-end startup or CPU/memory improvement
claims. Full startup needs a new user run with the new release.

Regression coverage includes unchanged files, equal-size edits with restored
mtime, atomic replacement, unsupported change stamps, changes during hashing,
missing/corrupt packages, package-discovery expiry, caller isolation, priority
among already waiting workers, missing priorities, reentrancy, maintenance order,
shutdown admission and preservation of existing windows.

Validation completed: 1,020 manager tests succeeded with one platform-dependent
skip; release build, native shell self-tests and the packaged notification
callback fixture passed. The published runtime contains the exact tested source.
The source/privacy inventory and Gitleaks scan found no new private data or
detected secrets. Existing user sessions were not restarted or navigated.

The Windows timestamp fields are documented in Microsoft's
[FILE_BASIC_INFO reference](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info).
