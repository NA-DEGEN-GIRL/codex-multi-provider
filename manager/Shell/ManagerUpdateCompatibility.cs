using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal enum ManagerUpdateState { Checking, Current, Ready, NeedsStop, Unknown, Deferred }
internal sealed record ManagerUpdateNotice(ManagerUpdateState State, string Title, string Detail)
{
    internal static ManagerUpdateNotice Checking => new(ManagerUpdateState.Checking,
        "업데이트 호환성 확인 중", "실행 중인 작업은 유지됩니다.");
    internal static ManagerUpdateNotice Unknown(string reason) => new(ManagerUpdateState.Unknown,
        "업데이트 호환성 확인 불가", reason + " 현재 창과 작업을 유지해 주세요.");
}

// A read-only preflight: never starts a release, migrates a service, or opens a
// profile. Missing/partial/future metadata cannot advertise a safe restart.
internal static class ManagerUpdateCompatibility
{
    internal static ManagerUpdateNotice Inspect(string root, string currentExecutable, int currentRevision, JsonElement? service)
    {
        try
        {
            var selected = InstalledManagerRelease.SelectedExecutable(root);
            if (selected is null) return ManagerUpdateNotice.Unknown("설치된 업데이트 정보가 없습니다.");
            var path = Path.Combine(Path.GetDirectoryName(selected)!, "runtime-manifest.json");
            if (!File.Exists(path) || new FileInfo(path).Length > 1024 * 1024)
                return ManagerUpdateNotice.Unknown("새 버전의 호환성 정보가 없습니다.");
            using var manifest = JsonDocument.Parse(File.ReadAllText(path));
            var release = manifest.RootElement;
            if (!Integer(release, "version", out var manifestVersion) || manifestVersion != 1 ||
                !release.TryGetProperty("shell_compatibility", out var shell) || shell.ValueKind != JsonValueKind.Object ||
                !Integer(shell, "version", out var schema) || schema != 1 ||
                !Integer(shell, "revision", out var revision) || revision < currentRevision ||
                !Integer(shell, "service_protocol", out var protocol) || protocol <= 0 ||
                !Revision(release, "service_revision", out var targetService))
                return ManagerUpdateNotice.Unknown("새 버전의 호환성 정보를 검증하지 못했습니다.");
            var isCurrent = string.Equals(selected, Path.GetFullPath(currentExecutable), StringComparison.OrdinalIgnoreCase);
            if (isCurrent && revision != currentRevision)
                return ManagerUpdateNotice.Unknown("실행 파일과 버전 정보가 일치하지 않습니다.");
            if (service is not { ValueKind: JsonValueKind.Object } live ||
                !Integer(live, "version", out var liveProtocol) || liveProtocol <= 0 ||
                !Revision(live, "service_revision", out var liveRevision))
                return ManagerUpdateNotice.Unknown("실행 중인 서비스에 연결해 확인해야 합니다.");
            var target = isCurrent ? "현재 관리창" : $"수정 {revision}";
            if (protocol != liveProtocol)
                return new(ManagerUpdateState.NeedsStop, "작업 종료 후 적용 필요",
                    $"{target}과 실행 중인 서비스가 호환되지 않습니다. 작업을 마친 뒤 완전 종료하고 다시 실행하세요.");
            if (!live.TryGetProperty("preserves_background_profiles", out _))
                return new(ManagerUpdateState.NeedsStop, "작업 종료 후 적용 필요",
                    "구버전 서비스입니다. 작업 유지 기능을 처음 적용하려면 작업을 마친 뒤 완전 종료하세요.");
            if (!Boolean(live, "preserves_background_profiles", out var preserves))
                return ManagerUpdateNotice.Unknown("서비스의 작업 유지 지원 여부를 확인하지 못했습니다.");
            if (!preserves)
                return new(ManagerUpdateState.NeedsStop, "작업 종료 후 적용 필요",
                    $"{target}에 필요한 서비스 업데이트가 있습니다. 작업을 마친 뒤 완전 종료하고 다시 실행하세요.");
            // Closing/reopening the shell leaves a busy service running. The
            // protocol and background-work capability do not prove that the
            // selected release's backend changes have actually been applied.
            // Check the live service before either the Ready or Current branch.
            if (!string.Equals(targetService, liveRevision, StringComparison.OrdinalIgnoreCase))
                return new(ManagerUpdateState.NeedsStop, "작업 종료 후 적용 필요",
                    isCurrent
                        ? $"관리창은 수정 {revision}이지만 이전 관리 서비스가 실행 중입니다. 창만 다시 열어도 서비스는 유지됩니다. 작업을 마친 뒤 완전 종료하고 다시 실행하세요."
                        : $"수정 {revision}에는 관리 서비스 업데이트가 포함되어 있습니다. 현재 창만 다시 열면 서비스 적용은 남습니다. 작업을 마친 뒤 완전 종료하고 다시 실행하세요.");
            if (!isCurrent)
                return new(ManagerUpdateState.Ready, "작업 유지하며 업데이트 가능",
                    $"수정 {revision} 준비됨 · X로 창만 닫고 다시 실행하세요. 작업은 계속됩니다.");
            return new(ManagerUpdateState.Current, "관리창 최신 · 작업 유지 가능",
                "X는 창만 닫습니다. 작업까지 끝내려면 완전 종료를 누르세요.");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException
            or InvalidOperationException or ArgumentException or NotSupportedException)
        {
            return ManagerUpdateNotice.Unknown("업데이트 정보를 읽지 못했습니다.");
        }
    }

    private static bool Integer(JsonElement value, string name, out int number)
    {
        number = 0;
        return value.ValueKind == JsonValueKind.Object && value.TryGetProperty(name, out var property)
            && property.ValueKind == JsonValueKind.Number && property.TryGetInt32(out number);
    }
    private static bool Boolean(JsonElement value, string name, out bool flag)
    {
        flag = false;
        if (value.ValueKind != JsonValueKind.Object || !value.TryGetProperty(name, out var property)
            || property.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return false;
        flag = property.ValueKind == JsonValueKind.True;
        return true;
    }
    private static bool Revision(JsonElement value, string name, out string text)
    {
        text = "";
        if (value.ValueKind != JsonValueKind.Object || !value.TryGetProperty(name, out var property)
            || property.ValueKind != JsonValueKind.String) return false;
        text = property.GetString() ?? "";
        return text.Length == 64 && text.All(Uri.IsHexDigit);
    }
}
