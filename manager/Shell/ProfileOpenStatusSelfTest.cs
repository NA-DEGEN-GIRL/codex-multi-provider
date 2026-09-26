using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Windows.Controls;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal static class ProfileOpenStatusSelfTest
{
    internal static async Task<List<string>> RunAsync(string root)
    {
        // Exercise the real RPC error and sidebar paths without showing a
        // window, attaching native windows or connecting a live service.
        const string message = "다른 업데이트 작업이 진행 중입니다.";
        string code = "update_in_progress";
        var requests = new List<string>();
        var window = new MainWindow(root, fixture: true, fixtureRequest: (command, _) =>
        {
            requests.Add(command);
            return Task.FromException<JsonElement>(new ManagerException(code, message));
        });
        var flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var type = typeof(MainWindow);
        var stateField = type.GetField("_state", flags)!;
        var selectedField = type.GetField("_selectedProfile", flags)!;
        var navigationField = type.GetField("_navigation", flags)!;
        var status = (TextBlock)type.GetField("_status", flags)!.GetValue(window)!;
        var show = type.GetMethod("ShowProfileAsync", flags)!;
        var safe = type.GetMethod("Safe", flags)!;
        var resolve = type.GetMethod("ResolveProfileOpenNotice", flags)!;
        var setStatus = type.GetMethod("SetStatus", flags)!;
        var checks = new List<string>();
        static void Require(bool condition, string message)
        { if (!condition) throw new InvalidOperationException(message); }
        static JsonElement Profile(string id = "03", string phase = "complete", string status = "running")
            => JsonSerializer.SerializeToElement(new { id, alias = id, status, generation = "fixture-generation", restart = new { phase } });
        static JsonElement State(string? gate = null, string gateState = "held")
        {
            var data = new Dictionary<string, object> { ["profiles"] = new[] { Profile(status: "stopped"), Profile("02", status: "stopped") } };
            if (gate is not null)
                data[gate] = gate == "update_maintenance" ? new { state = gateState }
                    : new Dictionary<string, object> { ["03"] = new { state = gateState } };
            return JsonSerializer.SerializeToElement(data);
        }
        async Task FailOpen(string errorCode = "update_in_progress", string command = "profile.show")
        {
            code = errorCode;
            window.UseFixture(State());
            await (Task)safe.Invoke(window, [(Func<Task>)(() => (Task)show.Invoke(window, ["03", command])!)])!;
            Require(status.Text == message, "Profile RPC error did not reach the sidebar.");
        }
        // Only attachment-success branches call this callback in production.
        void Attached(JsonElement profile) => resolve.Invoke(window, [profile]);
        try
        {
            await FailOpen();
            Attached(Profile("02"));
            Require(status.Text == message, "Another profile's successful switch cleared profile 03's error.");
            selectedField.SetValue(window, "02");
            var navigation = navigationField.GetValue(window);
            Attached(Profile());
            Require(status.Text == "03 프로필의 Codex 창이 준비되었습니다." && Equals(status.ToolTip, status.Text),
                "Background recovery left the old profile-open error in the sidebar or tooltip.");
            Require(Equals(selectedField.GetValue(window), "02") && Equals(navigationField.GetValue(window), navigation),
                "Resolving a background profile's notice changed selection or navigation.");
            checks.Add("Recovered background attachment clears only its profile-open error without changing selection or navigation.");

            await FailOpen("profile_prepare_busy");
            Attached(Profile());
            Require(status.Text != message, "The profile preparation contention notice did not recover.");
            checks.Add("Both legacy update-lock and scoped preparation-busy profile-open notices recover.");

            foreach (var gate in new[] { "update_maintenance", "profile_maintenance", "ssh_maintenance" })
            {
                await FailOpen();
                foreach (var gateState in new[] { "held", "attention" })
                {
                    stateField.SetValue(window, State(gate, gateState));
                    Attached(Profile());
                    Require(status.Text == message, gate + " " + gateState + " was incorrectly reported as recovered.");
                }
                stateField.SetValue(window, State(gate, "released"));
                Attached(Profile());
                Require(status.Text != message, gate + " release did not allow a later attachment check to resolve the notice.");
            }
            await FailOpen();
            foreach (var phase in new[] { "attention", "waiting", "connecting", "closing" })
            {
                Attached(Profile(phase: phase));
                Require(status.Text == message, "An unresolved restart was incorrectly reported as recovered: " + phase);
            }
            Attached(Profile(status: "stopped"));
            Require(status.Text == message, "A stopped profile was reported as recovered.");
            Attached(Profile());
            Require(status.Text != message, "Completed restart did not resolve the notice on a later retained-window check.");
            checks.Add("Held or attention maintenance, unfinished restarts and stopped profiles keep the notice until recovery is confirmed.");

            foreach (var unrelated in new[] { "service_stopped", "maintenance_release_pending", "update_maintenance" })
            {
                await FailOpen(unrelated);
                Attached(Profile());
                Require(status.Text == message, "Unrelated error was cleared merely because its text matched: " + unrelated);
            }
            await FailOpen(command: "profile.login");
            Attached(Profile());
            Require(status.Text == message, "Login failure was cleared by a profile-open recovery.");
            await FailOpen();
            setStatus.Invoke(window, [message, true]);
            Attached(Profile());
            Require(status.Text == message, "A later unrelated error with identical text inherited the old profile scope.");
            setStatus.Invoke(window, ["다른 작업 완료", false]);
            Attached(Profile());
            Require(status.Text == "다른 작업 완료", "Background recovery overwrote a newer operation's status.");
            Require(requests.All(command => command is "profile.show" or "profile.login"), "Fixture reached an unexpected RPC.");
            checks.Add("Unrelated failures, login errors and later status messages are preserved even when their text matches the old error.");

            // Attach-first: the profile.show/login payload is attached, pinned with
            // its own 25 s budget, while the state request is still unanswered. The
            // action gate ends at attach; until a state requested after the show
            // lands, re-clicks, stale renders and background hosts use that payload.
            static JsonElement Launch(string id, string status = "running", bool blocked = false,
                string generation = "new-generation", int pid = 4242, int hwnd = 0) => JsonSerializer.SerializeToElement(new
            {
                id, alias = id, status, generation, process_id = pid, window_handle = hwnd, executable_path = @"C:\fixture\Codex.exe",
                restart = new { phase = "complete" }, login_health = new { blocks_launch = blocked, reason = blocked ? "expired" : "" }
            });
            static JsonElement Profiles(params JsonElement[] profiles) => JsonSerializer.SerializeToElement(new { profiles });
            static async Task Until(Func<bool> condition, string failure)
            {
                for (var started = Environment.TickCount64; !condition(); await Task.Delay(10))
                    Require(Environment.TickCount64 - started < 5000, failure);
            }
            var expected = type.GetField("_expectedWindowLaunch", flags)!;
            var deadline = type.GetField("_attachDeadline", flags)!;
            var embed = type.GetField("_embedRequested", flags)!;
            var shownField = type.GetField("_shownProfiles", flags)!;
            var gateField = type.GetField("_profileActions", flags)!;
            var retryField = type.GetField("_layoutRetryQueued", flags)!;
            // WPF's UIElement has an internal parameterless Render() as well.
            var render = type.GetMethod("Render", flags | BindingFlags.DeclaredOnly)!;
            var refresh = type.GetMethod("RefreshAsync", flags)!;
            var pinned = new WindowLaunchIdentity("03", "new-generation", 4242);
            const string searching = "Codex 기본 창을 찾고 있습니다";
            // Shows answer at once with the launch they return; every state request
            // waits on its own completion source, in request order.
            (MainWindow Window, List<string> Sent, List<TaskCompletionSource<JsonElement>> States) Fixture(bool blocked = false)
            {
                var sent = new List<string>();
                var states = new List<TaskCompletionSource<JsonElement>>();
                MainWindow fixtureWindow = null!;
                fixtureWindow = new MainWindow(root, fixture: true, fixtureRequest: (sentCommand, args) =>
                {
                    sent.Add(sentCommand);
                    if (sentCommand == "state")
                    {
                        states.Add(new(TaskCreationOptions.RunContinuationsAsynchronously));
                        return states[^1].Task;
                    }
                    // The show outlived BeginAttach's budget, as during admission waits.
                    deadline.SetValue(fixtureWindow, DateTime.UtcNow.AddSeconds(-1));
                    var id = JsonSerializer.SerializeToElement(args).S("profile_id");
                    return Task.FromResult(JsonSerializer.SerializeToElement(new { state = "launched", profile_id = id,
                        profile = id == "03" ? Launch(id, blocked: blocked) : Launch(id, generation: "other-generation", pid: 4343) }));
                }) { FixtureRefreshesState = true };
                return (fixtureWindow, sent, states);
            }
            Dictionary<string, JsonElement> ShownOf(MainWindow w) => (Dictionary<string, JsonElement>)shownField.GetValue(w)!;
            bool Kept(MainWindow w) => ShownOf(w).TryGetValue("03", out var kept) && WindowLaunchIdentity.From(kept) == pinned;
            JsonElement StateOf(MainWindow w) => (JsonElement)stateField.GetValue(w)!;
            bool Searching(MainWindow w) => Equals(expected.GetValue(w), pinned) && Equals(embed.GetValue(w), true) &&
                ((TextBlock)type.GetField("_empty", flags)!.GetValue(w)!).Text.StartsWith(searching, StringComparison.Ordinal);
            bool GateFree(MainWindow w)
            {
                try { ((ProfileActionGate)gateField.GetValue(w)!).Enter("03", "fixture").Dispose(); return true; }
                catch (InvalidOperationException) { return false; }
            }
            Task Open(MainWindow w, string id, string command = "profile.show") => (Task)show.Invoke(w, [id, command])!;
            Task Click(MainWindow w, string id) => (Task)safe.Invoke(w, [(Func<Task>)(() => Open(w, id))])!;
            foreach (var (command, blocked) in new[] { ("profile.show", false), ("profile.login", true) })
            {
                var (shownWindow, sent, states) = Fixture(blocked);
                try
                {
                    // For login the older state is blocked and stopped: login recovery.
                    var fresh = Profiles(Launch("03", blocked: blocked), Profile("02", status: "stopped"));
                    shownWindow.UseFixture(Profiles(Launch("03", "stopped", blocked, "fixture-generation", 0), Profile("02", status: "stopped")));
                    var opening = Open(shownWindow, "03", command);
                    Require(!opening.IsCompleted && sent.SequenceEqual(new[] { command, "state" }) && Searching(shownWindow) && Kept(shownWindow),
                        command + " did not attach its returned window before the state refresh answered.");
                    Require((DateTime)deadline.GetValue(shownWindow)! > DateTime.UtcNow.AddSeconds(20),
                        command + " left its returned launch on the show request's expired attach budget.");
                    Require(GateFree(shownWindow), command + " kept the profile's action gate after attaching its window.");
                    render.Invoke(shownWindow, null);
                    Require(Searching(shownWindow), "A stale state render failed or hid the launch returned by " + command + ".");
                    var reopening = Click(shownWindow, "03");
                    Require(!reopening.IsCompleted && sent.SequenceEqual(new[] { command, "state", "profile.show" }) && Searching(shownWindow),
                        "A re-click before the refresh was judged on the stale state instead of the returned launch.");
                    // The first refresh was requested before the re-click's show.
                    states[0].SetResult(fresh);
                    await opening;
                    Require(Kept(shownWindow) && StateOf(shownWindow).Arr("profiles").First().S("status") == "stopped",
                        "A state requested before the re-click's show replaced its returned launch.");
                    await Until(() => sent.Count == 4, "The re-click did not refresh state after its show.");
                    var held = ((ProfileActionGate)gateField.GetValue(shownWindow)!).Enter("03", "fixture");
                    states[1].SetResult(fresh);
                    await reopening;
                    Require(!GateFree(shownWindow), "A finished profile open released a later holder's action gate.");
                    held.Dispose();
                    Require(sent.SequenceEqual(new[] { command, "state", "profile.show", "state" }) && ShownOf(shownWindow).Count == 0 &&
                        Searching(shownWindow) && GateFree(shownWindow),
                        "The refreshed state did not replace the returned payload or lost the pinned launch.");
                }
                finally { shownWindow.Close(); }
            }
            checks.Add("profile.show and profile.login attach their returned launch before the state refresh with a fresh 25 s budget and free the action gate; a re-click and stale renders use that launch, never the older state's login recovery.");

            // A timer poll in flight when the show returns: the wait loop lets it
            // finish, its older result is discarded and exactly one fresh state follows.
            {
                var (pollWindow, sent, states) = Fixture();
                try
                {
                    pollWindow.UseFixture(Profiles(Launch("03", "stopped", false, "fixture-generation", 0), Profile("02", status: "stopped")));
                    var poll = (Task)refresh.Invoke(pollWindow, null)!;
                    var opening = Open(pollWindow, "03");
                    Require(sent.SequenceEqual(new[] { "state", "profile.show" }) && Searching(pollWindow) && GateFree(pollWindow),
                        "A show answered during a poll did not attach and free its gate at once.");
                    await Task.Delay(100);
                    Require(!opening.IsCompleted && sent.Count == 2, "The post-show refresh did not wait for the poll in flight.");
                    states[0].SetResult(Profiles(Launch("03", "stopped", false, "polled-before-show", 0), Profile("02", status: "stopped")));
                    await poll;
                    Require(StateOf(pollWindow).Arr("profiles").First().S("generation") == "fixture-generation" && Kept(pollWindow),
                        "A poll requested before the show replaced its returned launch.");
                    await Until(() => sent.Count == 3, "No state was requested after the poll in flight finished.");
                    states[1].SetResult(Profiles(Launch("03"), Profile("02", status: "stopped")));
                    await opening;
                    Require(sent.SequenceEqual(new[] { "state", "profile.show", "state" }) && ShownOf(pollWindow).Count == 0 &&
                        StateOf(pollWindow).Arr("profiles").First().S("generation") == "new-generation" && Searching(pollWindow),
                        "The post-show refresh did not replace the returned launch with exactly one fresh state.");
                }
                finally { pollWindow.Close(); }
            }
            checks.Add("A poll in flight when the show returns is waited for and discarded; exactly one fresh state then replaces the returned launch.");

            // A failed post-show state: the gate is already free, the open ends
            // without an error of its own and the returned launch stays in use.
            {
                var (failWindow, sent, states) = Fixture();
                try
                {
                    failWindow.UseFixture(Profiles(Launch("03", "stopped", false, "fixture-generation", 0), Profile("02", status: "stopped")));
                    var opening = Open(failWindow, "03");
                    Require(!opening.IsCompleted && GateFree(failWindow), "The action gate was held during the post-show refresh.");
                    states[0].SetException(new ManagerException("service_busy", "fixture state failed"));
                    await opening;
                    render.Invoke(failWindow, null);
                    var failStatus = (TextBlock)type.GetField("_status", flags)!.GetValue(failWindow)!;
                    Require(GateFree(failWindow) && Kept(failWindow) && Searching(failWindow) && sent.SequenceEqual(new[] { "profile.show", "state" }) &&
                        failStatus.Text.StartsWith("상태 갱신", StringComparison.Ordinal),
                        "A failed post-show state held the gate, dropped the returned launch or was not reported.");
                }
                finally { failWindow.Close(); }
            }
            checks.Add("A failed post-show state leaves the action gate free and keeps the returned launch until a later state lands.");

            // Switching away before the refresh lands. The older state still lists the
            // replaced launch as running (HWND 777); the returned launch has no window
            // yet. Background reconciliation must keep using the returned launch and
            // never hand the replaced one to AttachProfileWindow, which would detach
            // the first profile's window (in this windowless fixture: queue a layout retry).
            {
                var (switchWindow, sent, states) = Fixture();
                try
                {
                    var fresh = Profiles(Launch("03"), Launch("02", generation: "other-generation", pid: 4343));
                    switchWindow.UseFixture(Profiles(Launch("03", "running", false, "old-generation", 100, 777), Profile("02", status: "stopped")));
                    var opening = Open(switchWindow, "03");
                    Require(sent.SequenceEqual(new[] { "profile.show", "state" }) && Searching(switchWindow) && Equals(retryField.GetValue(switchWindow), false),
                        "The replacement launch was not attached before the state refresh answered.");
                    var switching = Click(switchWindow, "02");
                    Require(sent.SequenceEqual(new[] { "profile.show", "state", "profile.show" }) && Equals(selectedField.GetValue(switchWindow), "02") &&
                        Kept(switchWindow) && Equals(retryField.GetValue(switchWindow), false),
                        "Switching away handed the first profile's host the replaced launch from the older state.");
                    // The switch's navigation discards the first refresh; the launch survives it.
                    states[0].SetResult(fresh);
                    await opening;
                    render.Invoke(switchWindow, null);
                    Require(Kept(switchWindow) && StateOf(switchWindow).Arr("profiles").First().S("generation") == "old-generation" &&
                        Equals(retryField.GetValue(switchWindow), false),
                        "A discarded refresh dropped the returned launch or let the older state reach the background host.");
                    await Until(() => sent.Count == 4, "The second profile did not refresh state after its show.");
                    states[1].SetResult(fresh);
                    await switching;
                    Require(ShownOf(switchWindow).Count == 0 && StateOf(switchWindow).Arr("profiles").First().S("generation") == "new-generation" &&
                        Equals(retryField.GetValue(switchWindow), false),
                        "The state requested after both shows did not replace their returned launches.");
                }
                finally { switchWindow.Close(); }
            }
            checks.Add("Switching to another profile before the refresh lands keeps the first profile's returned launch; the older state never reaches its background host.");

            // A launch the service still reports in progress (queued behind the
            // leader gate, preparing, or spawned without a window) keeps waiting
            // past the 25 s budget, up to 90 s; any other missing window fails at 25 s.
            {
                var attach = type.GetMethod("TryAttach", flags)!;
                var empty = (TextBlock)type.GetField("_empty", flags)!.GetValue(window)!;
                static JsonElement Warmup(string state, JsonElement profile) => JsonSerializer.SerializeToElement(new
                {
                    profiles = new[] { profile },
                    profile_warmup = new { worker_active = true, profiles = new[] { new { profile_id = "03", state } } }
                });
                bool Waits(JsonElement state, JsonElement profile, int overdueSeconds)
                {
                    window.UseFixture(state);
                    embed.SetValue(window, true);
                    expected.SetValue(window, null);
                    deadline.SetValue(window, DateTime.UtcNow.AddSeconds(-overdueSeconds));
                    attach.Invoke(window, [profile, false]);
                    return Equals(embed.GetValue(window), true);
                }
                var closed = Profile(status: "not_started");
                foreach (var queued in new[] { "queued", "checking", "opening" })
                    Require(Waits(Warmup(queued, closed), closed, 30) && empty.Text.StartsWith(searching, StringComparison.Ordinal) &&
                        empty.Text.Contains("프로필을 여는 중입니다", StringComparison.Ordinal),
                        "A warmup launch still " + queued + " failed at the 25 s attach budget.");
                var spawned = Launch("03");
                Require(Waits(Warmup("started", spawned), spawned, 30), "A spawned launch without its first window failed at 25 s.");
                Require(!Waits(Warmup("opening", closed), closed, 70) && status.Text.StartsWith("90초", StringComparison.Ordinal),
                    "A launch in progress waited past the 90 s cap.");
                Require(!Waits(Warmup("attention", closed), closed, 1) && status.Text.StartsWith("25초", StringComparison.Ordinal),
                    "A failed warmup launch without a window waited past 25 s.");
            }
            checks.Add("A launch still queued, preparing or spawned without a window extends the 25 s attach budget up to 90 s; a failed one still reports at 25 s.");

            // A toast that starts the app: its profile leads the startup warmup,
            // read from the real ticket file before any state. An unreadable
            // ticket names no leader. A stale recent/activity owner of the
            // toast's task (a removed profile) never leads: before any state it
            // cannot be told from a live one, and the toast's open skips it.
            var notices = Path.Combine(root, "work", "control-center", "notifications");
            {
                string restored = Guid.NewGuid().ToString(), toast = Guid.NewGuid().ToString(), ticket = Guid.NewGuid().ToString("N");
                string removed = Guid.NewGuid().ToString(), thread = Guid.NewGuid().ToString();
                var activity = Path.Combine(notices, "activity");
                Directory.CreateDirectory(activity);
                var created = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                File.WriteAllText(Path.Combine(notices, ticket + ".json"), JsonSerializer.Serialize(new NotificationTarget(
                    toast, thread, "local", "fixture", created)));
                File.WriteAllText(Path.Combine(notices, "recent.json"), JsonSerializer.Serialize(new[] {
                    new NotificationTarget(removed, thread, "local", "fixture", created) }));
                File.WriteAllText(Path.Combine(activity, removed + ".json"), JsonSerializer.Serialize(new { version = 1, profileId = removed,
                    turns = new[] { new { threadId = thread, hostId = "local", at = created } } }));
                var routes = new WorkspaceNotifications(root, _ => { });
                Require(routes.ReadTicket(ticket, _ => true)?.ProfileId == removed && routes.ReadTicket(ticket, id => id != removed)?.ProfileId == toast,
                    "The fixture's recent/activity owner does not override the toast's profile only while it exists.");
                var session = new WorkspaceSession(1, restored, false);
                Require(MainWindow.StartupLeader(session, null, routes) == restored, "Without a toast the restored profile did not lead the startup warmup.");
                Require(MainWindow.StartupLeader(session, ticket, routes) == toast && MainWindow.StartupLeader(session with { ViewingCatalog = true }, ticket, routes) == toast,
                    "A toast that started the app did not make the profile its open uses the startup leader (a stale recent owner led).");
                Require(MainWindow.StartupLeader(session, Guid.NewGuid().ToString("N"), routes) is null && MainWindow.StartupLeader(session, ticket, null) is null,
                    "An unreadable toast ticket fell back to the restored profile instead of naming no leader.");
                Require(MainWindow.StartupLeader(session with { ViewingCatalog = true }, null, routes) is null && MainWindow.StartupLeader(null, null, routes) is null,
                    "A catalog session or a missing session named a startup leader.");
                // The toast's own stop derives from TryAttach's deadline, so TryAttach
                // always reports first (at 25 s, or 90 s while the launch is in progress).
                var now = DateTime.UtcNow;
                var slack = (TimeSpan)type.GetField("NotificationAttachSlack", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
                var grace = (TimeSpan)type.GetField("LaunchProgressGrace", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
                foreach (var launching in new[] { false, true })
                {
                    var due = now + (launching ? grace : TimeSpan.Zero);
                    Require(slack > TimeSpan.Zero && MainWindow.AttachOverdue(due, now, launching) && !MainWindow.NotificationAttachOverdue(due, now, launching) &&
                        MainWindow.NotificationAttachOverdue(due + slack, now, launching),
                        "The toast's attach wait can end before TryAttach reports the missing window.");
                }
            }
            checks.Add("A toast that starts the app makes the profile its open uses the startup leader, never a stale recent/activity owner (none for an unreadable ticket); its attach wait ends only after TryAttach's.");

            // A toast's wait ends with TryAttach's report. Its launch is still in
            // progress (spawned without a window) when TryAttach's 90 s pass:
            // that error stays, and the toast neither keeps polling nor replaces it.
            {
                string toastProfile = Guid.NewGuid().ToString(), toastTicket = Guid.NewGuid().ToString("N");
                File.WriteAllText(Path.Combine(notices, toastTicket + ".json"), JsonSerializer.Serialize(new NotificationTarget(
                    toastProfile, Guid.NewGuid().ToString(), "local", "fixture", DateTimeOffset.UtcNow.ToUnixTimeMilliseconds())));
                var spawnedToast = Launch(toastProfile);
                MainWindow toastWindow = null!;
                toastWindow = new MainWindow(root, fixture: true, fixtureRequest: (sentCommand, _) =>
                {
                    // The state after the show lands past TryAttach's extended budget.
                    if (sentCommand == "state") deadline.SetValue(toastWindow, DateTime.UtcNow.AddSeconds(-70));
                    return Task.FromResult(sentCommand == "state" ? Profiles(spawnedToast)
                        : JsonSerializer.SerializeToElement(new { state = "launched", profile_id = toastProfile, profile = spawnedToast }));
                }) { FixtureRefreshesState = true };
                try
                {
                    type.GetField("_workspaceNotifications", flags)!.SetValue(toastWindow, new WorkspaceNotifications(root, _ => { }));
                    toastWindow.UseFixture(Profiles(Launch(toastProfile, "not_started", pid: 0)));
                    var openToast = type.GetMethod("OpenNotificationAsync", flags)!;
                    var toastOpen = (Task)safe.Invoke(toastWindow, [(Func<Task>)(() => (Task)openToast.Invoke(toastWindow, [toastTicket])!)])!;
                    Require(await Task.WhenAny(toastOpen, Task.Delay(3000)) == toastOpen,
                        "A toast kept waiting after TryAttach had reported the missing window.");
                    await toastOpen;
                    var toastStatus = (TextBlock)type.GetField("_status", flags)!.GetValue(toastWindow)!;
                    Require(toastStatus.Text.StartsWith("90초", StringComparison.Ordinal) && Equals(embed.GetValue(toastWindow), false),
                        "A toast replaced TryAttach's missing-window error: " + toastStatus.Text);
                }
                finally { toastWindow.Close(); }
            }
            checks.Add("A toast's window wait ends quietly once TryAttach reports the missing window and keeps that error.");

            // Usage refreshes every 300 s, so card and header age labels start at 600 s.
            static JsonElement Usage(int age) => JsonSerializer.SerializeToElement(new { id = "03", alias = "03", status = "running",
                usage = new { age_seconds = age, freshness = "live", windows = new[] { new { label = "주간", remaining_percent = 50 } } } });
            var header = type.GetMethod("Usage", BindingFlags.Static | BindingFlags.NonPublic)!;
            string Header(int age) => (string)header.Invoke(null, [Usage(age).Get("usage")])!;
            Require(ProfileCardData.Create(Usage(599), "03").Freshness == "" && ProfileCardData.Create(Usage(600), "03").Freshness == "10분 전 값" &&
                !Header(599).Contains("분 전 값", StringComparison.Ordinal) && Header(600).EndsWith(" · 10분 전 값", StringComparison.Ordinal),
                "Usage age labels do not match the 300 s refresh cadence.");
            checks.Add("Usage cards and header label a value's age only from 600 s, two 300 s refresh periods.");
            return checks;
        }
        finally { window.Close(); }
    }
}
