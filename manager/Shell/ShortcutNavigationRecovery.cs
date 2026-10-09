using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal enum ShortcutRecoveryResult { Selected, Recovered, Cancelled, Unverified }

// Verify the renderer's selected route, then send at most one viewport-scoped
// navigation. Never relaunch Electron, resume a task, or refresh global state.
internal static class ShortcutNavigationRecovery
{
    internal static bool TryTarget(JsonElement result, JsonElement item, out NoteTask target)
    {
        target = new("local", "");
        if (result.S("state") != "request_sent" || result.B("readonly_viewer") || result.B("readonly_projection")
            || !Guid.TryParse(item.S("thread_id"), out var thread)
            || result.S("thread_id") != thread.ToString()
            || !Uri.TryCreate(result.S("uri"), UriKind.Absolute, out var uri)
            || uri.Scheme != "codex" || uri.Host != "threads" || uri.Fragment != "" || uri.UserInfo != "" || uri.Port != -1
            || uri.AbsolutePath != "/" + thread.ToString()) return false;
        string host = "local";
        var query = uri.Query.TrimStart('?');
        if (query != "")
        {
            var pairs = query.Split('&');
            if (pairs.Length != 1 || !pairs[0].StartsWith("hostId=", StringComparison.Ordinal)) return false;
            host = Uri.UnescapeDataString(pairs[0][7..]);
            if (host.Length is 0 or > 160 || host.Any(c => !char.IsAsciiLetterOrDigit(c) && c is not ':' and not '_' and not '.' and not '@' and not '-')) return false;
        }
        var requestedHost = item.S("host_id", "local");
        if (requestedHost == "local" ? host != "local"
            : host == "local" || result.S("host_id") != requestedHost) return false;
        target = new(host, thread.ToString());
        return true;
    }

    internal static bool SameWindow(JsonElement value, WindowLaunchIdentity launch, long hwnd) =>
        value.N("version") == 1 && value.S("profile_id") == launch.ProfileId
        && value.S("generation") == launch.Generation && value.N("app_pid") == launch.ProcessId
        && long.TryParse(value.S("hwnd"), out var selectedHwnd) && hwnd > 0 && selectedHwnd == hwnd;
    internal static bool Matches(JsonElement value, WindowLaunchIdentity launch, long hwnd, NoteTask task) =>
        SameWindow(value, launch, hwnd) && value.S("thread_id") == task.ThreadId && value.S("host_id", "local") == task.HostId;

    // An SSH task's desktop still loads the thread from its host after the
    // link arrives (the service sent it only once the connection was up): its
    // ordinary chance and the wait after a direct send are longer.
    internal const int LocalChance = 2000, RemoteChance = 5000, LocalSettle = 6000, RemoteSettle = 20000;

    // selectionWait(ms): while attached, wait for the desktop's selected-task
    // file to change (TaskContextWatcher) or ms at most (never over 1 s), instead
    // of re-reading it every 200 ms. Defaults to delay.
    internal static async Task<ShortcutRecoveryResult> RunAsync(Func<bool> current, Func<bool> attached,
        Func<Task<bool>> selected, Func<CancellationToken, Task> send,
        Func<int, Task>? delay = null, Func<long>? now = null, bool remote = false, Func<int, Task>? selectionWait = null)
    {
        delay ??= ms => Task.Delay(ms);
        selectionWait ??= delay;
        now ??= () => Environment.TickCount64;
        var start = now();
        int chance = remote ? RemoteChance : LocalChance, settle = remote ? RemoteSettle : LocalSettle;
        int Left(long until) => (int)Math.Clamp(until - now(), 1, 1000);
        // Give the ordinary deep link a short chance; a cold window gets a
        // bounded attachment wait without launching or polling the service again.
        while (current())
        {
            if (attached())
            {
                if (await selected()) return current() ? ShortcutRecoveryResult.Selected : ShortcutRecoveryResult.Cancelled;
                if (now() - start >= chance) break;
                await selectionWait(Left(start + chance));
                continue;
            }
            if (now() - start >= 25000) return ShortcutRecoveryResult.Unverified;
            await delay(200);
        }
        if (!current() || !attached()) return ShortcutRecoveryResult.Cancelled;
        using var cancellation = new CancellationTokenSource();
        Task sending;
        try { sending = send(cancellation.Token); }
        catch (Exception error) when (Expected(error)) { return ShortcutRecoveryResult.Unverified; }
        var deadline = now() + 8000;
        try
        {
            while (!sending.IsCompleted && current() && attached() && now() < deadline)
            {
                await Task.WhenAny(sending, selectionWait(Left(deadline)));
                if (current() && attached() && await selected())
                    return current() ? ShortcutRecoveryResult.Recovered : ShortcutRecoveryResult.Cancelled;
            }
            if (!current() || !attached()) return ShortcutRecoveryResult.Cancelled;
            if (sending.IsCompleted) await sending;
        }
        catch (Exception error) when (Expected(error)) { /* Delivery may have succeeded without its acknowledgement. */ }
        finally
        {
            cancellation.Cancel();
            // Observe a cooperative sender's late cancellation/failure without
            // retaining the UI action gate or awaiting an unresponsive pipe.
            _ = ObserveAsync(sending);
        }
        deadline = now() + settle;
        while (current() && attached())
        {
            if (await selected()) return current() ? ShortcutRecoveryResult.Recovered : ShortcutRecoveryResult.Cancelled;
            if (now() >= deadline) return ShortcutRecoveryResult.Unverified;
            await selectionWait(Left(deadline));
        }
        return ShortcutRecoveryResult.Cancelled;
    }

    private static bool Expected(Exception error) => error is IOException or UnauthorizedAccessException
        or InvalidOperationException or TimeoutException or OperationCanceledException;
    private static async Task ObserveAsync(Task task)
    {
        try { await task; }
        catch (Exception error) when (Expected(error)) { }
    }
}
