using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal sealed class ChecklistEntry
{
    public string Id { get; set; } = Guid.NewGuid().ToString();
    public string Text { get; set; } = "";
    public bool Done { get; set; }
}
internal sealed class TaskNote
{
    public string Id { get; set; } = Guid.NewGuid().ToString();
    public string Title { get; set; } = "메모";
    public string Kind { get; set; } = "text";
    public string Body { get; set; } = "";
    public List<ChecklistEntry> Items { get; set; } = [];
    public long Revision { get; set; }
    public bool Deleted { get; set; }
    public long UpdatedAt { get; set; }
    public override string ToString() => Title;
}
internal sealed class NoteDraft(NoteTask task, TaskNote note)
{
    public NoteTask Task { get; } = task;
    public TaskNote Note { get; set; } = note;
    public long Changed, Saved;
    public bool Saving;
    public string? Error;
}
internal sealed class NoteDrafts(string root)
{
    internal static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    private readonly string directory = Path.Combine(root, "work", "control-center", "note-drafts");
    private readonly object gate = new();
    private readonly Dictionary<string, long> written = [];
    private string PathFor(NoteTask task, string id) => Path.Combine(directory,
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(task.Key + "/" + id))) + ".json");
    internal void Write(NoteTask task, TaskNote note, long version)
    {
        lock (gate)
        {
        Directory.CreateDirectory(directory);
        var path = PathFor(task, note.Id);
        if (written.GetValueOrDefault(path) > version) return;
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try { File.WriteAllText(temporary, JsonSerializer.Serialize(new { task, note }, Json)); File.Move(temporary, path, true); written[path] = version; }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
    }
    internal void Remove(NoteTask task, string id, long through = long.MaxValue) { lock (gate) { var path = PathFor(task, id); if (written.GetValueOrDefault(path) <= through && File.Exists(path)) File.Delete(path); } }
    internal IEnumerable<TaskNote> Recover(NoteTask task)
    {
        if (!Directory.Exists(directory)) return [];
        var result = new List<TaskNote>();
        foreach (var path in Directory.EnumerateFiles(directory, "*.json").Take(1024))
        {
            try
            {
                if (new FileInfo(path).Length > 256 * 1024) continue;
                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                var stored = doc.RootElement.GetProperty("task").Deserialize<NoteTask>(Json);
                if (stored == task && doc.RootElement.GetProperty("note").Deserialize<TaskNote>(Json) is { } note) result.Add(note);
            }
            catch (Exception error) when (error is IOException or JsonException or KeyNotFoundException) { }
        }
        return result;
    }
}
