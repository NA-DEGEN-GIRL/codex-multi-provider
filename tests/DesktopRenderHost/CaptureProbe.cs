using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Media;
using System.Windows.Media.Imaging;

internal static class CaptureProbe
{
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct Header
    {
        public uint Size; public int Width, Height; public ushort Planes, Bits;
        public uint Compression, ImageSize; public int X, Y; public uint Used, Important;
    }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint window, out Rect bounds);
    [DllImport("user32.dll")] private static extern bool PrintWindow(nint window, nint dc, uint flags);
    [DllImport("gdi32.dll")] private static extern nint CreateCompatibleDC(nint dc);
    [DllImport("gdi32.dll")] private static extern nint CreateDIBSection(nint dc, in Header header, uint usage, out nint bits, nint section, uint offset);
    [DllImport("gdi32.dll")] private static extern nint SelectObject(nint dc, nint obj);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(nint obj);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(nint dc);
    internal static int Save(nint window, string path)
    {
        GetWindowRect(window, out var bounds);
        int width = bounds.Right - bounds.Left, height = bounds.Bottom - bounds.Top;
        var header = new Header { Size = 40, Width = width, Height = -height, Planes = 1, Bits = 32 };
        nint dc = CreateCompatibleDC(0);
        nint bitmap = CreateDIBSection(dc, in header, 0, out var pixels, 0, 0);
        nint previous = SelectObject(dc, bitmap);
        try
        {
            if (!PrintWindow(window, dc, 2)) throw new InvalidOperationException("PrintWindow failed.");
            var bytes = new byte[width * height * 4];
            Marshal.Copy(pixels, bytes, 0, bytes.Length);
            var source = BitmapSource.Create(width, height, 96, 96, PixelFormats.Bgr32, null, bytes, width * 4);
            var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(source));
            using var stream = File.Create(path); png.Save(stream);
            // Ignore frame/caption colors: only the native viewport body counts.
            var colors = new HashSet<int>();
            for (int y = height / 5; y < height * 4 / 5; y++)
                for (int x = width / 5; x < width * 4 / 5; x++)
                {
                    int i = (y * width + x) * 4;
                    colors.Add(bytes[i] | bytes[i + 1] << 8 | bytes[i + 2] << 16);
                }
            return colors.Count;
        }
        finally { SelectObject(dc, previous); DeleteObject(bitmap); DeleteDC(dc); }
    }
}
