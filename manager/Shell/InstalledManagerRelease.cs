using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class InstalledManagerRelease
{
    // A shortcut may still point to a former release folder. Resolve before
    // constructing any manager window or connecting to its supervisor.
    public static string? Replacement(string root, string currentExecutable)
    {
        var shell = SelectedExecutable(root);
        return shell is null || string.Equals(shell, Path.GetFullPath(currentExecutable), StringComparison.OrdinalIgnoreCase) ? null : shell;
    }

    internal static string? SelectedExecutable(string root)
    {
        root = Path.GetFullPath(root);
        var pointer = Path.Combine(root, "artifacts", "manager", "current.json");
        if (!File.Exists(pointer)) return null;
        if (new FileInfo(pointer).Length > 16384) throw new IOException("관리 앱의 업데이트 정보가 올바르지 않습니다.");
        using var document = JsonDocument.Parse(File.ReadAllText(pointer));
        var shell = Path.GetFullPath(document.RootElement.GetProperty("shell").GetString()
            ?? throw new InvalidOperationException("관리 앱의 새 버전 경로가 비어 있습니다."));
        var releases = Path.Combine(root, "artifacts", "manager", "releases") + Path.DirectorySeparatorChar;
        if (!shell.StartsWith(releases, StringComparison.OrdinalIgnoreCase)
            || !string.Equals(Path.GetFileName(shell), "Codex.ControlCenter.exe", StringComparison.OrdinalIgnoreCase)
            || !File.Exists(shell))
            throw new InvalidOperationException("설치된 관리 앱의 새 버전을 확인하지 못했습니다. 배포 상태를 확인하세요.");
        return shell;
    }
}
