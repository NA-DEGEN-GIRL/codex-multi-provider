using System.Reflection;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class ShortcutLoginSelfTest
{
    internal static void Run(string root, List<string> checks)
    {
        void Require(bool ok, string message) { if (!ok) throw new InvalidOperationException(message); }
        // Exercise activation races without activating desktop windows or sending input.
        nint foreground = 10;
        bool selected = true, managerValid = true, activateOk = true;
        int activations = 0, identityChecks = 0;
        Action verify = () => identityChecks++;
        nint Prepare(nint initial = 10) => ConversationCaptureActivation.Prepare(20, initial, 10,
            () => foreground, () => managerValid, () => selected, () => verify(), hwnd =>
            { activations++; if (activateOk) foreground = hwnd; return activateOk; });
        void Reject(string reason)
        {
            bool rejected = false;
            try { Prepare(); } catch (InvalidOperationException) { rejected = true; }
            Require(rejected, reason);
        }
        Require(Prepare() == 20 && activations == 1 && identityChecks == 2,
            "Foreground manager did not activate exactly its verified viewport once.");
        Require(Prepare(20) == 20 && activations == 1, "Already active viewport was activated again.");
        foreground = 30; Reject("Alt-tab during clipboard snapshot was accepted.");
        Require(activations == 1, "An unrelated foreground window was stolen.");
        foreground = 10; selected = false; Reject("Changed profile selection was accepted.");
        Require(activations == 1, "Stale profile was activated.");
        selected = true; managerValid = false; Reject("Foreign or disabled manager was accepted.");
        Require(activations == 1, "Invalid manager caused activation.");
        managerValid = true; verify = () => { identityChecks++; foreground = 30; };
        Reject("Foreground change during identity validation was ignored.");
        Require(activations == 1, "Identity-check race stole focus.");
        foreground = 10; verify = () => identityChecks++; activateOk = false;
        Reject("Rejected Windows activation was accepted.");
        Require(activations == 2, "Activation failure was retried.");
        checks.Add("Explicit capture transfers foreground manager to its selected viewport once; active viewport, Alt-tab, changed selection, invalid manager and rejected activation are handled without input.");

        JsonElement Json(object value) => JsonSerializer.SerializeToElement(value);
        var target = new { id = "00000000-0000-4000-8000-000000000004", alias = "04", auth_mode = "native",
            status = "running", runtime_channel = "managed", generation = "fixture-generation", process_id = 0 };
        var other = new { id = "00000000-0000-4000-8000-000000000001", alias = "01", auth_mode = "native" };
        var requests = new List<(string Command, JsonElement Args)>();
        string loginState = "unverified";
        bool approved = false, failStop = false;
        int confirms = 0;
        var state = Json(new { profiles = new object[] { other, target }, shortcuts = Array.Empty<object>() });
        var window = new MainWindow(root, fixture: true, fixtureRequest: (command, args) =>
        {
            requests.Add((command, Json(args ?? new { })));
            if (command == "profile.login_status") return Task.FromResult(Json(new { state = loginState }));
            if (command == "profile.recover" && failStop) return Task.FromException<JsonElement>(new InvalidOperationException("fixture_stop_failed"));
            return Task.FromResult(command == "state" ? state : Json(new { state = "stopped", profile = target }));
        });
        window.UseFixture(state);
        window.ConfirmProfileRestart = (message, _) =>
        {
            confirms++;
            Require(message.Contains("04") && message.Contains("SSH") && message.Contains("다른 프로필"),
                "Login restart confirmation omitted its target or impact.");
            return approved;
        };
        void Login()
        {
            var method = typeof(MainWindow).GetMethod("LoginProfileAsync", BindingFlags.NonPublic | BindingFlags.Instance,
                null, [typeof(string)], null)!;
            var task = (Task)method.Invoke(window, [target.id])!;
            Require(task.IsCompleted, "Fixture login unexpectedly started background work.");
            task.GetAwaiter().GetResult();
        }
        try
        {
            Login();
            Require(confirms == 1 && requests.Select(r => r.Command).SequenceEqual(["profile.login_status"]),
                "Cancelled login restart changed a running profile.");
            requests.Clear(); approved = true;
            Login();
            Require(requests.Where(r => r.Command.StartsWith("profile.")).Select(r => r.Command)
                .SequenceEqual(["profile.login_status", "profile.recover", "profile.login"]),
                "Login repair reopened a normal bound window or skipped verified exit.");
            var stop = requests.Single(r => r.Command == "profile.recover").Args;
            Require(stop.S("profile_id") == target.id && stop.S("expected_generation") == target.generation && stop.B("interrupt_running_work"),
                "Login repair lost the selected launch identity or explicit consent.");
            Require(requests.Where(r => r.Command.StartsWith("profile.")).All(r => r.Args.S("profile_id") == target.id),
                "Login repair affected another profile.");
            requests.Clear(); failStop = true;
            try { Login(); throw new Exception("Failed exit still opened login."); }
            catch (InvalidOperationException error) when (error.Message == "fixture_stop_failed") { }
            Require(requests.All(r => r.Command != "profile.login"), "Login opened before old process exit was confirmed.");
            requests.Clear(); failStop = false; loginState = "signed_in"; int before = confirms;
            Login();
            Require(confirms == before && requests.All(r => r.Command != "profile.recover"),
                "A valid login unnecessarily stopped its profile.");
        }
        finally { window.Close(); }
        foreach (var profile in new[] {
            Json(new { status = "not_started", runtime_channel = "managed" }),
            Json(new { status = "running", runtime_channel = "packaged" }),
            Json(new { status = "running", runtime_channel = "managed", native_login_pending = true }) })
            Require(!ProfileLoginPresentation.RequiresRestartForLogin(profile, Json(new { state = "unverified" })),
                "An already prepared login window was restarted.");
        checks.Add("Actual login action keeps cancellation side-effect free, stops only the consented launch then opens profile.login, aborts on failed stop, and preserves valid/pending logins.");
    }
}
