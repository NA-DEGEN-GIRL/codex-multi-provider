using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Threading;
using Microsoft.Win32.SafeHandles;
using static Codex.ControlCenter.Shell.NativeWindowInterop;

namespace Codex.ControlCenter.Shell;

/// <summary>
/// Displays an independent top-level Codex window over the native viewport.
/// Cross-process parenting AND ownership attach input queues and break Chromium
/// TSF activation. Neither relationship is ever installed by this host.
/// Geometry and visibility are coordinated without forwarding keyboard input.
/// </summary>
public sealed class NativeWindowHost : HwndHost
{
    private nint _container, _foregroundHook, _presentationHook;
    private Attachment? _attachment;
    private HwndSource? _rootSource;
    private readonly HwndSourceHook _rootMessages;
    private readonly WinEventCallback _foregroundChanged, _presentationChanged;
    private readonly DispatcherTimer _watch, _settleWatch;
    private bool _changingWindow, _resizing;
    private DispatcherOperation? _queuedLayout;
    private bool _liveResize, _settling, _forceRepair, _settleHidden;
    private bool _settleFailureQueued;
    private long _settleStarted;
    private int _settleVersion;
    private string? _lastInputState, _lastPresentationState, _lastLayoutFailure;
    internal string? WindowStateDirectory { get; set; }
    internal ResponsivenessMonitor? Responsiveness { get; set; }
    internal Func<long> ResizeClock = () => Environment.TickCount64;
    // Overridden only by the off-screen native self-test. Never transfers focus.
    internal Func<nint> ForegroundWindow = GetForegroundWindow;
    internal nint ContainerHandle => _container;
    public bool IsTransitioning => _changingWindow || _resizing;
    internal bool IsViewportSettling => _settling;
    public nint AttachedHandle => _attachment?.Hwnd ?? 0;
    public bool IsAttached => AttachedHandle != 0;
    public bool HasLiveAttachment => _attachment is { } attached && IsAlive(attached) && IsIndependent(attached);
    public bool IsLayoutReady => _container != 0 && GetClientRect(_container, out var rect) && rect.Right > 1 && rect.Bottom > 1;
    public string LastError { get; private set; } = "";
    public string DpiSummary { get; private set; } = "";
    public event EventHandler? AttachmentChanged;
    public event Action<nint, bool>? AttachmentLost;
    public event Action<string>? ViewportRecoveryFailed;
    public event EventHandler? ViewportRecoveryCompleted;
    public event Action<string>? Diagnostic;
    internal Task NavigateAsync(NoteTask task, CancellationToken cancellation) => HasLiveAttachment && _attachment?.Lease is { } lease
        ? lease.NavigateAsync(task, cancellation) : Task.FromException(new InvalidOperationException("Codex 창이 아직 연결되지 않았습니다."));

    public NativeWindowHost()
    {
        // Keep the exact root hook delegate for the entire host lifetime and
        // use that same instance when removing it from a replaced root source.
        _rootMessages = RootMessages;
        Focusable = true;
        _foregroundChanged = (_, _, _, _, _, _, _) => QueueLayout();
        _presentationChanged = (_, eventId, hwnd, objectId, _, _, _) =>
        {
            // Owl can finish a previously queued show after the manager hid the
            // viewport. React to the actual native transition, not just its ack.
            if (objectId == 0 && hwnd == AttachedHandle && eventId is 0x8002 or 0x8003 or 0x800B) QueueLayout();
            // Top-level Z-order changes report the desktop container, not the
            // reordered HWND. This hook is already scoped to the attached PID.
            if (eventId == 0x8004 && hwnd == GetDesktopWindow()) QueueLayout();
        };
        _watch = new DispatcherTimer(TimeSpan.FromSeconds(1), DispatcherPriority.Background,
            (_, _) => CheckAttachedWindow(), Dispatcher);
        _watch.Stop();
        _settleWatch = new DispatcherTimer(TimeSpan.FromMilliseconds(40), DispatcherPriority.Render,
            (_, _) => SynchronizeLayout(), Dispatcher);
        _settleWatch.Stop();
        IsVisibleChanged += (_, _) => { SynchronizeLayout(); QueueLayout(); };
        IsEnabledChanged += (_, _) => QueueLayout();
        Loaded += (_, _) => { ObserveRoot(); QueueLayout(); };
        Unloaded += (_, _) => { if (_attachment is { } a && IsAlive(a)) SetVisible(a, false); };
    }

    /// <summary>Hide/show only an identity-verified detached primary window; never activates it.</summary>
    public static bool SetWindowVisibility(nint hwnd, int pid, string expectedExecutable, bool visible, out string error)
    {
        error = "";
        try
        {
            if (hwnd == 0 || pid <= 0 || pid == Environment.ProcessId || !IsWindow(hwnd) ||
                !Path.IsPathFullyQualified(expectedExecutable))
                throw new ArgumentException("A valid external window and absolute executable path are required.");
            GetWindowThreadProcessId(hwnd, out uint actualPid);
            if (actualPid != (uint)pid || GetAncestor(hwnd, GaRoot) != hwnd || GetWindow(hwnd, GwOwner) != 0)
                throw new InvalidOperationException("The window is not the expected detached primary window.");
            using SafeProcessHandle process = OpenProcess(ProcessQueryLimitedInformation, false, actualPid);
            if (process.IsInvalid)
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot verify the external process.");
            var executable = new StringBuilder(32768);
            uint length = (uint)executable.Capacity;
            if (!QueryFullProcessImageNameW(process, 0, executable, ref length) ||
                !SamePath(executable.ToString(), expectedExecutable))
                throw new InvalidOperationException("The executable does not match the expected path.");
            if (!GetExitCodeProcess(process, out uint code) || code != StillActive)
                throw new InvalidOperationException("The external process has exited.");
            // ShowWindow returns previous visibility, not success. Verify the final state.
            ShowWindow(hwnd, visible ? 4 /* SW_SHOWNOACTIVATE */ : 0 /* SW_HIDE */);
            if (!IsWindow(hwnd) || IsWindowVisible(hwnd) != visible)
                throw new InvalidOperationException("The window did not reach the requested visibility.");
            return true;
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException or ArgumentException)
        {
            error = ex.Message;
            return false;
        }
    }

    public bool Attach(nint hwnd, int pid, string expectedExecutable, out string error, bool allowHidden = false)
    {
        using var timing = Responsiveness?.Stage($"native.attach.{pid}");
        Dispatcher.VerifyAccess();
        if (IsTransitioning) return Fail("A native window operation is already in progress.", out error);
        if (!IsLayoutReady) return Fail("The native host is waiting for its measured client area.", out error);
        using var ownership = NativeViewportRecovery.TryLock(pid, hwnd);
        if (ownership is null) return Fail("이 창의 표시 상태를 복구하고 있습니다. 잠시 후 다시 연결합니다.", out error);
        _changingWindow = true;
        Attachment? candidate = null;
        error = "";
        try
        {
            if (_attachment?.Hwnd == hwnd)
            {
                if (pid != _attachment.Pid || !SamePath(expectedExecutable, _attachment.Executable) || !HasLiveAttachment)
                    return Fail("The attached window identity no longer matches.", out error);
                return ResizeChildCore() || Fail(LastError, out error);
            }
            candidate = ValidateAndCapture(hwnd, pid, expectedExecutable, allowHidden);
            DetachCore();
            if (IsAttached) throw new InvalidOperationException(LastError);
            _attachment = candidate; candidate = null;
            var attached = _attachment;
            _lastPresentationState = null;
            if (!SetPropW(hwnd, attached.MarkerName, attached.MarkerValue))
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot mark the verified window lifetime.");
            attached.Marked = true;
            _presentationHook = SetWinEventHook(0x8002 /* OBJECT_SHOW */, 0x800B /* OBJECT_LOCATIONCHANGE */,
                0, _presentationChanged, (uint)pid, 0, 0);
            if (_presentationHook == 0)
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot observe native viewport visibility.");
            attached.Region = CreateRectRgn(0, 0, 0, 0);
            if (GetWindowRgn(hwnd, attached.Region) == 0) { DeleteObject(attached.Region); attached.Region = 0; }
            if (WindowStateDirectory is not null) attached.Lease = new NativeWindowLease(WindowStateDirectory, pid, hwnd,
                () => Dispatcher.BeginInvoke(new Action(QueueLayout)), new NativeViewportRecovery.Snapshot(
                    attached.Executable, attached.MarkerName, attached.Style.ToInt64(), attached.ExStyle.ToInt64(),
                    attached.Bounds.Left, attached.Bounds.Top, attached.Bounds.Right - attached.Bounds.Left,
                    attached.Bounds.Bottom - attached.Bounds.Top, NativeViewportRecovery.SaveRegion(attached.Region)));
            Diagnostic?.Invoke($"독립 입력 창 연결 시작 · PID {pid} · HWND {hwnd} · {DpiSummary}");
            SetVisible(attached, false);
            // The native window stays unowned and top level for its entire life.
            // Keep its TSF document manager, activation and IME composition native.
            ApplyViewportStyle(attached);
            ObserveRoot();
            if (!ResizeChildCore(frameChanged: true)) throw new InvalidOperationException(LastError);
            if (!IsIndependent(attached)) throw new InvalidOperationException("The native input window is no longer independent.");
            _watch.Start();
            Diagnostic?.Invoke("독립 입력 창 연결 완료 · 한글 조합은 Codex가 직접 처리");
            AttachmentChanged?.Invoke(this, EventArgs.Empty);
            return true;
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException or ArgumentException or IOException or UnauthorizedAccessException)
        {
            candidate?.Dispose();
            if (_attachment?.Hwnd == hwnd) DetachCore(restoreVisibility: !allowHidden);
            return Fail(ex.Message, out error);
        }
        finally { _changingWindow = false; }
    }

    private static bool IsIndependent(Attachment a) => GetParent(a.Hwnd) == a.Parent &&
        GetAncestor(a.Hwnd, GaRoot) == a.Hwnd && GetWindow(a.Hwnd, GwOwner) == 0;

    private static void ApplyViewportStyle(Attachment a)
    {
        // Keep Chromium's non-client styles intact. Its own activation/show
        // path restores them; repeatedly stripping them interrupts mouse/TSF.
        // A native window region clips the frame to the viewport instead.
        WriteStyle(a.Hwnd, GwlExStyle, (nint)((ReadStyle(a.Hwnd, GwlExStyle).ToInt64() & ~WsExAppWindow) | WsExToolWindow));
    }

    private bool RepairStyle(Attachment a, out bool repaired)
    {
        repaired = false;
        var extended = ReadStyle(a.Hwnd, GwlExStyle).ToInt64();
        if ((extended & (WsExAppWindow | WsExToolWindow)) == WsExToolWindow) return true;
        ApplyViewportStyle(a);
        repaired = true;
        return true;
    }

    private void ObserveRoot()
    {
        var source = HwndSource.FromHwnd(GetAncestor(_container, GaRoot));
        if (source != _rootSource)
        {
            _rootSource?.RemoveHook(_rootMessages);
            _rootSource = source;
            source?.AddHook(_rootMessages);
        }
        if (_foregroundHook == 0)
            _foregroundHook = SetWinEventHook(3 /* EVENT_SYSTEM_FOREGROUND */, 3, 0, _foregroundChanged, 0, 0, 0);
        // A newly selected/created profile may join a root whose move loop has
        // already started, before this host was present to receive ENTERSIZEMOVE.
        var gui = new GuiThreadInfo { Size = (uint)Marshal.SizeOf<GuiThreadInfo>() };
        if (source is not null && GetGUIThreadInfo(GetWindowThreadProcessId(source.Handle, out _), ref gui) &&
            (gui.Flags & 2 /* GUI_INMOVESIZE */) != 0)
        {
            _liveResize = true;
            BeginViewportSettlement(park: true);
        }
    }

    private nint RootMessages(nint hwnd, int message, nint wParam, nint lParam, ref bool handled)
    {
        // Native movement is screen-space; HwndHost's relative layout can remain
        // unchanged while the manager moves to a different monitor.
        if (message == 0x231 /* ENTERSIZEMOVE */)
        {
            _liveResize = true;
            _settleWatch.Stop();
            try { BeginViewportSettlement(park: true); }
            catch (Win32Exception error) { LastError = error.Message; Diagnostic?.Invoke(error.Message); }
            QueueLayout();
        }
        else if (message == 0x232 /* EXITSIZEMOVE */)
        {
            _liveResize = false;
            _settleStarted = Environment.TickCount64;
            _forceRepair = true;
            if (_settling) _settleWatch.Start();
            QueueLayout();
        }
        if (message is 0x47 /* WINDOWPOSCHANGED */ or 5 /* SIZE */ or 0x18 /* SHOWWINDOW */
            or 6 /* ACTIVATE */ or 10 /* ENABLE */ or 0x2E0 /* DPICHANGED */) QueueLayout();
        return 0;
    }

    private void QueueLayout()
    {
        if (Dispatcher.HasShutdownStarted) return;
        var priority = _liveResize || _settling ? DispatcherPriority.Render : DispatcherPriority.Background;
        if (_queuedLayout is { Status: DispatcherOperationStatus.Pending } pending)
        {
            if (pending.Priority < priority) pending.Priority = priority;
            return;
        }
        _queuedLayout = Dispatcher.BeginInvoke(priority, new Action(() =>
        {
            _queuedLayout = null;
            SynchronizeLayout();
        }));
    }

    /// <summary>Repair the current viewport without detaching or changing native input ownership.</summary>
    public bool RestoreViewport()
    {
        Dispatcher.VerifyAccess();
        if (_attachment is null || !HasLiveAttachment) return false;
        BeginViewportSettlement(park: false);
        if (!_liveResize) _settleWatch.Start();
        return SynchronizeLayout();
    }

    private void BeginViewportSettlement(bool park)
    {
        if (_attachment is not { } a || !HasLiveAttachment || !ShouldShow(a)) return;
        _settling = true;
        _settleHidden = false;
        _settleFailureQueued = false;
        _settleStarted = Environment.TickCount64;
        _settleVersion++;
        _forceRepair = true;
        a.Lease?.SetInteractiveMove(true);
        UpdateCaptureMirror(a, true, force: true);
        SynchronizeZOrder(a);
        if (!park || a.Parked) return;
        if (!GetWindowRect(a.Hwnd, out var bounds)) return;
        // A demoted source would still protrude when the manager moves away or
        // shrinks. Keep it visible for DWM, but wholly outside the virtual desktop.
        GetWindowRect(GetAncestor(_container, GaRoot), out var rootBounds);
        int width = bounds.Right - bounds.Left, height = bounds.Bottom - bounds.Top;
        var desktop = new NativeWindowInterop.Rect
        {
            Left = GetSystemMetrics(76 /* SM_XVIRTUALSCREEN */), Top = GetSystemMetrics(77 /* SM_YVIRTUALSCREEN */)
        };
        desktop.Right = desktop.Left + GetSystemMetrics(78 /* SM_CXVIRTUALSCREEN */);
        desktop.Bottom = desktop.Top + GetSystemMetrics(79 /* SM_CYVIRTUALSCREEN */);
        bool Outside(int x, int y, NativeWindowInterop.Rect rect) =>
            x + width <= rect.Left || x >= rect.Right || y + height <= rect.Top || y >= rect.Bottom;
        foreach (var candidate in new[]
        {
            (desktop.Left - width - 128, desktop.Top - height - 128),
            (desktop.Right + 128, desktop.Top - height - 128),
            (desktop.Left - width - 128, desktop.Bottom + 128),
            (desktop.Right + 128, desktop.Bottom + 128)
        })
        {
            // USER32 clamps extreme coordinates; an off-screen test manager can
            // already be at that limit. Base parking on the desktop, not its root.
            int x = Math.Clamp(candidate.Item1, -32768, Math.Max(-32768, 32767 - width));
            int y = Math.Clamp(candidate.Item2, -32768, Math.Max(-32768, 32767 - height));
            if (!Outside(x, y, desktop) || !Outside(x, y, rootBounds)) continue;
            if (SetWindowPos(a.Hwnd, 0, x, y, 0, 0, SwpNoActivate | SwpNoZOrder | SwpNoSize | SwpAsyncWindowPos))
                a.Parked = true;
            break;
        }
    }

    private bool ShouldShow(Attachment a)
    {
        var root = GetAncestor(_container, GaRoot);
        // ShowDialog disables its owner, not its presentation. Keep both the
        // source compositor and DWM mirror live while settings receive input.
        if (!IsVisible || !IsWindowVisible(_container) || !IsWindowVisible(root) || IsIconic(root)) return false;
        return true;
    }

    private void SynchronizeZOrder(Attachment a)
    {
        var root = GetAncestor(_container, GaRoot);
        const uint flags = SwpNoActivate | SwpNoMove | SwpNoSize | SwpAsyncWindowPos;
        // TOPMOST is global, not relative to our manager. Never use it for a
        // viewport: it floats over browsers and capture overlays independently.
        if ((ReadStyle(a.Hwnd, GwlExStyle).ToInt64() & 8) != 0)
        {
            SetWindowPos(a.Hwnd, -2, 0, 0, 0, 0, flags);
            QueueLayout();
            return;
        }
        if (_settling || !IsEnabled || !IsWindowEnabled(root))
        {
            // The disabled manager covers the independent input window. Its
            // live DWM mirror supplies the backdrop, so clicks cannot reach the
            // editor or raise it over a settings dialog (including nested ones).
            // Never disable another process synchronously or join input queues.
            if (GetWindow(root, 2 /* GW_HWNDNEXT */) != a.Hwnd &&
                !SetWindowPos(a.Hwnd, root, 0, 0, 0, 0, flags))
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot place viewport behind modal backdrop.");
            return;
        }
        var foreground = ForegroundWindow();
        GetWindowThreadProcessId(foreground, out var pid);
        if (foreground == a.Hwnd || (foreground != 0 && pid == (uint)a.Pid))
        {
            // Clicking the native editor activates its own input queue. Bring
            // the manager directly underneath it WITHOUT changing keyboard focus.
            if (GetWindow(a.Hwnd, 2 /* GW_HWNDNEXT */) != root)
                SetWindowPos(root, a.Hwnd, 0, 0, 0, 0, flags);
            return;
        }
        var previous = GetWindow(root, 3 /* GW_HWNDPREV */);
        if (previous == a.Hwnd) return;
        // HWND_TOP is in the normal band. Inserting after a TOPMOST window can
        // promote a normal window, so never use a topmost neighbor as an anchor.
        if (previous != 0 && (ReadStyle(previous, GwlExStyle).ToInt64() & 8) != 0) previous = 0;
        if (!SetWindowPos(a.Hwnd, previous, 0, 0, 0, 0, flags))
            throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot align viewport window order.");
    }

    private void SetVisible(Attachment a, bool visible)
    {
        using var timing = Responsiveness?.Stage($"native.visibility.{a.Pid}.{visible}");
        a.Lease?.SetVisible(visible);
        UpdateCaptureMirror(a, visible);
        bool topmost = (ReadStyle(a.Hwnd, GwlExStyle).ToInt64() & 8) != 0;
        if (IsWindowVisible(a.Hwnd) == visible && !topmost) return;
        if (!SetWindowPos(a.Hwnd, -2, 0, 0, 0, 0, SwpNoActivate | SwpNoMove | SwpNoSize |
                SwpAsyncWindowPos | (topmost ? 0 : SwpNoZOrder) | (visible ? SwpShowWindow : SwpHideWindow)))
            throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot change viewport visibility.");
    }

    private void UpdateCaptureMirror(Attachment a, bool visible, bool force = false)
    {
        int captureStatus = a.CaptureMirror.Update(GetAncestor(_container, GaRoot), a.Hwnd, _container, visible, force);
        if (captureStatus != a.CaptureStatus)
        {
            a.CaptureStatus = captureStatus;
            Diagnostic?.Invoke(captureStatus >= 0 ? "캡처 화면 연결 완료 · 관리자창에 Codex 화면 합성" :
                $"캡처 화면 연결 실패 · DWM 0x{captureStatus:X8}");
        }
    }

    public void Detach() => DetachWindow(true);
    public void DetachForClose() => DetachWindow(false);
    private void DetachWindow(bool restoreVisibility)
    {
        Dispatcher.VerifyAccess();
        if (IsTransitioning) { LastError = "Wait for the current native window operation before detaching."; return; }
        _changingWindow = true;
        try { DetachCore(restoreVisibility); }
        finally { _changingWindow = false; }
    }

    private void DetachCore(bool restoreVisibility = true, bool restoreMaximized = true, bool forceVisible = false)
    {
        _watch.Stop();
        _settleWatch.Stop();
        if (_attachment is not { } a) return;
        LastError = "";
        if (!IsAlive(a)) { ForgetAttachment(""); return; }
        try
        {
            bool visible = restoreVisibility && (forceVisible || (a.Style.ToInt64() & WsVisible) != 0);
            a.Lease?.Release(visible);
            a.Lease?.Dispose(); a.Lease = null;
            if (SetWindowRgn(a.Hwnd, a.Region, true) != 0) a.Region = 0;
            WriteStyle(a.Hwnd, GwlStyle, (nint)(a.Style.ToInt64() & ~WsVisible));
            WriteStyle(a.Hwnd, GwlExStyle, a.ExStyle);
            if (!SetWindowPos(a.Hwnd, (a.ExStyle.ToInt64() & 8) != 0 ? -1 : -2,
                    a.Bounds.Left, a.Bounds.Top, a.Bounds.Right - a.Bounds.Left, a.Bounds.Bottom - a.Bounds.Top,
                    SwpNoActivate | SwpFrameChanged | SwpAsyncWindowPos))
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot restore window bounds.");
            // No parenting restoration: it never changed. Preserve the native
            // maximized placement only on explicit detach, never during close.
            var placement = a.Placement;
            placement.ShowCmd = visible ?
                (restoreMaximized && placement.ShowCmd == 3 ? 3u : 4u) : 0;
            if (!SetWindowPlacement(a.Hwnd, in placement))
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot restore window placement.");
            SetWindowPos(a.Hwnd, 0, 0, 0, 0, 0, SwpNoZOrder | SwpNoActivate | SwpNoMove | SwpNoSize | SwpAsyncWindowPos |
                (visible ? SwpShowWindow : SwpHideWindow));
        }
        catch (Exception ex) when (ex is Win32Exception or IOException or UnauthorizedAccessException)
        {
            LastError = "창 표시 상태 복원 실패 · " + ex.Message;
        }
        finally { ForgetAttachment(LastError); }
    }

    protected override HandleRef BuildWindowCore(HandleRef parent)
    {
        _container = CreateWindowExW(0, "STATIC", "", (uint)(WsChild | WsVisible | WsClipChildren | WsClipSiblings | 4),
            0, 0, 1, 1, parent.Handle, 0, 0, 0);
        if (_container == 0) throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot create native viewport.");
        return new HandleRef(this, _container);
    }
    protected override void DestroyWindowCore(HandleRef hwnd)
    {
        DetachForClose();
        _rootSource?.RemoveHook(_rootMessages); _rootSource = null;
        if (_foregroundHook != 0) UnhookWinEvent(_foregroundHook);
        _foregroundHook = 0;
        if (IsWindow(hwnd.Handle)) DestroyWindow(hwnd.Handle);
        _container = 0; _watch.Stop();
    }
    protected override void OnWindowPositionChanged(System.Windows.Rect bounds)
    {
        base.OnWindowPositionChanged(bounds);
        SynchronizeLayout();
    }
    protected override nint WndProc(nint hwnd, int message, nint wParam, nint lParam, ref bool handled)
    {
        // WPF toggles the placeholder HWND after IsVisibleChanged. Observe that
        // completed native change rather than waiting for the one-second watch.
        if (message is 0x18 or 0x47 or 10) QueueLayout();
        return base.WndProc(hwnd, message, wParam, lParam, ref handled);
    }
    protected override bool TabIntoCore(TraversalRequest request)
    {
        // Explicit keyboard traversal can activate the native window. Never call
        // SetFocus on another thread or attach its queue to the manager.
        return _attachment is { } a && HasLiveAttachment && ShouldShow(a) && SetForegroundWindow(a.Hwnd);
    }

    private Attachment ValidateAndCapture(nint hwnd, int pid, string expectedExecutable, bool allowHidden)
    {
        if (hwnd == 0 || pid <= 0 || pid == Environment.ProcessId || !IsWindow(hwnd))
            throw new ArgumentException("Select a valid window belonging to a separate process.");
        if (!Path.IsPathFullyQualified(expectedExecutable))
            throw new ArgumentException("The expected executable must be an absolute path.");
        GetWindowThreadProcessId(hwnd, out uint actualPid);
        if (actualPid != (uint)pid)
            throw new InvalidOperationException("The window does not belong to the expected process.");
        if (WindowStateDirectory is not null)
        {
            var leasePath = Path.Combine(WindowStateDirectory, $"{pid}.json");
            if (File.Exists(leasePath))
            {
                using var lease = System.Text.Json.JsonDocument.Parse(File.ReadAllText(leasePath));
                var state = lease.RootElement;
                if (state.TryGetProperty("mode", out var mode) && mode.GetString() == "viewport" &&
                    state.GetProperty("hwnd").GetString() == hwnd.ToString() &&
                    state.TryGetProperty("recovery", out var recovery) && recovery.ValueKind == System.Text.Json.JsonValueKind.Object &&
                    GetPropW(hwnd, recovery.GetProperty("Marker").GetString()!) == 1)
                {
                    if (NativeViewportRecovery.Run(leasePath) != 0)
                        throw new InvalidOperationException("이 Codex 창은 이미 다른 관리창에 연결되어 있습니다. 기존 관리창에서 분리한 뒤 선택하세요.");
                }
            }
        }
        if ((!allowHidden && !IsWindowVisible(hwnd)) || GetAncestor(hwnd, GaRoot) != hwnd || GetWindow(hwnd, GwOwner) != 0 ||
            (ReadStyle(hwnd, GwlStyle).ToInt64() & WsChild) != 0)
            throw new InvalidOperationException("Only a visible, unowned primary window can be attached.");
        if (!IsWindowEnabled(hwnd))
            throw new InvalidOperationException("Codex의 다른 대화상자가 입력을 막고 있습니다. 원래 창에서 열린 대화상자를 닫고 다시 연결하세요.");
        SafeProcessHandle process = OpenProcess(ProcessQueryLimitedInformation, false, actualPid);
        if (process.IsInvalid)
        {
            process.Dispose();
            throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot verify process identity. Check that both programs run at the same privilege level.");
        }
        try
        {
            var executable = new StringBuilder(32768);
            uint length = (uint)executable.Capacity;
            if (!QueryFullProcessImageNameW(process, 0, executable, ref length))
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot verify the executable path.");
            if (!SamePath(executable.ToString(), expectedExecutable))
                throw new InvalidOperationException("The process executable does not exactly match the expected path.");
            nint hostContext = GetWindowDpiAwarenessContext(_container);
            nint childContext = GetWindowDpiAwarenessContext(hwnd);
            DpiSummary = $"Host {GetDpiForWindow(_container)} DPI / child {GetDpiForWindow(hwnd)} DPI; awareness " +
                $"{GetAwarenessFromDpiAwarenessContext(hostContext)}/{GetAwarenessFromDpiAwarenessContext(childContext)}";
            if (hostContext == 0 || childContext == 0 || !AreDpiAwarenessContextsEqual(hostContext, childContext))
                throw new InvalidOperationException("DPI awareness differs. Open this instance as a separate window until matching DPI settings are configured. " + DpiSummary);
            var placement = new WindowPlacement { Length = (uint)Marshal.SizeOf<WindowPlacement>() };
            if (!GetWindowPlacement(hwnd, ref placement) || !GetWindowRect(hwnd, out NativeWindowInterop.Rect rect))
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot save original window placement.");
            var attachment = new Attachment(hwnd, pid, executable.ToString(), process, GetParent(hwnd),
                ReadStyle(hwnd, GwlStyle), ReadStyle(hwnd, GwlExStyle), rect, placement);
            if (!IsAlive(attachment)) throw new InvalidOperationException("The process closed during verification.");
            return attachment;
        }
        catch
        {
            process.Dispose();
            throw;
        }
    }

    private static bool SamePath(string first, string second)
    {
        try
        {
            return Path.IsPathFullyQualified(first) && Path.IsPathFullyQualified(second) &&
                string.Equals(Path.GetFullPath(first), Path.GetFullPath(second), StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or IOException)
        {
            return false;
        }
    }

    private static bool IsAlive(Attachment attached)
    {
        if (!IsWindow(attached.Hwnd) || attached.Process.IsClosed || attached.Process.IsInvalid ||
            !GetExitCodeProcess(attached.Process, out uint code) || code != StillActive) return false;
        // A destroyed HWND can be reused even while the same process is alive. Window
        // properties disappear on destruction, so the marker distinguishes that lifetime.
        if (attached.Marked && GetPropW(attached.Hwnd, attached.MarkerName) != attached.MarkerValue) return false;
        GetWindowThreadProcessId(attached.Hwnd, out uint pid);
        return pid == (uint)attached.Pid;
    }

    private void CheckAttachedWindow()
    {
        if (IsTransitioning) return;
        if (_attachment is not { } a) return;
        if (!IsAlive(a) || !IsIndependent(a))
        {
            bool changed = IsAlive(a);
            ForgetAttachment(changed ? "The native window changed its parent or owner." : "The external window closed.");
            AttachmentLost?.Invoke(a.Hwnd, changed);
            return;
        }
        if (_settling) a.Lease?.SetInteractiveMove(true);
        SynchronizeLayout();
        if (IsVisible && !_liveResize && !_settling)
        {
            var state = InputDiagnostic();
            if (_lastInputState != state) { _lastInputState = state; Diagnostic?.Invoke(state); }
        }
        var presentation = a.Lease?.ReadPresentationStatus();
        if (presentation is not null && presentation != _lastPresentationState)
        { _lastPresentationState = presentation; Diagnostic?.Invoke(presentation); }
    }

    public bool SynchronizeLayout()
    {
        using var timing = Responsiveness?.Stage($"native.layout.{_attachment?.Pid}");
        Dispatcher.VerifyAccess();
        if (IsTransitioning) { QueueLayout(); return false; }
        _resizing = true;
        try
        {
            var result = ResizeChildCore();
            if (_attachment?.Lease?.PublicationError is { } pending)
            {
                if (_lastLayoutFailure != pending)
                    Diagnostic?.Invoke($"창 표시 상태 전달 지연 · 자동 재시도 · PID {_attachment.Pid} · {pending}");
                _lastLayoutFailure = pending;
                return false;
            }
            if (result && _lastLayoutFailure is not null)
            {
                _lastLayoutFailure = null;
                LastError = "";
                Diagnostic?.Invoke($"창 표시 상태 재전달 완료 · PID {_attachment?.Pid}");
            }
            return result;
        }
        catch (Exception ex) when (ex is Win32Exception or IOException or UnauthorizedAccessException)
        {
            LastError = ex.Message;
            if (_lastLayoutFailure != ex.Message)
            {
                _lastLayoutFailure = ex.Message;
                Diagnostic?.Invoke($"창 표시 상태 전달 지연 · 자동 재시도 · PID {_attachment?.Pid} · {ex.Message}");
            }
            return false;
        }
        finally { _resizing = false; }
    }

    private bool ResizeChildCore(bool frameChanged = false)
    {
        if (_attachment is not { } a || !HasLiveAttachment) return false;
        bool show = ShouldShow(a);
        if (!show) { SetVisible(a, false); a.Shown = false; _settleHidden = true; _settleWatch.Stop(); return true; }
        if (_liveResize)
        {
            a.NeedsFrameChange |= frameChanged;
            if (!_settling)
            {
                // A profile can finish attaching or become selected mid-drag.
                BeginViewportSettlement(park: true);
                SetVisible(a, true);
            }
            // The manager owns this DWM surface, so it follows native movement
            // immediately. No cross-process geometry, region or lease write is
            // needed for each drag event; the source keeps its original frame.
            UpdateCaptureMirror(a, true);
            SynchronizeZOrder(a);
            return true;
        }
        if (_settling && _settleHidden)
        {
            // Time spent minimized or on another profile is not a failed
            // placement attempt. Resume with a fresh bounded verification.
            _settleStarted = Environment.TickCount64;
            _forceRepair = true;
            _settleWatch.Start();
        }
        _settleHidden = false;
        if (_settling && Environment.TickCount64 - _settleStarted >= 6000)
        {
            FailViewportSettlement(a);
            return false;
        }
        bool repaired = false;
        if (show && !RepairStyle(a, out repaired)) return false;
        frameChanged |= repaired || a.NeedsFrameChange;
        if (!GetClientRect(_container, out var size) || !GetWindowRect(_container, out var target)) return false;
        int width = size.Right, height = size.Bottom;
        if (width <= 1 || height <= 1) return true;
        var origin = new NativeWindowInterop.Point();
        if (!GetWindowRect(a.Hwnd, out var bounds) || !GetClientRect(a.Hwnd, out var client) || !ClientToScreen(a.Hwnd, ref origin)) return false;
        int insetX = origin.X - bounds.Left, insetY = origin.Y - bounds.Top;
        int outerWidth = width + (bounds.Right - bounds.Left - client.Right);
        int outerHeight = height + (bounds.Bottom - bounds.Top - client.Bottom);
        bool moved = a.Left != target.Left || a.Top != target.Top;
        bool matches = origin.X == target.Left && origin.Y == target.Top && client.Right == width && client.Bottom == height;
        bool clipChanged = a.ClipX != insetX || a.ClipY != insetY || a.Width != width || a.Height != height;
        bool dimensionsChanged = a.Width != width || a.Height != height;
        // WPF splitters, expanders and heading reflow change the viewport without
        // ENTER/EXITSIZEMOVE. An asynchronous Win32 request is not an applied
        // resize: keep the source behind the manager until bounds AND region
        // have been observed at the new allocation, just as after a window drag.
        // In particular, the old wide source must not cover the notes panel.
        if (a.Shown && a.Width > 0 && a.Height > 0 && (moved || dimensionsChanged))
        {
            BeginViewportSettlement(park: false);
            _settleWatch.Start();
        }
        a.Lease?.SetBounds(target.Left - insetX, target.Top - insetY, outerWidth, outerHeight, GetDpiForWindow(_container));
        bool forceRepair = _forceRepair;
        bool resize = a.ResizePolicy.Request(width, height, frameChanged || moved || clipChanged || forceRepair,
            matches, ResizeClock(), out var report);
        if (report)
        {
            Diagnostic?.Invoke("Codex가 요청 크기를 유지하지 않아 반복 조절을 멈췄습니다.");
            if (_settling) { FailViewportSettlement(a); return false; }
        }
        if (resize)
        {
            uint flags = SwpNoActivate | SwpNoZOrder | SwpAsyncWindowPos | (frameChanged ? SwpFrameChanged : 0);
            if (!SetWindowPos(a.Hwnd, 0, target.Left - insetX, target.Top - insetY, outerWidth, outerHeight, flags))
                throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot position native viewport.");
            a.Parked = false;
            a.NeedsFrameChange = false;
            _forceRepair = false;
        }
        // Cache the desired geometry even if a native region operation fails.
        // Re-observation checks reality, while retries keep their bounded budget.
        a.Left = target.Left; a.Top = target.Top;
        a.Width = width; a.Height = height;
        a.ClipX = insetX; a.ClipY = insetY;
        // Chromium may replace its window region independently of its bounds.
        // Never infer actual clipping from our last requested dimensions alone.
        bool regionMatches = HasViewportRegion(a, insetX, insetY, width, height);
        bool repairRegion = a.RegionPolicy.Request(width, height, clipChanged || frameChanged || forceRepair,
            regionMatches, ResizeClock(), out var reportRegion);
        if (reportRegion)
        {
            Diagnostic?.Invoke("Codex 창 테두리 복구를 반복해서 적용할 수 없어 조절을 멈췄습니다.");
            if (_settling) { FailViewportSettlement(a); return false; }
        }
        if (repairRegion && !regionMatches)
        {
            using var timing = Responsiveness?.Stage($"native.SetWindowRgn.{a.Pid}");
            bool wholeWindow = insetX == 0 && insetY == 0 &&
                bounds.Right - bounds.Left == width && bounds.Bottom - bounds.Top == height;
            nint region = wholeWindow ? 0 : CreateRectRgn(insetX, insetY, insetX + width, insetY + height);
            if ((!wholeWindow && region == 0) || SetWindowRgn(a.Hwnd, region, true) == 0)
            { if (region != 0) DeleteObject(region); throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot clip native viewport."); }
            // SetWindowRgn owns the region after success.
        }
        if (dimensionsChanged) Diagnostic?.Invoke($"표시 영역 크기 요청 · {width} × {height} px");
        SetVisible(a, show);
        // A posted placement is not an acknowledgement. Keep the live mirror
        // above the source until a later observation verifies geometry AND clip.
        if (_settling && !resize && matches && HasViewportRegion(a, insetX, insetY, width, height))
        {
            a.Lease?.SetInteractiveMove(false);
            if (a.Lease?.PublicationError is null)
            {
                _settling = false;
                a.Parked = false;
                _settleWatch.Stop();
                ViewportRecoveryCompleted?.Invoke(this, EventArgs.Empty);
            }
        }
        if (show) SynchronizeZOrder(a);

        a.Shown = show;
        LastError = "";
        return true;
    }

    private static bool HasViewportRegion(Attachment a, int x, int y, int width, int height)
    {
        if (a.RegionProbe == 0) a.RegionProbe = CreateRectRgn(0, 0, 0, 0);
        if (a.RegionProbe == 0) return false;
        int kind = GetWindowRgn(a.Hwnd, a.RegionProbe);
        // A frameless Chromium window commonly removes its explicit region on
        // placement. That is already an exact clip when the entire HWND is the
        // requested client viewport; forcing a region back would fight Chromium.
        if (kind == 0) return x == 0 && y == 0 && GetWindowRect(a.Hwnd, out var bounds) &&
            bounds.Right - bounds.Left == width && bounds.Bottom - bounds.Top == height;
        return kind == 2 /* SIMPLEREGION */ &&
            GetRgnBox(a.RegionProbe, out var actual) == 2 && actual.Left == x && actual.Top == y &&
            actual.Right == x + width && actual.Bottom == y + height;
    }

    private void FailViewportSettlement(Attachment a)
    {
        if (_settleFailureQueued) return;
        _settleFailureQueued = true;
        int version = _settleVersion;
        _settleWatch.Stop();
        // Leave the resize guard before lifecycle callbacks. Only this verified
        // independent lifetime may be restored; a changed parent/owner is never
        // reclaimed by geometry or style writes.
        Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() =>
        {
            if (_attachment != a || !_settling || version != _settleVersion || !IsAlive(a) || !IsIndependent(a)) return;
            _changingWindow = true;
            try { DetachCore(restoreVisibility: true, restoreMaximized: false, forceVisible: true); }
            finally { _changingWindow = false; }
            LastError = LastError.Length == 0 ?
                "Codex 표시 영역을 복구하지 못해 원래 창으로 돌려보냈습니다. ‘관리창 안에 표시’로 다시 연결할 수 있습니다." :
                "Codex 표시 영역 복구와 원래 창 복원이 지연되었습니다. ‘관리창 안에 표시’로 다시 시도해 주세요. " + LastError;
            Diagnostic?.Invoke(LastError);
            AttachmentLost?.Invoke(a.Hwnd, false);
            ViewportRecoveryFailed?.Invoke(LastError);
        }));
    }

    public string InputDiagnostic()
    {
        if (_attachment is not { } a || !IsAlive(a)) return "연결된 Codex 창이 없습니다.";
        var gui = new GuiThreadInfo { Size = (uint)Marshal.SizeOf<GuiThreadInfo>() };
        GetGUIThreadInfo(GetWindowThreadProcessId(a.Hwnd, out _), ref gui);
        bool nativeFocus = gui.Focus == a.Hwnd || IsChild(a.Hwnd, gui.Focus);
        return $"입력 상태 · 독립 입력 큐 · Codex {(IsWindowEnabled(a.Hwnd) ? "허용" : "차단")}" +
            $" · Windows 응답 {(IsHungAppWindow(a.Hwnd) ? "없음" : "있음")}" +
            $" · 키보드 대상 {(nativeFocus ? "Codex" : "다른 창")}";
    }

    private bool Fail(string message, out string error) { error = LastError = message; return false; }
    private void ForgetAttachment(string error)
    {
        _watch.Stop();
        _settleWatch.Stop();
        _settling = _forceRepair = _settleHidden = _settleFailureQueued = false;
        if (_presentationHook != 0) UnhookWinEvent(_presentationHook);
        _presentationHook = 0;
        if (_attachment is { Marked: true } a && IsWindow(a.Hwnd) && GetPropW(a.Hwnd, a.MarkerName) == a.MarkerValue)
            RemovePropW(a.Hwnd, a.MarkerName);
        _attachment?.Dispose(); _attachment = null; _lastInputState = null;
        LastError = error; AttachmentChanged?.Invoke(this, EventArgs.Empty);
    }

    private sealed record Attachment(nint Hwnd, int Pid, string Executable, SafeProcessHandle Process,
        nint Parent, nint Style, nint ExStyle, NativeWindowInterop.Rect Bounds, WindowPlacement Placement) : IDisposable
    {
        public string MarkerName { get; } = "Codex.ControlCenter.NativeHost." + Guid.NewGuid().ToString("N");
        public nint MarkerValue { get; } = 1;
        public bool Marked { get; set; }
        public NativeResizePolicy ResizePolicy { get; } = new();
        public NativeResizePolicy RegionPolicy { get; } = new();
        public int Width, Height, Left, Top, ClipX = -1, ClipY = -1;
        public nint Region, RegionProbe;
        public bool Shown, Parked, NeedsFrameChange;
        public NativeWindowLease? Lease;
        public NativeWindowHostCaptureMirror CaptureMirror { get; } = new();
        public int? CaptureStatus;
        public void Dispose()
        {
            CaptureMirror.Dispose();
            try { Lease?.Dispose(); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Text.Json.JsonException) { }
            finally { if (Region != 0) DeleteObject(Region); if (RegionProbe != 0) DeleteObject(RegionProbe); Process.Dispose(); }
        }
    }
}
