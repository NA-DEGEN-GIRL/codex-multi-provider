using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

namespace Codex.ControlCenter.Shell;

internal static class NativeProcessMetricsSelfTest
{
    internal static void Run()
    {
        foreach (int pid in new[] { -1, 0, int.MaxValue })
            Require(!NativeProcessMetrics.TryRead(pid, out var missing) && missing == default,
                "An invalid/missing process produced successful zero counters.");

        using var own = Process.GetCurrentProcess();
        long cpuBefore = (long)own.TotalProcessorTime.TotalMilliseconds;
        var initial = ReadOwn();
        long cpuAfter = (long)own.TotalProcessorTime.TotalMilliseconds;
        Require(initial.PrivateBytes > 0 && initial.WorkingBytes > 0 && initial.Handles > 0,
            "Current process memory/handle counters were empty.");
        Require(initial.CpuMilliseconds >= cpuBefore && initial.CpuMilliseconds <= cpuAfter,
            "Native CPU counters disagree with current-process CPU time.");

        // Committed private memory is deterministic even when Windows trims
        // resident pages. Touch every page to exercise working-set reporting too.
        const int bytes = 16 * 1024 * 1024;
        nint block = VirtualAlloc(0, (nuint)bytes, 0x3000 /* RESERVE | COMMIT */, 4 /* READWRITE */);
        Require(block != 0, "Could not allocate the isolated memory counter fixture.");
        try
        {
            for (int offset = 0; offset < bytes; offset += Environment.SystemPageSize) Marshal.WriteByte(block, offset, 1);
            var held = ReadOwn();
            long growth = held.PrivateBytes - initial.PrivateBytes;
            Require(growth >= bytes && growth <= bytes + 4 * 1024 * 1024,
                "Private-byte counters did not track a known committed allocation.");
            Require(held.WorkingBytes > 0, "Touched pages lost working-set reporting.");
        }
        finally { Require(VirtualFree(block, 0, 0x8000 /* MEM_RELEASE */), "Could not release the memory fixture."); }

        // Warm the framework's event path before taking the comparison; only
        // these fixture-owned handles are created, queried and disposed.
        using (var warmup = new EventWaitHandle(false, EventResetMode.ManualReset)) { }
        var handlesBefore = ReadOwn();
        var events = Enumerable.Range(0, 16).Select(_ => new EventWaitHandle(false, EventResetMode.ManualReset)).ToArray();
        try
        {
            var handlesHeld = ReadOwn();
            Require(handlesHeld.Handles >= handlesBefore.Handles + events.Length,
                "Handle counters did not observe the fixture-owned events.");
        }
        finally { foreach (var signal in events) signal.Dispose(); }

        // The retained Process handle pins this child's PID until disposal, so
        // an exited-PID assertion cannot accidentally query a reused lifetime.
        using var exited = Process.Start(new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"), "/d /c exit /b 0")
        { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden })
            ?? throw new InvalidOperationException("Could not start the isolated exit fixture.");
        Require(exited.WaitForExit(5000), "The isolated exit fixture did not finish.");
        Require(!NativeProcessMetrics.TryRead(exited.Id, out _), "An exited process produced live counters.");
    }

    private static NativeProcessMetrics.Snapshot ReadOwn()
    {
        Require(NativeProcessMetrics.TryRead(Environment.ProcessId, out var result), "Current-process query failed.");
        return result;
    }
    private static void Require(bool value, string message) { if (!value) throw new InvalidOperationException(message); }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern nint VirtualAlloc(nint address, nuint size, uint allocationType, uint protect);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool VirtualFree(nint address, nuint size, uint freeType);
}
