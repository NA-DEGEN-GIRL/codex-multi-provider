using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using Codex.ControlCenter.Shell;

internal static class Program
{
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(nint hwnd,nint after,int x,int y,int width,int height,uint flags);
    [DllImport("user32.dll")] private static extern nint GetParent(nint hwnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(nint hwnd,int cmd);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] private static extern bool GetClientRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] private static extern bool ClientToScreen(nint hwnd,ref Point p);
    [StructLayout(LayoutKind.Sequential)] private struct Point {public int X,Y;}
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Contains("--input")) return InputScenario.Run(args);
        if (args.Contains("--shutdown")) return ShutdownScenario.Run(args);
        // Only a caller-created private fixture is accepted. No live user window
        // is discovered, and no native application is launched by this host.
        string run = Path.GetFullPath(args[0]);
        int pid = int.Parse(args[1]);
        string executable = Path.Combine(run, "app", "ChatGPT.exe");
        var log = new List<string>();
        var checks = new List<string>();
        var host = new NativeWindowHost();
        typeof(NativeWindowHost).GetProperty("WindowStateDirectory", BindingFlags.Instance | BindingFlags.NonPublic)!
            .SetValue(host, Path.Combine(run, "work", "control-center", "window-hosts"));
        host.Diagnostic += log.Add;
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        var window = new Window { Content = host, ShowActivated = false, ShowInTaskbar = false,
            WindowStyle = WindowStyle.None, Width = 800, Height = 600, Left = -28000, Top = -28000 };
        bool passed = false;
        nint hwnd = 0;
        window.Loaded += async (_, _) =>
        {
            try
            {
                typeof(NativeWindowHost).GetField("ForegroundWindow", BindingFlags.Instance | BindingFlags.NonPublic)!
                    .SetValue(host, (Func<nint>)(() => new System.Windows.Interop.WindowInteropHelper(window).Handle));
                var deadline = DateTime.UtcNow.AddSeconds(8);
                while (hwnd == 0 && DateTime.UtcNow < deadline)
                {
                    try
                    {
                        using var state = JsonDocument.Parse(File.ReadAllText(Path.Combine(run, "status.json")));
                        hwnd = nint.Parse(state.RootElement.GetProperty("hwnd").GetString()!);
                    }
                    catch (Exception error) when (error is IOException or JsonException or KeyNotFoundException) { }
                    if (hwnd == 0) await Task.Delay(40);
                }
                Require(hwnd != 0 && host.Attach(hwnd, pid, executable, out _, allowHidden: true), "Real desktop attach failed: " + host.LastError);
                if (args.Contains("--hidden-start"))
                {
                    host.Visibility = Visibility.Hidden;
                    await Task.Delay(2200);
                    host.Visibility = Visibility.Visible;
                    checks.Add("Profile switched away before its renderer became ready, then selected again.");
                }
                await Task.Delay(3500);
                Require(log.Any(s => s.Contains("화면 표시 초기화 완료")), "Native renderer presentation was not acknowledged.");
                if (args.Contains("--notification-navigation"))
                {
                    var assembly = typeof(NativeWindowHost).Assembly;
                    var leaseType = assembly.GetType("Codex.ControlCenter.Shell.NativeWindowLease")!;
                    leaseType.GetProperty("NotificationPipe", BindingFlags.Static | BindingFlags.NonPublic)!.SetValue(null,
                        $"codex-workspace-notify-{Environment.ProcessId}-{Guid.NewGuid():N}");
                    // A selection transition republishes the fixture's scoped lease.
                    host.Visibility = Visibility.Hidden; await Task.Delay(100); host.Visibility = Visibility.Visible;
                    await Task.Delay(800);
                    string thread = File.ReadAllText(Path.Combine(run,"notification-task.txt")).Trim();
                    var task = Activator.CreateInstance(assembly.GetType("Codex.ControlCenter.Shell.NoteTask")!, "local", thread)!;
                    var navigate = typeof(NativeWindowHost).GetMethod("NavigateAsync", BindingFlags.Instance | BindingFlags.NonPublic)!;
                    await ((Task)navigate.Invoke(host, [task, CancellationToken.None])!).WaitAsync(TimeSpan.FromSeconds(5));
                    await Task.Delay(800);
                    string context = Directory.EnumerateFiles(Path.Combine(run, "work", "control-center", "instances"), "active-task.json", SearchOption.AllDirectories).Single();
                    using var selected = JsonDocument.Parse(File.ReadAllText(context));
                    Require(selected.RootElement.GetProperty("thread_id").GetString() == thread,
                        "Native notification navigation was acknowledged but the actual renderer selected a different task: " + selected.RootElement.GetRawText());
                    checks.Add("Production notification navigation reached the real native renderer and its selected-task context without OS deep links, focus or model execution.");
                }
                if (args.Contains("--frame-progress"))
                {
                    long ReadFrames()
                    {
                        using var data = JsonDocument.Parse(File.ReadAllText(Path.Combine(run, "frames.json")));
                        return data.RootElement.GetProperty("frames").GetInt64();
                    }
                    long before = ReadFrames();
                    await Task.Delay(450);
                    Require(ReadFrames() > before + 2, "Selected renderer is native-visible but no new animation/compositor frames are scheduled.");
                    checks.Add("Selected occluded viewport keeps scheduling real renderer frames without capture or resize.");
                }
                CheckBounds(); checks.Add("Cold renderer presents inside production NativeWindowHost.");
                var captureHwnd = new System.Windows.Interop.WindowInteropHelper(window).Handle;
                int capturedColors = CaptureProbe.Save(captureHwnd, Path.Combine(run, "manager-capture.png"));
                Require(capturedColors > 32, "Manager-only capture contains a blank viewport.");
                var attachment = typeof(NativeWindowHost).GetField("_attachment", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(host)!;
                var mirror = (IDisposable)attachment.GetType().GetProperty("CaptureMirror")!.GetValue(attachment)!;
                mirror.Dispose();
                int absentColors = CaptureProbe.Save(captureHwnd, Path.Combine(run, "manager-without-mirror.png"));
                Require(absentColors < 8, "Negative control did not reproduce the blank manager-only capture.");
                host.SynchronizeLayout();
                checks.Add($"Manager-only PrintWindow captures real Chromium content ({capturedColors} colors); removing mirror reproduces blank capture ({absentColors} colors).");
                if (args.Contains("--modal-popups"))
                    await ModalBackdropScenario.RunAsync(window, host, hwnd, pid, run, checks);
                window.Width += 120; window.Height += 80;
                await Task.Delay(1800); CheckBounds(); checks.Add("Real Chromium client bounds follow resized WPF host.");
                for (var episode=0;episode<2;episode++)
                {
                    SetWindowPos(hwnd,0,-27000,-27000,450,350,0x4014); // async, no activation/z-order
                    await Task.Delay(1800); CheckBounds();
                }
                typeof(NativeWindowHost).GetField("ForegroundWindow", BindingFlags.Instance | BindingFlags.NonPublic)!
                    .SetValue(host,(Func<nint>)(() => (nint)1));
                await Task.Delay(1000);
                Require(IsWindowVisible(hwnd),"Unrelated foreground hid Chromium viewport.");
                CheckBounds(); checks.Add("Native geometry drift recovers and another foreground app does not blank the selected viewport.");
                var managerHwnd=new System.Windows.Interop.WindowInteropHelper(window).Handle;
                for(var cycle=0;cycle<2;cycle++)
                {
                    ShowWindow(managerHwnd,7);
                    await Task.Delay(cycle==0?90:850);
                    ShowWindow(managerHwnd,4);
                    await Task.Delay(1000);CheckBounds();
                    Require(IsWindowVisible(hwnd),"Real renderer did not return after minimize/restore.");
                }
                checks.Add("Real native renderer survives fast and slow manager minimize/restore without profile switching.");
                int restoredColors = CaptureProbe.Save(captureHwnd, Path.Combine(run, "manager-capture-restored.png"));
                if (restoredColors <= 32) checks.Add("Source restored capture colors: " + CaptureProbe.Save(hwnd, Path.Combine(run, "source-restored.png")));
                Require(restoredColors > 32, "Manager-only capture became blank after minimize/restore.");
                checks.Add("Manager capture stays populated after resize and both minimize/restore cycles.");
                host.Visibility = Visibility.Hidden;
                await Task.Delay(850);
                Require(CaptureProbe.Save(captureHwnd, Path.Combine(run, "manager-capture-hidden.png")) < 8,
                    "Hidden profile left its content in the manager capture.");
                host.Visibility = Visibility.Visible;
                await Task.Delay(1300); CheckBounds(); checks.Add("Hide/show retains the same live renderer and parent.");
                Require(CaptureProbe.Save(captureHwnd, Path.Combine(run, "manager-capture-selected.png")) > 32,
                    "Reselecting the profile left its capture blank.");
                checks.Add("Hidden profile removes its captured content; reselecting restores it.");
                // A single failed atomic lease write used to stick at hidden
                // forever, until a resize changed another cached property.
                var leaseFile = Path.Combine(run, "work", "control-center", "window-hosts", $"{pid}.json");
                for (int cycle = 0; cycle < 2; cycle++)
                {
                    host.Visibility = Visibility.Hidden;
                    await Task.Delay(180);
                    using (var reader = new FileStream(leaseFile, FileMode.Open, FileAccess.Read, FileShare.Read))
                    {
                        host.Visibility = Visibility.Visible;
                        await Task.Delay(180);
                        Require(!host.SynchronizeLayout(), "Expected the fixture file lock to reject a show command.");
                    }
                    // Retry the same visibility and geometry; never resize.
                    Require(host.SynchronizeLayout(), "Published visibility did not recover after releasing the reader.");
                    await Task.Delay(850);
                    using var lease = JsonDocument.Parse(File.ReadAllText(leaseFile));
                    using var ack = JsonDocument.Parse(File.ReadAllText(leaseFile + ".render.json"));
                    Require(lease.RootElement.GetProperty("visible").GetBoolean() &&
                        lease.RootElement.GetProperty("presentationEpoch").GetInt64() == ack.RootElement.GetProperty("presentationEpoch").GetInt64() &&
                        ack.RootElement.GetProperty("shown").GetBoolean(), "Native renderer did not acknowledge the current selection.");
                    CheckBounds();
                    Require(CaptureProbe.Save(captureHwnd, Path.Combine(run, $"manager-retry-{cycle}.png")) > 32,
                        "Recovered selection still has a blank compositor surface.");
                }
                checks.Add("Two lost show commands recover at unchanged size, acknowledged by the real renderer with a populated capture.");
                Require(log.Count(s => s.Contains("독립 창 표시 스타일 복원")) <= 3, "Presentation triggered repeated style repair.");
                Require(!log.Any(s => s.Contains("반복 조절을 멈")), "Resize loop exhausted its retry budget.");
                Require(GetForegroundWindow() != new System.Windows.Interop.WindowInteropHelper(window).Handle,
                    "Background fixture stole foreground focus.");
                checks.Add("Bounded style repair at presentation; no restart, focus activation or repeating resize cycle.");
                passed = true;
            }
            catch (Exception error) { log.Add(error.ToString()); }
            finally
            {
                host.DetachForClose();
                File.WriteAllText(Path.Combine(run, "wpf-host.json"), JsonSerializer.Serialize(new { passed, checks, log }));
                window.Close(); app.Shutdown(passed ? 0 : 1);
            }
        };
        void CheckBounds()
        {
            Require(host.HasLiveAttachment, "Live attachment lost.");
            GetClientRect(hwnd, out var child);var origin=new Point();ClientToScreen(hwnd,ref origin);
            Require(GetParent(hwnd) == 0, "Native input queue was joined to another window.");
            var parent = (nint)typeof(NativeWindowHost).GetProperty("ContainerHandle", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(host)!;
            GetWindowRect(parent, out var bounds); GetClientRect(parent, out var client);
            Require(origin.X == bounds.Left && origin.Y == bounds.Top &&
                child.Right == client.Right && child.Bottom == client.Bottom,
                "Native desktop escaped or stopped matching its host bounds.");
        }
        window.Show(); return app.Run();
    }
    private static void Require(bool value, string message) { if (!value) throw new InvalidOperationException(message); }
}
