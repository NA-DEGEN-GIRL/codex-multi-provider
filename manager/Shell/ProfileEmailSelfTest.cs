using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class ProfileEmailSelfTest
{
    internal static async Task<IEnumerable<string>> RunAsync()
    {
        static void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
        static JsonElement State(string account = "first") => JsonSerializer.SerializeToElement(new { profiles = new object[]
        {
            new { id = "native", alias = "01", auth_mode = "native", account_fingerprint = account, status = "stopped" },
            new { id = "claude", alias = "Claude", auth_mode = "claude_code", status = "stopped",
                claude_status = new { logged_in = true, state = "ready", masked_email = "c***@example.test" } },
            new { id = "api", alias = "Local model", auth_mode = "external", status = "stopped" }
        } });
        static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
        {
            for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
            {
                var child = VisualTreeHelper.GetChild(parent, i); yield return child;
                foreach (var item in Descendants(child)) yield return item;
            }
        }
        static async Task Pump(Window window)
        {
            await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            window.UpdateLayout();
        }
        static async Task Until(Window window, Func<bool> condition)
        {
            for (var i = 0; i < 100 && !condition(); i++) { await Task.Delay(10); await Pump(window); }
            Require(condition(), "Email UI response did not settle.");
        }
        var requests = new List<(string Id, TaskCompletionSource<JsonElement> Reply)>();
        var root = Path.Combine(Path.GetTempPath(), "codex-email-ui-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        var window = new MainWindow(root, fixture: true, fixtureRequest: (command, args) =>
        {
            Require(command == "profile.email", "Email toggle launched a profile, refreshed quota or used another RPC.");
            var request = JsonSerializer.SerializeToElement(args);
            Require(request.B("reveal") && request.S("profile_id") != "api", "Email request lacked opt-in or targeted an API profile.");
            var reply = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
            requests.Add((request.S("profile_id"), reply));
            return reply.Task;
        }) { Width = 1100, Height = 960, WindowState = WindowState.Normal, Left = -28000, Top = -28000,
            ShowInTaskbar = false, ShowActivated = false };
        try
        {
            window.UseFixture(State()); window.Show(); await Pump(window);
            var button = Descendants(window).OfType<Button>().Single(item => item.Name == "ProfileEmailToggle");
            var profiles = (ListBox)typeof(MainWindow).GetField("_profiles", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(window)!;
            Choice Native() => profiles.Items.OfType<Choice>().Single(item => item.Id == "native");
            void RequireHidden()
            {
                Require(profiles.Items.OfType<Choice>().All(item => item.Card.Email == "" && !item.Hint.Contains('@') && !item.Label.Contains('@')),
                    "Email remained in a card, tooltip or accessible label while hidden.");
                Require(!Descendants(window).OfType<TextBlock>().Any(item => item.IsVisible && item.Text.Contains('@')),
                    "Email remained visible while hidden.");
            }
            void Click() => button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            static void Complete((string Id, TaskCompletionSource<JsonElement> Reply) request) => request.Reply.SetResult(
                JsonSerializer.SerializeToElement(new { profile_id = request.Id, state = "available", email = request.Id + "@example.test" }));
            RequireHidden();
            Require(requests.Count == 0 && Equals(button.Content, "이메일 표시"), "Opening the window queried or revealed email.");
            Click(); await Until(window, () => requests.Count == 2);
            Require(button.IsEnabled && Equals(button.Content, "이메일 숨기기"), "Slow reads prevented immediate hiding.");
            Click(); RequireHidden();
            foreach (var request in requests.ToArray()) Complete(request);
            await Pump(window); await Task.Delay(30); await Pump(window); RequireHidden();
            Click(); await Until(window, () => requests.Count == 4);
            foreach (var request in requests.Skip(2).ToArray()) Complete(request);
            await Until(window, () => Native().Card.Email == "native@example.test");
            Require(profiles.Items.OfType<Choice>().Single(item => item.Id == "claude").Card.Email == "claude@example.test"
                && profiles.Items.OfType<Choice>().Single(item => item.Id == "api").Card.Email == ""
                && (profiles.SelectedItem as Choice)?.Id == "native", "Reveal mixed accounts, showed API email or changed selection.");
            Require(Descendants(window).OfType<TextBlock>().Any(item => item.IsVisible && item.Text == "native@example.test"
                && item.TextTrimming == TextTrimming.CharacterEllipsis && Equals(item.ToolTip, item.Text)),
                "Email was not rendered as a compact line with an opt-in full tooltip.");
            window.UseFixture(State()); await Pump(window);
            Require(Native().Card.Email == "native@example.test" && requests.Count == 4, "Routine state polling lost email or repeated reads.");
            window.UseFixture(State("reassigned")); await Pump(window);
            Require(!Native().Card.Email.Contains('@'), "Account reassignment left the old email on the profile.");
            Click(); await Pump(window); RequireHidden();
            var logs = string.Join("\n", Directory.EnumerateFiles(root, "*.log", SearchOption.AllDirectories).Select(File.ReadAllText));
            Require(!logs.Contains("@example.test"), "Email appeared in copied diagnostic logs.");
            var fresh = new MainWindow(root, fixture: true);
            try
            {
                fresh.UseFixture(State());
                var list = (ListBox)typeof(MainWindow).GetField("_profiles", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(fresh)!;
                Require(list.Items.OfType<Choice>().All(item => item.Card.Email == ""), "A new window restored revealed email.");
            }
            finally { fresh.Close(); }
        }
        finally { window.Close(); }
        return ["Email is hidden without reads by default; actual button shows both account types and immediately hides pending/late replies.",
            "Email stays out of diagnostics, resets on a new window, invalidates after identity changes and survives state refresh without extra requests."];
    }
}
