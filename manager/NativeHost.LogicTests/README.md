# Native host lifecycle regression tests

Run `dotnet run --project manager/NativeHost.LogicTests/Codex.ControlCenter.NativeHost.LogicTests.csproj -c Release` from the repository root.

The project compiles the **production NativeWindowHost.cs** against deterministic
WPF and Win32 stand-ins. It does not reference WPF, run a native window API, create
windows, enumerate the desktop, inspect credentials, or interact with Codex.

Callbacks are delivered in the middle of parent/style/size transitions, including
the log-triggered WPF layout seen in the user's 03 failure. The original code
reproduced the same `The external window changed its parent` error in the first
two cases. Original results: 4/9 passing. After the guard and the additional nested
operation test: 10/10 passing.

The tests also preserve genuine parent-loss detection, window-lifetime checks,
rollback on rejected attachment, and ownership after a failed detach. These are
control-flow tests; the stand-ins do not establish actual Windows/Electron behavior.

The existing `--native-host-self-test` was separately updated to force real WPF
layout before SetParent. It requires an authorized GUI test run. This phase did
not execute it because desktop automation remains paused after Escape.
