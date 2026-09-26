using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows.Threading;
using System.Xml.Linq;
using Microsoft.Win32;
using Microsoft.Toolkit.Uwp.Notifications;
using Windows.UI.Notifications;

namespace Codex.ControlCenter.Shell;

internal sealed record WorkspaceNotice(string Id, string Kind, string Title, string Body, string ThreadId, string HostId);
internal sealed record NotificationTarget(string ProfileId, string ThreadId, string HostId, string Title, long CreatedAt);

// The toolkit's first use resolves Start-menu shortcuts, extracts the app icon
// and registers COM/registry state for seconds. Every toolkit/WinRT toast call
// therefore runs on one STA worker with a message loop, never the WPF UI thread.
// recent/delivered are shared with the UI thread (Remember, ReadTicket) under gate.
internal sealed class WorkspaceNotifications : IDisposable
{
    private readonly string root, directory;
    private readonly Dictionary<string, NotificationTarget> recent = new();
    private readonly Dictionary<string, long> delivered = new();
    private readonly Action<string> log;
    private readonly object gate = new();
    private Dispatcher? worker;
    private ToastNotifierCompat? notifier;
    private bool registered;
    private int warmed;
    internal WorkspaceNotifications(string root, Action<string> log)
    {
        this.root = root; this.log = log;
        directory = Path.Combine(root, "work", "control-center", "notifications");
        try
        {
            var file = Path.Combine(directory, "recent.json");
            if (new FileInfo(file) is { Exists: true, Length: < 524288 })
                foreach (var target in JsonSerializer.Deserialize<NotificationTarget[]>(File.ReadAllText(file)) ?? [])
                    if (Valid(target)) recent[Key(target.HostId, target.ThreadId)] = target;
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException) { }
    }
    private static string Key(string host, string thread) => host + "/" + thread;
    internal static bool Valid(NotificationTarget value) => Guid.TryParse(value.ProfileId, out _) &&
        Guid.TryParse(value.ThreadId, out _) && value.HostId is { Length: > 0 and <= 256 } && !value.HostId.Any(char.IsControl);
    internal void Remember(string profileId, SelectedTask? task)
    {
        if (task is null) return;
        var value = new NotificationTarget(profileId, task.Task.ThreadId, task.Task.HostId, task.Title, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        if (!Valid(value)) return;
        NotificationTarget[] snapshot;
        lock (gate)
        {
            recent[Key(value.HostId, value.ThreadId)] = value;
            if (recent.Count > 512) recent.Remove(recent.MinBy(x => x.Value.CreatedAt).Key);
            snapshot = recent.Values.ToArray();
        }
        // Coalesced by TaskContextWatcher (selection changes only, not each RPC).
        try { Save(Path.Combine(directory, "recent.json"), snapshot); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        { log("최근 알림 프로필 저장 지연 · " + error.Message); }
    }
    internal NotificationTarget Resolve(NotificationTarget fallback, Func<string, bool> exists)
    {
        NotificationTarget? latest;
        lock (gate) recent.TryGetValue(Key(fallback.HostId, fallback.ThreadId), out latest);
        var target = latest is not null && exists(latest.ProfileId) ? latest : fallback;
        // Prefer the account that last actually sent a turn. Merely looking at
        // the shared history in another profile must not take over its alerts.
        long newest = 0;
        try
        {
            var activity = Path.Combine(directory, "activity");
            if (!Directory.Exists(activity)) return target;
            foreach (var file in new DirectoryInfo(activity).EnumerateFiles("*.json").Take(64))
            {
                string profile = Path.GetFileNameWithoutExtension(file.Name);
                if (!Guid.TryParse(profile, out _) || !exists(profile) || file.Length > 131072) continue;
                try
                {
                    using var input = new FileStream(file.FullName, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                    using var document = JsonDocument.Parse(input);
                    var state = document.RootElement;
                    if (state.GetProperty("version").GetInt32() != 1 || state.GetProperty("profileId").GetString() != profile) continue;
                    foreach (var turn in state.GetProperty("turns").EnumerateArray().Take(256))
                    {
                        long at = turn.GetProperty("at").GetInt64();
                        if (at > newest && at <= DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() + 1000 &&
                            turn.GetProperty("hostId").GetString() == fallback.HostId && turn.GetProperty("threadId").GetString() == fallback.ThreadId)
                        { newest = at; target = target with { ProfileId = profile }; }
                    }
                }
                catch (Exception error) when (error is IOException or JsonException or KeyNotFoundException or InvalidOperationException or FormatException) { }
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
        return target;
    }

    // Native delivery is suppressed only after acceptance (revision 40), so reply
    // true only once Windows took the toast, and false by the caller's deadline
    // (before Electron's 1.8 s native fallback). exists/alias must not read UI state.
    internal async Task<bool> ShowAsync(WorkspaceNotice notice, string sourceProfile, Func<string, bool> exists,
        Func<string, string> alias, DateTimeOffset deadline)
    {
        var attempt = new Attempt();
        var remaining = deadline - DateTimeOffset.UtcNow;
        if (remaining <= TimeSpan.Zero) return false;
        try { return await Run(() => Show(notice, sourceProfile, exists, alias, attempt)).WaitAsync(remaining).ConfigureAwait(false); }
        catch (Exception error) when (error is TimeoutException or OperationCanceledException) { return !attempt.Abandon(); }
    }
    private bool Show(WorkspaceNotice notice, string sourceProfile, Func<string, bool> exists, Func<string, string> alias, Attempt attempt)
    {
        try
        {
            if (attempt.Abandoned) return false;
            var target = Resolve(new(sourceProfile, notice.ThreadId, notice.HostId, notice.Title, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()), exists);
            if (!Valid(target)) return false;
            string dedupe = Key(target.HostId, target.ThreadId) + "/" + notice.Kind + "/" + notice.Id;
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            lock (gate) if (delivered.TryGetValue(dedupe, out var at) && now - at < 60000) return true;
            Register();
            string ticket = Guid.NewGuid().ToString("N");
            Save(Path.Combine(directory, ticket + ".json"), target with { CreatedAt = now });
            foreach (var old in new DirectoryInfo(directory).EnumerateFiles("*.json")
                .Where(f => Guid.TryParseExact(Path.GetFileNameWithoutExtension(f.Name), "N", out _))
                .OrderByDescending(f => f.LastWriteTimeUtc).Skip(512))
                try { old.Delete(); } catch (IOException) { }
            var xml = BuildXml(WorkspaceActivation.Scheme(root) + "://notification/" + ticket,
                alias(target.ProfileId), notice);
            var document = new Windows.Data.Xml.Dom.XmlDocument(); document.LoadXml(xml);
            var toast = new ToastNotification(document) { Tag = ticket[..16], Group = "workspace", ExpirationTime = DateTimeOffset.Now.AddDays(2) };
            var toasts = Notifier();
            if (attempt.Abandoned) return false;
            toasts.Show(toast);
            if (!attempt.Commit())
            {
                // The pipe already answered "not accepted" and Electron showed its
                // native notice. Withdraw this late toast instead of alerting twice.
                try { toasts.Hide(toast); ToastNotificationManagerCompat.History.Remove(toast.Tag, toast.Group); }
                catch (Exception error) when (error is ExternalException or InvalidOperationException or ArgumentException) { }
                log($"작업 알림 지연 · {notice.Kind} · 원본 알림으로 대체");
                return false;
            }
            lock (gate)
            {
                delivered[dedupe] = now;
                if (delivered.Count > 512) delivered.Remove(delivered.MinBy(x => x.Value).Key);
            }
            log($"작업 알림 · {alias(target.ProfileId)} · {notice.Kind} · 작업공간에서 열기");
            return true;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ExternalException or InvalidOperationException or ArgumentException)
        { log("작업공간 알림 전달 실패 · " + error.Message); return false; }
    }
    // Once, after the first state render: the toolkit's static registration,
    // the URI handler and the first WinRT XML projection, all on the worker.
    internal void Warm()
    {
        if (Interlocked.Exchange(ref warmed, 1) != 0) return;
        _ = Run(() =>
        {
            try { Register(); Notifier(); new Windows.Data.Xml.Dom.XmlDocument().LoadXml("<toast/>"); }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or ExternalException or InvalidOperationException or ArgumentException)
            { log("작업공간 알림 준비 지연 · " + error.Message); }
            return true;
        });
    }
    internal Task<T> Run<T>(Func<T> work) => Worker().InvokeAsync(work).Task;
    private ToastNotifierCompat Notifier() => notifier ??= ToastNotificationManagerCompat.CreateToastNotifier();
    private Dispatcher Worker()
    {
        lock (gate)
        {
            if (worker is not null) return worker;
            var ready = new TaskCompletionSource<Dispatcher>();
            // A message loop, not a bare wait: the toolkit registers its COM
            // activator in this apartment. OnActivated is never subscribed.
            var thread = new Thread(() => { ready.SetResult(Dispatcher.CurrentDispatcher); Dispatcher.Run(); })
            { IsBackground = true, Name = "Codex workspace notifications" };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            return worker = ready.Task.Result;
        }
    }
    public void Dispose() { lock (gate) worker?.BeginInvokeShutdown(DispatcherPriority.Normal); }

    // One reply per request: whichever of "Windows took the toast" or "the
    // deadline passed" happens first decides it; the loser withdraws.
    internal sealed class Attempt
    {
        private int state; // 0 pending, 1 shown, 2 abandoned
        internal bool Abandoned => Volatile.Read(ref state) == 2;
        internal bool Commit() => Interlocked.CompareExchange(ref state, 1, 0) != 2;
        internal bool Abandon() => Interlocked.CompareExchange(ref state, 2, 0) != 1;
    }
    internal static string BuildXml(string uri, string alias, WorkspaceNotice notice) => new XElement("toast",
        new XAttribute("activationType", "protocol"), new XAttribute("launch", uri),
        new XAttribute("duration", notice.Kind is "question" or "permission" ? "long" : "short"),
        new XElement("visual", new XElement("binding", new XAttribute("template", "ToastGeneric"),
            new XElement("text", $"[{alias}] {notice.Title}"), new XElement("text", notice.Body),
            new XElement("text", new XAttribute("placement", "attribution"), "Codex 작업 공간 · 작업으로 이동")))).ToString(SaveOptions.DisableFormatting);

    internal NotificationTarget? ReadTicket(string ticket, Func<string, bool> exists)
        => ReadStoredTicket(ticket) is { } target ? Resolve(target, exists) : null;
    // The target as the toast saved it, resolved against the profiles that
    // existed when it was shown. Without live profiles, Resolve's recent and
    // activity owners cannot be checked: a stale one may be a removed profile.
    internal NotificationTarget? ReadStoredTicket(string ticket)
    {
        if (!Guid.TryParseExact(ticket, "N", out _)) return null;
        try
        {
            var file = Path.Combine(directory, ticket + ".json");
            if (new FileInfo(file) is not { Exists: true, Length: < 8192 }) return null;
            var target = JsonSerializer.Deserialize<NotificationTarget>(File.ReadAllText(file));
            if (target is null || !Valid(target) || target.CreatedAt < DateTimeOffset.UtcNow.AddDays(-7).ToUnixTimeMilliseconds()) return null;
            return target;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { return null; }
    }
    private void Register()
    {
        if (registered) return;
        var executable = Environment.ProcessPath ?? throw new InvalidOperationException("관리 앱 실행 경로가 없습니다.");
        string key = @"Software\Classes\" + WorkspaceActivation.Scheme(root), description = "URL:Codex 작업 공간 알림",
            handler = $"\"{executable}\" --root \"{root}\" --workspace-notification \"%1\"";
        // Only a new release path changes the handler; identical rewrites
        // would still wake every watcher of HKCU\Software\Classes.
        if (!Registered(key, description, handler))
        {
            using var scheme = Registry.CurrentUser.CreateSubKey(key);
            scheme.SetValue("", description); scheme.SetValue("URL Protocol", "");
            using var command = scheme.CreateSubKey(@"shell\open\command");
            command.SetValue("", handler);
        }
        registered = true;
    }
    internal static bool Registered(string key, string description, string handler)
    {
        using var scheme = Registry.CurrentUser.OpenSubKey(key);
        using var command = scheme?.OpenSubKey(@"shell\open\command");
        return scheme is not null && command is not null && scheme.GetValue("") as string == description &&
            scheme.GetValue("URL Protocol") as string == "" && command.GetValue("") as string == handler;
    }
    private static void Save<T>(string path, T value)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(value));
        File.Move(temporary, path, true);
    }
}
