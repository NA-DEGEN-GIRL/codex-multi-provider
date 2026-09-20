using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// UI selection only. Work, input drafts and runtime state stay in their original
// live Codex process; reopening must never replay a message or task navigation.
internal sealed record WorkspaceSession(int Version, string? ProfileId, bool ViewingCatalog)
{
    private static string FilePath(string root) => Path.Combine(root, "work", "control-center", "workspace-session.json");

    internal static WorkspaceSession? Read(string root)
    {
        try
        {
            var file = new FileInfo(FilePath(root));
            if (!file.Exists || file.Length > 4096) return null;
            var session = JsonSerializer.Deserialize<WorkspaceSession>(File.ReadAllText(file.FullName));
            return session is { Version: 1 } && (session.ProfileId is null || Guid.TryParse(session.ProfileId, out _)) ? session : null;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { return null; }
    }

    internal void Save(string root)
    {
        var path = FilePath(root);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temporary = path + "." + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(this));
            File.Move(temporary, path, overwrite: true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
