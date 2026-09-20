# Profile preparation contention and stale notices (revision 71)

## Problem

A foreground profile selection can overlap the startup warmup for the same
profile. The SSH-aware opening path holds the profile's restart claim while
waiting for the new desktop window. Only after that wait does it publish the
background SSH reconciliation job that another request can join.

The second caller previously used a nonblocking claim and failed before reaching
the existing-job branch. The shared lock helper reported this as another update
being in progress, even though the operation was normal profile preparation.
Once the background open completed, selecting an already attached window could
succeed without replacing the old sidebar error, leaving a misleading warning.

The reported occurrence had completed SSH preparation and matching desired and
launched policy revisions by the time it was inspected. No persistent update
maintenance fence remained. No live profile was closed during diagnosis.

## Change

- Same-profile restart-claim contention waits for the owner to publish its
  result, with a bounded and cancellable wait. State is read again after the
  claim is acquired, so the foreground request reuses the existing launch/job.
- A preparation timeout reports profile preparation, rather than claiming a
  separate update exists. Actual maintenance checks still run and are not
  retried or bypassed as ordinary claim contention.
- Claim acquisition precedes the service's restart-state mutex on both paths.
  This avoids a lock-order deadlock between opening a profile and scheduling a
  policy change, and leaves other profiles free to schedule work.
- The shell tracks transient profile-open notices with their profile and error
  code. A verified successful attachment/presentation can retire the matching
  notice. It preserves unrelated errors and unresolved restart/maintenance
  failures; historical events remain in the log.

## Validation

Fixture tests cover concurrent same-profile opens, policy scheduling during an
open, timeout/cancellation and real maintenance rejection. UI fixtures exercise
notice recovery and retention without starting installed Codex profiles or
changing their selection. The normal manager build and relevant existing
restart/admission checks are also run.
