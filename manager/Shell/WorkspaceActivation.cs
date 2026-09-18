using System.IO;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Runtime.InteropServices;

namespace Codex.ControlCenter.Shell;

// A private URI for workspace notifications. Never replace the global codex://
// handler. An opaque ticket is the only payload; it cannot execute a command.
internal sealed class WorkspaceActivation : IDisposable
{
    private readonly CancellationTokenSource stop = new();
    internal static string Identity(string root) => Convert.ToHexStringLower(SHA256.HashData(
        Encoding.UTF8.GetBytes(Path.GetFullPath(root).TrimEnd('\\').ToUpperInvariant() + "|" + Environment.UserName)))[..20];
    internal static string Scheme(string root) => "codex-workspace-" + Identity(root);
    internal static string? Ticket(string root, string? uri)
    {
        if (!Uri.TryCreate(uri, UriKind.Absolute, out var value) || value.Scheme != Scheme(root) ||
            value.Host != "notification" || value.Query != "" || value.Fragment != "" || value.UserInfo != "" || !value.IsDefaultPort)
            return null;
        var ticket = value.AbsolutePath.TrimStart('/');
        return Guid.TryParseExact(ticket, "N", out _) ? ticket : null;
    }
    internal WorkspaceActivation(string root, Action<string> activate)
    {
        _ = Task.Run(async () =>
        {
            while (!stop.IsCancellationRequested)
            {
                try
                {
                    using var pipe = new NamedPipeServerStream(Scheme(root), PipeDirection.InOut, 1,
                        PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                    await pipe.WaitForConnectionAsync(stop.Token).ConfigureAwait(false);
                    using var deadline = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
                    deadline.CancelAfter(2000);
                    var bytes = new byte[34]; int length = 0;
                    while (length < bytes.Length)
                    {
                        int read = await pipe.ReadAsync(bytes.AsMemory(length), deadline.Token).ConfigureAwait(false);
                        if (read == 0) break;
                        length += read;
                        if (bytes[length - 1] == '\n') break;
                    }
                    string ticket = Encoding.ASCII.GetString(bytes, 0, length).TrimEnd('\n');
                    if (ticket != "" && !Guid.TryParseExact(ticket, "N", out _)) continue;
                    activate(ticket);
                    await pipe.WriteAsync(new byte[] { 1 }, deadline.Token).ConfigureAwait(false);
                }
                catch (Exception error) when (error is IOException or OperationCanceledException or UnauthorizedAccessException) { }
            }
        });
    }
    internal static async Task<bool> ForwardAsync(string root, string ticket)
    {
        try
        {
            using var timeout = new CancellationTokenSource(2500);
            using var pipe = new NamedPipeClientStream(".", Scheme(root), PipeDirection.InOut, PipeOptions.Asynchronous);
            await pipe.ConnectAsync(timeout.Token).ConfigureAwait(false);
            // The URI-launched process received the user's activation. Pass
            // that foreground permission to the existing manager, otherwise
            // Windows may only flash its taskbar button.
            if (GetNamedPipeServerProcessId(pipe.SafePipeHandle, out uint serverPid)) AllowSetForegroundWindow(serverPid);
            await pipe.WriteAsync(Encoding.ASCII.GetBytes(ticket + "\n"), timeout.Token).ConfigureAwait(false);
            var response = new byte[1];
            return await pipe.ReadAsync(response, timeout.Token).ConfigureAwait(false) == 1 && response[0] == 1;
        }
        catch (Exception error) when (error is IOException or OperationCanceledException or UnauthorizedAccessException) { return false; }
    }
    public void Dispose() => stop.Cancel();
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetNamedPipeServerProcessId(Microsoft.Win32.SafeHandles.SafePipeHandle handle, out uint pid);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AllowSetForegroundWindow(uint pid);
}
