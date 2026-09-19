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
    [DllImport("user32.dll")] private static extern nint GetAncestor(nint hwnd,uint flags);
    [DllImport("user32.dll")] private static extern bool PostMessageW(nint hwnd,uint message,nuint wParam,nint lParam);
    [DllImport("user32.dll")] private static extern nint GetWindow(nint hwnd,uint command);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] private static extern nint GetWindowLongPtrW(nint hwnd,int index);
    [DllImport("user32.dll")] private static extern int SetWindowRgn(nint hwnd,nint region,bool redraw);
    [DllImport("user32.dll")] private static extern int GetWindowRgn(nint hwnd,nint region);
    [DllImport("gdi32.dll")] private static extern nint CreateRectRgn(int left,int top,int right,int bottom);
    [DllImport("gdi32.dll")] private static extern int GetRgnBox(nint region,out Rect rect);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(nint obj);
    [StructLayout(LayoutKind.Sequential)] private struct Point {public int X,Y;}
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    private const uint WmEnterSizeMove = 0x0231, WmExitSizeMove = 0x0232, GwOwner = 4;
    private const int GwlStyle = -16, GwlExStyle = -20;

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
        // Asymmetric solid margins around the native host: a target-offset bug now
        // shifts the source inside the manager instead of hiding behind a full-bleed
        // host. The center sample stays inside the host for the capture controls.
        var surface = new System.Windows.Controls.Border { Background = System.Windows.Media.Brushes.SlateGray,
            Padding = new Thickness(80, 60, 24, 36), Child = host };
        var window = new Window { Content = surface, ShowActivated = false, ShowInTaskbar = false,
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
                checks.Add("Renderer frames at first capture: " + FramesSummary(out _,out _) + ".");
                if (args.Contains("--modal-popups"))
                    await ModalBackdropScenario.RunAsync(window, host, hwnd, pid, run, checks);
                window.Width += 120; window.Height += 80;
                await Task.Delay(1800); CheckBounds(); checks.Add("Real Chromium client bounds follow resized WPF host.");
                // Interactive off-screen drag: the manager presents a mirror while the
                // still-visible independent source parks instead of following or
                // resizing per frame; ending the drag forces exact geometry and the
                // clip region back onto the live client viewport.
                nint dragRoot = captureHwnd;
                nint styleBefore = GetWindowLongPtrW(hwnd,GwlStyle), exStyleBefore = GetWindowLongPtrW(hwnd,GwlExStyle);
                var dragContainer = (nint)typeof(NativeWindowHost).GetProperty("ContainerHandle", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(host)!;
                Require(GetAncestor(dragContainer,2 /* GA_ROOT */) == dragRoot,
                    "The native host container is not rooted in the manager window that receives the drag messages.");
                // Certify the host's root-message hook survives a full collection
                // before the drag messages are posted; HwndSource hooks historically
                // depended on delegate lifetime.
                GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect();
                Require(PostMessageW(dragRoot,WmEnterSizeMove,0,0), "Enter-size-move fixture message was rejected.");
                window.Left += 40; window.Top += 25; window.Width += 70; window.Height += 45; window.UpdateLayout();
                await Until("drag-park",() => OutsideHost(hwnd,dragRoot),TimeSpan.FromMilliseconds(600),
                    "The source was not parked wholly outside the manager during the drag.");
                Require(IsWindowVisible(hwnd), "The parked source became invisible during the drag.");
                GetWindowRect(hwnd,out var parkedRect);
                var parked = (parkedRect.Left,parkedRect.Top,parkedRect.Right,parkedRect.Bottom);
                for (var step = 0; step < 3; step++)
                {
                    window.Left += 40; window.Top += 25; window.Width += 70; window.Height += 45; window.UpdateLayout();
                    await Task.Delay(140);
                    GetWindowRect(hwnd,out var held);
                    Require((held.Left,held.Top,held.Right,held.Bottom) == parked,
                        "The parked source moved or resized on a drag frame.");
                    if (step == 1)
                    {
                        Require(CaptureProbe.Save(dragRoot,Path.Combine(run,"manager-capture-drag.png")) > 32,
                            "Mid-drag manager capture lost the mirrored Chromium content.");
                        Require(GetParent(hwnd) == 0 && GetWindow(hwnd,GwOwner) == 0,
                            "Drag mirror joined or owned the independent source window.");
                    }
                }
                Require(PostMessageW(dragRoot,WmExitSizeMove,0,0), "Exit-size-move fixture message was rejected.");
                await Until("drag-exit",() => BoundsExact(hwnd) && RegionMatches(hwnd),TimeSpan.FromMilliseconds(2000),
                    "Drag end did not force exact physical client bounds and a valid clip.");
                CheckBounds();
                Require(GetWindowLongPtrW(hwnd,GwlStyle) == styleBefore && GetWindowLongPtrW(hwnd,GwlExStyle) == exStyleBefore,
                    "Interactive drag rewrote native window styles.");
                host.SynchronizeLayout(); await Task.Delay(160); CheckBounds();
                await Task.Delay(1250); CheckBounds();
                string dragFrames = FramesSummary(out var viewportPhysicalWidth,out var viewportPhysicalHeight);
                if (viewportPhysicalWidth is { } viewportWidth && viewportPhysicalHeight is { } viewportHeight && GetClientRect(hwnd,out var settledClient))
                    Require(Math.Abs(viewportWidth - settledClient.Right) <= 2 && Math.Abs(viewportHeight - settledClient.Bottom) <= 2,
                        $"Renderer viewport {viewportWidth}x{viewportHeight} physical does not match the settled Win32 client " +
                        $"{settledClient.Right}x{settledClient.Bottom} within 2px: {dragFrames}.");
                checks.Add("Renderer frames after drag (≤2px viewport check): " + dragFrames + ".");
                checks.Add("Drag parked the visible source without per-frame resizes, kept a populated mid-drag mirror capture, and forced exact bounds with no stale snapback after 1.2s.");
                // A framed source still requires its clip (covered by the native
                // self-test); a full-window source may validly stay region-free, so
                // the restore only has to reach a valid clip, not force one.
                Require(SetWindowRgn(hwnd,0,true) != 0, "Fixture could not clear the native viewport region.");
                nint clearProbe = CreateRectRgn(0,0,0,0);
                try { Require(GetWindowRgn(hwnd,clearProbe) == 0, "Fixture clear did not remove the clip region."); }
                finally { DeleteObject(clearProbe); }
                Require(host.HasLiveAttachment, "Explicit clip repair needs a live attachment.");
                bool clearRestore = host.RestoreViewport();
                checks.Add($"Cleared clip: immediate RestoreViewport returned {clearRestore}; waiting for eventual valid clip and settled flag.");
                await Until("region-cleared",() => BoundsExact(hwnd) && RegionMatches(hwnd) && !Settling(),TimeSpan.FromMilliseconds(1500),
                    "Explicit viewport restore did not reach exact bounds, a valid clip and a cleared settling flag.");
                checks.Add("Cleared clip reached a valid state after explicit restore (" + RegionState(hwnd) + ").");
                Require(SetWindowRgn(hwnd,CreateRectRgn(0,0,100,100),true) != 0, "Fixture could not apply a wrong native viewport region.");
                nint wrongProbe = CreateRectRgn(0,0,0,0);
                try
                {
                    int wrongType = GetWindowRgn(hwnd,wrongProbe);
                    Rect wrongBox = default;
                    bool wrongBoxRead = wrongType != 0 && GetRgnBox(wrongProbe,out wrongBox) != 0;
                    if (RegionMatches(hwnd))
                        checks.Add("Wrong 100x100 clip was already replaced by a valid native clip before the fixture observed it; " +
                            "native self-repair is accepted here (forced nonzero-inset repair stays deterministic in the native self-test).");
                    else if (wrongBoxRead && wrongBox.Left == 0 && wrongBox.Top == 0 && wrongBox.Right == 100 && wrongBox.Bottom == 100)
                        checks.Add($"Wrong 100x100 clip stuck as injected (type{wrongType}); the explicit restore must repair it.");
                    else
                        throw new InvalidOperationException("Wrong region injection produced an unexpected invalid clip: " +
                            (wrongType == 0 ? "region=absent" :
                                $"type{wrongType} box={(wrongBoxRead ? $"{wrongBox.Left},{wrongBox.Top},{wrongBox.Right},{wrongBox.Bottom}" : "unreadable")}"));
                }
                finally { DeleteObject(wrongProbe); }
                Require(host.HasLiveAttachment, "Explicit clip repair after a wrong region needs a live attachment.");
                bool wrongRestore = host.RestoreViewport();
                checks.Add($"Wrong clip: immediate RestoreViewport returned {wrongRestore}; waiting for eventual valid clip and settled flag.");
                await Until("region-wrong",() => BoundsExact(hwnd) && RegionMatches(hwnd) && !Settling(),TimeSpan.FromMilliseconds(1500),
                    "Explicit viewport restore did not reach exact bounds, a valid clip and a cleared settling flag after a wrong region.");
                CheckBounds();
                checks.Add("Wrong clip case settled to a valid clip (" + RegionState(hwnd) + ").");
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
            Require(GetParent(hwnd) == 0 && GetWindow(hwnd,GwOwner) == 0,
                "Native input queue was joined to another window or gained an owner.");
            var parent = (nint)typeof(NativeWindowHost).GetProperty("ContainerHandle", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(host)!;
            GetWindowRect(parent, out var bounds); GetClientRect(parent, out var client);
            Require(origin.X == bounds.Left && origin.Y == bounds.Top &&
                child.Right == client.Right && child.Bottom == client.Bottom,
                "Native desktop escaped or stopped matching its host bounds.");
        }
        bool BoundsExact(nint target)
        {
            if (!host.HasLiveAttachment) return false;
            var parent = (nint)typeof(NativeWindowHost).GetProperty("ContainerHandle", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(host)!;
            if (!GetClientRect(target,out var child)) return false;
            var origin = new Point(); if (!ClientToScreen(target,ref origin)) return false;
            if (GetParent(target) != 0 || GetWindow(target,GwOwner) != 0) return false;
            if (!GetWindowRect(parent,out var bounds) || !GetClientRect(parent,out var client)) return false;
            return origin.X == bounds.Left && origin.Y == bounds.Top &&
                child.Right == client.Right && child.Bottom == client.Bottom;
        }
        bool OutsideHost(nint target,nint manager)
        {
            var parent = (nint)typeof(NativeWindowHost).GetProperty("ContainerHandle", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(host)!;
            if (!GetWindowRect(target,out var source)) return false;
            bool Apart(Rect slot) => source.Right <= slot.Left || source.Left >= slot.Right
                || source.Bottom <= slot.Top || source.Top >= slot.Bottom;
            return GetWindowRect(parent,out var hostRect) && Apart(hostRect)
                && GetWindowRect(manager,out var managerRect) && Apart(managerRect);
        }
        bool RegionMatches(nint target)
        {
            // No explicit clip is a valid clip only when the viewport is the whole
            // window: zero insets AND outer pixels == client pixels. Any frame
            // outside the viewport still requires a matching region.
            if (!GetClientRect(target,out var client)) return false;
            if (!GetWindowRect(target,out var frame)) return false;
            var origin = new Point(); if (!ClientToScreen(target,ref origin)) return false;
            int insetX = origin.X - frame.Left, insetY = origin.Y - frame.Top;
            bool fullWindow = insetX == 0 && insetY == 0 &&
                frame.Right - frame.Left == client.Right && frame.Bottom - frame.Top == client.Bottom;
            var probe = CreateRectRgn(0,0,0,0);
            try
            {
                int type = GetWindowRgn(target,probe);
                if (type == 0) return fullWindow;
                if (GetRgnBox(probe,out var box) == 0) return false;
                return box.Left == insetX && box.Top == insetY &&
                    box.Right == insetX + client.Right && box.Bottom == insetY + client.Bottom;
            }
            finally { DeleteObject(probe); }
        }
        string RegionState(nint target)
        {
            var probe = CreateRectRgn(0,0,0,0);
            try
            {
                int type = GetWindowRgn(target,probe);
                if (type == 0) return "region=absent valid=" + (RegionMatches(target) ? "True" : "False");
                return GetRgnBox(probe,out var box) == 0 ? "region=unreadable"
                    : $"region=type{type} box={box.Left},{box.Top},{box.Right},{box.Bottom}";
            }
            finally { DeleteObject(probe); }
        }
        bool Settling()
        {
            // IsViewportSettling is internal to the Shell assembly; reflect for the fixture.
            var property = typeof(NativeWindowHost).GetProperty("IsViewportSettling",BindingFlags.Instance | BindingFlags.NonPublic);
            return property?.GetValue(host) is true;
        }
        JsonElement? FramesState(out string? error)
        {
            error = null;
            try
            {
                var path = Path.Combine(run,"frames.json");
                if (!File.Exists(path)) { error = "frames.json missing"; return null; }
                using var document = JsonDocument.Parse(File.ReadAllText(path));
                return document.RootElement.Clone();
            }
            catch (Exception failure) { error = failure.Message; return null; }
        }
        static double? FrameNumber(JsonElement? frames,string? section,string name)
        {
            if (frames is not { } value) return null;
            var node = value;
            if (section is not null && (!value.TryGetProperty(section,out node) || node.ValueKind != JsonValueKind.Object)) return null;
            return node.TryGetProperty(name,out var field) && field.ValueKind == JsonValueKind.Number && field.TryGetDouble(out var number) ? number : null;
        }
        string FramesSummary(out int? physicalWidth,out int? physicalHeight)
        {
            var frames = FramesState(out var error);
            double? innerWidth = FrameNumber(frames,"viewport","width"), innerHeight = FrameNumber(frames,"viewport","height");
            double? dpr = FrameNumber(frames,"viewport","dpr");
            double? boundsWidth = FrameNumber(frames,"bounds","width"), boundsHeight = FrameNumber(frames,"bounds","height");
            double? contentWidth = FrameNumber(frames,"contentBounds","width"), contentHeight = FrameNumber(frames,"contentBounds","height");
            physicalWidth = innerWidth is { } width && dpr is { } widthScale ? (int?)Math.Round(width * widthScale) : null;
            physicalHeight = innerHeight is { } height && dpr is { } heightScale ? (int?)Math.Round(height * heightScale) : null;
            string Number(double? value,string format) => value?.ToString(format) ?? "?";
            return frames is null
                ? "frames unavailable (" + (error ?? "unknown") + ")"
                : $"inner={Number(innerWidth,"0.##")}x{Number(innerHeight,"0.##")} dpr={Number(dpr,"0.###")} " +
                  $"physicalPx={Number(physicalWidth,"0")}x{Number(physicalHeight,"0")} " +
                  $"win32Bounds={Number(boundsWidth,"0.###")}x{Number(boundsHeight,"0.###")} " +
                  $"contentBounds={Number(contentWidth,"0.###")}x{Number(contentHeight,"0.###")}";
        }
        (string Summary,object Payload) FailureDiagnostic(string stage,string message,long elapsedMs)
        {
            // Bounded fixture-local snapshot: names the exact mismatch and never
            // contains credentials; only this run directory and the fixture PIDs.
            var container = (nint)typeof(NativeWindowHost).GetProperty("ContainerHandle", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(host)!;
            var manager = new System.Windows.Interop.WindowInteropHelper(window).Handle;
            object RectOf(nint target)
            {
                if (target == 0 || !GetWindowRect(target,out var rect)) return new { error = "GetWindowRect failed" };
                return new { left = rect.Left, top = rect.Top, right = rect.Right, bottom = rect.Bottom,
                    width = rect.Right - rect.Left, height = rect.Bottom - rect.Top };
            }
            object ClientOf(nint target)
            {
                if (target == 0 || !GetClientRect(target,out var rect)) return new { error = "GetClientRect failed" };
                var origin = new Point();
                bool mapped = ClientToScreen(target,ref origin);
                return new { width = rect.Right, height = rect.Bottom,
                    originX = mapped ? origin.X : (int?)null, originY = mapped ? origin.Y : (int?)null };
            }
            object RegionOf(nint target)
            {
                var probe = CreateRectRgn(0,0,0,0);
                try
                {
                    int type = GetWindowRgn(target,probe);
                    if (type == 0) return new { type, box = (object?)null };
                    int boxResult = GetRgnBox(probe,out var box);
                    return new { type, box = boxResult == 0 ? (object?)null :
                        new { left = box.Left, top = box.Top, right = box.Right, bottom = box.Bottom } };
                }
                finally { DeleteObject(probe); }
            }
            object? Private(string name) => typeof(NativeWindowHost).GetField(name,BindingFlags.Instance | BindingFlags.NonPublic)?.GetValue(host);
            object LeaseState()
            {
                try
                {
                    var path = Path.Combine(run,"work","control-center","window-hosts",$"{pid}.json");
                    if (!File.Exists(path)) return new { present = false };
                    using var document = JsonDocument.Parse(File.ReadAllText(path));
                    var state = document.RootElement;
                    object? bounds = null;
                    if (state.TryGetProperty("bounds",out var value) && value.ValueKind == JsonValueKind.Object)
                        bounds = new { x = value.GetProperty("x").GetInt32(), y = value.GetProperty("y").GetInt32(),
                            width = value.GetProperty("width").GetInt32(), height = value.GetProperty("height").GetInt32() };
                    return new { present = true,
                        mode = state.TryGetProperty("mode",out var mode) ? mode.GetString() : null,
                        visible = state.TryGetProperty("visible",out var visible) && visible.GetBoolean(),
                        presentationEpoch = state.TryGetProperty("presentationEpoch",out var epoch) ? epoch.GetInt64() : (long?)null,
                        geometryOwner = state.TryGetProperty("geometryOwner",out var owner) ? owner.GetString() : null,
                        interactiveMove = state.TryGetProperty("interactiveMove",out var move) && move.GetBoolean(),
                        bounds };
                }
                catch (Exception error) { return new { error = error.Message }; }
            }
            object AckState()
            {
                try
                {
                    var path = Path.Combine(run,"work","control-center","window-hosts",$"{pid}.json.render.json");
                    if (!File.Exists(path)) return new { present = false };
                    using var document = JsonDocument.Parse(File.ReadAllText(path));
                    var state = document.RootElement;
                    return new { present = true,
                        rendererReady = state.TryGetProperty("rendererReady",out var ready) && ready.GetBoolean(),
                        shown = state.TryGetProperty("shown",out var shown) && shown.GetBoolean(),
                        presentationEpoch = state.TryGetProperty("presentationEpoch",out var epoch) ? epoch.GetInt64() : (long?)null,
                        error = state.TryGetProperty("error",out var failure) ? failure.GetString() : null };
                }
                catch (Exception error) { return new { parseError = error.Message }; }
            }
            bool parentZero = GetParent(hwnd) == 0, ownerZero = GetWindow(hwnd,GwOwner) == 0, visible = IsWindowVisible(hwnd);
            bool originMatches = false, sizeMatches = false, regionPresent = false, regionValid = false, originMapped = false, fullWindowClip = false;
            int insetX = 0, insetY = 0, childRight = 0, childBottom = 0;
            int childOriginX = 0, childOriginY = 0, containerLeft = 0, containerTop = 0, containerRight = 0, containerBottom = 0;
            if (GetClientRect(hwnd,out var childClient) && GetWindowRect(hwnd,out var childFrame) &&
                GetWindowRect(container,out var slot) && GetClientRect(container,out var containerClient))
            {
                childRight = childClient.Right; childBottom = childClient.Bottom;
                containerLeft = slot.Left; containerTop = slot.Top;
                containerRight = containerClient.Right; containerBottom = containerClient.Bottom;
                var origin = new Point();
                if (ClientToScreen(hwnd,ref origin))
                {
                    originMapped = true; childOriginX = origin.X; childOriginY = origin.Y;
                    originMatches = origin.X == slot.Left && origin.Y == slot.Top;
                    insetX = origin.X - childFrame.Left; insetY = origin.Y - childFrame.Top;
                    fullWindowClip = insetX == 0 && insetY == 0 &&
                        childFrame.Right - childFrame.Left == childClient.Right && childFrame.Bottom - childFrame.Top == childClient.Bottom;
                }
                sizeMatches = childClient.Right == containerClient.Right && childClient.Bottom == containerClient.Bottom;
            }
            var regionProbe = CreateRectRgn(0,0,0,0);
            try
            {
                regionPresent = GetWindowRgn(hwnd,regionProbe) != 0;
                regionValid = regionPresent
                    ? GetRgnBox(regionProbe,out var box) != 0 && box.Left == insetX && box.Top == insetY &&
                        box.Right == insetX + childRight && box.Bottom == insetY + childBottom
                    : fullWindowClip;
            }
            finally { DeleteObject(regionProbe); }
            string summary = $"stage={stage} parentZero={parentZero} ownerZero={ownerZero} visible={visible} " +
                $"originMatches={originMatches} sizeMatches={sizeMatches} regionPresent={regionPresent} regionValid={regionValid} fullWindowClip={fullWindowClip} " +
                $"childOrigin=({(originMapped ? childOriginX : (int?)null)},{(originMapped ? childOriginY : (int?)null)}) " +
                $"containerOrigin=({containerLeft},{containerTop}) childSize={childRight}x{childBottom} containerClient={containerRight}x{containerBottom} " +
                $"liveResize={Private("_liveResize")} settling={Private("_settling")} forceRepair={Private("_forceRepair")} settleHidden={Private("_settleHidden")}";
            object payload = new { message, elapsedMs,
                checks = new { parentZero, ownerZero, visible, originMapped, originMatches, sizeMatches, regionPresent, regionValid, fullWindowClip,
                    childOriginX = originMapped ? childOriginX : (int?)null, childOriginY = originMapped ? childOriginY : (int?)null,
                    childRight, childBottom, containerLeft, containerTop, containerRight, containerBottom, insetX, insetY },
                expected = new { childClientOriginMatchesContainerWindowOrigin = originMatches,
                    childClientSizeMatchesContainerClient = sizeMatches,
                    childClientOriginX = originMapped ? childOriginX : (int?)null, childClientOriginY = originMapped ? childOriginY : (int?)null,
                    containerWindowLeft = containerLeft, containerWindowTop = containerTop,
                    containerClientRight = containerRight, containerClientBottom = containerBottom,
                    regionBox = new { left = insetX, top = insetY, right = insetX + childRight, bottom = insetY + childBottom } },
                child = new { handle = hwnd.ToString(), window = RectOf(hwnd), client = ClientOf(hwnd), region = RegionOf(hwnd),
                    style = GetWindowLongPtrW(hwnd,GwlStyle).ToInt64().ToString("X"), exStyle = GetWindowLongPtrW(hwnd,GwlExStyle).ToInt64().ToString("X"),
                    parent = GetParent(hwnd).ToString(), owner = GetWindow(hwnd,GwOwner).ToString() },
                container = new { handle = container.ToString(), window = RectOf(container), client = ClientOf(container) },
                manager = new { handle = manager.ToString(), window = RectOf(manager), client = ClientOf(manager) },
                flags = new { liveResize = Private("_liveResize"), settling = Private("_settling"), forceRepair = Private("_forceRepair"),
                    settleHidden = Private("_settleHidden"), queued = Private("_queuedLayout")?.ToString() },
                host = new { attached = host.IsAttached, live = host.HasLiveAttachment, attachedHandle = host.AttachedHandle.ToString(),
                    lastError = host.LastError },
                lease = LeaseState(), ack = AckState(),
                frames = FramesState(out var framesError), framesError };
            return (summary,payload);
        }
        async Task Until(string stage,Func<bool> ready,TimeSpan timeout,string message)
        {
            var started = DateTime.UtcNow;
            while (DateTime.UtcNow - started < timeout) { if (ready()) return; await Task.Delay(60); }
            if (ready()) return;
            var (summary,payload) = FailureDiagnostic(stage,message,(long)(DateTime.UtcNow - started).TotalMilliseconds);
            string json;
            try
            {
                json = JsonSerializer.Serialize(new { stage, message, elapsedMs = (long)(DateTime.UtcNow - started).TotalMilliseconds, summary,
                    state = payload }, new JsonSerializerOptions { WriteIndented = true });
            }
            catch (Exception error) { json = "{\"diagnostic\":\"unavailable\",\"error\":" + JsonSerializer.Serialize(error.Message) + "}"; }
            string path = Path.Combine(run,"until-failure-" + string.Concat(stage.Where(char.IsLetterOrDigit)) + ".json");
            try { File.WriteAllText(path,json); } catch { path = "(diagnostic not written)"; }
            throw new InvalidOperationException(message + " · " + summary + " · diagnostic=" + path + " · " +
                (json.Length > 3000 ? json[..3000] + "…" : json));
        }
        window.Show(); return app.Run();
    }
    private static void Require(bool value, string message) { if (!value) throw new InvalidOperationException(message); }
}
