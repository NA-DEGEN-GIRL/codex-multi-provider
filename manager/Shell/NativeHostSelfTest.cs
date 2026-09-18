using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;
using static Codex.ControlCenter.Shell.NativeWindowInterop;

namespace Codex.ControlCenter.Shell;

public sealed record NativeHostSelfTestResult(bool Passed, IReadOnlyList<string> Checks, string? Error);

/// <summary>
/// Exercises only a dedicated child fixture, never an installed/running Codex window.
/// Route --native-host-fixture to RunFixture() at application startup. Run RunAsync()
/// on the WPF dispatcher. This does not certify Electron, IME, popups or shell-crash survival.
/// </summary>
public static class NativeHostSelfTest
{
    public static int RunFixture()
    {
        nint hwnd = CreateWindowExW(0x08000000 /* WS_EX_NOACTIVATE */, "STATIC",
            "Codex Control Center native host test fixture",
            (uint)(WsVisible | WsCaption | WsThickFrame | WsSysMenu | WsMinimizeBox | WsMaximizeBox),
            -30000, -30000, 640, 420, 0, 0, 0, 0);
        if (hwnd == 0) return 70;
        CreateWindowExW(0, "EDIT", "Dedicated native fixture — no Codex session is opened.",
            (uint)(WsChild | WsVisible | 0x00800000 /* WS_BORDER */), 16, 16, 500, 60, hwnd, 0, 0, 0);
        // The process is launched with a hidden startup window. Explicitly show this
        // off-screen HWND without activation so the primary-window guard can be tested.
        ShowWindow(hwnd, 4 /* SW_SHOWNOACTIVATE */);
        try
        {
            // A finite lifetime also cleans up a fixture if the test runner crashes.
            var lifetime = Stopwatch.StartNew();
            while (IsWindow(hwnd) && lifetime.Elapsed < TimeSpan.FromSeconds(45))
            {
                while (PeekMessageW(out Message message, 0, 0, 0, 1 /* PM_REMOVE */))
                {
                    TranslateMessage(in message);
                    DispatchMessageW(in message);
                }
                Thread.Sleep(15);
            }
        }
        finally
        {
            if (IsWindow(hwnd)) DestroyWindow(hwnd);
        }
        return 0;
    }

    public static async Task<NativeHostSelfTestResult> RunAsync()
    {
        Application.Current.Dispatcher.VerifyAccess();
        var checks = new List<string>();
        Process? fixture = null;
        nint fixtureWindow = 0;
        var host = new NativeWindowHost();
        var surface = new System.Windows.Controls.Grid();
        surface.Children.Add(host);
        var deck = new NativeHostDeck(child => surface.Children.Add(child));
        var window = new Window
        {
            Title = "Native host self-test", Width = 800, Height = 600,
            Left = -28000, Top = -28000, ShowActivated = false,
            ShowInTaskbar = false, Content = surface
        };
        string? failure = null;
        try
        {
            string executable = Environment.ProcessPath ?? throw new InvalidOperationException("Current executable path is unavailable.");
            var startInfo = new ProcessStartInfo(executable)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            if (string.Equals(Path.GetFileNameWithoutExtension(executable), "dotnet", StringComparison.OrdinalIgnoreCase))
                startInfo.ArgumentList.Add(Assembly.GetEntryAssembly()?.Location ?? throw new InvalidOperationException("Entry assembly path is unavailable."));
            startInfo.ArgumentList.Add("--native-host-fixture");
            fixture = Process.Start(startInfo) ?? throw new InvalidOperationException("Could not start dedicated native fixture.");
            window.Show();
            host.ForegroundWindow = () => new WindowInteropHelper(window).Handle;
            await Application.Current.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            var timeout = Stopwatch.StartNew();
            while (fixtureWindow == 0 && timeout.Elapsed < TimeSpan.FromSeconds(10))
            {
                if (fixture.HasExited) throw new InvalidOperationException($"Fixture exited before creating its window ({fixture.ExitCode}). Check --native-host-fixture startup routing.");
                fixtureWindow = FindVisiblePrimaryWindow(fixture.Id);
                if (fixtureWindow == 0) await Task.Delay(50);
            }
            Require(fixtureWindow != 0, "Fixture did not create a visible primary window.");
            nint originalParent = GetParent(fixtureWindow);
            nint originalStyle = ReadStyle(fixtureWindow, GwlStyle);
            nint originalExStyle = ReadStyle(fixtureWindow, GwlExStyle);
            checks.Add("Created a separate fixture process; no installed Codex windows were enumerated by title or changed.");
            Require(!host.Attach(fixtureWindow, Environment.ProcessId, executable, out _), "Wrong PID was accepted.");
            Require(!host.Attach(fixtureWindow, fixture.Id, Path.Combine(Path.GetDirectoryName(executable)!, "untrusted-different-executable.exe"), out _), "Wrong executable was accepted.");
            Require(!host.Attach(fixtureWindow, fixture.Id, Path.GetFileName(executable), out _), "Relative executable path was accepted.");
            Require(GetParent(fixtureWindow) == originalParent, "Rejected attachment changed the fixture parent.");
            checks.Add("Rejected wrong PID, mismatched executable and relative path without changing the fixture.");
            int feedbackLayoutCount = 0;
            Action<string> feedbackLayout = message =>
            {
                if (!message.StartsWith("독립 입력 창 연결 시작", StringComparison.Ordinal)) return;
                feedbackLayoutCount++;
                window.Width += 1;
                window.UpdateLayout();
            };
            host.Diagnostic += feedbackLayout;
            Require(host.Attach(fixtureWindow, fixture.Id, executable, out string error), error);
            host.Diagnostic -= feedbackLayout;
            Require(feedbackLayoutCount == 1, "Feedback-layout regression stimulus did not run.");
            Require(host.AttachedHandle == fixtureWindow, "Attached handle was not recorded.");
            Require(GetParent(fixtureWindow) == originalParent && GetWindow(fixtureWindow, GwOwner) == 0, "Independent parent/owner changed.");
            Require((ReadStyle(fixtureWindow, GwlStyle).ToInt64() & WsChild) == 0, "Native input window became a child.");
            checks.Add("Kept the verified native input queue independent during reentrant WPF layout. " + host.DpiSummary);
            window.Width = 910;
            window.Height = 670;
            await Application.Current.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            await Task.Delay(100);
            Require(GetClientRect(host.ContainerHandle, out NativeWindowInterop.Rect parentRect), "Cannot read host client bounds.");
            Require(GetClientRect(fixtureWindow, out NativeWindowInterop.Rect childRect), "Cannot read attached fixture bounds.");
            Require(Math.Abs(childRect.Right - parentRect.Right) <= 1 &&
                Math.Abs(childRect.Bottom - parentRect.Bottom) <= 1, "Child size did not follow native host client pixels.");
            checks.Add("Positioned the independent viewport in native screen pixels.");
            nint stableParent = GetAncestor(fixtureWindow, GaParent);
            // The reported failure changed style only AFTER Attach returned.
            // Exercise that real Win32 timing rather than a synchronous callback.
            WriteStyle(fixtureWindow, GwlStyle, (nint)0x10C70000);
            await Task.Delay(1300);
            Require((ReadStyle(fixtureWindow, GwlStyle).ToInt64() & WsChild) == 0,
                "Late native style rewrite was not repaired.");
            Require(GetAncestor(fixtureWindow, GaParent) == stableParent && !fixture.HasExited,
                "Late style repair replaced the parent or terminated the fixture.");
            var origin = new NativeWindowInterop.Point();
            Require(ClientToScreen(fixtureWindow, ref origin) && GetWindowRect(host.ContainerHandle, out var containerBounds)
                && origin.X == containerBounds.Left && origin.Y == containerBounds.Top,
                "Native client area was left at the wrong screen origin.");
            checks.Add("Kept native frame/input styles; clipped and aligned its client area without reparenting.");
            window.IsEnabled = false;
            await Task.Delay(100);
            Require(IsWindowVisible(fixtureWindow) && GetWindow(new WindowInteropHelper(window).Handle, 2) == fixtureWindow,
                "Disabled manager lost its live backdrop or exposed the native input window.");
            // Simulate an app-side show already queued before the modal opened.
            // Native visibility events must correct it without the 1s fallback.
            SetWindowPos(fixtureWindow, -1, 0, 0, 0, 0,
                SwpShowWindow | SwpNoActivate | SwpNoMove | SwpNoSize | SwpAsyncWindowPos);
            await Task.Delay(250);
            Require(IsWindowVisible(fixtureWindow) && (ReadStyle(fixtureWindow, GwlExStyle).ToInt64() & 8) == 0 &&
                GetWindow(new WindowInteropHelper(window).Handle, 2) == fixtureWindow,
                $"A delayed native show covered the disabled manager: visible={IsWindowVisible(fixtureWindow)}, " +
                $"exStyle={ReadStyle(fixtureWindow, GwlExStyle):X}, next={GetWindow(new WindowInteropHelper(window).Handle, 2)}, fixture={fixtureWindow}.");
            window.IsEnabled = true;
            await Task.Delay(100);
            Require(GetWindow(new WindowInteropHelper(window).Handle, 3) == fixtureWindow,
                "Closing settings did not return the native editor above its manager.");
            checks.Add("Disabled manager retains its live backdrop with native input behind it; delayed TOPMOST show is corrected and closing restores editor order.");
            // Losing foreground must keep the viewport painted behind the new
            // foreground app, without topmost, owner or activation links.
            host.ForegroundWindow = () => (nint)1;
            await Task.Delay(1200);
            Require(IsWindowVisible(fixtureWindow) && (ReadStyle(fixtureWindow, GwlExStyle).ToInt64() & 8) == 0,
                "Unrelated foreground hid the selected viewport or left it topmost.");
            Require(GetWindow(new WindowInteropHelper(window).Handle, 3) == fixtureWindow,
                "Background viewport is not immediately above its manager.");
            host.ForegroundWindow = () => new WindowInteropHelper(window).Handle;
            for (var episode = 0; episode < 5; episode++)
            {
                SetWindowPos(fixtureWindow, 0, -27000, -27000, 300, 250,
                    SwpNoActivate | SwpNoZOrder | SwpAsyncWindowPos);
                await Task.Delay(1200);
                Require(GetClientRect(fixtureWindow, out var repaired) &&
                    Math.Abs(repaired.Right - parentRect.Right) <= 1 && Math.Abs(repaired.Bottom - parentRect.Bottom) <= 1,
                    "A later native settings resize exhausted an earlier episode's budget.");
            }
            checks.Add("Kept background viewport visible below other apps and repaired five independent native resize episodes.");
            var rootHandle = new WindowInteropHelper(window).Handle;
            Require((ReadStyle(fixtureWindow, GwlExStyle).ToInt64() & 8) == 0, "Foreground viewport became globally topmost.");
            host.ForegroundWindow = () => fixtureWindow;
            SetWindowPos(fixtureWindow, 0, 0, 0, 0, 0, SwpNoActivate | SwpNoMove | SwpNoSize | SwpAsyncWindowPos);
            await Task.Delay(300);
            host.SynchronizeLayout(); await Task.Delay(150);
            Require(GetWindow(fixtureWindow, 2) == rootHandle, "Native editor activation left the manager behind unrelated windows.");
            host.ForegroundWindow = () => rootHandle;
            for (var cycle = 0; cycle < 3; cycle++)
            {
                ShowWindow(rootHandle, 7); // SW_SHOWMINNOACTIVE
                await Task.Delay(180);
                Require(!IsWindowVisible(fixtureWindow), "Minimized manager left its viewport on screen.");
                ShowWindow(rootHandle, 4); // SW_SHOWNOACTIVATE
                await Task.Delay(250);
                Require(IsWindowVisible(fixtureWindow), "Restoring manager did not restore its viewport.");
                Require((ReadStyle(fixtureWindow, GwlExStyle).ToInt64() & 8) == 0, "Restore promoted viewport to topmost.");
            }
            checks.Add("Foreground editor keeps its manager adjacent without TOPMOST; three minimize/restore cycles show the same viewport.");
            host.Detach();
            await Task.Delay(100);
            Require(!host.IsAttached, "Detach retained an attachment: " + host.LastError);
            Require(GetParent(fixtureWindow) == originalParent, "Original parent was not restored.");
            Require(ReadStyle(fixtureWindow, GwlStyle) == originalStyle, $"Original style was not restored: expected {originalStyle:X}, actual {ReadStyle(fixtureWindow, GwlStyle):X}.");
            Require(ReadStyle(fixtureWindow, GwlExStyle) == originalExStyle, $"Original extended style was not restored: expected {originalExStyle:X}, actual {ReadStyle(fixtureWindow, GwlExStyle):X}.");
            Require(!fixture.HasExited, "Detach terminated the fixture process.");
            checks.Add("Restored parent and window styles; the independent fixture process stayed alive.");
            Require(NativeWindowHost.SetWindowVisibility(fixtureWindow, fixture.Id, executable, false, out error), error);
            Require(!IsWindowVisible(fixtureWindow), "Fixture did not hide.");
            Require(!NativeWindowHost.SetWindowVisibility(fixtureWindow, Environment.ProcessId, executable, true, out _), "Visibility operation accepted wrong PID.");
            Require(NativeWindowHost.SetWindowVisibility(fixtureWindow, fixture.Id, executable, true, out error), error);
            checks.Add("Verified hide/show for an inactive managed window; wrong-PID visibility request was rejected.");
            var foreground = deck.Select("foreground");
            foreground.Visibility = Visibility.Visible;
            var background = deck.Ensure("background");
            background.ForegroundWindow = host.ForegroundWindow;
            window.UpdateLayout();
            await Application.Current.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            Require(NativeWindowHost.SetWindowVisibility(fixtureWindow, fixture.Id, executable, false, out error), error);
            Require(background.Attach(fixtureWindow, fixture.Id, executable, out error, allowHidden: true), error);
            var backgroundParent = GetParent(fixtureWindow);
            await Task.Delay(100);
            Require(deck.Current == foreground && !IsWindowVisible(fixtureWindow), "Background attachment changed the active profile or exposed the fixture.");
            Require(GetClientRect(background.ContainerHandle, out var backgroundRect) && backgroundRect.Right > 100 && backgroundRect.Bottom > 100,
                "A hidden background host did not receive a usable client area.");
            deck.Select("background").Visibility = Visibility.Visible;
            window.UpdateLayout();
            await Application.Current.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            await Task.Delay(100);
            Require(IsWindowVisible(fixtureWindow) && GetParent(fixtureWindow) == backgroundParent,
                "Selecting a prepared profile did not show its independent viewport.");
            deck.Select("foreground").Visibility = Visibility.Visible;
            window.UpdateLayout();
            await Application.Current.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            await Task.Delay(100);
            Require(!IsWindowVisible(fixtureWindow) && GetParent(fixtureWindow) == backgroundParent,
                $"Switching profiles exposed or detached the background window: visible={IsWindowVisible(fixtureWindow)}, parent={GetParent(fixtureWindow)}, expected={backgroundParent}.");
            background.DetachForClose();
            await Task.Delay(100);
            Require(!IsWindowVisible(fixtureWindow), "Detaching a managed hidden profile exposed a separate window.");
            checks.Add("Profile switches preserved independent ownership and kept background viewports hidden.");
            Require(NativeWindowHost.SetWindowVisibility(fixtureWindow, fixture.Id, executable, true, out error), error);
            Require(host.Attach(fixtureWindow, fixture.Id, executable, out error), error);
            host.Detach(); await Task.Delay(100);
            var leaseDirectory = Path.Combine(Path.GetTempPath(), "codex-viewport-test-" + Guid.NewGuid().ToString("N"));
            host.WindowStateDirectory = leaseDirectory;
            Require(host.Attach(fixtureWindow, fixture.Id, executable, out error), error);
            var leasePath = Path.Combine(leaseDirectory, $"{fixture.Id}.json");
            Require(NativeViewportRecovery.Run(leasePath) == 3, "Recovery changed a window with a live manager.");
            var orphan = System.Text.Json.Nodes.JsonNode.Parse(File.ReadAllText(leasePath))!;
            orphan["shellPid"] = int.MaxValue; // Dedicated fixture only; no real manager is killed.
            File.WriteAllText(leasePath, orphan.ToJsonString());
            Require(NativeViewportRecovery.Run(leasePath) == 0, "Orphan viewport recovery failed.");
            await Task.Delay(1200);
            Require(!host.IsAttached && GetParent(fixtureWindow) == 0 && !fixture.HasExited,
                "Recovery lost the native window or left a stale host attachment.");
            Require(ReadStyle(fixtureWindow, GwlExStyle) == originalExStyle, "Recovery left a tool/topmost window behind.");
            nint recoveredRegion = CreateRectRgn(0,0,0,0);
            Require(GetWindowRgn(fixtureWindow,recoveredRegion)==0, "Recovery left the viewport clipping region behind.");
            DeleteObject(recoveredRegion);
            checks.Add("Rejected recovery of a live owner; restored an orphan's frame, region and taskbar without terminating its work.");
            host.WindowStateDirectory = null;
            Require(host.Attach(fixtureWindow, fixture.Id, executable, out error), error);
            // Only this dedicated fixture receives WM_CLOSE. A later timer tick must forget a
            // destroyed HWND instead of resizing/focusing a recycled unrelated handle.
            PostMessageW(fixtureWindow, WmClose, 0, 0);
            await Task.Delay(1300);
            Require(!host.IsAttached && !IsWindow(fixtureWindow), "Closed fixture window was not released by the host.");
            checks.Add("Detected fixture window closure and cleared the stale attachment.");
        }
        catch (Exception ex)
        {
            failure = ex.ToString();
        }
        finally
        {
            foreach (var child in deck.Hosts) child.DetachForClose();
            host.Detach();
            if (!host.IsAttached) window.Close();
            if (fixtureWindow != 0 && IsWindow(fixtureWindow))
            {
                GetWindowThreadProcessId(fixtureWindow, out uint actualPid);
                if (fixture is not null && actualPid == (uint)fixture.Id)
                    PostMessageW(fixtureWindow, WmClose, 0, 0);
            }
            if (fixture is not null)
            {
                // Never kill: the fixture closes normally or exits on its 45-second lease.
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                try { await fixture.WaitForExitAsync(timeout.Token); }
                catch (OperationCanceledException) { failure ??= "Fixture did not exit promptly after WM_CLOSE; its finite lifetime will clean it up."; }
                fixture.Dispose();
            }
        }
        return new NativeHostSelfTestResult(failure is null, checks, failure);
    }

    private static nint FindVisiblePrimaryWindow(int pid)
    {
        nint match = 0;
        EnumWindows((hwnd, _) =>
        {
            GetWindowThreadProcessId(hwnd, out uint candidatePid);
            if (candidatePid == (uint)pid && IsWindowVisible(hwnd) && GetWindow(hwnd, GwOwner) == 0)
            {
                match = hwnd;
                return false;
            }
            return true;
        }, 0);
        return match;
    }

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }
}
