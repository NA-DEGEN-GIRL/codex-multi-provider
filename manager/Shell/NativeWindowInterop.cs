using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Codex.ControlCenter.Shell;

internal static class NativeWindowInterop
{
    internal const int GwlStyle = -16, GwlExStyle = -20;
    internal const long WsChild = 0x40000000, WsVisible = 0x10000000, WsPopup = 0x80000000L;
    internal const long WsClipChildren = 0x02000000, WsClipSiblings = 0x04000000;
    internal const long WsCaption = 0x00C00000, WsThickFrame = 0x00040000;
    internal const long WsMinimizeBox = 0x00020000, WsMaximizeBox = 0x00010000, WsSysMenu = 0x00080000;
    internal const long WsMinimize = 0x20000000, WsMaximize = 0x01000000;
    internal const long WsExAppWindow = 0x00040000, WsExWindowEdge = 0x00000100;
    internal const long WsExClientEdge = 0x00000200, WsExStaticEdge = 0x00020000;
    internal const long WsExToolWindow = 0x00000080;
    internal const uint SwpNoZOrder = 0x0004, SwpNoActivate = 0x0010, SwpFrameChanged = 0x0020;
    internal const uint SwpNoMove = 0x0002, SwpNoSize = 0x0001;
    internal const uint SwpAsyncWindowPos = 0x4000;
    internal const uint SwpShowWindow = 0x0040, SwpHideWindow = 0x0080;
    internal const int SwRestore = 9, WmSetFocus = 0x0007, WmMouseActivate = 0x0021;
    internal const uint WmClose = 0x0010, GaParent = 1, GaRoot = 2, GwOwner = 4, StillActive = 259;
    internal const uint ProcessQueryLimitedInformation = 0x1000;

    [StructLayout(LayoutKind.Sequential)]
    internal struct Rect { internal int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    internal struct Point { internal int X, Y; }
    [StructLayout(LayoutKind.Sequential)]
    internal struct GuiThreadInfo
    {
        internal uint Size, Flags;
        internal nint Active, Focus, Capture, Menu, MoveSize, Caret;
        internal Rect CaretRect;
    }
    [DllImport("user32.dll")]
    internal static extern bool GetGUIThreadInfo(uint thread, ref GuiThreadInfo info);
    internal delegate void WinEventCallback(nint hook, uint eventId, nint hwnd, int objectId, int childId, uint thread, uint time);
    [DllImport("user32.dll")]
    internal static extern nint SetWinEventHook(uint first, uint last, nint module, WinEventCallback callback, uint process, uint thread, uint flags);
    [DllImport("user32.dll")]
    internal static extern bool UnhookWinEvent(nint hook);
    [StructLayout(LayoutKind.Sequential)]
    internal struct WindowPlacement
    {
        internal uint Length, Flags, ShowCmd;
        internal Point MinPosition, MaxPosition;
        internal Rect NormalPosition;
    }
    [StructLayout(LayoutKind.Sequential)]
    internal struct Message
    {
        internal nint Hwnd;
        internal uint Id;
        internal nuint WParam;
        internal nint LParam;
        internal uint Time;
        internal Point Pt;
        internal uint Private;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern nint CreateWindowExW(uint extendedStyle, string className, string windowName,
        uint style, int x, int y, int width, int height, nint parent, nint menu, nint instance, nint param);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool DestroyWindow(nint hwnd);
    [DllImport("user32.dll", SetLastError = true)]
    internal static extern nint SetParent(nint child, nint parent);
    [DllImport("user32.dll")]
    internal static extern nint GetParent(nint hwnd);
    [DllImport("user32.dll")]
    internal static extern nint GetDesktopWindow();
    [DllImport("user32.dll")]
    internal static extern nint GetAncestor(nint hwnd, uint flags);
    [DllImport("user32.dll")]
    internal static extern nint GetWindow(nint hwnd, uint command);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsWindowEnabled(nint hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsHungAppWindow(nint hwnd);
    [DllImport("user32.dll")]
    internal static extern nint GetLastActivePopup(nint hwnd);
    [DllImport("user32.dll")]
    internal static extern nint GetFocus();
    [DllImport("user32.dll")]
    internal static extern nint GetForegroundWindow();
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool SetForegroundWindow(nint hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsChild(nint parent, nint child);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsIconic(nint hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsZoomed(nint hwnd);
    [DllImport("user32.dll", SetLastError = true)]
    internal static extern uint GetWindowThreadProcessId(nint hwnd, out uint processId);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW", SetLastError = true)]
    private static extern nint GetWindowLongPtr64(nint hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW", SetLastError = true)]
    private static extern int GetWindowLong32(nint hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)]
    private static extern nint SetWindowLongPtr64(nint hwnd, int index, nint value);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongW", SetLastError = true)]
    private static extern int SetWindowLong32(nint hwnd, int index, int value);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetClientRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")]
    internal static extern bool ClientToScreen(nint hwnd, ref Point point);
    [DllImport("gdi32.dll")]
    internal static extern nint CreateRectRgn(int left, int top, int right, int bottom);
    [DllImport("gdi32.dll")]
    internal static extern bool DeleteObject(nint handle);
    [DllImport("user32.dll")]
    internal static extern int SetWindowRgn(nint hwnd, nint region, bool redraw);
    [DllImport("user32.dll")]
    internal static extern int GetWindowRgn(nint hwnd, nint region);
    [DllImport("gdi32.dll")]
    internal static extern int GetRgnBox(nint region, out Rect bounds);
    [DllImport("user32.dll")]
    internal static extern int GetSystemMetrics(int index);
    [DllImport("gdi32.dll")]
    internal static extern uint GetRegionData(nint region, uint size, byte[]? data);
    [DllImport("gdi32.dll")]
    internal static extern nint ExtCreateRegion(nint transform, uint size, byte[] data);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetWindowPlacement(nint hwnd, ref WindowPlacement placement);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool SetWindowPlacement(nint hwnd, in WindowPlacement placement);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool SetWindowPos(nint hwnd, nint after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool ShowWindow(nint hwnd, int command);
    [DllImport("user32.dll")]
    internal static extern nint SetFocus(nint hwnd);
    [DllImport("user32.dll")]
    internal static extern nint GetWindowDpiAwarenessContext(nint hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool AreDpiAwarenessContextsEqual(nint first, nint second);
    [DllImport("user32.dll")]
    internal static extern int GetAwarenessFromDpiAwarenessContext(nint context);
    [DllImport("user32.dll")]
    internal static extern uint GetDpiForWindow(nint hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool SetPropW(nint hwnd, string name, nint value);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    internal static extern nint GetPropW(nint hwnd, string name);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    internal static extern nint RemovePropW(nint hwnd, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern SafeProcessHandle OpenProcess(uint access, bool inheritHandle, uint processId);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool QueryFullProcessImageNameW(SafeProcessHandle process, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetExitCodeProcess(SafeProcessHandle process, out uint exitCode);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool PostMessageW(nint hwnd, uint message, nuint wParam, nint lParam);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool PeekMessageW(out Message message, nint hwnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool TranslateMessage(in Message message);
    [DllImport("user32.dll")]
    internal static extern nint DispatchMessageW(in Message message);
    internal delegate bool EnumWindowsCallback(nint hwnd, nint param);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool EnumWindows(EnumWindowsCallback callback, nint param);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool EnumChildWindows(nint parent, EnumWindowsCallback callback, nint param);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    internal static extern int GetClassNameW(nint hwnd, StringBuilder name, int length);

    internal static nint ReadStyle(nint hwnd, int index)
    {
        Marshal.SetLastPInvokeError(0);
        nint value = IntPtr.Size == 8 ? GetWindowLongPtr64(hwnd, index) : GetWindowLong32(hwnd, index);
        if (value == 0 && Marshal.GetLastPInvokeError() is int error && error != 0)
            throw new Win32Exception(error, "Cannot read window style.");
        return value;
    }

    internal static void WriteStyle(nint hwnd, int index, nint value)
    {
        Marshal.SetLastPInvokeError(0);
        nint previous = IntPtr.Size == 8 ? SetWindowLongPtr64(hwnd, index, value) : SetWindowLong32(hwnd, index, value.ToInt32());
        if (previous == 0 && Marshal.GetLastPInvokeError() is int error && error != 0)
            throw new Win32Exception(error, "Cannot change window style.");
    }

    internal static void Reparent(nint hwnd, nint parent)
    {
        Marshal.SetLastPInvokeError(0);
        nint previous = SetParent(hwnd, parent);
        if (previous == 0 && Marshal.GetLastPInvokeError() is int error && error != 0)
            throw new Win32Exception(error, "Cannot reparent window.");
        // GetParent may return an owner or NULL based on the current window style.
        // GA_PARENT checks the actual structural parent even if Chromium changed style.
        nint actualParent = GetAncestor(hwnd, GaParent);
        // SetParent(NULL) attaches a still-WS_CHILD window to the desktop. Once its
        // original top-level style is restored, GetParent returns NULL again.
        if (actualParent != parent && !(parent == 0 && actualParent == GetDesktopWindow()))
            throw new InvalidOperationException($"Window parent did not match after SetParent. HWND={hwnd}; requested={parent}; actual={actualParent}; owner={GetWindow(hwnd, GwOwner)}; style=0x{ReadStyle(hwnd, GwlStyle).ToInt64():X}; targetValid={IsWindow(parent)}.");
    }
}
