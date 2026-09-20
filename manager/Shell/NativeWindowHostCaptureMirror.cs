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
    private PixelSize appliedSourceSize;
    internal int UpdateCount { get; private set; }
    internal Rect? SourceBounds => applied?.Source;
    internal Rect? DestinationBounds => applied?.Destination;
    internal int Update(nint root, nint input, nint viewport, bool visible, bool force = false)
    {
        if (destination != root || source != input)
        {
            Dispose();
            int error = DwmRegisterThumbnail(root, input, out thumbnail);
            if (error < 0) return error;
            destination = root;
            source = input;
        }
        // Hidden profiles need no per-frame source-size queries. Showing them
        // again always refreshes the current crop and destination together.
        if (!visible && !force && applied is { Visible: false }) return 0;
        if (!GetWindowRect(viewport, out var bounds)) return unchecked((int)0x80004005);
        var origin = new Point();
        if (!ClientToScreen(root, ref origin)) return unchecked((int)0x80004005);
        // Destination layout can settle BEFORE the asynchronous source resize.
        // An unchanged destination does not mean the DWM mapping is unchanged.
        // Use an explicit client crop in window pixels and observe source size
        // as well, instead of retaining a transform from an earlier large/small
        // source. SourceClientAreaOnly's implicit crop is not our cache key.
        var clientOrigin = new Point();
        if (!GetWindowRect(input, out var window) || !GetClientRect(input, out var client) ||
            !ClientToScreen(input, ref clientOrigin)) return unchecked((int)0x80004005);
        int query = DwmQueryThumbnailSourceSize(thumbnail, out var sourceSize);
        if (query < 0) return query;
        int insetX = clientOrigin.X - window.Left, insetY = clientOrigin.Y - window.Top;
        var properties = new ThumbnailProperties
        {
            Flags = 1 | 2 | 4 | 8 | 16, // destination, source crop, opacity, visible, client-area-only
            Destination = new Rect
            {
                Left = bounds.Left - origin.X, Top = bounds.Top - origin.Y,
                Right = bounds.Right - origin.X, Bottom = bounds.Bottom - origin.Y
            },
            Source = new Rect { Left = insetX, Top = insetY, Right = insetX + client.Right, Bottom = insetY + client.Bottom },
            Opacity = 255, Visible = visible, SourceClientAreaOnly = false
        };
        if (!force && applied is { } previous && previous.Visible == visible &&
            previous.Destination.Equals(properties.Destination) && previous.Source.Equals(properties.Source) &&
            appliedSourceSize.Equals(sourceSize)) return 0;
        int status = DwmUpdateThumbnailProperties(thumbnail, in properties);
        if (status >= 0) { applied = properties; appliedSourceSize = sourceSize; UpdateCount++; }
        return status;
    }

    public void Dispose()
    {
        if (thumbnail != 0) DwmUnregisterThumbnail(thumbnail);
        thumbnail = destination = source = 0;
        applied = null;
        appliedSourceSize = default;
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
    [StructLayout(LayoutKind.Sequential)]
    private struct PixelSize { internal int Width, Height; }
    [DllImport("dwmapi.dll")] private static extern int DwmRegisterThumbnail(nint destination, nint source, out nint thumbnail);
    [DllImport("dwmapi.dll")] private static extern int DwmUpdateThumbnailProperties(nint thumbnail, in ThumbnailProperties properties);
    [DllImport("dwmapi.dll")] private static extern int DwmUnregisterThumbnail(nint thumbnail);
    [DllImport("dwmapi.dll")] private static extern int DwmQueryThumbnailSourceSize(nint thumbnail, out PixelSize size);
}
