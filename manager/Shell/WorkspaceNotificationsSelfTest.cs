using System.Diagnostics;
using System.IO;
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
        var routes = new WorkspaceNotifications(directory, _ => { });
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

        // Actual Windows registration, toast storage and URI activation. This
        // notification is SILENT and NEVER shown as a popup. The activation
        // process has no window and forwards only this disposable fixture ticket.
        var activated = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        using var listener = new WorkspaceActivation(directory, value => activated.TrySetResult(value));
        try
        {
            using (var key = Registry.CurrentUser.CreateSubKey(@"Software\Classes\" + scheme))
            {
                key.SetValue("", "URL:Codex notification fixture"); key.SetValue("URL Protocol", "");
                using var command = key.CreateSubKey(@"shell\open\command");
                command.SetValue("", $"\"{Environment.ProcessPath}\" --notification-activation-fixture --root \"{directory}\" --workspace-notification \"%1\"");
            }
            var document = new Windows.Data.Xml.Dom.XmlDocument(); document.LoadXml(xml);
            var toast = new ToastNotification(document) { SuppressPopup = true, Tag = ticket[..16], Group = "fixture", ExpirationTime = DateTimeOffset.Now.AddMinutes(1) };
            ToastNotificationManagerCompat.CreateToastNotifier().Show(toast);
            await Task.Delay(250);
            var recorded = ToastNotificationManagerCompat.History.GetHistory().FirstOrDefault(t => t.Tag == ticket[..16]);
            Require(recorded is not null && recorded.Content.DocumentElement.GetAttribute("launch") == uri, "Windows did not retain the workspace activation URI.");
            using var process = Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true, WindowStyle = ProcessWindowStyle.Hidden });
            Require(await activated.Task.WaitAsync(TimeSpan.FromSeconds(8)) == ticket, "Windows URI activation did not reach the running workspace listener.");
            checks.Add("Windows stored the real protocol toast and invoked its registered handler; an isolated child process forwarded the exact ticket over a CurrentUserOnly pipe. No popup or user app was opened.");
        }
        finally
        {
            ToastNotificationManagerCompat.History.Remove(ticket[..16], "fixture");
            ToastNotificationManagerCompat.Uninstall(); // Only this self-test executable's notification registration.
            Registry.CurrentUser.DeleteSubKeyTree(@"Software\Classes\" + scheme, false); // Unique fixture URI only.
        }
        return checks;
    }
}
