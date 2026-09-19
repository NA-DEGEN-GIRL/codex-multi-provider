using System.Diagnostics;
using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class NativeWindowShutdown
{
    // Called only for a shell-owned, lifetime-verified HWND. Hold the process
    // handle across the await so PID reuse cannot turn success into a new target.
    internal static async Task<bool> RequestAsync(string root, int pid, nint hwnd, string executable, long expectedCreated = 0)
    {
        Process process;
        try { process = Process.GetProcessById(pid); }
        catch (ArgumentException) { return true; }
        using (process)
        {
            _ = process.Handle;
            if (process.HasExited) return true;
            if (expectedCreated != 0 && process.StartTime.ToUniversalTime().ToFileTimeUtc() != expectedCreated)
                throw new InvalidOperationException("Codex process lifetime changed before shutdown.");
            if (!string.Equals(process.MainModule?.FileName, executable, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Codex process identity changed before shutdown.");
            NativeWindowInterop.GetWindowThreadProcessId(hwnd, out var ownerPid);
            if (ownerPid != (uint)pid)
                throw new InvalidOperationException("Codex window owner changed before shutdown.");
            var directory = Path.Combine(root, "work", "control-center", "window-hosts");
            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, $"{pid}.json");
            var token = Guid.NewGuid().ToString("N");
            var temporary = path + "." + token;
            try
            {
                File.WriteAllText(temporary, JsonSerializer.Serialize(new {
                    version = 1, appPid = pid, hwnd = hwnd.ToString(), shellPid = Environment.ProcessId,
                    token, mode = "shutdown", visible = false
                }));
                File.Move(temporary, path, overwrite: true);
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(15));
                return true;
            }
            catch (TimeoutException) { return false; }
            finally
            {
                if (File.Exists(temporary)) File.Delete(temporary);
                // Never remove a replacement host's command.
                try
                {
                    using var document = JsonDocument.Parse(File.ReadAllText(path));
                    if (document.RootElement.GetProperty("token").GetString() == token)
                    { File.Delete(path); File.Delete(path + ".render.json"); }
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { }
            }
        }
    }
}
