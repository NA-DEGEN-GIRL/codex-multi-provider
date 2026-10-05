using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// One usage window as a meter: "주간 남음 46%" plus its bar and reset time.
// GPT windows and Claude subscription windows share this shape for display.
internal sealed record UsageMeter(string Label, string Text, double Remaining, bool HasValue, string ResetText, bool Stale)
{
    public Visibility MeterVisibility => HasValue ? Visibility.Visible : Visibility.Collapsed;
    public Visibility ResetVisibility => ResetText.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Brush ValueBrush => !HasValue || Stale ? ProfileCardData.FaintBrush : ProfileCardData.QuotaTone(Remaining);
    public string Detail => string.Join(" · ", new[] { $"{Label} {Text}", ResetText }.Where(value => value.Length > 0));

    internal static UsageMeter From(ClaudeUsageWindow window) =>
        new(window.Label, window.Text, window.Remaining, window.HasValue, window.ResetText, window.Stale);
}

// Separate account identity, quota and runtime state instead of wrapping a
// single sentence. This value also participates in list refresh comparisons,
// so it holds only values (no collections); brushes and geometry are derived.
internal sealed record ProfileCardData(string Name, string Status, string StatusTone,
    string QuotaLabel, string QuotaText, double Remaining, bool HasQuota,
    string ResetText, string RedeemText, string Model, bool External,
    string Freshness, string Notice, string Cache = "", string CacheTone = "",
    string ProviderBadge = "API", string Account = "", string UsageHint = "",
    ClaudeUsageWindow? ClaudeFiveHour = null, ClaudeUsageWindow? ClaudeWeekly = null, string Email = "",
    string State = "", string Provider = "gpt", string Short = "?", string NoticeTone = "",
    UsageMeter? Primary = null, UsageMeter? Secondary = null, string Ssh = "", string SshDetail = "")
{
    public Visibility QuotaVisibility => HasQuota ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NativeVisibility => External || QuotaLabel.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    public Visibility ExternalVisibility => External ? Visibility.Visible : Visibility.Collapsed;
    public Visibility FreshnessVisibility => Freshness.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NoticeVisibility => Notice.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility AccountVisibility => Account.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility EmailVisibility => Email.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility UsageHintVisibility => UsageHint.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility ClaudeUsageVisibility => ClaudeFiveHour is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility ModelVisibility => Model.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility SshVisibility => Ssh.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility PrimaryVisibility => Primary is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility SecondaryVisibility => Secondary is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility UsageVisibility => Primary is null && Secondary is null ? Visibility.Collapsed : Visibility.Visible;
    // Reset, redeem and freshness of a GPT account; Claude meters carry their own reset.
    public Visibility ResetLineVisibility => NativeVisibility;
    // Prompt-cache line for the selected task (ProfileCacheLine, cache_warmth.py).
    public Visibility CacheVisibility => Cache.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Brush CacheBrush => CacheTone switch { "ready" => ReadyBrush, "warning" => WarningBrush, _ => CacheMutedBrush };
    public Brush StatusBrush => StatusTone switch { "ready" => ReadyBrush, "warning" => WarningBrush, _ => MutedBrush };
    public Brush StatusPillBrush => StatusTone switch { "ready" => ReadyPill, "warning" => WarningPill, _ => MutedPill };
    public Brush QuotaBrush => Remaining <= 10 ? WarningBrush : AccentBrush;
    public Brush NoticeBrush => NoticeTone == "warning" ? WarningBrush : CacheMutedBrush;
    public Brush NoticePillBrush => NoticeTone == "warning" ? WarningPill : MutedPill;
    public string ProviderLabel => ProfileBadge.ProviderLabel(Provider);
    public Brush TintBrush => WorkspaceAppearance.Tint(Provider).Fill;
    public Brush TintTextBrush => WorkspaceAppearance.Tint(Provider).Text;

    // Rail status dot: attention first, then running (solid) or ready (ring).
    private bool Attention => StatusTone == "warning" || NoticeTone == "warning";
    public Brush DotBrush => Attention ? WarningBrush : StatusTone == "ready" ? ReadyBrush : DotMutedBrush;
    public Brush DotFill => Attention || State != "ready" ? DotBrush : Brushes.Transparent;

    // Weekly remaining as an arc in a 48-DIP box (centre 24, stroke radius 22.5).
    private UsageMeter? RingMeter => Primary is { HasValue: true, Stale: false } ? Primary : null;
    public Visibility RingVisibility => RingMeter is null ? Visibility.Collapsed : Visibility.Visible;
    public Geometry RingArc => RingMeter is { } meter ? Arc(meter.Remaining, 24, 22.5) : Geometry.Empty;
    public Brush RingBrush => RingMeter is { } meter ? QuotaTone(meter.Remaining) : FaintBrush;

    public string UsageSummary => Primary is null && Secondary is null
        ? External && Provider != "claude" ? Model : ""
        : string.Join(" · ", new[] { Primary, Secondary }.OfType<UsageMeter>().Select(meter => $"{meter.Label} {meter.Text}"));
    public string AccessibleName => string.Join(", ", new[] { Name, Status, UsageSummary, Notice }.Where(value => value.Length > 0));
    public string DetailHint => string.Join("\n", new[] { Name, Status, Account, ClaudeFiveHour?.Detail, ClaudeWeekly?.Detail, UsageHint, QuotaLabel + " " + QuotaText, ResetText, RedeemText, Freshness, Notice, Cache }.Where(v => !string.IsNullOrWhiteSpace(v)));

    internal static readonly Brush ReadyBrush = Brush("#77C6A0"), WarningBrush = Brush("#E5B773"), CriticalBrush = Brush("#E57B73"),
        MutedBrush = Brush("#9BA6B8"), AccentBrush = Brush("#94A9F8"), CacheMutedBrush = Brush("#8E9BB2"), FaintBrush = Brush("#8E9BB2"),
        ReadyPill = Brush("#1E3A2E"), WarningPill = Brush("#3A3020"), MutedPill = Brush("#2A2E37"),
        DotMutedBrush = Brush("#6F7A8E");
    private static Brush Brush(string color) { var brush = (SolidColorBrush)new BrushConverter().ConvertFromString(color)!; brush.Freeze(); return brush; }
    internal static Brush QuotaTone(double remaining) => remaining < 10 ? CriticalBrush : remaining < 30 ? WarningBrush : AccentBrush;

    internal static Geometry Arc(double percent, double center, double radius)
    {
        if (!double.IsFinite(percent) || percent <= 0.5) return Geometry.Empty;
        percent = Math.Min(percent, 99.9);
        double angle = percent / 100 * 2 * Math.PI;
        double x = center + radius * Math.Sin(angle), y = center - radius * Math.Cos(angle);
        var geometry = Geometry.Parse(string.Create(CultureInfo.InvariantCulture,
            $"M {center},{center - radius} A {radius},{radius} 0 {(percent > 50 ? 1 : 0)} 1 {x:0.###},{y:0.###}"));
        geometry.Freeze();
        return geometry;
    }

    internal static ProfileCardData Create(JsonElement profile, string fallback, string notice = "", string cache = "", string cacheTone = "", string noticeTone = "")
    {
        var name = profile.S("alias", fallback.Split('\n')[0]);
        var provider = ProfileBadge.Provider(profile);
        var (ssh, sshDetail) = SshHosts(profile);
        if (ClaudeProfilePresentation.IsClaude(profile))
        {
            var claude = ClaudeProfilePresentation.Status(profile);
            var claudeUsage = ClaudeUsagePresentation.Read(profile);
            return new(name, ClaudeProfilePresentation.Label(claude), ClaudeProfilePresentation.Tone(claude),
                "", "", 0, false, "", "", ClaudeProfilePresentation.Model(profile), true, "", notice.Trim(),
                ProviderBadge: "Claude", Account: ClaudeProfilePresentation.Account(claude), UsageHint: claudeUsage.Hint,
                ClaudeFiveHour: claudeUsage.FiveHour, ClaudeWeekly: claudeUsage.Weekly,
                State: profile.S("status"), Provider: provider, Short: ProfileBadge.Short(name), NoticeTone: noticeTone,
                Primary: UsageMeter.From(claudeUsage.Weekly), Secondary: UsageMeter.From(claudeUsage.FiveHour), Ssh: ssh, SshDetail: sshDetail);
        }
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
        var number = Percent(window);
        var hasQuota = !external && double.IsFinite(number);
        var resetText = Reset(window) ?? "초기화 시간 확인 안 됨";
        var credits = usage.Get("reset_credits").Get("available");
        var redeem = credits.ValueKind == JsonValueKind.Number && credits.TryGetInt64(out var count) && count is >= 0 and <= 1_000_000
            ? $"리딤 {count:N0}회" : "리딤 확인 안 됨";
        var age = usage.N("age_seconds");
        // Usage refreshes every 300 s; label a value only after two missed periods.
        var freshness = usage.B("refreshing") ? "갱신 중" : age >= 86400 ? $"{age / 86400}일 전 값"
            : age >= 3600 ? $"{age / 3600}시간 전 값" : age >= 600 ? $"{age / 60}분 전 값"
            : usage.S("freshness") == "stale" || usage.Get("error").ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null) ? "이전 값" : "";
        var quotaLabel = external || profile.ValueKind != JsonValueKind.Object ? "" : hasQuota ? window.S("label", window.S("name", "사용량")) + " 남음" : "사용량";
        var quotaText = external ? "" : hasQuota ? $"{Math.Clamp(number, 0, 100):0}%" : "확인 안 됨";
        UsageMeter? primary = null, secondary = null;
        if (quotaLabel.Length > 0)
        {
            // The reset of the primary (weekly) window shares the redeem line.
            primary = new(quotaLabel, quotaText, hasQuota ? Math.Clamp(number, 0, 100) : 0, hasQuota, "", false);
            var other = windows.FirstOrDefault(w => w.ValueKind == JsonValueKind.Object
                && w.S("label", w.S("name")) != window.S("label", window.S("name")));
            if (other.ValueKind == JsonValueKind.Object)
            {
                var value = Percent(other);
                secondary = new(other.S("label", other.S("name", "사용량")) + " 남음",
                    double.IsFinite(value) ? $"{Math.Clamp(value, 0, 100):0}%" : "확인 안 됨",
                    double.IsFinite(value) ? Math.Clamp(value, 0, 100) : 0, double.IsFinite(value), Reset(other) ?? "", false);
            }
        }
        return new(name, status, tone, quotaLabel, quotaText,
            hasQuota ? Math.Clamp(number, 0, 100) : 0, hasQuota,
            external ? "" : resetText, external ? "" : redeem,
            external ? profile.S("external_model_name", "외부 API 모델") : "", external,
            external ? "" : freshness, notice.Trim(), cache.Trim(), cacheTone,
            ProviderBadge: LocalModelPresentation.IsLocalProfile(profile) ? "로컬" : "API",
            State: profile.S("status"), Provider: provider, Short: ProfileBadge.Short(name), NoticeTone: noticeTone,
            Primary: primary, Secondary: secondary, Ssh: ssh, SshDetail: sshDetail);
    }

    private static double Percent(JsonElement window)
    {
        var value = window.Get("remaining_percent");
        if (value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var remaining)) return remaining;
        value = window.Get("used_percent");
        return value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var used) ? 100 - used : double.NaN;
    }

    private static string? Reset(JsonElement window)
    {
        var reset = window.Get("resets_at");
        if (reset.ValueKind != JsonValueKind.Number || !reset.TryGetInt64(out var seconds)) return null;
        try { return $"{DateTimeOffset.FromUnixTimeSeconds(seconds).ToLocalTime():M/d HH:mm} 초기화"; }
        catch (ArgumentOutOfRangeException) { return null; }
    }

    // Prepared SSH hosts of this profile: "SSH · remote-host +1".
    private static (string Chip, string Detail) SshHosts(JsonElement profile)
    {
        var hosts = profile.Arr("remote_bindings").Where(binding => binding.B("prepared"))
            .Select(binding => binding.S("alias")).Where(alias => alias.Length > 0).Distinct().ToArray();
        if (hosts.Length == 0) return ("", "");
        return ("SSH · " + ProfileBadge.Clip(hosts[0], 14) + (hosts.Length > 1 ? $" +{hosts.Length - 1}" : ""),
            "SSH 연결 · " + string.Join(", ", hosts));
    }
}
