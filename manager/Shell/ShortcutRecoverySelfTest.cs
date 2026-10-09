using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class ShortcutRecoverySelfTest
{
    internal static async Task<List<string>> RunAsync(string root)
    {
        static void Require(bool value, string reason) { if (!value) throw new InvalidOperationException(reason); }
        static JsonElement Json(object value) => JsonSerializer.SerializeToElement(value);
        var checks = new List<string>();
        string profile = Guid.NewGuid().ToString(), generation = Guid.NewGuid().ToString(), thread = Guid.NewGuid().ToString();
        var item = Json(new { thread_id = thread, host_id = "ssh:fixture" });
        JsonElement Result(string uri, bool projection = false) => Json(new { state = "request_sent", thread_id = thread,
            host_id = "ssh:fixture", uri, readonly_projection = projection });
        string link = "codex://threads/" + thread + "?hostId=remote-ssh-discovered%3Afixture";
        Require(ShortcutNavigationRecovery.TryTarget(Result(link), item, out var target)
            && target == new NoteTask("remote-ssh-discovered:fixture", thread), "Exact SSH host was not retained.");
        foreach (var bad in new[] { link + "&hostId=other", link + "#other", link.Replace(thread, Guid.NewGuid().ToString()),
                     link.Replace("codex:", "https:"), link.Replace("threads/", "other/"), link.Split('?')[0] })
            Require(!ShortcutNavigationRecovery.TryTarget(Result(bad), item, out _), "Invalid route accepted: " + bad);
        Require(!ShortcutNavigationRecovery.TryTarget(Result(link, true), item, out _), "Read-only projection was changed into an editable route.");
        Require(!ShortcutNavigationRecovery.TryTarget(Result(link), Json(new { thread_id = thread, host_id = "local" }), out _), "Remote/local route mismatch accepted.");
        checks.Add("Recovery uses the exact server-approved task/SSH host; malformed, duplicate, foreign and read-only routes are rejected.");

        var launch = new WindowLaunchIdentity(profile, generation, 123);
        JsonElement Context(string gen, int pid, string hwnd, string host) => Json(new { version = 1,
            profile_id = profile, generation = gen, app_pid = pid, hwnd, thread_id = thread, host_id = host });
        Require(ShortcutNavigationRecovery.Matches(Context(generation, 123, "456", target.HostId), launch, 456, target), "Selected renderer context did not match.");
        foreach (var stale in new[] { Context("old", 123, "456", target.HostId), Context(generation, 999, "456", target.HostId),
                     Context(generation, 123, "789", target.HostId), Context(generation, 123, "456", "local") })
            Require(!ShortcutNavigationRecovery.Matches(stale, launch, 456, target), "Stale or foreign selection falsely confirmed.");
        checks.Add("Selection confirmation requires the same launch, process, hosted window, task and host.");

        long clock = 0; int sent = 0; bool current = true, selected = true;
        Task Delay(int ms) { clock += ms; return Task.CompletedTask; }
        Task Send(CancellationToken _) { sent++; selected = true; return Task.CompletedTask; }
        async Task<ShortcutRecoveryResult> Run(Func<CancellationToken, Task>? send = null, Func<int, Task>? delay = null, Func<bool>? attached = null, bool remote = false) =>
            await ShortcutNavigationRecovery.RunAsync(() => current, attached ?? (() => true), () => Task.FromResult(selected), send ?? Send, delay ?? Delay, () => clock, remote);
        Require(await Run() == ShortcutRecoveryResult.Selected && sent == 0, "Already selected task was navigated twice.");
        selected = false;
        Require(await Run() == ShortcutRecoveryResult.Recovered && sent == 1, "Stalled open was not recovered exactly once.");
        sent = 0; selected = false; clock = 0;
        Require(await Run(_ => { sent++; return Task.CompletedTask; }) == ShortcutRecoveryResult.Unverified && sent == 1,
            "An acknowledgement without selected-route evidence was reported as success or retried.");
        checks.Add("Normal opens send no recovery; stalled opens send once; acknowledgement alone never counts as success.");
        sent = 0; selected = false; clock = 0;
        Require(await Run(_ => { sent++; return Task.CompletedTask; }, remote: true) == ShortcutRecoveryResult.Unverified && sent == 1
            && clock >= ShortcutNavigationRecovery.RemoteChance + ShortcutNavigationRecovery.RemoteSettle,
            "An SSH task was given up before its host could load the task.");
        sent = 0; selected = false; clock = 0;
        Require(await Run(token => { sent++; if (clock >= 9000) selected = true; return Task.CompletedTask; },
                ms => { clock += ms; if (sent > 0 && clock >= 15000) selected = true; return Task.CompletedTask; }, remote: true)
            == ShortcutRecoveryResult.Recovered && sent == 1, "An SSH task selected 10 s after its direct send was reported unverified.");
        checks.Add("SSH tasks get a longer ordinary chance and settle wait before a selection counts as unverified.");
        selected = false;

        sent = 0; clock = 0; current = true;
        Require(await Run(delay: ms => { clock += ms; current = false; return Task.CompletedTask; }) == ShortcutRecoveryResult.Cancelled && sent == 0,
            "Changing the selection did not cancel before dispatch.");
        current = true; clock = 0; var waiting = new TaskCompletionSource(); bool cancelled = false;
        Require(await Run(token => { sent++; token.Register(() => { cancelled = true; waiting.TrySetCanceled(token); }); return waiting.Task; },
            ms => { clock += ms; if (sent > 0) current = false; return Task.CompletedTask; }) == ShortcutRecoveryResult.Cancelled && sent == 1 && cancelled,
            "Changing profile while awaiting delivery left the old navigation alive.");
        current = true; clock = 0; sent = 0;
        Require(await Run(attached: () => false) == ShortcutRecoveryResult.Unverified && sent == 0, "A missing window was relaunched or polled forever.");
        checks.Add("Profile/task changes cancel before and during delivery; an unattached window has a bounded wait without a relaunch.");

        // Exercise the actual file transport used by an already-running desktop.
        var directory = Path.Combine(root, "navigation-lease");
        using var lease = new NativeWindowLease(directory, 98765, (nint)456);
        string command = Path.Combine(directory, "98765.json.navigate.json");
        using var cancel = new CancellationTokenSource();
        var pending = lease.NavigateAsync(target, cancel.Token);
        for (int i = 0; i < 100 && !File.Exists(command); i++) await Task.Delay(10);
        Require(File.Exists(command), "Navigation command was not published.");
        cancel.Cancel();
        try { await pending; } catch (OperationCanceledException) { }
        Require(!File.Exists(command), "Cancelled command could execute after changing task.");

        using var secondCancel = new CancellationTokenSource();
        pending = lease.NavigateAsync(target, secondCancel.Token);
        for (int i = 0; i < 100 && !File.Exists(command); i++) await Task.Delay(10);
        Require(File.Exists(command), "Second command was not published.");
        using (var doc = JsonDocument.Parse(await File.ReadAllTextAsync(command)))
        {
            var replacement = doc.RootElement.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.Clone());
            replacement["id"] = Json(Guid.NewGuid().ToString("N"));
            await File.WriteAllTextAsync(command, JsonSerializer.Serialize(replacement));
        }
        secondCancel.Cancel();
        try { await pending; } catch (OperationCanceledException) { }
        Require(File.Exists(command), "Cancelling an old navigation deleted its replacement.");
        checks.Add("Actual navigation files are withdrawn on cancellation; a newer navigation command remains intact.");
        return checks;
    }
}
