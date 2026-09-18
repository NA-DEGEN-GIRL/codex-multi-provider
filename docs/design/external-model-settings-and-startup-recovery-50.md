# External model settings and startup recovery (50)

The installed desktop changed from 26.911.7940.0 to 26.915.3509.0 across the reported reboot. The profile failed before launching a process because the manager could not match the renamed route-context bindings. No account or conversation reset is needed for this error.

## Desktop preparation

- The 26.915 main/renderer route, history, browser-helper and reasoning-picker entry points are verified before publication. Installed assets, signatures and fuses remain unchanged.
- Preparation checks compatibility before copying program assets. Publication has an OS-owned interprocess lock, a private staging directory, a file inventory and hashes for the executable, Chromium and patched archive. The final rename publishes only a complete copy.
- An incomplete staging directory left by a power loss cannot be selected. A damaged published copy is preserved under an invalid name and rebuilt from installed assets. Account/history directories are not part of this operation.
- If a later installed package has unrecognized entry points, a previous complete copy may be used only with exactly matching manager adapters and verified program files. The UI identifies the selected version and compatibility status. An unverified stock app is never substituted for a managed profile.
- The synced-original launcher uses the same publication/recovery mechanism.

## External primary profiles

`프로필 추가 → 외부 API` asks for the primary model and its actual reasoning effort, context size and compaction percentage. Existing profiles expose `외부 모델 설정` in profile management and the profile context menu. Saved values are per profile; editing one profile does not edit another.

Known DeepSeek Flash/V4 models expose `none`, `low`, `high`, `max`. Compatibility aliases are normalized at the request boundary (`medium`/`xhigh` to `high`, `ultra` to `max`, `minimal` to `low`). The desktop picker shows the provider catalog instead of filtering it through GPT's default efforts. Explicit native task settings persist; omitted turn settings do not reset them.

Context is bounded by the registered model's capacity. Auto-compaction is configurable from 10% through the native runtime's 90% ceiling. At 1,048,576 tokens, 85% gives 891,289 tokens. The UI previews the resulting threshold. Compaction runs at request boundaries, not at each character entered.

Provider/model registration supports supported-effort lists, model capacity and a default compaction percentage, including later edits. A new provider must still pass the existing connection verification before use.

## Subagent selection

The external-agent settings allow multiple checked models and two modes:

- Automatic: the agent chooses native GPT or selected external roles when delegation is appropriate.
- External only: every spawned/resumed subagent must resolve to one of the selected external provider/model pairs. The Rust runtime enforces this independently of prompt wording. A role belonging only to the primary model is not advertised as an enabled subagent role.

No delegation is forced merely by enabling these settings. External role configuration includes its own context and compaction settings. SSH preparation carries the same policy and per-profile primary settings, and stale bindings are invalidated when these options change.

## Validation

- Windows and Linux native runtime fixtures: max/high/low wire effort, persistent task settings, 1M context and 85% compaction; all passed with loopback responses, zero paid model calls.
- Native shared-storage and shared-editing fixtures: one conversation store, destination credentials/model/effort and project membership; passed.
- Actual 26.915 desktop fixture: live visible-renderer history, unsent draft preservation, foreign-provider resume, archive/restore, shared SSH project addition/removal, and local project sidebar creation/edit/removal; passed.
- Publication tests: copy interruption, orphan staging directory, corrupt/missing assets, exact-adapter fallback, and unknown implementation rejection.
- Eight Rust cross-provider integration tests passed, including external-only execution and native-child rejection. A full core run was interrupted by the reboot; its observed cold-resume failure passed when rerun individually. No claim of a completed full-workspace regression run is made.

Windows evidence: `artifacts/results/model-settings-c077c882`, `shared-editing-e92fadce`, `canonical-store-d9483195`, `desktop-sync-d1c3b267`, `desktop-sync-7120062f`. Linux evidence: `artifacts/results/linux-model-settings-c979d375`. Remote GUI sessions and paid provider responses are not covered by these fixtures.
