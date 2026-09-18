using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class NotificationActivationSelfTest
{
    internal static async Task RunAsync(string report)
    {
        string directory = Path.Combine(Path.GetTempPath(), "codex-notification-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var received = new List<NotificationClick>();
        var notices = new List<WorkspaceNotice>();
        using var server = new NotificationActivation(directory, click => { lock (received) received.Add(click); },
            (_, notice) => { lock (notices) notices.Add(notice); return Task.FromResult(true); });
        string token = Guid.NewGuid().ToString("N");
        var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        object Message(int pid, long at, string? leaseToken = null, string? id = null) => new
        { version = 1, appPid = pid, clickedAt = at, hwnd = "123", token = leaseToken ?? token, id = id ?? Guid.NewGuid().ToString() };
        void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
        var checks = new List<string>();
        Require(NotificationActivation.Parse(JsonSerializer.SerializeToUtf8Bytes(Message(Environment.ProcessId + 1, now)), (uint)Environment.ProcessId, now) is null,
            "Forged process identity was accepted.");
        Require(NotificationActivation.Parse(JsonSerializer.SerializeToUtf8Bytes(Message(Environment.ProcessId, now - 10000)), (uint)Environment.ProcessId, now) is null,
            "Stale click was accepted.");
        Require(NotificationActivation.Parse(Encoding.UTF8.GetBytes("{}"), (uint)Environment.ProcessId, now) is null, "Malformed request was accepted.");
        checks.Add("Rejects malformed, stale and wrong-process activation requests.");
        string leasePath = Path.Combine(directory, $"{Environment.ProcessId}.json");
        await File.WriteAllTextAsync(leasePath, JsonSerializer.Serialize(new
        {version = 1, appPid = Environment.ProcessId, hwnd = "123", shellPid = Environment.ProcessId, token, notificationPipe = server.PipeName}));
        async Task Send(object message)
        {
            using var client = new NamedPipeClientStream(".", server.PipeName, PipeDirection.Out, PipeOptions.Asynchronous);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            await client.ConnectAsync(timeout.Token);
            await client.WriteAsync(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(message) + "\n"), timeout.Token);
            await client.FlushAsync(timeout.Token);
            await Task.Delay(75);
        }
        string id = Guid.NewGuid().ToString();
        await Send(Message(Environment.ProcessId, now, id: id));
        await Send(Message(Environment.ProcessId, now, id: id));
        await Send(Message(Environment.ProcessId, now, Guid.NewGuid().ToString("N")));
        lock (received) Require(received.Count == 1 && received[0].Id == id, "Lease check or duplicate suppression failed.");
        checks.Add("Real Windows pipe verifies OS peer PID and current lease; duplicate/wrong-lease clicks are ignored.");
        using (var client = new NamedPipeClientStream(".", server.PipeName, PipeDirection.InOut, PipeOptions.Asynchronous))
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            await client.ConnectAsync(timeout.Token);
            var notification = new { version = 1, kind = "show", appPid = Environment.ProcessId,
                clickedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), hwnd = "123", token, id = Guid.NewGuid().ToString(),
                notification = new { id = "question", kind = "question", title = "Task", body = "Question", threadId = Guid.NewGuid().ToString(), hostId = "ssh-one" } };
            await client.WriteAsync(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(notification) + "\n"), timeout.Token);
            var response = new byte[512]; int count = await client.ReadAsync(response, timeout.Token);
            using var accepted = JsonDocument.Parse(response.AsMemory(0, count));
            Require(accepted.RootElement.GetProperty("accepted").GetBoolean() && notices.Count == 1 && notices[0].HostId == "ssh-one", "Question delivery acknowledgement or host routing failed.");
        }
        checks.Add("Real Windows pipe accepts a scoped question with host+task data and acknowledges ownership before native notification fallback is suppressed.");
        File.Delete(leasePath);
        await Send(Message(Environment.ProcessId, now));
        lock (received) Require(received.Count == 1, "Detached window still activated its old shell.");
        checks.Add("Detached windows cannot switch the previous manager. No window was activated and no Codex process was contacted.");
        checks.AddRange(await WorkspaceNotificationsSelfTest.RunAsync(directory));
        await File.WriteAllTextAsync(report, JsonSerializer.Serialize(new { passed = true, checks }, new JsonSerializerOptions { WriteIndented = true }));
    }
}
