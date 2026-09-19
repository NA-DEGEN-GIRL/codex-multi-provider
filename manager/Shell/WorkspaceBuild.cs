using System.IO;
using System.Text.RegularExpressions;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

// Keep the visible version, copied details and startup log on one revision.
// Identify the running executable, never a newer release in current.json.
internal static class WorkspaceBuild
{
    internal const int Revision = 61;
    internal const string Description = "백그라운드 실행 중 SSH 연결 실패 수정";
    internal static string Label => $"수정 {Revision}";
    internal static string Title => $"Codex 작업 공간 · {Label}";
    internal static string BuildId { get; } = RunningBuildId();
    internal static string CopyText => $"{Title}\n변경: {Description}\n빌드: {BuildId}\nIPC: {ManagerProtocol.Version}";

    private static string RunningBuildId()
    {
        var directory = Path.GetFileName(Path.TrimEndingDirectorySeparator(AppContext.BaseDirectory));
        // Only the build identifier is shared; no local paths or account data.
        return Regex.IsMatch(directory, @"\A[0-9]{8}-[0-9]{6}-[0-9]{3}\z") ? directory : "개발 빌드";
    }
}
