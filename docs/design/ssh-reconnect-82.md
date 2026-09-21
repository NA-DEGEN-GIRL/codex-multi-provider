# SSH reconnect and stale update reservations (revision 82)

## Symptoms and cause

Some profiles could reconnect while others repeatedly reported that SSH execution
state could not be verified. One host's failure held the profile-wide maintenance
gate, so even its healthy hosts failed the native adapter's settings wait.

Three distinct states were involved:

1. Shared-record execution intentionally omits the older exclusive source
   manifest. Its runtime can therefore reject `thread/managedIdleStatus` with
   `managed idle status is not enabled for this instance`. This is a missing idle
   capability, not proof that the listener is broken or safe to stop.
2. A live descriptor can have a different revision identifier while containing
   the same generated configuration and runtime bundle. Requiring the desired
   descriptor identifier for every reconnect unnecessarily starts maintenance.
3. A queued SSH update can retain a previous desktop generation after relaunch.
   Its reservation then blocks native SSH attachment even if no stop/start was
   dispatched. An SSH-only journal could also be retired by desktop restart
   recovery while its SSH gate remained in attention.

## Required behavior

- Preserve shared-record routing. Do not enable the old exclusive source mode
  to make its maintenance RPC succeed.
- Classify known missing-capability responses explicitly. Read-only observation
  can report a verified process identity, but always retains `idle=false` and
  never supplies an exit proof.
- Reuse an unchanged connection without stopping its remote process. Adopting
  a different live descriptor requires matching host identity, runtime bundle
  and the exact generated immutable configuration file set and hashes.
- Recheck generation, policy/settings and published bindings before committing
  adopted metadata. Unknown identity, changed configuration, missing hosts and
  partial lifecycle journals must not bypass normal maintenance.
- Retire an outdated update reservation only when the matching preserved journal
  proves that no lifecycle change was dispatched. Observed preflight records are
  allowed; uncertain stop/start evidence remains blocked. Keep auto-update
  preferences and let future checks reconcile the current generation.
- SSH-only journals belong to SSH reconciliation, not desktop restart recovery.

## Validation

Focused tests cover capability classification, unchanged/equivalent live reuse,
configuration mismatch, concurrent metadata changes, stale-generation reservation
cleanup, partial lifecycle evidence and cross-process admission. Live verification
must compare identities before/after and inspect adapter authentication readiness;
unit-test success alone does not establish that a user's SSH UI has recovered.

No separately installed terminal Codex daemon is owned by this recovery path.

## Verified result

- 212 focused Python tests passed across the SSH maintenance, update queue,
  restore, reconnect, native shim and shared-execution modules. An existing
  standalone test requires `PYTHONPATH=scripts`; it passed with that environment.
- `scripts/build-manager.ps1 -SelfTest` completed for revision 82, including
  Windows UI and SSH update-panel fixtures. These fixtures use synthetic data.
- Live recovery retired one obsolete reservation and adopted one equivalent
  descriptor. The existing app then reported all nine SSH connections across
  three profiles authenticated and ready. The six remote process identities
  checked on the two affected profiles were unchanged; no stop/start was issued.
- Current connections were recovered without replacing the running manager.
  The durable code fix is packaged in revision 82 for the next manager update.
