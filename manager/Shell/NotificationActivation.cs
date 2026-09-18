using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal sealed record NotificationClick(int AppPid, nint Hwnd, string Token, long ClickedAt, string Id);

// Event-driven, bounded IPC. Never poll the UI or wait on a Codex RPC to bring
// an already hosted profile forward. The OS peer PID and current lease must agree.
internal sealed class NotificationActivation : IDisposable
{
    internal string PipeName { get; } = $"codex-workspace-notify-{Environment.ProcessId}-{Guid.NewGuid():N}";
    private readonly CancellationTokenSource stop = new();
    private readonly string directory;
    private readonly Action<NotificationClick> clicked;
    private readonly Func<NotificationClick, WorkspaceNotice, Task<bool>>? notify;
    private readonly Queue<string> recent = new();
    private readonly Task listener;

    internal NotificationActivation(string directory, Action<NotificationClick> clicked,
        Func<NotificationClick, WorkspaceNotice, Task<bool>>? notify = null)
    {
        this.directory = directory;
        this.clicked = clicked;
        this.notify = notify;
        listener = Task.Run(ListenAsync);
    }

    private async Task ListenAsync()
    {
        while (!stop.IsCancellationRequested)
        {
            try
            {
                using var pipe = new NamedPipeServerStream(PipeName, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await pipe.WaitForConnectionAsync(stop.Token).ConfigureAwait(false);
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
                timeout.CancelAfter(TimeSpan.FromSeconds(2));
                if (!GetNamedPipeClientProcessId(pipe.SafePipeHandle, out uint peer)) continue;
                var bytes = new byte[16384];
                int length = 0, newline = -1;
                while (length < bytes.Length && newline < 0)
                {
                    int read = await pipe.ReadAsync(bytes.AsMemory(length), timeout.Token).ConfigureAwait(false);
                    if (read == 0) break;
                    length += read;
                    newline = Array.IndexOf(bytes, (byte)'\n', 0, length);
                }
                if (newline < 0) continue;
                var message = Parse(bytes.AsSpan(0, newline), peer, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
                if (message is null || recent.Contains(message.Id)) continue;
                var file = Path.Combine(directory, $"{message.AppPid}.json");
                if (new FileInfo(file).Length > 2048) continue;
                await using var leaseFile = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 4096, true);
                using var lease = await JsonDocument.ParseAsync(leaseFile, cancellationToken: timeout.Token).ConfigureAwait(false);
                if (!MatchesLease(message, lease.RootElement, PipeName, Environment.ProcessId)) continue;
                recent.Enqueue(message.Id);
                if (recent.Count > 64) recent.Dequeue();
                using var envelope = JsonDocument.Parse(bytes.AsMemory(0, newline));
                if (envelope.RootElement.TryGetProperty("kind", out var kind) && kind.GetString() == "show")
                {
                    var notice = ParseNotice(envelope.RootElement);
                    bool accepted = notice is not null && notify is not null && await notify(message, notice).WaitAsync(timeout.Token).ConfigureAwait(false);
                    await pipe.WriteAsync(Encoding.UTF8.GetBytes(accepted ? "{\"accepted\":true}\n" : "{\"accepted\":false}\n"), timeout.Token).ConfigureAwait(false);
                }
                else clicked(message);
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or
                JsonException or OperationCanceledException or InvalidOperationException or FormatException or OverflowException)
            { /* Missing/stale leases and disconnected clients cannot break the listener. */ }
        }
    }

    internal static WorkspaceNotice? ParseNotice(JsonElement envelope)
    {
        try
        {
            var n = envelope.GetProperty("notification");
            string id = n.GetProperty("id").GetString()!, kind = n.GetProperty("kind").GetString()!,
                title = n.GetProperty("title").GetString()!, body = n.GetProperty("body").GetString()!,
                thread = n.GetProperty("threadId").GetString()!, host = n.GetProperty("hostId").GetString()!;
            if (string.IsNullOrEmpty(id) || id.Length > 256 || kind is not ("question" or "permission" or "turn-complete") ||
                title is null || title.Length > 300 || body is null || body.Length > 2000 || !Guid.TryParse(thread, out _) ||
                string.IsNullOrEmpty(host) || host.Length > 256 || host.Any(char.IsControl)) return null;
            return new(id, kind, title, body, thread, host);
        }
        catch (Exception error) when (error is KeyNotFoundException or InvalidOperationException) { return null; }
    }

    internal static NotificationClick? Parse(ReadOnlySpan<byte> bytes, uint peer, long now)
    {
        try
        {
            using var doc = JsonDocument.Parse(bytes.ToArray());
            var value = doc.RootElement;
            if (value.GetProperty("version").GetInt32() != 1) return null;
            int pid = value.GetProperty("appPid").GetInt32();
            long time = value.GetProperty("clickedAt").GetInt64();
            string? hwnd = value.GetProperty("hwnd").GetString(), token = value.GetProperty("token").GetString(), id = value.GetProperty("id").GetString();
            if (pid <= 0 || pid != peer || time < now - 3000 || time > now + 1000 ||
                !long.TryParse(hwnd, out long handle) || handle <= 0 ||
                !Guid.TryParseExact(token, "N", out _) || !Guid.TryParseExact(id, "D", out _)) return null;
            return new(pid, (nint)handle, token!, time, id!);
        }
        catch (Exception error) when (error is JsonException or KeyNotFoundException or InvalidOperationException or FormatException or OverflowException) { return null; }
    }

    internal static bool MatchesLease(NotificationClick message, JsonElement lease, string pipe, int shellPid)
    {
        try
        {
            return lease.GetProperty("version").GetInt32() == 1 &&
                lease.GetProperty("appPid").GetInt32() == message.AppPid &&
                lease.GetProperty("hwnd").GetString() == message.Hwnd.ToString() &&
                lease.GetProperty("token").GetString() == message.Token &&
                lease.GetProperty("shellPid").GetInt32() == shellPid &&
                lease.GetProperty("notificationPipe").GetString() == pipe;
        }
        catch (Exception error) when (error is KeyNotFoundException or InvalidOperationException or FormatException or OverflowException) { return false; }
    }

    public void Dispose() => stop.Cancel();

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetNamedPipeClientProcessId(Microsoft.Win32.SafeHandles.SafePipeHandle pipe, out uint pid);
}
