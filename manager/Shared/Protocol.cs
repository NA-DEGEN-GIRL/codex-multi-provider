using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;

namespace Codex.ControlCenter.Shared;

public static class ManagerProtocol
{
    public const int Version = 27;
    public const int MaxRequestBytes = 256 * 1024;
    public const int MaxResponseBytes = 8 * 1024 * 1024;
    public const string SupervisorFileName = "codex-workspace-service.exe";

    public static string NormalizeRoot(string root)
    {
        if (!Path.IsPathFullyQualified(root))
            throw new ArgumentException("The manager root must be an absolute path.", nameof(root));
        var path = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        if (!Directory.Exists(path))
            throw new DirectoryNotFoundException("The manager root directory was not found.");
        return path;
    }

    public static string PipeName(string root)
    {
        var sid = WindowsIdentity.GetCurrent().User?.Value
            ?? throw new InvalidOperationException("The current Windows account could not be identified.");
        var identity = $"{sid}\n{NormalizeRoot(root).ToUpperInvariant()}";
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity)))[..32];
        // One workspace owns one management service across application updates.
        return $"CodexControlCenter.service.{hash}";
    }

    public static string ServiceRevision()
    {
        var manifest = Path.Combine(AppContext.BaseDirectory, "runtime-manifest.json");
        if (!File.Exists(manifest)) return $"development-{Version}";
        using var document = JsonDocument.Parse(File.ReadAllText(manifest));
        return document.RootElement.GetProperty("service_revision").GetString()
            ?? throw new IOException("Missing manager service revision.");
    }

    public static string SafeMessage(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return "The manager request failed.";
        return new string(text.Where(c => !char.IsControl(c) || c == ' ').Take(1000).ToArray());
    }
}

/// <summary>Bounded UTF-8 JSONL reader. The byte limit is enforced before allocating a whole line.</summary>
public sealed class JsonLineReader(Stream stream, int maxBytes)
{
    private readonly byte[] buffer = new byte[8192];
    private int offset;
    private int count;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    public async Task<string?> ReadAsync(CancellationToken cancellationToken = default)
    {
        using var frame = new MemoryStream();
        while (true)
        {
            if (offset == count)
            {
                count = await stream.ReadAsync(buffer, cancellationToken).ConfigureAwait(false);
                offset = 0;
                if (count == 0)
                {
                    if (frame.Length != 0) throw new IOException("The manager sent an incomplete JSON frame.");
                    return null;
                }
            }
            var newline = Array.IndexOf(buffer, (byte)'\n', offset, count - offset);
            var end = newline < 0 ? count : newline;
            var length = end - offset;
            if (frame.Length + length > maxBytes)
                throw new IOException("The manager JSON frame exceeded its size limit.");
            frame.Write(buffer, offset, length);
            offset = newline < 0 ? end : end + 1;
            if (newline >= 0)
            {
                var bytes = frame.GetBuffer();
                var size = (int)frame.Length;
                if (size > 0 && bytes[size - 1] == '\r') size--;
                try { return StrictUtf8.GetString(bytes, 0, size); }
                catch (DecoderFallbackException e) { throw new IOException("The manager frame contained invalid UTF-8.", e); }
            }
        }
    }

    public static async Task WriteAsync(Stream stream, string line, int limit, CancellationToken cancellationToken = default)
    {
        if (line.Contains('\n') || line.Contains('\r'))
            throw new IOException("A JSON frame cannot contain literal line breaks.");
        var byteCount = Encoding.UTF8.GetByteCount(line);
        if (byteCount > limit) throw new IOException("The manager JSON frame exceeded its size limit.");
        var bytes = Encoding.UTF8.GetBytes(line + "\n");
        await stream.WriteAsync(bytes, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }
}

public sealed class ManagerException(string code, string message) : Exception(ManagerProtocol.SafeMessage(message))
{
    public string Code { get; } = code;
}
