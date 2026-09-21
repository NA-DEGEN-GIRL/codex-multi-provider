using System.Text.Json;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal static class WorkspaceShutdownSelfTest
{
    private static JsonElement Json(string value) => JsonDocument.Parse(value).RootElement.Clone();
    private const string State = """
        {"profiles":[{"status":"not_started","process_id":null}],"view_instances":[],
        "local_launches":{"active":0,"stopping":true},"profile_restarts":{},
        "updates":{"worker_active":false},"startup_updates":{"worker_active":false},
        "profile_warmup":{"worker_active":false},
        "remote_updates":{"worker_active":true,"items":[{"job":{"state":"waiting"}}]}}
        """;
    private const string Status = """
        {"supervisor_pid":123,"engine":"rust","clients":1,"pending_requests":0,"operations":[]}
        """;

    internal static void Run(List<string> checks) => Task.Run(async () =>
    {
        void Require(bool value, string message) { if (!value) throw new InvalidOperationException(message); }
        Require(WorkspaceShutdown.CanDrainLegacy(Json(State), Json(Status)), "SSH retry journal prevented graceful legacy drain.");
        foreach (var bad in new[] { State.Replace("not_started", "running"), State.Replace("null", "42"),
            State.Replace("\"stopping\":true", "\"stopping\":false"),
            State.Replace("\"worker_active\":false", "\"worker_active\":true"), "{}" })
            Require(!WorkspaceShutdown.CanDrainLegacy(Json(bad), Json(Status)), "Unsafe legacy drain admitted.");
        foreach (var bad in new[] { Status.Replace("\"clients\":1", "\"clients\":2"),
            Status.Replace("\"pending_requests\":0", "\"pending_requests\":1"), "{}" })
            Require(!WorkspaceShutdown.CanDrainLegacy(Json(State), Json(bad)), "Other clients or pending requests bypassed drain guard.");
        checks.Add("Full exit accepts a saved SSH retry only after closed profiles, stopped launches and idle management; malformed state is rejected.");

        var calls = new List<string>();
        var legacy = new WorkspaceShutdown();
        int drains = 0;
        Task<JsonElement> LegacyRequest(string command, CancellationToken token)
        {
            calls.Add(command);
            if (command == "supervisor.reconnect" && drains++ == 0) throw new ManagerException("backend_busy", "SSH 확인 마무리 중");
            return Task.FromResult(command == "state" ? Json(State) : Json(Status));
        }
        Require(await legacy.FinishAsync(LegacyRequest, _ => { }, CancellationToken.None) == 123 && legacy.DrainStarted,
            "Legacy full exit failed.");
        Require(calls.Count(c => c == "state") == 1 && calls.Count(c => c == "supervisor.reconnect") == 2 && calls.Last() == "supervisor.retire",
            "Legacy drain restarted backend or skipped verified exit.");
        checks.Add("An older service drains through its existing EOF path before retirement; retry does not start a replacement backend.");

        var modern = new WorkspaceShutdown();
        calls.Clear();
        int attempts = 0;
        Task<JsonElement> ModernRequest(string command, CancellationToken token)
        {
            calls.Add(command);
            if (command == "supervisor.shutdown" && attempts++ == 0) throw new ManagerException("backend_busy", "SSH 확인 마무리 중");
            return Task.FromResult(Json(Status[..^1] + ",\"graceful_shutdown\":true,\"shutdown_draining\":true}"));
        }
        await modern.FinishAsync(ModernRequest, _ => { }, CancellationToken.None);
        Require(modern.DrainStarted && calls.Count(c => c == "supervisor.shutdown") == 2 && !calls.Contains("supervisor.reconnect"),
            "New service bypassed atomic shutdown path.");
        checks.Add("New service uses guarded full shutdown, preserving drain state across a pending-worker reply.");

        var retry = new WorkspaceShutdown();
        using var deadline = new CancellationTokenSource();
        Task<JsonElement> TimeoutRequest(string command, CancellationToken token)
        {
            if (command == "supervisor.reconnect") { deadline.Cancel(); throw new ManagerException("backend_busy", "SSH 확인 마무리 중"); }
            return Task.FromResult(command == "state" ? Json(State) : Json(Status));
        }
        try { await retry.FinishAsync(TimeoutRequest, _ => { }, deadline.Token); throw new InvalidOperationException("Timeout disappeared."); }
        catch (InvalidOperationException error) when (error.Message.StartsWith("종료 대기 중:"))
        { Require(error.Message.Contains("SSH 확인 마무리 중"), "Timeout hid the blocker."); }
        Require(retry.DrainStarted, "Uncertain EOF was treated as safe to resume launching.");
        calls.Clear();
        await retry.FinishAsync(LegacyRequest, _ => { }, CancellationToken.None);
        Require(!calls.Contains("state"), "Retry restarted backend after EOF.");
        checks.Add("A timed-out drain reports its blocker and retries cleanup without relaunching profiles or backend.");
    }).GetAwaiter().GetResult();
}
