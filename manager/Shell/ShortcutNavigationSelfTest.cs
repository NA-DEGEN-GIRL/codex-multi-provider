using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class ShortcutNavigationSelfTest
{
    internal static async Task RunAsync(string report)
    {
        static void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        static T Field<T>(MainWindow window, string name) => (T)typeof(MainWindow).GetField(name, flags)!.GetValue(window)!;
        static Task Invoke(MainWindow window, string name, params object[] args) =>
            (Task)typeof(MainWindow).GetMethod(name, flags, args.Select(arg => arg.GetType()).ToArray())!.Invoke(window, args)!;
        var profile = JsonSerializer.SerializeToElement(new { id = "profile", alias = "계정", status = "running", generation = "old", process_id = 0 });
        var returned = JsonSerializer.SerializeToElement(new { id = "profile", alias = "계정", status = "running", generation = "new", process_id = 0 });
        var shortcut = new { id = "shortcut", profile_id = "profile", alias = "작업", thread_id = "thread", host_id = "local" };
        var state = JsonSerializer.SerializeToElement(new { profiles = new[] { profile }, shortcuts = new[] { shortcut } });
        var ready = JsonSerializer.SerializeToElement(new { state = "request_sent", profile_id = "profile", profile = returned });
        var waiting = JsonSerializer.SerializeToElement(new { state = "waiting_for_reader", navigation_id = "opaque", profile_id = "profile", profile = returned });
        var checks = new List<string>();
        var commands = new List<string>();
        var openReply = new TaskCompletionSource<JsonElement>();
        var stateReply = new TaskCompletionSource<JsonElement>();
        var root = Path.Combine(Path.GetTempPath(), "codex-shortcut-ui-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        var window = new MainWindow(root, fixture: true, fixtureRequest: (command, args) =>
        {
            commands.Add(command);
            if (command == "conversation.open")
                Require(JsonSerializer.SerializeToElement(args).S("expected_profile_id") == "profile",
                    "Shortcut navigation did not bind its request to the profile selected by the user.");
            return command switch { "conversation.open" => openReply.Task, "state" => stateReply.Task,
                _ => throw new InvalidOperationException("Unexpected request: " + command) };
        }) { FixtureRefreshesState = true, WindowState = WindowState.Normal, Width = 1200, Height = 900, Left = -28000, Top = -28000,
            ShowInTaskbar = false, ShowActivated = false };
        try
        {
            window.UseFixture(state); window.Show();
            await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            window.UpdateLayout();
            var surface = Field<Grid>(window, "_clientSurface");
            var height = surface.ActualHeight;
            var stalePoll = Invoke(window, "RefreshAsync");
            var navigation = Invoke(window, "OpenShortcutAsync", "shortcut");
            window.UpdateLayout();
            Require(Field<TextBlock>(window, "_operation").Visibility == Visibility.Collapsed && surface.ActualHeight == height,
                "Opening a shortcut resized the native viewport with an activity banner.");
            ShortcutCardData? Card() => Field<ListBox>(window, "_shortcuts").Items.OfType<Choice>().FirstOrDefault(c => c.Id == "shortcut")?.Shortcut;
            Require(Card() is { State: "opening" } opening && opening.StateText.StartsWith("대화 여는 중", StringComparison.Ordinal)
                && Field<TextBlock>(window, "_status").Text.StartsWith("대화 여는 중", StringComparison.Ordinal),
                "The clicked card and the status line did not show the open's phase.");
            openReply.SetResult(ready);
            await navigation.WaitAsync(TimeSpan.FromSeconds(3));
            Require(Card() is { State: "idle" }, "The card kept the open's progress after the open ended.");
            var shown = Field<Dictionary<string, JsonElement>>(window, "_shownProfiles");
            Require(shown["profile"].S("generation") == "new"
                && Field<WindowLaunchIdentity>(window, "_expectedWindowLaunch").Generation == "new",
                "Shortcut navigation did not immediately retain and pin its returned launch.");
            Require(!stalePoll.IsCompleted && commands.SequenceEqual(new[] { "state", "conversation.open" }),
                "Shortcut navigation waited for or sent an extra global state request.");
            using (Field<ProfileActionGate>(window, "_profileActions").Enter("profile", "fixture")) { }
            stateReply.SetResult(state); await stalePoll;
            Require(shown["profile"].S("generation") == "new", "A pre-navigation state poll replaced the newly returned launch.");
            checks.Add("Shortcut navigation keeps viewport height stable, shows its phase on the card and in the status line, pins its returned launch, ignores old state and releases its gate without waiting for a global refresh.");
        }
        finally { stateReply.TrySetResult(state); openReply.TrySetResult(ready); window.Close(); }

        commands.Clear();
        var waitingWindow = new MainWindow(root, fixture: true, fixtureRequest: (command, _) =>
        {
            commands.Add(command);
            return Task.FromResult(command switch { "conversation.open" => waiting, "conversation.navigate" => ready,
                _ => throw new InvalidOperationException("Unexpected request: " + command) });
        }) { FixtureRefreshesState = true };
        try
        {
            waitingWindow.UseFixture(state);
            await Invoke(waitingWindow, "OpenShortcutAsync", "shortcut").WaitAsync(TimeSpan.FromSeconds(3));
            Require(commands.SequenceEqual(new[] { "conversation.open", "conversation.navigate" }),
                "Cold task navigation repeated its open request or waited for global state before readiness.");
            Require(Field<Dictionary<string, JsonElement>>(waitingWindow, "_shownProfiles")["profile"].S("generation") == "new",
                "Cold navigation lost its returned window identity.");
            checks.Add("Cold task navigation sends one open and one readiness continuation, with no global state request between them.");
        }
        finally { waitingWindow.Close(); }

        var wrong = JsonSerializer.SerializeToElement(new { state = "request_sent", profile_id = "other", profile = new { id = "other" } });
        var invalidWindow = new MainWindow(root, fixture: true, fixtureRequest: (_, _) => Task.FromResult(wrong));
        try
        {
            invalidWindow.UseFixture(state);
            var rejected = false;
            try { await Invoke(invalidWindow, "OpenShortcutAsync", "shortcut"); }
            catch (InvalidOperationException) { rejected = true; }
            Require(rejected && Field<Dictionary<string, JsonElement>>(invalidWindow, "_shownProfiles").Count == 0,
                "A task navigation attached a different profile's returned window.");
            using (Field<ProfileActionGate>(invalidWindow, "_profileActions").Enter("profile", "fixture")) { }
            checks.Add("Mismatched returned profiles are rejected and the action gate is released on failure.");
        }
        finally { invalidWindow.Close(); }

        // Task opens of one account merge instead of rejecting each other.
        var other = new { id = "other", profile_id = "profile", alias = "다른 작업", thread_id = "thread-2", host_id = "local" };
        var pair = JsonSerializer.SerializeToElement(new { profiles = new[] { profile }, shortcuts = new[] { shortcut, other } });
        var opens = new List<(string Id, TaskCompletionSource<JsonElement> Reply)>();
        var mergeWindow = new MainWindow(root, fixture: true, fixtureRequest: (command, args) =>
        {
            if (command != "conversation.open") throw new InvalidOperationException("Unexpected request: " + command);
            opens.Add((JsonSerializer.SerializeToElement(args).S("shortcut_id"), new(TaskCreationOptions.RunContinuationsAsynchronously)));
            return opens[^1].Reply.Task;
        }) { FixtureRefreshesState = true };
        try
        {
            mergeWindow.UseFixture(pair);
            var first = Invoke(mergeWindow, "OpenShortcutAsync", "shortcut");
            var again = Invoke(mergeWindow, "OpenShortcutAsync", "shortcut");
            Require(again.IsCompletedSuccessfully && opens.Count == 1,
                "A re-click of the task being opened was rejected or sent a second open.");
            var replacement = Invoke(mergeWindow, "OpenShortcutAsync", "other");
            var repeat = Invoke(mergeWindow, "OpenShortcutAsync", "other");
            Require(!replacement.IsCompleted && repeat.IsCompletedSuccessfully && opens.Count == 1,
                "Another task of the account was rejected, or sent before the open in flight ended.");
            opens[0].Reply.SetResult(ready);
            await first.WaitAsync(TimeSpan.FromSeconds(3));
            for (var started = Environment.TickCount64; opens.Count < 2; await Task.Delay(10))
                Require(Environment.TickCount64 - started < 3000, "The replacing task was not opened after the open in flight ended.");
            Require(opens[1].Id == "other" && Field<Dictionary<string, JsonElement>>(mergeWindow, "_shownProfiles").Count == 0,
                "The replaced open presented its result, or the replacement opened the wrong task.");
            opens[1].Reply.SetResult(ready);
            await replacement.WaitAsync(TimeSpan.FromSeconds(3));
            Require(opens.Count == 2 && Field<Dictionary<string, JsonElement>>(mergeWindow, "_shownProfiles")["profile"].S("generation") == "new",
                "The replacing open did not present its returned launch.");
            using (Field<ProfileActionGate>(mergeWindow, "_profileActions").Enter("profile", "fixture")) { }
            checks.Add("A re-click of a task being opened joins it; another task of the account replaces it after the open in flight, with one open each and no rejection.");
        }
        finally { foreach (var open in opens) open.Reply.TrySetResult(ready); mergeWindow.Close(); }

        // A replaced open still waiting for Codex readiness fails silently too.
        var steps = new List<(string Command, string Id, TaskCompletionSource<JsonElement> Reply)>();
        var abandonWindow = new MainWindow(root, fixture: true, fixtureRequest: (command, args) =>
        {
            if (command is not ("conversation.open" or "conversation.navigate")) throw new InvalidOperationException("Unexpected request: " + command);
            steps.Add((command, JsonSerializer.SerializeToElement(args).S("shortcut_id"), new(TaskCreationOptions.RunContinuationsAsynchronously)));
            return steps[^1].Reply.Task;
        }) { FixtureRefreshesState = true };
        try
        {
            abandonWindow.UseFixture(pair);
            var first = Invoke(abandonWindow, "OpenShortcutAsync", "shortcut");
            steps[0].Reply.SetResult(waiting);
            for (var started = Environment.TickCount64; steps.Count < 2; await Task.Delay(10))
                Require(Environment.TickCount64 - started < 3000, "The cold open did not send its readiness continuation.");
            var replacement = Invoke(abandonWindow, "OpenShortcutAsync", "other");
            Require(steps[1].Command == "conversation.navigate" && !replacement.IsCompleted,
                "The replacing task did not wait for the open in flight.");
            steps[1].Reply.SetException(new InvalidOperationException("fixture readiness failure"));
            // Unsilenced, the replaced open faults here and Safe reports it as an error.
            await first.WaitAsync(TimeSpan.FromSeconds(3));
            for (var started = Environment.TickCount64; steps.Count < 3; await Task.Delay(10))
                Require(Environment.TickCount64 - started < 3000, "The replacing task was not opened after the replaced open ended.");
            Require(steps[2].Command == "conversation.open" && steps[2].Id == "other",
                "The replacement opened the wrong task after a replaced readiness failure.");
            steps[2].Reply.SetResult(ready);
            await replacement.WaitAsync(TimeSpan.FromSeconds(3));
            using (Field<ProfileActionGate>(abandonWindow, "_profileActions").Enter("profile", "fixture")) { }
            checks.Add("A replaced open's failed readiness continuation is not reported, and the replacing task still opens.");
        }
        finally { foreach (var step in steps) step.Reply.TrySetResult(ready); abandonWindow.Close(); }
        checks.AddRange(DesktopCompatibilitySelfTest.Run(root));
        checks.AddRange(LocalModelSelfTest.Run());
        checks.AddRange(await ProfileOpenStatusSelfTest.RunAsync(root));
        checks.AddRange(await ShortcutRecoverySelfTest.RunAsync(root));
        await File.WriteAllTextAsync(report, JsonSerializer.Serialize(new { passed = true, checks }, new JsonSerializerOptions { WriteIndented = true }));
    }
}
