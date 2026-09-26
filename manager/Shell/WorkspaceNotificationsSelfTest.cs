using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Xml.Linq;
using Microsoft.Win32;
using Microsoft.Toolkit.Uwp.Notifications;
using Windows.UI.Notifications;

namespace Codex.ControlCenter.Shell;

internal static class WorkspaceNotificationsSelfTest
{
    internal static async Task<List<string>> RunAsync(string directory)
    {
        void Require(bool ok, string message) { if (!ok) throw new InvalidOperationException(message); }
        var checks = new List<string>();
        string thread = Guid.NewGuid().ToString(), otherThread = Guid.NewGuid().ToString(), first = Guid.NewGuid().ToString(), second = Guid.NewGuid().ToString();
        var logs = new List<string>();
        var routes = new WorkspaceNotifications(directory, message => { lock (logs) logs.Add(message); });
        routes.Remember(first, new(new("local", thread), "처음"));
        routes.Remember(second, new(new("local", thread), "현재 작업"));
        routes.Remember(first, new(new("ssh-one", thread), "원격 작업"));
        var original = new NotificationTarget(first, thread, "local", "이전 알림", DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        Require(routes.Resolve(original, _ => true).ProfileId == second, "Last profile for this task was lost.");
        Require(routes.Resolve(original with { HostId = "ssh-one" }, _ => true).ProfileId == first, "Host identity was mixed with a local task.");
        Require(routes.Resolve(original with { ThreadId = otherThread }, _ => true).ProfileId == first, "Unrelated task changed notification destination.");
        Require(routes.Resolve(original, p => p != second).ProfileId == first, "Removed profile was selected.");
        var reopened = new WorkspaceNotifications(directory, _ => { });
        Require(reopened.Resolve(original, _ => true).ProfileId == second, "Last task profile did not persist across manager restart.");
        string activity = Path.Combine(directory, "work", "control-center", "notifications", "activity");
        Directory.CreateDirectory(activity);
        File.WriteAllText(Path.Combine(activity, first + ".json"), JsonSerializer.Serialize(new { version = 1, profileId = first,
            turns = new[] { new { threadId = thread, hostId = "local", at = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() } } }));
        Require(reopened.Resolve(original, _ => true).ProfileId == first, "Viewing a task from another account overrode its last actual chatting profile.");
        Require(reopened.Resolve(original, p => p != first).ProfileId == second, "Deleted writer did not fall back to the available last viewer.");
        string ticket = Guid.NewGuid().ToString("N"), scheme = WorkspaceActivation.Scheme(directory);
        string uri = scheme + "://notification/" + ticket;
        Require(WorkspaceActivation.Ticket(directory, uri) == ticket && WorkspaceActivation.Ticket(directory, "codex://threads/" + thread) is null,
            "Workspace URI changed or accepted the global Codex handler.");
        Require(WorkspaceActivation.Ticket(directory, uri + "?run=anything") is null && reopened.ReadTicket("../recent", _ => true) is null,
            "Arbitrary activation arguments were accepted.");
        var notice = new WorkspaceNotice("notice-1", "question", "질문 <>&", "대답 \"준비\" & <확인>", thread, "local");
        var xml = WorkspaceNotifications.BuildXml(uri, "02", notice);
        var parsed = XDocument.Parse(xml);
        Require(parsed.Root!.Attribute("activationType")!.Value == "protocol" && parsed.Root.Attribute("launch")!.Value == uri,
            "Question notification does not launch the workspace URI.");
        Require(parsed.Descendants("text").First().Value == "[02] " + notice.Title, "Profile label or XML escaping failed.");
        checks.Add("Question routes persist the last profile per host+task, handle removal, and reject arbitrary URI/path arguments; XML safely labels profile 02.");

        // Toast work leaves the caller's thread. A busy worker (for example the
        // toolkit's first registration) answers "not accepted" by the deadline,
        // and that abandoned request never writes a ticket or shows a toast.
        int caller = Environment.CurrentManagedThreadId;
        Require(await routes.Run(() => Thread.CurrentThread.GetApartmentState() == ApartmentState.STA && Environment.CurrentManagedThreadId != caller),
            "Toast work did not run on a separate STA worker.");
        using (var hold = new ManualResetEventSlim())
        {
            var busy = routes.Run(() => hold.Wait(TimeSpan.FromSeconds(5)));
            var watch = Stopwatch.StartNew();
            bool accepted = await routes.ShowAsync(notice, first, _ => true, _ => "01", DateTimeOffset.UtcNow.AddMilliseconds(200));
            Require(!accepted && watch.ElapsedMilliseconds < 1500, "A busy toast worker delayed the reply or accepted an undelivered toast.");
            hold.Set(); await busy; await routes.Run(() => true);
        }
        Require(!await routes.ShowAsync(notice, first, _ => true, _ => "01", DateTimeOffset.UtcNow.AddSeconds(-1)), "An expired notification was accepted.");
        Require(!new DirectoryInfo(Path.Combine(directory, "work", "control-center", "notifications")).EnumerateFiles("*.json")
            .Any(f => Guid.TryParseExact(Path.GetFileNameWithoutExtension(f.Name), "N", out _)), "An abandoned notification still created a toast ticket.");
        var shown = new WorkspaceNotifications.Attempt();
        Require(shown.Commit() && !shown.Abandon(), "A toast Windows already took was withdrawn after its acceptance.");
        var late = new WorkspaceNotifications.Attempt();
        Require(late.Abandon() && late.Abandoned && !late.Commit(), "A toast finishing after the rejected reply was not withdrawn.");
        checks.Add("Toasts are delivered on a separate STA worker; a busy worker answers 'not accepted' within the deadline, abandoned requests write no ticket and a late toast is withdrawn.");

        // Actual Windows registration, toast storage and URI activation. This
        // notification is SILENT and NEVER shown as a popup. The activation
        // process has no window and forwards only this disposable fixture ticket.
        var activated = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        using var listener = new WorkspaceActivation(directory, value => activated.TrySetResult(value));
        try
        {
            // The production toast path on the STA worker: Warm() runs the
            // toolkit's first initialisation and this fixture's URI registration
            // there, Windows stores a silent toast shown from it, and the
            // late-toast withdrawal (Hide + History.Remove) removes it again.
            routes.Warm();
            await routes.Run(() => true);
            lock (logs) Require(typeof(WorkspaceNotifications).GetField("notifier", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(routes) is not null &&
                logs.Count == 0, "Warm() did not initialise the toast toolkit on the worker: " + string.Join(" / ", logs));
            string workerTag = Guid.NewGuid().ToString("N")[..16];
            var (workerNotifier, workerToast) = await routes.Run(() =>
            {
                var content = new Windows.Data.Xml.Dom.XmlDocument(); content.LoadXml(xml);
                var silent = new ToastNotification(content) { SuppressPopup = true, Tag = workerTag, Group = "fixture", ExpirationTime = DateTimeOffset.Now.AddMinutes(1) };
                var toasts = ToastNotificationManagerCompat.CreateToastNotifier();
                toasts.Show(silent);
                return (toasts, silent);
            });
            async Task<bool> Stored(bool expected)
            {
                for (var attempt = 0; attempt < 20; attempt++)
                {
                    if (await routes.Run(() => ToastNotificationManagerCompat.History.GetHistory().Any(t => t.Tag == workerTag && t.Group == "fixture")) == expected) return true;
                    await Task.Delay(100);
                }
                return false;
            }
            Require(await Stored(true), "Windows did not store a toast shown from the STA worker.");
            await routes.Run(() =>
            {
                // A popup-suppressed toast was never on screen; Hide may report that.
                try { workerNotifier.Hide(workerToast); }
                catch (Exception error) when (error is ExternalException or InvalidOperationException or ArgumentException) { }
                ToastNotificationManagerCompat.History.Remove(workerTag, "fixture");
                return true;
            });
            Require(await Stored(false), "Hide + History.Remove on the worker did not withdraw its toast.");
            checks.Add("The STA worker runs the toolkit's first initialisation (Warm), shows a real silent toast that Windows stores, and withdraws it with Hide + History.Remove.");

            string handler = $"\"{Environment.ProcessPath}\" --notification-activation-fixture --root \"{directory}\" --workspace-notification \"%1\"";
            using (var key = Registry.CurrentUser.CreateSubKey(@"Software\Classes\" + scheme))
            {
                key.SetValue("", "URL:Codex notification fixture"); key.SetValue("URL Protocol", "");
                using var command = key.CreateSubKey(@"shell\open\command");
                command.SetValue("", handler);
            }
            Require(WorkspaceNotifications.Registered(@"Software\Classes\" + scheme, "URL:Codex notification fixture", handler) &&
                !WorkspaceNotifications.Registered(@"Software\Classes\" + scheme, "URL:Codex notification fixture", handler + " --changed") &&
                !WorkspaceNotifications.Registered(@"Software\Classes\" + scheme + "-missing", "URL:Codex notification fixture", handler),
                "URI registration comparison misread an unchanged, changed or missing handler.");
            var document = new Windows.Data.Xml.Dom.XmlDocument(); document.LoadXml(xml);
            var toast = new ToastNotification(document) { SuppressPopup = true, Tag = ticket[..16], Group = "fixture", ExpirationTime = DateTimeOffset.Now.AddMinutes(1) };
            ToastNotificationManagerCompat.CreateToastNotifier().Show(toast);
            await Task.Delay(250);
            var recorded = ToastNotificationManagerCompat.History.GetHistory().FirstOrDefault(t => t.Tag == ticket[..16]);
            Require(recorded is not null && recorded.Content.DocumentElement.GetAttribute("launch") == uri, "Windows did not retain the workspace activation URI.");
            using var process = Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true, WindowStyle = ProcessWindowStyle.Hidden });
            Require(await activated.Task.WaitAsync(TimeSpan.FromSeconds(8)) == ticket, "Windows URI activation did not reach the running workspace listener.");
            checks.Add("Windows stored the real protocol toast and invoked its registered handler; an isolated child process forwarded the exact ticket over a CurrentUserOnly pipe. No popup or user app was opened. An identical URI registration is recognised and not rewritten.");
        }
        finally
        {
            routes.Dispose();
            ToastNotificationManagerCompat.History.Remove(ticket[..16], "fixture");
            ToastNotificationManagerCompat.Uninstall(); // Only this self-test executable's notification registration.
            Registry.CurrentUser.DeleteSubKeyTree(@"Software\Classes\" + scheme, false); // Unique fixture URI only.
        }
        return checks;
    }
}
