using System.Text.Json;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

// Used only after the explicit full-exit action has drained and closed profiles.
// A normal window close and an automatic service upgrade never use this path.
internal sealed class WorkspaceShutdown
{
    internal bool DrainStarted { get; private set; }
    internal int ServicePid { get; private set; }

    internal async Task<int> FinishAsync(Func<string, CancellationToken, Task<JsonElement>> request,
        Action<string> progress, CancellationToken cancellation)
    {
        string last = "관리 서비스 종료 상태를 확인하고 있습니다.";
        void Report(string message)
        {
            if (message == last) return;
            last = message; progress(message);
        }
        try
        {
            var status = await request("supervisor.status", cancellation);
            int pid = (int)status.N("supervisor_pid");
            ServicePid = pid;
            if (status.B("graceful_shutdown"))
            {
                DrainStarted |= status.B("shutdown_draining");
                while (true)
                {
                    try { await request("supervisor.shutdown", cancellation); DrainStarted = true; return pid; }
                    catch (ManagerException error) when (error.Code == "backend_busy")
                    {
                        Report(error.Message);
                        status = await request("supervisor.status", cancellation);
                        DrainStarted |= status.B("shutdown_draining");
                        await Task.Delay(200, cancellation);
                    }
                }
            }

            // Revisions before 81 count persistent SSH retry journals as running
            // workers. Their existing reconnect RPC closes adapter stdin and
            // waits for normal exit. Use it only with closed profiles, a launch
            // barrier, no other clients and no active management request.
            if (!DrainStarted)
            {
                var state = await request("state", cancellation);
                status = await request("supervisor.status", cancellation);
                if (!CanDrainLegacy(state, status))
                    throw new InvalidOperationException("다른 관리창 또는 진행 중인 관리 작업이 있습니다. 다른 관리창을 닫고 잠시 뒤 완전 종료를 다시 눌러 주세요.");
                DrainStarted = true;
                Report("이전 관리 서비스의 자동 확인을 멈추고 종료합니다. SSH 업데이트 예약 기록은 보존됩니다.");
            }
            while (true)
            {
                try { await request("supervisor.reconnect", cancellation); break; }
                catch (ManagerException error) when (error.Code == "backend_busy")
                { Report(error.Message); await Task.Delay(200, cancellation); }
            }
            while (true)
            {
                try { await request("supervisor.retire", cancellation); return pid; }
                catch (ManagerException error) when (error.Code == "backend_busy")
                { Report(error.Message); await Task.Delay(200, cancellation); }
            }
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            throw new InvalidOperationException($"종료 대기 중: {last} 잠시 뒤 완전 종료를 다시 눌러 주세요.");
        }
    }

    internal static bool CanDrainLegacy(JsonElement state, JsonElement status)
    {
        static bool Exited(JsonElement list) => list.ValueKind == JsonValueKind.Array && list.EnumerateArray().All(p =>
            p.S("status") is "not_started" or "stopped" or "unprepared" &&
            p.Get("process_id").ValueKind is JsonValueKind.Null or JsonValueKind.Undefined);
        if (status.S("engine") != "rust" || status.N("clients") != 1 || status.N("pending_requests") != 0 ||
            status.Get("operations").ValueKind != JsonValueKind.Array || status.Arr("operations").Any() ||
            !Exited(state.Get("profiles")) || !Exited(state.Get("view_instances")) ||
            !state.Get("local_launches").B("stopping") || state.Get("local_launches").S("active") != "0") return false;
        foreach (var key in new[] { "updates", "startup_updates", "profile_warmup" })
            if (state.Get(key).Get("worker_active").ValueKind != JsonValueKind.False) return false;
        var restarts = state.Get("profile_restarts");
        return restarts.ValueKind == JsonValueKind.Object && restarts.EnumerateObject().All(j =>
            j.Value.S("phase") is "complete" or "attention" or "superseded");
    }
}
