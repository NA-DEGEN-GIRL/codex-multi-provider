using System.Diagnostics;
using System.IO;
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

        await CheckGracefulExitWaitAsync(checks);
    }).GetAwaiter().GetResult();

    private static async Task CheckGracefulExitWaitAsync(List<string> checks)
    {
        void Require(bool value, string message) { if (!value) throw new InvalidOperationException(message); }
        foreach (var (apps, seconds) in new[] { (0, 60), (1, 60), (3, 60), (4, 70), (11, 140), (15, 180), (40, 180) })
            Require(NativeWindowShutdown.GracefulWait(apps) == TimeSpan.FromSeconds(seconds),
                $"Full-exit wait for {apps} apps is not {seconds} s.");
        Require(NativeWindowShutdown.QuitGrace >= TimeSpan.FromSeconds(20),
            "A closed window is reaped sooner than a second 완전 종료 press would.");
        checks.Add("Full exit waits 60 s for up to three apps, 10 s more for each further app and at most 3 minutes (11 apps: 140 s); a closed window gets 20 s more.");

        Require(NativeWindowShutdown.Progress(9, 11, TimeSpan.FromSeconds(75.6), TimeSpan.FromSeconds(140)) ==
            "관리 중인 Codex 종료를 기다리고 있습니다 · 9/11 종료됨 · 75초 (최대 140초)", "Full-exit progress omits N/M or elapsed seconds.");
        Require(NativeWindowShutdown.Progress(10, 11, TimeSpan.FromSeconds(150), TimeSpan.FromSeconds(140)) ==
            "창을 닫은 Codex의 종료 정리를 기다리고 있습니다 · 10/11 종료됨 · 150초", "Grace progress still promised the expired limit.");

        var exited = new List<string>();
        var ticks = new List<int>();
        var watch = Stopwatch.StartNew();
        var results = await NativeWindowShutdown.WaitAllAsync(new[] { "exits", "quitting", "open", "changed" }, TimeSpan.FromMilliseconds(400),
            async (target, deadline) =>
            {
                if (target == "changed") throw new InvalidOperationException("Codex process identity changed before shutdown.");
                if (target == "exits") { await Task.Delay(50, CancellationToken.None); return NativeExitOutcome.Exited; }
                try { await Task.Delay(Timeout.Infinite, deadline); }
                catch (OperationCanceledException) when (deadline.IsCancellationRequested) { }
                return target == "quitting" ? NativeExitOutcome.Quitting : NativeExitOutcome.Unresponsive;
            },
            target => { lock (exited) exited.Add(target); },
            (count, _) => { lock (ticks) ticks.Add(count); }, TimeSpan.FromMilliseconds(20));
        Require(watch.Elapsed >= TimeSpan.FromMilliseconds(350) && watch.Elapsed < TimeSpan.FromSeconds(10),
            "Quit requests did not share the full-exit deadline.");
        var outcome = results.ToDictionary(r => r.Target);
        Require(exited.SequenceEqual(new[] { "exits" }) && outcome["exits"] is { Outcome: NativeExitOutcome.Exited, Error: null } &&
            outcome["quitting"] is { Outcome: NativeExitOutcome.Quitting, Error: null } &&
            outcome["open"] is { Outcome: NativeExitOutcome.Unresponsive, Error: null } &&
            outcome["changed"].Error is InvalidOperationException && results.Count == 4,
            "Full-exit wait lost an outcome, counted a failed request as exited or hid an identity change.");
        lock (ticks) Require(ticks.Count > 2 && ticks[0] == 0 && ticks[^1] == 1 && ticks.SequenceEqual(ticks.Order()),
            "Full-exit progress did not report confirmed exits while waiting.");
        Require((await NativeWindowShutdown.WaitAllAsync(Array.Empty<string>(), TimeSpan.FromSeconds(1),
                (_, _) => throw new InvalidOperationException("No request expected."), _ => { }, (_, _) => { })).Count == 0,
            "An exit with no managed apps waited or requested a quit.");
        var root = Path.Combine(Path.GetTempPath(), "codex-exit-selftest-" + Guid.NewGuid().ToString("N"));
        Require(await NativeWindowShutdown.RequestAsync(root, int.MaxValue, 0, "", 0, CancellationToken.None, TimeSpan.Zero) ==
            NativeExitOutcome.Exited && !Directory.Exists(root), "A process that is already gone was not treated as exited.");
        checks.Add("All quit requests share one deadline with N/M progress; exited, still quitting (window closed), open-window and identity-change outcomes stay distinct.");
    }
}
