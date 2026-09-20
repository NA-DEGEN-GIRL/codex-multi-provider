using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Supervisor;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length != 2 || args[0] != "--root") return 2;
        try
        {
            var root = ManagerProtocol.NormalizeRoot(args[1]);
            if (!File.Exists(Path.Combine(root, "scripts", "control_center.py"))) return 2;
            var pipe = ManagerProtocol.PipeName(root);
            // Keep ownership on this thread: named Mutex ownership is thread-affine.
            using var singleton = new Mutex(false, "Local\\" + pipe);
            var owns = false;
            try { owns = singleton.WaitOne(0); }
            catch (AbandonedMutexException) { owns = true; }
            if (!owns) return 0;
            // Do not await after acquiring a thread-affine Mutex on the main thread.
            try { new SupervisorHost(root, pipe).RunAsync().GetAwaiter().GetResult(); }
            finally { singleton.ReleaseMutex(); }
            return 0;
        }
        catch (Exception) { return 1; } // No raw diagnostic logs or secrets.
    }
}

internal sealed class SupervisorHost(string root, string pipeName)
{
    private static readonly HashSet<string> Commands = new(StringComparer.Ordinal)
    {
        "manager.startup",
        "manager.recover_legacy",
        "state", "accounts.refresh", "profile.add", "profile.register_current", "profile.remove", "profile.restore", "profile.bind", "profile.rename", "profile.show", "profile.prepare", "profile.login", "profile.login_status",
        "shortcut.add", "shortcut.move", "shortcut.rename", "shortcut.delete", "shortcut.undo", "conversation.open", "conversation.continue", "conversation.navigate", "handoff.preview", "catalog.list", "catalog.show", "catalog.resolve",
        "policy.set", "profile.model_settings", "profile.restart", "profile.recover", "providers.list", "providers.save", "providers.key", "providers.verify", "updates.check", "updates.prepare", "updates.apply",
        "remote.list", "remote.inspect", "remote.prepare",
        "remote.updates.status", "remote.updates.check", "remote.updates.settings",
        "remote.updates.schedule", "remote.updates.cancel", "remote.updates.stock_update",
    };
    private readonly object lifecycle = new();
    private readonly CancellationTokenSource shutdown = new();
    private readonly HashSet<Task> handlers = [];
    private readonly PythonBackend backend = new(root);
    private int clients;
    private DateTime noClientsSince = DateTime.UtcNow;
    private bool stopping;
    private bool upgrading;

    public async Task RunAsync()
    {
        var monitor = MonitorIdleAsync();
        try
        {
            while (!shutdown.IsCancellationRequested)
            {
                var pipe = new NamedPipeServerStream(pipeName, PipeDirection.InOut, 16, PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly, 16384, 16384);
                try { await pipe.WaitForConnectionAsync(shutdown.Token).ConfigureAwait(false); }
                catch { pipe.Dispose(); if (shutdown.IsCancellationRequested) break; throw; }
                Task handler;
                lock (lifecycle)
                {
                    if (stopping || clients >= 12) { pipe.Dispose(); continue; }
                    clients++;
                    handler = ServeAsync(pipe);
                    handlers.Add(handler);
                }
                _ = handler.ContinueWith(done => { lock (lifecycle) handlers.Remove(done); }, TaskScheduler.Default);
            }
        }
        finally
        {
            shutdown.Cancel();
            try { await monitor.ConfigureAwait(false); } catch (OperationCanceledException) { }
            Task[] pending;
            lock (lifecycle) pending = handlers.ToArray();
            await Task.WhenAll(pending).ConfigureAwait(false);
            await backend.DisposeAsync().ConfigureAwait(false);
        }
    }

    private async Task ServeAsync(NamedPipeServerStream pipe)
    {
        try
        {
            var reader = new JsonLineReader(pipe, ManagerProtocol.MaxRequestBytes);
            while (pipe.IsConnected && !shutdown.IsCancellationRequested)
            {
                var line = await reader.ReadAsync(shutdown.Token).ConfigureAwait(false);
                if (line is null) break;
                var response = await DispatchAsync(line).ConfigureAwait(false);
                await JsonLineReader.WriteAsync(pipe, response, ManagerProtocol.MaxResponseBytes).ConfigureAwait(false);
            }
        }
        catch (Exception e) when (e is IOException or JsonException or ObjectDisposedException or UnauthorizedAccessException or OperationCanceledException) { }
        finally
        {
            pipe.Dispose();
            lock (lifecycle)
            {
                clients--;
                if (clients == 0) noClientsSince = DateTime.UtcNow;
            }
        }
    }

    private async Task<string> DispatchAsync(string line)
    {
        string? id = null;
        try
        {
            using var document = JsonDocument.Parse(line, new JsonDocumentOptions { MaxDepth = 64 });
            var request = document.RootElement;
            if (request.ValueKind != JsonValueKind.Object || !request.TryGetProperty("id", out var idElement)
                || idElement.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(id = idElement.GetString()) || id.Length > 128)
                return PythonBackend.Error(null, "invalid_request", "요청 식별자가 올바르지 않습니다.");
            if (!request.TryGetProperty("command", out var commandElement) || commandElement.ValueKind != JsonValueKind.String)
                return PythonBackend.Error(id, "invalid_request", "요청 명령이 올바르지 않습니다.");
            var command = commandElement.GetString()!;
            if (!request.TryGetProperty("args", out var arguments) || arguments.ValueKind != JsonValueKind.Object)
                return PythonBackend.Error(id, "invalid_request", "요청 인자는 JSON 객체여야 합니다.");
            if (command == "supervisor.status")
            {
                int count;
                lock (lifecycle) count = clients;
                return JsonSerializer.Serialize(new { id, ok = true, result = new { version = ManagerProtocol.Version,
                    supervisor_pid = Environment.ProcessId, backend_pid = backend.ProcessId, backend_status = backend.Status,
                    service_revision = ManagerProtocol.ServiceRevision(),
                    clients = count, pending_requests = backend.PendingRequests } });
            }
            // These two lifecycle commands remain compatible across protocol
            // versions, so an idle old backend can be replaced without touching Codex.
            if (command == "supervisor.retire")
            {
                lock (lifecycle)
                {
                    if (clients > 1 || backend.PendingRequests != 0 || upgrading || stopping)
                        return PythonBackend.Error(id, "backend_busy", "다른 관리창 또는 관리 작업이 열려 있습니다. 기존 관리창을 닫은 뒤 다시 여세요.");
                    upgrading = true;
                }
                try
                {
                    var probeId = "retire-" + Guid.NewGuid().ToString("N");
                    var state = await backend.RequestAsync(JsonSerializer.Serialize(new { id = probeId, command = "state", args = new { } }), probeId).ConfigureAwait(false);
                    if (!CanStop(state)) return PythonBackend.Error(id, "backend_busy", "관리 설정 적용이 진행 중입니다. 완료 후 새 관리창을 열 수 있습니다.");
                    lock (lifecycle) stopping = true;
                    shutdown.Cancel();
                    return JsonSerializer.Serialize(new { id, ok = true, result = new { retiring = true } });
                }
                finally { lock (lifecycle) upgrading = false; }
            }
            if (!request.TryGetProperty("version", out var version) || !version.TryGetInt32(out var versionValue) || versionValue != ManagerProtocol.Version)
                return PythonBackend.Error(id, "protocol_version", "관리 프로그램 버전이 맞지 않습니다. 관리자 앱을 다시 열어 주세요.");
            lock (lifecycle)
                if (upgrading || stopping) return PythonBackend.Error(id, "service_updating", "관리 서비스를 새 버전으로 연결하고 있습니다.");
            if (command == "supervisor.reconnect")
            {
                if (backend.PendingRequests != 0)
                    return PythonBackend.Error(id, "backend_busy", "진행 중인 관리 작업을 마친 뒤 다시 연결할 수 있습니다.");
                var recovered = await backend.ReconnectAsync().ConfigureAwait(false);
                return recovered
                    ? JsonSerializer.Serialize(new { id, ok = true, result = new { reconnected = true } })
                    : PythonBackend.Error(id, "backend_busy", "기존 관리 작업이 아직 종료되지 않았습니다. 앱 작업은 종료하지 않았습니다.");
            }
            if (!Commands.Contains(command))
                return PythonBackend.Error(id, "unknown_command", "지원하지 않는 관리 명령입니다.");
            // The backend receives no root, executable, or shell overrides from this transport.
            var backendRequest = JsonSerializer.Serialize(new { id, command, args = arguments });
            return await backend.RequestAsync(backendRequest, id).ConfigureAwait(false);
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException or FormatException)
        {
            return PythonBackend.Error(id, "invalid_request", "관리 요청 형식이 올바르지 않습니다.");
        }
    }

    private async Task MonitorIdleAsync()
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(15));
        while (await timer.WaitForNextTickAsync(shutdown.Token).ConfigureAwait(false))
        {
            lock (lifecycle)
            {
                if (clients != 0 || backend.PendingRequests != 0 || DateTime.UtcNow - noClientsSince < TimeSpan.FromSeconds(60)) continue;
            }
            // Codex processes are independent. Only active management work,
            // not an open Codex window, keeps a disconnected supervisor alive.
            // Recover only the transport and query fresh state, never replay an uncertain mutation.
            if (backend.IsFaulted && !await backend.ReconnectAsync().ConfigureAwait(false)) continue;
            var id = "idle-" + Guid.NewGuid().ToString("N");
            var response = await backend.RequestAsync(JsonSerializer.Serialize(new { id, command = "state", args = new { } }), id).ConfigureAwait(false);
            if (!CanStop(response)) continue;
            lock (lifecycle)
            {
                if (clients != 0 || backend.PendingRequests != 0 || DateTime.UtcNow - noClientsSince < TimeSpan.FromSeconds(60)) continue;
                stopping = true;
                shutdown.Cancel();
                return;
            }
        }
    }

    private static bool CanStop(string response)
    {
        try
        {
            using var document = JsonDocument.Parse(response);
            var value = document.RootElement;
            if (!value.GetProperty("ok").GetBoolean()) return false;
            if (value.GetProperty("result").TryGetProperty("updates", out var update)
                && update.TryGetProperty("worker_active", out var updateActive)
                && updateActive.ValueKind != JsonValueKind.False) return false;
            if (value.GetProperty("result").TryGetProperty("startup_updates", out var startup)
                && startup.TryGetProperty("worker_active", out var startupActive)
                && startupActive.ValueKind != JsonValueKind.False) return false;
            if (value.GetProperty("result").TryGetProperty("remote_updates", out var remoteUpdates)
                && remoteUpdates.TryGetProperty("worker_active", out var remoteUpdatesActive)
                && remoteUpdatesActive.ValueKind != JsonValueKind.False) return false;
            if (value.GetProperty("result").TryGetProperty("profile_restarts", out var restarts))
            {
                if (restarts.ValueKind != JsonValueKind.Object) return false;
                foreach (var restart in restarts.EnumerateObject())
                    if (!restart.Value.TryGetProperty("phase", out var phase) || phase.ValueKind != JsonValueKind.String
                        || phase.GetString() is not ("complete" or "attention" or "superseded")) return false;
            }
            return value.GetProperty("result").TryGetProperty("profiles", out var profiles)
                && profiles.ValueKind == JsonValueKind.Array;
        }
        catch (Exception e) when (e is JsonException or InvalidOperationException or KeyNotFoundException) { return false; }
    }
}
