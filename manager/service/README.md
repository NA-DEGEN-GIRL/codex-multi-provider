# Windows management service

`codex-workspace-service.exe` owns the current-user named pipe, request correlation,
profile process lifecycle, and task notes. The WPF shell uses IPC version 27.
`scripts/control_center.py --serve` remains the compatibility adapter for existing
account/configuration/catalog/SSH/update policies. It delegates OS process actions
to a private, token-authenticated service channel. The token is never inherited by
managed Codex processes.

Notes and checklists are keyed by `(host_id, thread_id)`, independent of account or
title. Writes use revisions and atomic file replacement. Deletion is a tombstone.
The shell retains unsaved drafts and preserves conflicting edits in a recovery tab.

The service does not replay timed-out mutations. A newer shell with the same IPC
version reconnects to the existing service while profiles are alive, deferring
service replacement until the profiles exit. Ordinary shell close disconnects
only; the explicit full-exit action stops managed profiles and retires the service.
An unused service exits after a 60-second grace period only when all profiles are
known to have exited and management jobs are idle. An unknown process state is
not proof of exit. This preserves the adapter's SSH forwards and background work.

Build and checks on Windows:

```powershell
cargo fmt --check --manifest-path manager/service/Cargo.toml
cargo build --locked --manifest-path manager/service/Cargo.toml --bin process-fixture --features process-fixture
cargo test --locked --manifest-path manager/service/Cargo.toml --features process-fixture
cargo clippy --locked --manifest-path manager/service/Cargo.toml --all-targets --features process-fixture -- -D warnings
cargo build --locked --release --manifest-path manager/service/Cargo.toml
dotnet run --project manager/Supervisor.Tests/Codex.ControlCenter.Supervisor.Tests.csproj -- --idle
```

The process fixture is a hidden, temporary test executable that does not open
Codex, accounts, or user windows. `scripts/build-manager.ps1 -SelfTest` publishes a
new release only after native-window, caption, notification, and WPF notes tests
pass. `Supervisor.Tests` retains its directory name but tests the Rust service.

See [design and verification](../../docs/design/rust-manager-and-task-notes.md).
See [workspace lifetime and UI updates](../../docs/design/workspace-lifetime-73.md)
for close/reopen semantics and the limits of runtime replacement.
