using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class UpdatePresentation
{
    public static bool CanInstall(JsonElement result) => !result.B("worker_active") &&
        result.S("status") is "available" or "update_available" or "recovery_required" or "failed_restore";

    public static string Summary(JsonElement result)
    {
        var lines = new List<string> { result.Message("업데이트 상태 확인 중") };
        foreach (var field in new[] { "blockers", "remote_pending", "restore_failures" })
            foreach (var entry in result.Arr(field))
            {
                var message = entry.ValueKind == JsonValueKind.String ? entry.GetString()! : entry.Message();
                if (!lines.Contains(message)) lines.Add(message);
            }
        return string.Join("\n", lines);
    }

    public static string ProfileState(JsonElement result) => result.S("state") switch
    {
        "current" or "complete" => "최신 버전",
        "superseded" => "새 실행 확인 필요",
        "latest_on_open" => "최신 버전 준비됨",
        "waiting" => "작업 종료 후 자동 적용",
        "attention" => "업데이트 확인 필요",
        "login_pending" => "로그인 완료 대기",
        "unknown" => "실행 버전 확인 중",
        _ => "업데이트 적용 중"
    };
}
