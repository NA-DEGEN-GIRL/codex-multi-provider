using System.Diagnostics;
using System.IO;
using System.Text.Json;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

// A next-launch preference, never an instruction to stop a running service.
// The displayed authority must come from OS tokens, not this file.
internal static class WorkspaceExecutionMode
{
    internal static string PreferencePath(string root) =>
        Path.Combine(root, "work", "control-center", "workspace-launch.json");

    internal static bool ReadAdministrator(string root)
    {
        var path = PreferencePath(root);
        if (!File.Exists(path)) return false;
        try
        {
            if (new FileInfo(path).Length > 4096) throw new JsonException();
            using var document = JsonDocument.Parse(File.ReadAllText(path));
            var value = document.RootElement;
            if (value.ValueKind != JsonValueKind.Object ||
                !value.TryGetProperty("version", out var version) || !version.TryGetInt32(out var number) || number != 1 ||
                !value.TryGetProperty("administrator", out var mode) || mode.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw new JsonException();
            return mode.GetBoolean();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            throw new InvalidOperationException("실행 권한 설정을 읽지 못했습니다. workspace-launch.json을 백업에서 복구한 뒤 다시 열어 주세요.", error);
        }
    }

    internal static void SaveAdministrator(string root, bool administrator)
    {
        var path = PreferencePath(root);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(new { version = 1, administrator }));
            File.Move(temporary, path, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporary)) File.Delete(temporary);
        }
    }

    internal static bool RequiresElevation(bool administrator, bool explicitRequest, bool elevated) =>
        (administrator || explicitRequest) && !elevated;

    internal static bool DeferElevation(ExecutionAuthority current, ExecutionAuthority? service) =>
        current.Known && current.Elevated == false && service is { Known: true, Elevated: false } peer &&
        string.Equals(current.UserSid, peer.UserSid, StringComparison.OrdinalIgnoreCase);

    internal static void RequireSameUser(string? expectedUserSid, string actualUserSid)
    {
        if (expectedUserSid is not null && !string.Equals(expectedUserSid, actualUserSid, StringComparison.Ordinal))
            throw new InvalidOperationException("다른 Windows 계정으로는 이 작업공간을 관리자 실행할 수 없습니다. 기존 Windows 계정으로 실행해 주세요. 로그인과 API 키는 계정에 연결되어 있습니다.");
    }

    internal static ProcessStartInfo ElevatedStart(string executable, string root, string userSid, string? notificationUri)
    {
        var start = new ProcessStartInfo(executable) { UseShellExecute = true, Verb = "runas", WorkingDirectory = root };
        start.ArgumentList.Add("--root"); start.ArgumentList.Add(root);
        start.ArgumentList.Add("--require-administrator");
        start.ArgumentList.Add("--expected-user-sid"); start.ArgumentList.Add(userSid);
        if (notificationUri is not null)
        {
            start.ArgumentList.Add("--workspace-notification"); start.ArgumentList.Add(notificationUri);
        }
        return start;
    }
}
