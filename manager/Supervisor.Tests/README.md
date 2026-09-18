# Supervisor transport checks

This standalone console suite uses the real current-user named pipe supervisor and a synthetic Python backend in a unique temporary directory. It never starts the actual manager backend, original Codex, SSH connections, model calls, or package updates.

Run from the repository root:

```powershell
dotnet run --project manager/Supervisor.Tests/Codex.ControlCenter.Supervisor.Tests.csproj -c Release
```

Add `-- --idle` to test the real 60-second inactivity policy (up to 90 seconds). The suite checks bounded UTF-8 JSONL framing, malformed protocol rejection, simultaneous clients, startup singleton behavior, disconnected and cancelled mutation response draining, tracked process survival, fault recovery without command replay, and optional idle shutdown.

The synthetic tracked process only sleeps. Cleanup terminates the exact fixture supervisor/backend/child PIDs, never a process name or entire process tree, and deletes only the validated temporary fixture directories.
