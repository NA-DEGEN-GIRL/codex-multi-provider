using System.IO;
using System.Text.Json;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class ResponsivenessSelfTest
{
    internal static async Task RunAsync(string report, Dispatcher dispatcher)
    {
        var root = Path.Combine(Path.GetTempPath(), "codex-latency-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        NativeWindowLeaseSelfTest.Run(Path.Combine(root, "leases"));
        var log = Path.Combine(root, "shell.log");
        using var monitor = new ResponsivenessMonitor(log, root, dispatcher);
        await Task.Delay(400);
        // Freeze only this independent, windowless fixture dispatcher.
        using (monitor.Stage("fixture.blocking-call")) Thread.Sleep(1800);
        await Task.Delay(500);
        var events = File.ReadLines(monitor.Path).Select(line => JsonDocument.Parse(line)).ToArray();
        try
        {
            if (!events.Any(e => e.RootElement.GetProperty("kind").GetString() == "ui_stall" &&
                    e.RootElement.GetProperty("data").GetProperty("phase").GetString() == "fixture.blocking-call"))
                throw new InvalidOperationException("A blocked dispatcher did not leave a background stall record.");
            if (!events.Any(e => e.RootElement.GetProperty("kind").GetString() == "ui_operation" &&
                    e.RootElement.GetProperty("data").GetProperty("elapsed_ms").GetInt64() >= 1700))
                throw new InvalidOperationException("Slow operation duration was not captured.");
            if (!monitor.RecentText.Contains("ui_stall")) throw new InvalidOperationException("Copied diagnostics omit stalls.");
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, trace = monitor.Path,
                checks = new[] { "Lease visibility and bounds retry after a real sharing violation", "Stall written while dispatcher blocked", "Blocking stage and elapsed duration recorded", "Log-copy snapshot contains the event" } }));
        }
        finally { foreach (var entry in events) entry.Dispose(); }
    }
}
