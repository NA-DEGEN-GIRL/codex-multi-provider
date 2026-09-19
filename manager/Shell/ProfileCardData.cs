using System.Text.Json;
using System.Windows;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// Separate account identity, quota and runtime state instead of wrapping a
// single sentence. This value also participates in list refresh comparisons.
internal sealed record ProfileCardData(string Name, string Status, string StatusTone,
    string QuotaLabel, string QuotaText, double Remaining, bool HasQuota,
    string ResetText, string RedeemText, string Model, bool External,
    string Freshness, string Notice)
{
    public Visibility QuotaVisibility => HasQuota ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NativeVisibility => External || QuotaLabel.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    public Visibility ExternalVisibility => External ? Visibility.Visible : Visibility.Collapsed;
    public Visibility FreshnessVisibility => Freshness.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NoticeVisibility => Notice.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Brush StatusBrush => StatusTone switch { "ready" => ReadyBrush, "warning" => WarningBrush, _ => MutedBrush };
    public Brush QuotaBrush => Remaining <= 10 ? WarningBrush : AccentBrush;
    public string DetailHint => string.Join("\n", new[] { Name, Status, QuotaLabel + " " + QuotaText, ResetText, RedeemText, Freshness, Notice }.Where(v => !string.IsNullOrWhiteSpace(v)));
    private static readonly Brush ReadyBrush = Brush("#77C6A0"), WarningBrush = Brush("#E5B773"),
        MutedBrush = Brush("#9BA6B8"), AccentBrush = Brush("#94A9F8");
    private static Brush Brush(string color) { var brush = (SolidColorBrush)new BrushConverter().ConvertFromString(color)!; brush.Freeze(); return brush; }

    internal static ProfileCardData Create(JsonElement profile, string fallback, string notice = "")
    {
        var external = profile.S("auth_mode") == "external";
        var status = profile.S("status") switch
        {
            "running" => "실행 중", "ready" => "준비됨", "starting" => "여는 중",
            "not_started" or "stopped" => "닫힘", "unprepared" => "준비 전",
            "failed" or "error" or "blocked" => "확인 필요", _ => "확인 중"
        };
        var tone = profile.S("status") switch { "running" or "ready" => "ready", "failed" or "error" or "blocked" => "warning", _ => "muted" };
        if (ProfileLoginPresentation.NeedsLogin(profile)) { status = "로그인 필요"; tone = "warning"; }
        var usage = profile.Get("usage");
        var windows = usage.Arr("windows").ToArray();
        var window = windows.FirstOrDefault(w => w.S("label", w.S("name")) == "주간");
        if (window.ValueKind != JsonValueKind.Object) window = windows.FirstOrDefault();
        var value = window.Get("remaining_percent");
        var number = value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var remaining) ? remaining : double.NaN;
        if (!double.IsFinite(number))
        {
            value = window.Get("used_percent");
            number = value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var used) ? 100 - used : double.NaN;
        }
        var hasQuota = !external && double.IsFinite(number);
        var resetText = "초기화 시간 확인 안 됨";
        var reset = window.Get("resets_at");
        if (reset.ValueKind == JsonValueKind.Number && reset.TryGetInt64(out var seconds))
        {
            try { resetText = $"{DateTimeOffset.FromUnixTimeSeconds(seconds).ToLocalTime():M/d HH:mm} 초기화"; }
            catch (ArgumentOutOfRangeException) { }
        }
        var credits = usage.Get("reset_credits").Get("available");
        var redeem = credits.ValueKind == JsonValueKind.Number && credits.TryGetInt64(out var count) && count is >= 0 and <= 1_000_000
            ? $"리딤 {count:N0}회" : "리딤 확인 안 됨";
        var age = usage.N("age_seconds");
        var freshness = usage.B("refreshing") ? "갱신 중" : age >= 86400 ? $"{age / 86400}일 전 값"
            : age >= 3600 ? $"{age / 3600}시간 전 값" : age >= 120 ? $"{age / 60}분 전 값"
            : usage.S("freshness") == "stale" || usage.Get("error").ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null) ? "이전 값" : "";
        return new(profile.S("alias", fallback.Split('\n')[0]), status, tone,
            external || profile.ValueKind != JsonValueKind.Object ? "" : hasQuota ? window.S("label", window.S("name", "사용량")) + " 남음" : "사용량",
            external ? "" : hasQuota ? $"{Math.Clamp(number, 0, 100):0}%" : "확인 안 됨",
            hasQuota ? Math.Clamp(number, 0, 100) : 0, hasQuota,
            external ? "" : resetText, external ? "" : redeem,
            external ? profile.S("external_model_name", "외부 API 모델") : "", external,
            external ? "" : freshness, notice.Trim());
    }
}
