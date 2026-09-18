using System.Collections;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using Codex.ControlCenter.Shell;

internal static class ShutdownScenario
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool SetPropW(nint hwnd, string name, nint value);
    internal static int Run(string[] args)
    {
        string run = Path.GetFullPath(args[0]);
        int pid = int.Parse(args[1]);
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        var manager = new MainWindow(run, fixture: true) { WindowState = WindowState.Normal,
            ShowActivated = false, ShowInTaskbar = false, Left = -28000, Top = -28000 };
        var flags = BindingFlags.Instance | BindingFlags.NonPublic;
        bool closed = false;
        manager.Closed += (_, _) => closed = true;
        manager.Loaded += async (_, _) =>
        {
            bool passed = false; string? failure = null; double elapsed = 0;
            try
            {
                var watch = Stopwatch.StartNew();
                while (!File.Exists(Path.Combine(run, "status.json")) && watch.Elapsed.TotalSeconds < 8) await Task.Delay(100);
                using var status = JsonDocument.Parse(File.ReadAllText(Path.Combine(run, "status.json")));
                var hwnd = (nint)long.Parse(status.RootElement.GetProperty("hwnd").GetString()!);
                var identityType = typeof(MainWindow).GetNestedType("WindowIdentity", BindingFlags.NonPublic)!;
                var identity = Activator.CreateInstance(identityType, hwnd, pid, Path.Combine(run, "app", "ChatGPT.exe"))!;
                string marker = (string)identityType.GetProperty("Marker")!.GetValue(identity)!;
                if (!SetPropW(hwnd, marker, 1)) throw new Exception("Cannot mark fixture HWND");
                identityType.GetProperty("Marked")!.SetValue(identity, true);
                ((IDictionary)typeof(MainWindow).GetField("_parked", flags)!.GetValue(manager)!).Add(hwnd, identity);
                using var process = Process.GetProcessById(pid);
                _ = process.Handle;
                watch.Restart(); manager.Close();
                // Close must be cancellable/asynchronous until exit is verified.
                if (closed) throw new Exception("Manager closed without waiting for process exit");
                while (!closed && watch.Elapsed.TotalSeconds < 18) await Task.Delay(50);
                elapsed = watch.Elapsed.TotalSeconds;
                if (!closed || !process.HasExited) throw new Exception("Managed app survived manager shutdown");
                if (File.Exists(Path.Combine(run, "work/control-center/window-hosts", $"{pid}.json")))
                    throw new Exception("Shutdown lease was left behind");
                passed = true;
            }
            catch (Exception error) { failure = error.ToString(); }
            File.WriteAllText(Path.Combine(run, "shutdown.json"), JsonSerializer.Serialize(new { passed, closed, elapsed, failure }));
            app.Shutdown(passed ? 0 : 1);
        };
        manager.Show(); return app.Run();
    }
}
