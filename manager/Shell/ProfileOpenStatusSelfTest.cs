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
            return checks;
        }
        finally { window.Close(); }
    }
}
