using System.IO;
using System.Security.Cryptography;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Media.Imaging;

namespace Codex.ControlCenter.Shell;

internal sealed class NoteImage
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("width")] public int Width { get; set; }
    [JsonPropertyName("height")] public int Height { get; set; }
}

// Immutable PNGs are shared by note groups, split notes and recovery drafts.
// Removing an attachment removes its reference, never another note's image.
internal sealed class NoteImageStore(string root)
{
    internal const int MaximumImages = 24, MaximumBytes = 16 * 1024 * 1024;
    private readonly string directory = Path.Combine(root, "work", "control-center", "note-images");
    private string PathFor(NoteImage image)
    {
        if (!Regex.IsMatch(image.Id, "\\A[0-9a-f]{64}\\z")) throw new IOException("이미지 첨부 정보가 올바르지 않습니다.");
        return Path.Combine(directory, image.Id + ".png");
    }
    private static void CheckDimensions(int width, int height)
    {
        if (width <= 0 || height <= 0 || (long)width * height > 40_000_000)
            throw new IOException("이미지는 4천만 픽셀 이하로 첨부해 주세요.");
    }
    internal static BitmapSource ReadFile(string path)
    {
        if (new FileInfo(path).Length > MaximumBytes) throw new IOException("이미지 한 장은 16MB 이하여야 합니다.");
        using var stream = File.OpenRead(path);
        var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.DelayCreation, BitmapCacheOption.OnDemand);
        var frame = decoder.Frames[0]; CheckDimensions(frame.PixelWidth, frame.PixelHeight);
        var bitmap = new WriteableBitmap(frame); bitmap.Freeze(); return bitmap;
    }
    internal NoteImage Save(BitmapSource bitmap)
    {
        CheckDimensions(bitmap.PixelWidth, bitmap.PixelHeight);
        using var buffer = new MemoryStream();
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); encoder.Save(buffer);
        if (buffer.Length > MaximumBytes) throw new IOException("이미지 한 장은 16MB 이하여야 합니다.");
        var bytes = buffer.ToArray();
        var image = new NoteImage { Id = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant(), Width = bitmap.PixelWidth, Height = bitmap.PixelHeight };
        Directory.CreateDirectory(directory);
        var destination = PathFor(image);
        if (!File.Exists(destination))
        {
            var temporary = Path.Combine(directory, Guid.NewGuid().ToString("N") + ".tmp");
            try
            {
                using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                { file.Write(bytes); file.Flush(flushToDisk: true); }
                try { File.Move(temporary, destination); }
                catch (IOException) when (File.Exists(destination)) { /* Another paste stored the same immutable image. */ }
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
        return image;
    }
    internal BitmapSource Load(NoteImage image, int thumbnailWidth = 0)
    {
        var path = PathFor(image);
        CheckDimensions(image.Width, image.Height);
        if (new FileInfo(path).Length > MaximumBytes) throw new IOException("이미지 파일이 너무 큽니다.");
        using var stream = File.OpenRead(path);
        var bitmap = new BitmapImage(); bitmap.BeginInit(); bitmap.CacheOption = BitmapCacheOption.OnLoad;
        if (thumbnailWidth > 0)
        {
            if (image.Width >= image.Height) bitmap.DecodePixelWidth = Math.Min(thumbnailWidth, image.Width);
            else bitmap.DecodePixelHeight = Math.Min(thumbnailWidth, image.Height);
        }
        bitmap.StreamSource = stream; bitmap.EndInit(); bitmap.Freeze();
        if (thumbnailWidth == 0 && (bitmap.PixelWidth != image.Width || bitmap.PixelHeight != image.Height))
            throw new IOException("이미지 원본이 변경되었습니다.");
        return bitmap;
    }
    internal (BitmapSource Bitmap, byte[] Png) ReadClipboardImage(NoteImage image) => (Load(image), File.ReadAllBytes(PathFor(image)));
    internal static DataObject CopyData((BitmapSource Bitmap, byte[] Png) image)
    {
        var data = new DataObject(); data.SetImage(image.Bitmap);
        // Electron/browser consumers prefer the original PNG; native Windows
        // editors can use the standard Bitmap/DIB format from the same payload.
        data.SetData("PNG", new MemoryStream(image.Png), false);
        return data;
    }
}
