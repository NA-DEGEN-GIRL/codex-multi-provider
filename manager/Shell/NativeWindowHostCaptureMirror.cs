using System.Runtime.InteropServices;
using static Codex.ControlCenter.Shell.NativeWindowInterop;

namespace Codex.ControlCenter.Shell;

/// <summary>
/// Composes the independent input window into the manager's own DWM surface.
/// Without this live mirror a window-only capture sees only the gray placeholder.
/// This never reparents, owns, focuses, or sends input to the source window.
/// No screenshots are written to disk or sent across a network.
/// </summary>
internal sealed class NativeWindowHostCaptureMirror : IDisposable
{
    private nint thumbnail, destination, source;
    private ThumbnailProperties? applied;
    internal int Update(nint root, nint input, nint viewport, bool visible)
    {
        if (destination != root || source != input)
        {
            Dispose();
            int error = DwmRegisterThumbnail(root, input, out thumbnail);
            if (error < 0) return error;
            destination = root;
            source = input;
        }
        if (!GetWindowRect(viewport, out var bounds)) return unchecked((int)0x80004005);
        var origin = new Point();
        if (!ClientToScreen(root, ref origin)) return unchecked((int)0x80004005);
        var properties = new ThumbnailProperties
        {
            Flags = 1 | 4 | 8 | 16, // destination, opacity, visible, client-area-only
            Destination = new Rect
            {
                Left = bounds.Left - origin.X, Top = bounds.Top - origin.Y,
                Right = bounds.Right - origin.X, Bottom = bounds.Bottom - origin.Y
            },
            Opacity = 255, Visible = visible, SourceClientAreaOnly = true
        };
        if (applied is { } previous && previous.Visible == visible && previous.Destination.Equals(properties.Destination)) return 0;
        int status = DwmUpdateThumbnailProperties(thumbnail, in properties);
        if (status >= 0) applied = properties;
        return status;
    }

    public void Dispose()
    {
        if (thumbnail != 0) DwmUnregisterThumbnail(thumbnail);
        thumbnail = destination = source = 0;
        applied = null;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ThumbnailProperties
    {
        internal uint Flags;
        internal Rect Destination, Source;
        internal byte Opacity;
        [MarshalAs(UnmanagedType.Bool)] internal bool Visible;
        [MarshalAs(UnmanagedType.Bool)] internal bool SourceClientAreaOnly;
    }
    [DllImport("dwmapi.dll")] private static extern int DwmRegisterThumbnail(nint destination, nint source, out nint thumbnail);
    [DllImport("dwmapi.dll")] private static extern int DwmUpdateThumbnailProperties(nint thumbnail, in ThumbnailProperties properties);
    [DllImport("dwmapi.dll")] private static extern int DwmUnregisterThumbnail(nint thumbnail);
}
