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
        var pendingRemote = State.Replace("\"profile_restarts\":{}",
            "\"profile_restarts\":{\"profile\":{\"phase\":\"waiting\",\"remote_background\":true,\"stop_only\":true}}");
        Require(WorkspaceShutdown.CanDrainLegacy(Json(pendingRemote), Json(Status)),
            "Saved remote reconciliation prevented explicit local shutdown.");
        foreach (var bad in new[] { pendingRemote.Replace("\"remote_background\":true", "\"remote_background\":false"),
            pendingRemote.Replace("\"remote_background\":true", "\"remote_background\":\"true\""),
            pendingRemote.Replace("\"phase\":\"waiting\"", "\"phase\":\"opening\"") })
            Require(!WorkspaceShutdown.CanDrainLegacy(Json(bad), Json(Status)),
                "A local, malformed or active-opening job bypassed the shutdown guard.");
        foreach (var bad in new[] { State.Replace("not_started", "running"), State.Replace("null", "42"),
            State.Replace("\"stopping\":true", "\"stopping\":false"),
            State.Replace("\"worker_active\":false", "\"worker_active\":true"), "{}" })
            Require(!WorkspaceShutdown.CanDrainLegacy(Json(bad), Json(Status)), "Unsafe legacy drain admitted.");
        foreach (var bad in new[] { Status.Replace("\"clients\":1", "\"clients\":2"),
            Status.Replace("\"pending_requests\":0", "\"pending_requests\":1"), "{}" })
            Require(!WorkspaceShutdown.CanDrainLegacy(Json(State), Json(bad)), "Other clients or pending requests bypassed drain guard.");
        checks.Add("Full exit accepts a saved SSH retry only after closed profiles, stopped launches and idle management; malformed state is rejected.");

        var pendingLocal = State.Replace("\"profile_restarts\":{}",
            "\"profile_restarts\":{\"profile\":{\"phase\":\"waiting\",\"automatic_key\":\"next-version\"}}");
        Require(!WorkspaceShutdown.CanDrainLegacy(Json(pendingLocal), Json(Status)),
            "Local restart skipped its checkpoint.");
        Require(WorkspaceShutdown.CanCheckpointLegacy(Json(pendingLocal), Json(Status)),
            "Unstarted local settings reservation blocked legacy checkpoint.");
        foreach (var unsafeState in new[] {
            pendingLocal.Replace("\"automatic_key\":\"next-version\"", "\"transaction_id\":\"already-started\""),
            pendingLocal.Replace("\"automatic_key\":\"next-version\"", "\"recovery_transaction_id\":\"old-close\""),
            pendingLocal.Replace("\"automatic_key\":\"next-version\"", "\"connection_transaction_id\":\"connecting\""),
            pendingLocal.Replace("\"phase\":\"waiting\"", "\"phase\":\"closing\""),
            pendingLocal.Replace("not_started", "running"),
            pendingLocal.Replace("\"stopping\":true", "\"stopping\":false") })
            Require(!WorkspaceShutdown.CanCheckpointLegacy(Json(unsafeState), Json(Status)),
                "An active transaction, profile or launch queue bypassed legacy checkpoint.");
        checks.Add("Legacy checkpoint accepts only closed profiles with unstarted local reservations; admitted restart transactions remain guarded.");

        var checkpoint = new WorkspaceShutdown();
        var checkpointCalls = new List<string>();
        int checkpointAttempts = 0;
        Task<JsonElement> CheckpointRequest(string command, CancellationToken token)
        {
            checkpointCalls.Add(command);
            if (command == "supervisor.reconnect" && checkpointAttempts++ == 0)
                throw new ManagerException("backend_busy", "예약 처리기 종료 중");
            return Task.FromResult(command == "state" ? Json(pendingLocal) : Json(Status));
        }
        await checkpoint.SettleLegacyRestartsAsync(CheckpointRequest, _ => { }, CancellationToken.None);
        Require(!checkpoint.BackendResetting && !checkpoint.DrainStarted &&
            checkpointCalls.SequenceEqual(new[] { "state", "supervisor.status", "supervisor.reconnect",
                "supervisor.reconnect", "manager.stop_warmup" }),
            "Legacy reservation checkpoint replayed startup or failed to restore the launch barrier.");
        checks.Add("Old adapter exits by EOF before its replacement handles remote stops, without restarting profiles or automatic startup jobs.");

        using var checkpointDeadline = new CancellationTokenSource();
        Task<JsonElement> PendingCheckpoint(string command, CancellationToken token)
        {
            if (command == "supervisor.reconnect")
            { checkpointDeadline.Cancel(); throw new ManagerException("backend_busy", "예약 처리기 종료 중"); }
            return Task.FromResult(command == "state" ? Json(pendingLocal) : Json(Status));
        }
        try
        {
            await checkpoint.SettleLegacyRestartsAsync(PendingCheckpoint, _ => { }, checkpointDeadline.Token);
            throw new InvalidOperationException("Checkpoint timeout disappeared.");
        }
        catch (OperationCanceledException) { }
        Require(checkpoint.BackendResetting, "Uncertain adapter exit resumed profile launches.");
        checkpointCalls.Clear();
        await checkpoint.SettleLegacyRestartsAsync(CheckpointRequest, _ => { }, CancellationToken.None);
        Require(checkpointCalls.SequenceEqual(new[] { "supervisor.reconnect", "manager.stop_warmup" }),
            "Checkpoint retry requested state before old adapter exit was confirmed.");
        checks.Add("Interrupted legacy checkpoint resumes from EOF confirmation, keeping launches and automatic navigation paused.");

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

        var earlyModern = new WorkspaceShutdown();
        calls.Clear();
        Task<JsonElement> EarlyModernRequest(string command, CancellationToken token)
        {
            calls.Add(command);
            if (command == "supervisor.shutdown")
                throw new InvalidOperationException("An older service would refuse its nonterminal remote journal.");
            return Task.FromResult(command == "state" ? Json(pendingRemote) :
                Json(Status[..^1] + ",\"graceful_shutdown\":true,\"shutdown_draining\":false}"));
        }
        await earlyModern.FinishAsync(EarlyModernRequest, _ => { }, CancellationToken.None);
        Require(earlyModern.DrainStarted && calls.Count(c => c == "supervisor.reconnect") == 1 &&
            calls.Last() == "supervisor.retire", "Early graceful service failed to stop with a saved remote journal.");
        checks.Add("Early graceful services use guarded EOF when remote journals would prevent their shutdown endpoint from starting.");

        var modern = new WorkspaceShutdown();
        calls.Clear();
        int attempts = 0;
        Task<JsonElement> ModernRequest(string command, CancellationToken token)
        {
            calls.Add(command);
            if (command == "supervisor.shutdown" && attempts++ == 0) throw new ManagerException("backend_busy", "SSH 확인 마무리 중");
            return Task.FromResult(Json(Status[..^1] + ",\"graceful_shutdown\":true,\"shutdown_remote_journals\":true,\"shutdown_draining\":false}"));
        }
        await modern.FinishAsync(ModernRequest, _ => { }, CancellationToken.None);
        Require(modern.DrainStarted && calls.Count(c => c == "supervisor.shutdown") == 2 && !calls.Contains("supervisor.reconnect"),
            "New service bypassed atomic shutdown path.");
        checks.Add("New service uses guarded full shutdown, preserving drain state across a pending-worker reply.");

        var alreadyDraining = new WorkspaceShutdown();
        calls.Clear();
        Task<JsonElement> DrainingRequest(string command, CancellationToken token)
        {
            calls.Add(command);
            return Task.FromResult(Json(Status[..^1] + ",\"graceful_shutdown\":true,\"shutdown_draining\":true}"));
        }
        await alreadyDraining.FinishAsync(DrainingRequest, _ => { }, CancellationToken.None);
        Require(!calls.Contains("state") && !calls.Contains("supervisor.reconnect") &&
            calls.Last() == "supervisor.shutdown", "Already-draining service incorrectly restarted legacy observation.");
        checks.Add("An older service that already began EOF stays on its atomic shutdown path without a new backend state request.");

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
