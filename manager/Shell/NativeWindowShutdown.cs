using System.Diagnostics;
using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// Where a requested quit stands when the full-exit wait ends.
internal enum NativeExitOutcome
{
    // The process exited.
    Exited,
    // Its window is gone but the process is still in its quit cleanup. Electron
    // closes the windows of a quitting app first. A second 완전 종료 press
    // treats such a window as closed and reaps the profile's leftovers.
    Quitting,
    // Its window is still there: the app has not begun to quit.
    Unresponsive,
}

internal static class NativeWindowShutdown
{
    // 26.930's quit path runs its own cleanup (app-server shutdown,
    // startup-requirement session) and often needs more than 15 s (rev 116:
    // 60 s). Apps that quit together share CPU and a pressured commit charge:
    // 11 at once needed about 80 s. Beyond three apps each adds 10 s, up to 3 min.
    internal static TimeSpan GracefulWait(int apps) =>
        TimeSpan.FromSeconds(Math.Min(180, 60 + 10 * Math.Max(0, apps - 3)));

    // An app whose window already closed gets this much longer before the
    // leftover cleanup reaps it, at least the time a person took to press
    // 완전 종료 again.
    internal static readonly TimeSpan QuitGrace = TimeSpan.FromSeconds(20);

    internal static string Progress(int exited, int total, TimeSpan elapsed, TimeSpan limit) =>
        elapsed < limit
            ? $"관리 중인 Codex 종료를 기다리고 있습니다 · {exited}/{total} 종료됨 · {(int)elapsed.TotalSeconds}초 (최대 {(int)limit.TotalSeconds}초)"
            : $"창을 닫은 Codex의 종료 정리를 기다리고 있습니다 · {exited}/{total} 종료됨 · {(int)elapsed.TotalSeconds}초";

    // Requests every quit at once under one deadline and reports progress
    // until all have settled. `exited` runs as each exit is confirmed, on the
    // caller's synchronization context. A request that throws is returned with
    // its error and never counts as exited.
    internal static async Task<IReadOnlyList<(T Target, NativeExitOutcome Outcome, Exception? Error)>> WaitAllAsync<T>(
        IReadOnlyCollection<T> targets, TimeSpan limit,
        Func<T, CancellationToken, Task<NativeExitOutcome>> request,
        Action<T> exited, Action<int, TimeSpan> progress, TimeSpan? tick = null)
    {
        using var deadline = new CancellationTokenSource(limit);
        var watch = Stopwatch.StartNew();
        int done = 0;
        async Task<(T, NativeExitOutcome, Exception?)> One(T target)
        {
            NativeExitOutcome outcome;
            try { outcome = await request(target, deadline.Token); }
            catch (Exception error) { return (target, NativeExitOutcome.Unresponsive, error); }
            if (outcome == NativeExitOutcome.Exited)
            {
                Interlocked.Increment(ref done);
                exited(target);
            }
            return (target, outcome, null);
        }
        var all = Task.WhenAll(targets.Select(One).ToArray());
        while (!all.IsCompleted)
        {
            progress(Volatile.Read(ref done), watch.Elapsed);
            await Task.WhenAny(all, Task.Delay(tick ?? TimeSpan.FromSeconds(1)));
        }
        return await all;
    }

    // Called only for a shell-owned, lifetime-verified HWND. Hold the process
    // handle across the await so PID reuse cannot turn success into a new target.
    internal static async Task<NativeExitOutcome> RequestAsync(string root, int pid, nint hwnd, string executable,
        long expectedCreated, CancellationToken deadline, TimeSpan quitGrace)
    {
        Process process;
        try { process = Process.GetProcessById(pid); }
        catch (ArgumentException) { return NativeExitOutcome.Exited; }
        using (process)
        {
            _ = process.Handle;
            if (process.HasExited) return NativeExitOutcome.Exited;
            if (expectedCreated != 0 && process.StartTime.ToUniversalTime().ToFileTimeUtc() != expectedCreated)
                throw new InvalidOperationException("Codex process lifetime changed before shutdown.");
            if (!string.Equals(process.MainModule?.FileName, executable, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Codex process identity changed before shutdown.");
            NativeWindowInterop.GetWindowThreadProcessId(hwnd, out var ownerPid);
            if (ownerPid != (uint)pid)
                throw new InvalidOperationException("Codex window owner changed before shutdown.");
            var directory = Path.Combine(root, "work", "control-center", "window-hosts");
            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, $"{pid}.json");
            var token = Guid.NewGuid().ToString("N");
            var temporary = path + "." + token;
            try
            {
                File.WriteAllText(temporary, JsonSerializer.Serialize(new {
                    version = 1, appPid = pid, hwnd = hwnd.ToString(), shellPid = Environment.ProcessId,
                    token, mode = "shutdown", visible = false
                }));
                File.Move(temporary, path, overwrite: true);
                if (await ExitsAsync(process, deadline)) return NativeExitOutcome.Exited;
                // Our HWND still belongs to this process: the quit never began.
                if (NativeWindowInterop.IsWindow(hwnd) &&
                    NativeWindowInterop.GetWindowThreadProcessId(hwnd, out var owner) != 0 && owner == (uint)pid)
                    return NativeExitOutcome.Unresponsive;
                using var grace = new CancellationTokenSource(quitGrace);
                return await ExitsAsync(process, grace.Token) ? NativeExitOutcome.Exited : NativeExitOutcome.Quitting;
            }
            finally
            {
                if (File.Exists(temporary)) File.Delete(temporary);
                // Never remove a replacement host's command.
                try
                {
                    using var document = JsonDocument.Parse(File.ReadAllText(path));
                    if (document.RootElement.GetProperty("token").GetString() == token)
                    { File.Delete(path); File.Delete(path + ".render.json"); }
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { }
            }
        }
    }

    private static async Task<bool> ExitsAsync(Process process, CancellationToken deadline)
    {
        try { await process.WaitForExitAsync(deadline); return true; }
        catch (OperationCanceledException) when (deadline.IsCancellationRequested) { return process.HasExited; }
    }
}
