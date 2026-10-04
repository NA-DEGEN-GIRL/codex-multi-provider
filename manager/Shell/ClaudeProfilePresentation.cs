using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// Only the CLI's allowlisted status is used here; credentials never enter the UI.
internal static class ClaudeProfilePresentation
{
    internal static bool IsClaude(JsonElement profile) => profile.S("auth_mode") == "claude_code" || profile.S("profile_kind") == "claude_code";
    internal static JsonElement Status(JsonElement profile) => profile.Get("claude_status");
    internal static bool NeedsLogin(JsonElement profile) => Status(profile).S("state") is "signed_out" or "login_required" or "login_needed" or "not_logged_in" or "authentication_failed";
    internal static string Label(JsonElement status) => status.S("state") switch
    {
        "missing_cli" or "cli_missing" or "not_installed" => "CLI 설치 필요",
        "outdated_cli" or "cli_outdated" or "update_required" => "CLI 업데이트 필요",
        "disabled" or "blocked" => "사용 중지됨",
        "rate_limited" or "limit_reached" => "한도 도달",
        "login_started" or "login_pending" or "logging_in" or "awaiting_user" => "로그인 진행 중",
        "signed_out" or "login_required" or "login_needed" or "not_logged_in" or "authentication_failed" => "로그인 필요",
        "error" or "status_error" or "auth_status" or "cli_version" => "상태 확인 필요",
        _ when status.B("logged_in") => "로그인됨",
        _ => "로그인 상태 미확인"
    };
    internal static string Tone(JsonElement status) => status.B("logged_in") && Label(status) == "로그인됨" ? "ready"
        : Label(status) is "로그인 상태 미확인" or "로그인 진행 중" ? "muted" : "warning";
    internal static string Model(JsonElement profile) => profile.Get("claude_settings").S("model", "opus") switch
    { "opus" => "Opus", "sonnet" => "Sonnet", "fable" => "Fable", "claude-opus-5-5" => "Opus 5.5", var model => model };
    // Email is opt-in window state, never inferred from the routine status payload.
    internal static string Account(JsonElement status) => status.S("subscription_type");
    internal static string Detail(JsonElement profile)
    {
        var status = Status(profile);
        return string.Join(" · ", new[] { "Claude " + Model(profile), Label(status), Account(status),
            status.S("cli_version") is { Length: > 0 } version ? "CLI " + version : "" }.Where(value => value.Length > 0));
    }
    internal const string UsageHint = "Claude 구독 사용량 미확인 · 사용량 새로 확인";
}
