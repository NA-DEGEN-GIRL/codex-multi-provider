using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Windows.Interop;

namespace Codex.ControlCenter.Shell;

// Owl consumes WM_MOUSEACTIVATE instead of forwarding it to HwndHost. Receive
// button-down notifications independently; never intercept or replay input.
// One registration per manager window, shared by all profile tabs.
internal sealed class EmbeddedMouseActivation : IDisposable
{
    private readonly HwndSource source;
    private readonly Func<bool> enabled;
    private readonly Action<nint> clicked;
    private readonly byte[] packet = new byte[64];
    private bool disposed;

    internal EmbeddedMouseActivation(HwndSource source, Func<bool> enabled, Action<nint> clicked)
    {
        this.source = source;
        this.enabled = enabled;
        this.clicked = clicked;
        var device = new RawDevice { UsagePage = 1, Usage = 2, Flags = 0x100 /* INPUTSINK */, Target = source.Handle };
        if (!RegisterRawInputDevices([device], 1, (uint)Marshal.SizeOf<RawDevice>()))
            throw new Win32Exception(Marshal.GetLastPInvokeError(), "내부 창 클릭 활성화를 연결하지 못했습니다.");
        source.AddHook(OnMessage);
    }

    private nint OnMessage(nint hwnd, int message, nint wParam, nint lParam, ref bool handled)
    {
        if (message != 0xFF /* WM_INPUT */ || disposed || !enabled()) return 0;
        uint size = (uint)packet.Length;
        uint received = GetRawInputData(lParam, 0x10000003 /* RID_INPUT */, packet, ref size, (uint)(8 + 2 * IntPtr.Size));
        if (received == uint.MaxValue || received > packet.Length ||
            !IsFreshButtonDown(packet.AsSpan(0, (int)received), IntPtr.Size,
                unchecked((uint)Environment.TickCount), unchecked((uint)GetMessageTime()))) return 0;
        if (GetCursorPos(out var point)) clicked(WindowFromPoint(point));
        // Do not consume the message: Windows/WPF still perform normal cleanup.
        // No focus forwarding, synthetic click, retry, timer or delayed activation.
        return 0;
    }

    internal static bool IsFreshButtonDown(ReadOnlySpan<byte> packet, int pointerSize, uint now, uint messageTime)
    {
        int header = 8 + 2 * pointerSize;
        if (pointerSize is not (4 or 8) || packet.Length < header + 24 ||
            unchecked(now - messageTime) > 200 || BitConverter.ToUInt32(packet) != 0) return false;
        // Left/right/middle/X1/X2 DOWN only. Movement, release and wheel never raise a window.
        return (BitConverter.ToUInt16(packet.Slice(header + 4)) & 0x155) != 0;
    }

    public void Dispose()
    {
        if (disposed) return;
        disposed = true;
        source.RemoveHook(OnMessage);
        RegisterRawInputDevices([new RawDevice { UsagePage = 1, Usage = 2, Flags = 1 /* REMOVE */ }],
            1, (uint)Marshal.SizeOf<RawDevice>());
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RawDevice { internal ushort UsagePage, Usage; internal uint Flags; internal nint Target; }
    [StructLayout(LayoutKind.Sequential)]
    private struct Point { internal int X, Y; }
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool RegisterRawInputDevices(RawDevice[] devices, uint count, uint size);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetRawInputData(nint handle, uint command, [Out] byte[] data, ref uint size, uint headerSize);
    [DllImport("user32.dll")]
    private static extern int GetMessageTime();
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")]
    private static extern nint WindowFromPoint(Point point);
}
