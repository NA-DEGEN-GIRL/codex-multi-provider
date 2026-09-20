using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;
using System.Collections.Concurrent;

namespace Codex.ControlCenter.Shared;

/// <summary>A current-user connection. Disposing disconnects only; it never stops managed apps.</summary>
public sealed class ManagerClient : IAsyncDisposable
{
    private readonly NamedPipeClientStream pipe;
    private readonly JsonLineReader reader;
    private readonly SemaphoreSlim writes = new(1, 1);
    private readonly ConcurrentDictionary<string, TaskCompletionSource<JsonElement>> pending = new();
    private readonly CancellationTokenSource lifetime = new();
    private bool disposed;

    private ManagerClient(NamedPipeClientStream connection)
    {
        pipe = connection;
        reader = new JsonLineReader(pipe, ManagerProtocol.MaxResponseBytes);
        _ = ReadResponsesAsync();
    }

    public bool IsConnected => !disposed && pipe.IsConnected;
    public bool ServiceUpdateDeferred { get; private set; }
    public bool PreservesBackgroundProfiles { get; private set; }

    public static async Task<ManagerClient> ConnectAsync(string root, CancellationToken cancellationToken = default)
    {
        root = ManagerProtocol.NormalizeRoot(root);
        var pipeName = ManagerProtocol.PipeName(root);
        var initial = await TryConnectAsync(pipeName, 350, cancellationToken).ConfigureAwait(false);
        if (initial is not null)
        {
            var existing = new ManagerClient(initial);
            try
            {
                var status = await existing.RequestAsync("supervisor.status", cancellationToken: cancellationToken).ConfigureAwait(false);
                existing.ObserveService(status);
                if (!existing.ServiceUpdateDeferred)
                    return existing;
                var state = await existing.RequestAsync("state", cancellationToken: cancellationToken).ConfigureAwait(false);
                if (!ProfilesHaveExited(state))
                {
                    existing.ServiceUpdateDeferred = true;
                    return existing;
                }
                try { await existing.RequestAsync("supervisor.retire", cancellationToken: cancellationToken).ConfigureAwait(false); }
                catch (ManagerException error) when (error.Code == "backend_busy")
                {
                    // Same IPC: the new shell can use the older service while
                    // a management operation drains. Never force its replacement.
                    existing.ServiceUpdateDeferred = true;
                    return existing;
                }
                await existing.DisposeAsync().ConfigureAwait(false);
                // The service drains its own backend; no account process is stopped.
                try
                {
                    using var old = Process.GetProcessById(status.GetProperty("supervisor_pid").GetInt32());
                    await old.WaitForExitAsync(cancellationToken).WaitAsync(TimeSpan.FromSeconds(10), cancellationToken).ConfigureAwait(false);
                }
                catch (ArgumentException) { }
            }
            catch { await existing.DisposeAsync().ConfigureAwait(false); throw; }
        }

        var supervisor = Path.Combine(AppContext.BaseDirectory, ManagerProtocol.SupervisorFileName);
        if (!File.Exists(supervisor))
            throw new ManagerException("supervisor_missing", "관리 프로그램 구성요소가 없습니다. 관리자 앱을 다시 빌드하거나 설치해 주세요.");
        var start = new ProcessStartInfo(supervisor)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            WorkingDirectory = root,
        };
        start.ArgumentList.Add("--root");
        start.ArgumentList.Add(root);
        try { using var process = Process.Start(start); }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            throw new ManagerException("supervisor_start_failed", "관리 프로그램 구성요소를 시작하지 못했습니다.");
        }
        // This is a connection retry only. Application commands are never automatically replayed.
        var deadline = Stopwatch.StartNew();
        while (deadline.Elapsed < TimeSpan.FromSeconds(25))
        {
            cancellationToken.ThrowIfCancellationRequested();
            var connection = await TryConnectAsync(pipeName, 650, cancellationToken).ConfigureAwait(false);
            if (connection is not null)
            {
                var client = new ManagerClient(connection);
                try
                {
                    var status = await client.RequestAsync("supervisor.status", cancellationToken: cancellationToken).ConfigureAwait(false);
                    client.ObserveService(status);
                    return client;
                }
                catch { await client.DisposeAsync().ConfigureAwait(false); throw; }
            }
            await Task.Delay(100, cancellationToken).ConfigureAwait(false);
        }
        throw new ManagerException("supervisor_unavailable", "관리 프로그램에 연결하지 못했습니다. Python 설치와 관리자 앱 파일을 확인해 주세요.");
    }

    private void ObserveService(JsonElement status)
    {
        if (!status.TryGetProperty("version", out var protocol) || !protocol.TryGetInt32(out var version) || version != ManagerProtocol.Version)
            throw new ManagerException("service_protocol_update_required",
                "실행 중인 작업은 유지했습니다. 이 업데이트는 관리 서비스와 호환되지 않아 기존 버전에서 완전 종료한 뒤 적용해야 합니다.");
        PreservesBackgroundProfiles = status.TryGetProperty("preserves_background_profiles", out var background)
            && background.ValueKind == JsonValueKind.True;
        ServiceUpdateDeferred = !status.TryGetProperty("service_revision", out var revision)
            || revision.GetString() != ManagerProtocol.ServiceRevision();
    }

    private static bool ProfilesHaveExited(JsonElement state)
    {
        if (!state.TryGetProperty("profiles", out var profiles) || profiles.ValueKind != JsonValueKind.Array) return false;
        static bool Exited(JsonElement list) => list.ValueKind == JsonValueKind.Array && list.EnumerateArray().All(profile =>
            profile.TryGetProperty("status", out var status) && (status.GetString() is "not_started" or "stopped" or "unprepared") &&
            (!profile.TryGetProperty("process_id", out var pid) || pid.ValueKind == JsonValueKind.Null));
        return Exited(profiles) && (!state.TryGetProperty("view_instances", out var viewers) || Exited(viewers));
    }

    private static async Task<NamedPipeClientStream?> TryConnectAsync(string name, int timeout, CancellationToken cancellationToken)
    {
        var connection = new NamedPipeClientStream(".", name, PipeDirection.InOut,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        try
        {
            await connection.ConnectAsync(timeout, cancellationToken).ConfigureAwait(false);
            return connection;
        }
        catch (Exception e) when (e is TimeoutException or IOException)
        {
            connection.Dispose();
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            connection.Dispose();
            throw new ManagerException("supervisor_access_denied", "관리자 창과 연결 구성요소를 같은 Windows 계정·실행 권한으로 실행해 주세요.");
        }
        catch { connection.Dispose(); throw; }
    }

    public async Task<JsonElement> RequestAsync(string command, object? args = null, CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(disposed, this);
        var id = Guid.NewGuid().ToString("N");
        var completion = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (pending.Count >= 64) throw new ManagerException("client_busy", "진행 중인 관리 요청이 많습니다.");
        pending[id] = completion;
        try
        {
            var request = JsonSerializer.Serialize(new { version = ManagerProtocol.Version, id, command, args = args ?? new { } });
            await writes.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                // Finish the frame once writing starts. Cancelling one waiter must
                // not corrupt the shared connection or cancel other profiles.
                await JsonLineReader.WriteAsync(pipe, request, ManagerProtocol.MaxRequestBytes, lifetime.Token).ConfigureAwait(false);
            }
            finally { writes.Release(); }
            return await completion.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (Exception e) when (e is IOException or ObjectDisposedException)
        {
            Disconnect();
            throw new ManagerException("connection_lost", "관리 프로그램 연결이 끊겼습니다. 작업을 다시 실행하기 전에 상태를 새로 확인해 주세요.");
        }
        finally { pending.TryRemove(id, out _); }
    }

    private async Task ReadResponsesAsync()
    {
        try
        {
            while (!lifetime.IsCancellationRequested)
            {
                var line = await reader.ReadAsync(lifetime.Token).ConfigureAwait(false);
                if (line is null) break;
                using var document = JsonDocument.Parse(line);
                var response = document.RootElement;
                var id = response.GetProperty("id").GetString();
                if (id is null || !pending.TryGetValue(id, out var completion)) continue; // Late cancelled response.
                if (response.GetProperty("ok").GetBoolean()) completion.TrySetResult(response.GetProperty("result").Clone());
                else
                {
                    var error = response.GetProperty("error");
                    completion.TrySetException(new ManagerException(error.GetProperty("code").GetString() ?? "manager_error",
                        error.GetProperty("message").GetString() ?? "관리 요청 실패"));
                }
                pending.TryRemove(id, out _);
            }
        }
        catch (Exception error) when (error is IOException or JsonException or ObjectDisposedException or OperationCanceledException or InvalidOperationException or KeyNotFoundException) { }
        finally { Disconnect(); }
    }

    private void Disconnect()
    {
        disposed = true; lifetime.Cancel(); pipe.Dispose();
        foreach (var request in pending)
            if (pending.TryRemove(request.Key, out var completion)) completion.TrySetException(new ManagerException("connection_lost", "관리 서비스 연결이 종료되었습니다. 이미 시작한 요청은 자동 재실행하지 않았습니다."));
    }

    public ValueTask DisposeAsync()
    {
        Disconnect();
        return ValueTask.CompletedTask;
    }
}
