using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// UI-only: whether each profile shows its task-notes panel, so opening it in one
// profile never opens it in another. A profile without an entry starts closed,
// which is also what every profile did before this was remembered (the old
// toggle was a single in-memory value, never persisted, so nothing migrates).
internal sealed class NotesPanelVisibility
{
    // The read-only all-records view has its own slot; profile ids are GUIDs.
    internal const string CatalogKey = "catalog";
    private const int MaxEntries = 512;
    private const int MaxKeyLength = 128;
    private const long MaxFileBytes = 64 * 1024;
    private readonly string _path;
    private readonly Action<string> _report;
    private readonly Dictionary<string, bool> _open = new(StringComparer.Ordinal);

    internal NotesPanelVisibility(string root, Action<string> report)
    {
        _path = Path.Combine(root, "work", "control-center", "notes-panel.json");
        _report = report;
        Read();
    }

    // "" is the no-profile state: remembered for this window only.
    internal static string Key(string? profileId, bool viewingCatalog)
        => viewingCatalog ? CatalogKey : profileId ?? "";

    internal bool IsOpen(string key) => _open.GetValueOrDefault(key);

    internal void Set(string key, bool open)
    {
        if (_open.TryGetValue(key, out var current) && current == open) return;
        _open[key] = open;
        if (Persistable(key)) Save();
    }

    private static bool Persistable(string key) => key.Length is > 0 and <= MaxKeyLength;

    private void Read()
    {
        try
        {
            var file = new FileInfo(_path);
            if (!file.Exists || file.Length > MaxFileBytes) return;
            using var json = JsonDocument.Parse(File.ReadAllText(file.FullName));
            if (json.RootElement.ValueKind != JsonValueKind.Object ||
                !json.RootElement.TryGetProperty("version", out var version) ||
                version.ValueKind != JsonValueKind.Number || !version.TryGetInt32(out var number) || number != 1 ||
                !json.RootElement.TryGetProperty("profiles", out var profiles) || profiles.ValueKind != JsonValueKind.Object)
                return;
            foreach (var entry in profiles.EnumerateObject())
            {
                if (_open.Count >= MaxEntries) break;
                if (Persistable(entry.Name) && entry.Value.ValueKind is JsonValueKind.True or JsonValueKind.False)
                    _open[entry.Name] = entry.Value.GetBoolean();
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { _open.Clear(); }
    }

    private void Save()
    {
        // Bounded like the read; real profile counts stay far below it.
        var entries = _open.Where(entry => Persistable(entry.Key)).Take(MaxEntries)
            .ToDictionary(entry => entry.Key, entry => entry.Value, StringComparer.Ordinal);
        var temporary = _path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            File.WriteAllText(temporary, JsonSerializer.Serialize(new { version = 1, profiles = entries }));
            File.Move(temporary, _path, overwrite: true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            _report("작업 메모 표시 상태 저장 실패 · " + error.Message);
        }
        finally
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
        }
    }
}
