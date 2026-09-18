using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using static Codex.ControlCenter.Shell.NativeWindowInterop;

namespace Codex.ControlCenter.Shell;

// A crashed manager must not leave an unowned, clipped, topmost window behind.
// The private app invokes this non-UI helper for its exact lifetime-marked HWND.
internal static class NativeViewportRecovery
{
    internal static IDisposable? TryLock(int pid, nint hwnd)
    {
        var mutex = new Mutex(false, $"Local\\CodexViewport-{pid}-{hwnd}");
        try { if (!mutex.WaitOne(0)) { mutex.Dispose(); return null; } }
        catch (AbandonedMutexException) { }
        return new LockLease(mutex);
    }
    private sealed class LockLease(Mutex mutex) : IDisposable
    {
        public void Dispose() { mutex.ReleaseMutex(); mutex.Dispose(); }
    }
    internal sealed record Snapshot(string Executable, string Marker, long Style, long ExStyle,
        int X, int Y, int Width, int Height, string? Region);

    internal static string? SaveRegion(nint region)
    {
        if (region == 0) return null;
        uint size = GetRegionData(region, 0, null);
        if (size == 0 || size > 16384) throw new InvalidOperationException("Cannot preserve the native window region.");
        var bytes = new byte[size];
        if (GetRegionData(region, size, bytes) != size) throw new InvalidOperationException("Cannot read the native window region.");
        return Convert.ToBase64String(bytes);
    }

    internal static int Run(string file)
    {
        try
        {
            var info = new FileInfo(file);
            if (!info.Exists || info.Length > 32768) return 2;
            using var json = JsonDocument.Parse(File.ReadAllText(file));
            var state = json.RootElement;
            if (state.GetProperty("version").GetInt32() != 1 || state.GetProperty("mode").GetString() != "viewport") return 2;
            int shellPid = state.GetProperty("shellPid").GetInt32();
            try { using var shell = Process.GetProcessById(shellPid); if (!shell.HasExited) return 3; }
            catch (ArgumentException) { }
            int pid = state.GetProperty("appPid").GetInt32();
            nint hwnd = nint.Parse(state.GetProperty("hwnd").GetString()!);
            using var ownership = TryLock(pid, hwnd);
            if (ownership is null) return 3;
            var original = state.GetProperty("recovery").Deserialize<Snapshot>()!;
            if (pid <= 0 || original is null || !original.Marker.StartsWith("Codex.ControlCenter.NativeHost.", StringComparison.Ordinal) ||
                !IsWindow(hwnd) || GetPropW(hwnd, original.Marker) != 1 || GetParent(hwnd) != 0 || GetWindow(hwnd, GwOwner) != 0) return 2;
            GetWindowThreadProcessId(hwnd, out uint actualPid);
            if (actualPid != pid || !Path.IsPathFullyQualified(original.Executable)) return 2;
            using var process = OpenProcess(ProcessQueryLimitedInformation, false, actualPid);
            var name = new StringBuilder(32768); uint length = (uint)name.Capacity;
            if (process.IsInvalid || !QueryFullProcessImageNameW(process, 0, name, ref length) ||
                !string.Equals(Path.GetFullPath(original.Executable), Path.GetFullPath(name.ToString()), StringComparison.OrdinalIgnoreCase)) return 2;
            nint region = 0;
            if (original.Region is not null)
            {
                var bytes = Convert.FromBase64String(original.Region);
                region = ExtCreateRegion(0, (uint)bytes.Length, bytes);
                if (region == 0) return 2;
            }
            if (SetWindowRgn(hwnd, region, true) == 0) { if (region != 0) DeleteObject(region); return 2; }
            WriteStyle(hwnd, GwlStyle, (nint)original.Style);
            WriteStyle(hwnd, GwlExStyle, (nint)original.ExStyle);
            if (!SetWindowPos(hwnd, (original.ExStyle & 8) != 0 ? -1 : -2, original.X, original.Y, original.Width, original.Height,
                SwpNoActivate | SwpFrameChanged | SwpAsyncWindowPos | SwpShowWindow)) return 2;
            RemovePropW(hwnd, original.Marker);
            File.Delete(file); File.Delete(file + ".render.json");
            return 0;
        }
        catch { return 2; } // Recovery must never open a dialog or another manager.
    }
}
