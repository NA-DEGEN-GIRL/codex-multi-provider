using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Codex.ControlCenter.Shell;

// Process.PrivateMemorySize64/Threads load a system-wide process snapshot on
// Windows, even for one PID. Keep the two-second diagnostic path proportional
// to the selected PIDs instead, without retaining handles across process lives.
internal static class NativeProcessMetrics
{
    internal readonly record struct Snapshot(long PrivateBytes, long WorkingBytes,
        uint Handles, long CpuMilliseconds);

    internal static bool TryRead(int pid, out Snapshot snapshot)
    {
        snapshot = default;
        if (pid <= 0) return false;
        using var process = OpenProcess(0x1000 /* PROCESS_QUERY_LIMITED_INFORMATION */, false, pid);
        if (process.IsInvalid) return false;
        var memory = new ProcessMemoryCountersEx { Size = (uint)Marshal.SizeOf<ProcessMemoryCountersEx>() };
        if (!K32GetProcessMemoryInfo(process, ref memory, memory.Size) ||
            !GetProcessHandleCount(process, out var handles) ||
            !GetProcessTimes(process, out _, out _, out long kernel, out long user) ||
            !GetExitCodeProcess(process, out var exitCode) || exitCode != 259 /* STILL_ACTIVE */) return false;
        // PrivateUsage is private committed bytes; WorkingSetSize is resident
        // bytes. FILETIME CPU durations use 100 ns units, matching TimeSpan ticks.
        snapshot = new((long)memory.PrivateUsage, (long)memory.WorkingSetSize,
            handles, (kernel + user) / TimeSpan.TicksPerMillisecond);
        return true;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessMemoryCountersEx
    {
        internal uint Size, PageFaultCount;
        internal nuint PeakWorkingSetSize, WorkingSetSize, QuotaPeakPagedPoolUsage,
            QuotaPagedPoolUsage, QuotaPeakNonPagedPoolUsage, QuotaNonPagedPoolUsage,
            PagefileUsage, PeakPagefileUsage, PrivateUsage;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeProcessHandle OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool K32GetProcessMemoryInfo(SafeProcessHandle process, ref ProcessMemoryCountersEx counters, uint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetProcessHandleCount(SafeProcessHandle process, out uint count);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetProcessTimes(SafeProcessHandle process, out long creation, out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetExitCodeProcess(SafeProcessHandle process, out uint code);
}
