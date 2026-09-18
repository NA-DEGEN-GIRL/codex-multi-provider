using System.Text.Json;
using System.IO;

namespace Codex.ControlCenter.Shell;

// Shared only with the private desktop copy, before any native style changes.
// No polling, credentials, task identifiers, or user input are involved.
internal sealed class NativeWindowLease : IDisposable
{
    internal static string? NotificationPipe { get; set; }
    private readonly string path;
    private readonly string token = Guid.NewGuid().ToString("N");
    private readonly int appPid;
    private readonly string hwnd;
    private readonly FileSystemWatcher? watcher;
    private bool visible;
    private bool released;
    private bool dirty = true;
    internal string? PublicationError { get; private set; }
    private long presentationEpoch;
    private readonly NativeViewportRecovery.Snapshot? recovery;
    private ViewportBounds? bounds;
    internal sealed record ViewportBounds(int x, int y, int width, int height, uint dpi);
    internal void SetBounds(int x, int y, int width, int height, uint dpi)
    {
        var next = new ViewportBounds(x, y, width, height, dpi);
        if (bounds == next && !dirty) return;
        bounds = next; dirty = true; Write();
    }

    internal NativeWindowLease(string directory, int appPid, nint hwnd, Action? presented = null,
        NativeViewportRecovery.Snapshot? recovery = null)
    {
        this.appPid = appPid;
        this.hwnd = hwnd.ToString();
        this.recovery = recovery;
        Directory.CreateDirectory(directory);
        path = Path.Combine(directory, $"{appPid}.json");
        Write();
        if (presented is not null)
        {
            watcher = new FileSystemWatcher(directory, $"{appPid}.json.render.json") { NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.FileName };
            watcher.Changed += (_, _) => presented();
            watcher.Created += (_, _) => presented();
            watcher.EnableRaisingEvents = true;
        }
    }

    internal void SetVisible(bool value)
    {
        if (visible == value && !dirty) return;
        if (visible != value && value) presentationEpoch++;
        visible = value;
        dirty = true;
        Write();
    }

    internal void Release(bool show) { visible = show; released = true; dirty = true; Write(); }

    internal async Task NavigateAsync(NoteTask task, CancellationToken cancellation)
    {
        if (released) throw new InvalidOperationException("Codex 창 연결이 해제되었습니다.");
        string id = Guid.NewGuid().ToString("N"), command = path + ".navigate.json";
        var payload = JsonSerializer.Serialize(new { id, token, appPid, hwnd, shellPid = Environment.ProcessId,
            createdAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), threadId = task.ThreadId, hostId = task.HostId });
        await File.WriteAllTextAsync(command + ".tmp", payload, cancellation);
        File.Move(command + ".tmp", command, true);
        for (int attempt = 0; attempt < 60; attempt++)
        {
            cancellation.ThrowIfCancellationRequested();
            if (released) throw new InvalidOperationException("Codex 창 연결이 해제되었습니다.");
            try
            {
                using var ack = JsonDocument.Parse(await File.ReadAllTextAsync(command + ".ack", cancellation));
                if (ack.RootElement.GetProperty("id").GetString() == id && ack.RootElement.GetProperty("token").GetString() == token) return;
            }
            catch (Exception error) when (error is IOException or JsonException) { }
            await Task.Delay(250, cancellation);
        }
        throw new TimeoutException("Codex의 알림 작업 이동 응답이 늦습니다. 작업은 중단하지 않았습니다.");
    }

    private void Write()
    {
        string temporary = path + "." + token;
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(new
            {
                version = 1, appPid, hwnd, shellPid = Environment.ProcessId, token,
                mode = released ? "released" : "viewport", visible, presentationEpoch,
                bounds,
                recovery,
                notificationPipe = NotificationPipe
            }));
            File.Move(temporary, path, overwrite: true);
            dirty = false;
            PublicationError = null;
        }
        catch (Exception error) when (!released && error is IOException or UnauthorizedAccessException)
        {
            // A short-lived reader can deny atomic replacement, including the
            // FIRST presentation. Keep the verified attachment and retry the
            // pending state on the next layout; never detach a healthy editor.
            PublicationError = error.Message;
        }
        finally
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch (Exception error) when (!released && error is IOException or UnauthorizedAccessException)
            { PublicationError ??= error.Message; }
        }
    }

    internal string? ReadPresentationStatus()
    {
        if (!visible || dirty) return null;
        try
        {
            var file = new FileInfo(path + ".render.json");
            if (!file.Exists || file.Length > 2048) return null;
            using var json = JsonDocument.Parse(File.ReadAllText(file.FullName));
            var state = json.RootElement;
            if (state.GetProperty("version").GetInt32() != 1 ||
                state.GetProperty("appPid").GetInt32() != appPid ||
                state.GetProperty("hwnd").GetString() != hwnd ||
                state.GetProperty("shellPid").GetInt32() != Environment.ProcessId ||
                state.GetProperty("token").GetString() != token ||
                !state.TryGetProperty("presentationEpoch", out var epoch) ||
                epoch.GetInt64() != presentationEpoch) return null;
            if (state.GetProperty("rendererReady").GetBoolean() && state.GetProperty("shown").GetBoolean())
                return "Codex 화면 표시 초기화 완료 · 내부 표시 상태 확인";
            return "Codex 화면 표시 초기화 실패 · 창 연결과 화면 표시 상태가 다릅니다.";
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException
            or KeyNotFoundException or InvalidOperationException or FormatException) { return null; }
    }

    public void Dispose()
    {
        watcher?.Dispose();
        if (released) return; // The native app acknowledges and removes this final command.
        try
        {
            using var json = JsonDocument.Parse(File.ReadAllText(path));
            if (json.RootElement.GetProperty("token").GetString() == token)
            {
                File.Delete(path);
                File.Delete(path + ".render.json");
            }
        }
        catch (FileNotFoundException) { }
        catch (DirectoryNotFoundException) { }
    }
}
