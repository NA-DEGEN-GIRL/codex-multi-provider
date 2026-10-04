using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

internal sealed record ClaudeUsageWindow(string Label, string Text, double Remaining, bool HasValue, string ResetText, bool Stale)
{
    public Visibility MeterVisibility => HasValue ? Visibility.Visible : Visibility.Collapsed;
    public Brush ValueBrush => !HasValue || Stale ? Muted : Remaining <= 10 ? Warning : Accent;
    public string Detail => $"{Label} {Text} · {ResetText}";
    private static readonly Brush Muted = Freeze("#8E9BB2"), Warning = Freeze("#E5B773"), Accent = Freeze("#94A9F8");
    private static Brush Freeze(string color)
    {
        var brush = (SolidColorBrush)new BrushConverter().ConvertFromString(color)!;
        brush.Freeze(); return brush;
    }
}

internal sealed record ClaudeUsageData(ClaudeUsageWindow FiveHour, ClaudeUsageWindow Weekly, string Hint)
{
    public string Summary => string.Join("\n", new[] { FiveHour.Detail, Weekly.Detail, Hint }.Where(value => value.Length > 0));
}

// Only subscription windows explicitly marked as Claude data are eligible.
// Missing or invalid values must never become an exhausted (0%) meter.
internal static class ClaudeUsagePresentation
{
    internal static ClaudeUsageData Read(JsonElement profile)
    {
        var usage = profile.Get("usage");
        if (usage.S("provider") != "claude_code") usage = default;
        var stale = usage.S("freshness") == "stale";
        var unknown = usage.S("freshness", "unknown") == "unknown";
        var error = usage.Get("error");
        ClaudeUsageWindow Window(string key, string label)
        {
            var window = usage.Arr("windows").FirstOrDefault(value => value.S("key") == key);
            static double Percent(JsonElement value) => value.ValueKind == JsonValueKind.Number
                && value.TryGetDouble(out var number) && double.IsFinite(number) && number is >= 0 and <= 100 ? number : double.NaN;
            var remaining = Percent(window.Get("remaining_percent"));
            if (!double.IsFinite(remaining))
            {
                var used = Percent(window.Get("used_percent"));
                if (double.IsFinite(used)) remaining = 100 - used;
            }
            var known = !unknown && double.IsFinite(remaining);
            var windowStale = error.ValueKind == JsonValueKind.Object
                || window.S("freshness", stale ? "stale" : "live") == "stale";
            var reportedReset = window.S("reset_text").Trim();
            var resetText = reportedReset.Length is > 0 and <= 100 && !reportedReset.Any(char.IsControl)
                ? reportedReset : "초기화 시간 미확인";
            var reset = window.Get("resets_at");
            if (reset.ValueKind == JsonValueKind.Number && reset.TryGetInt64(out var seconds))
            {
                try { resetText = $"{DateTimeOffset.FromUnixTimeSeconds(seconds).ToLocalTime():M/d HH:mm} 초기화"; }
                catch (ArgumentOutOfRangeException) { }
            }
            return new(label + " 남음", known ? $"{remaining:0.#}%" + (windowStale ? " · 이전 값" : "") : "미확인",
                known ? remaining : 0, known, resetText, windowStale);
        }
        var fiveHour = Window("five_hour", "5시간");
        var weekly = Window("seven_day", "주간");
        var notes = new List<string>();
        if (usage.B("refreshing")) notes.Add("사용량 갱신 중");
        else if (stale) notes.Add(fiveHour.Stale && weekly.Stale ? "이전 사용량 · 새로 확인 필요" : "일부 사용량 이전 값 · 새로 확인 필요");
        else if (unknown && error.ValueKind != JsonValueKind.Object) notes.Add(ClaudeProfilePresentation.UsageHint);
        var message = error.S("code") switch
        {
            "interactive_usage_required" => "현재 한도는 Claude Code /usage에서 확인",
            "onboarding_required" => "Claude Code 첫 실행 설정 필요",
            _ => error.S("message")
        };
        if (message.Length > 0) notes.Add(message);
        if (DateTimeOffset.TryParse(usage.S("observed_at"), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var observed))
            notes.Add($"마지막 확인 {observed.ToLocalTime():M/d HH:mm}");
        return new(fiveHour, weekly, string.Join(" · ", notes));
    }
}
