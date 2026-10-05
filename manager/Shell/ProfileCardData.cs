using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// One usage window: "주간 남음 97%", its reset time and whether the value is
// old. Short/Value are the glanceable form ("주간" "97%"); Text, ResetText and
// Detail are for tooltips. Weekly windows draw the outer ring, shorter ones
// (5 hours) the inner ring; both turn amber below 25% and red below 10%.
internal sealed record UsageMeter(string Label, string Text, double Remaining, bool HasValue, string ResetText, bool Stale, bool Weekly = true)
{
    public string Short => Label.EndsWith(" 남음", StringComparison.Ordinal) ? Label[..^3] : Label;
    public string Value => HasValue ? $"{Remaining:0.#}%" : "미확인";
    public Visibility MeterVisibility => HasValue ? Visibility.Visible : Visibility.Collapsed;
    public Visibility ResetVisibility => ResetText.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    // A stale value keeps its number, dimmed, with a clock icon instead of words.
    public Visibility StaleVisibility => HasValue && Stale ? Visibility.Visible : Visibility.Collapsed;
    public Brush ValueBrush => !HasValue ? ProfileCardData.FaintBrush : Stale ? ProfileCardData.StaleBrush : ProfileCardData.QuotaTone(Remaining, Weekly);
    public Brush RingBrush => ValueBrush;
    // Gauge arc in a 48-DIP box with a 5-DIP stroke (centre 24, radius 21.5).
    public Geometry GaugeArc => HasValue ? ProfileCardData.Arc(Remaining, 24, 21.5) : Geometry.Empty;
    public string Detail => string.Join(" · ", new[] { $"{Label} {Text}", ResetText }.Where(value => value.Length > 0));

    internal static UsageMeter From(ClaudeUsageWindow window, bool weekly) =>
        new(window.Label, window.Text, window.Remaining, window.HasValue, window.ResetText, window.Stale, weekly);
}

// Separate account identity, quota and runtime state instead of wrapping a
// single sentence. This value also participates in list refresh comparisons,
// so it holds only values (no collections); brushes and geometry are derived.
//
// What the cards show inline is deliberately small: name, one status pill
// (or a transient state such as "여는 중"), the usage values, one provider
// chip and at most one short warning. Everything else (resets, redeem count,
// subscription, long provider messages, SSH hosts, last check) is tooltip-only.
internal sealed record ProfileCardData(string Name, string Status, string StatusTone,
    string QuotaLabel, string QuotaText, double Remaining, bool HasQuota,
    string ResetText, string RedeemText, string Model, bool External,
    string Freshness, string Notice, string Cache = "", string CacheTone = "",
    string ProviderBadge = "API", string Account = "", string UsageHint = "",
    ClaudeUsageWindow? ClaudeFiveHour = null, ClaudeUsageWindow? ClaudeWeekly = null, string Email = "",
    string State = "", string Provider = "gpt", string Short = "?", string NoticeTone = "",
    UsageMeter? Primary = null, UsageMeter? Secondary = null, string Ssh = "", string SshDetail = "",
    string Warning = "", string WarningDetail = "", string Transient = "")
{
    public Visibility QuotaVisibility => HasQuota ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NativeVisibility => External || QuotaLabel.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    public Visibility ExternalVisibility => External ? Visibility.Visible : Visibility.Collapsed;
    public Visibility FreshnessVisibility => Freshness.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NoticeVisibility => Notice.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility AccountVisibility => Account.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility EmailVisibility => Email.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility UsageHintVisibility => UsageHint.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    // In the tooltip the hint appears once: as the warning explanation, or here.
    public Visibility UsageHintTipVisibility => UsageHint.Length > 0 && UsageHint != WarningDetail ? Visibility.Visible : Visibility.Collapsed;
    public Visibility ClaudeUsageVisibility => ClaudeFiveHour is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility ModelVisibility => Model.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility SshVisibility => Ssh.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility PrimaryVisibility => Primary is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility SecondaryVisibility => Secondary is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility UsageVisibility => Primary is null && Secondary is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility WarningVisibility => Warning.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    // The header shows the warning beside its gauges, or alone when there are none.
    public Visibility WarningOnlyVisibility => Warning.Length > 0 && Primary is null && Secondary is null ? Visibility.Visible : Visibility.Collapsed;
    // Reset, redeem and freshness of a GPT account; Claude meters carry their own reset.
    public Visibility ResetLineVisibility => NativeVisibility;
    // Prompt-cache line for the selected task (ProfileCacheLine, cache_warmth.py).
    public Visibility CacheVisibility => Cache.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Brush CacheBrush => CacheTone switch { "ready" => ReadyBrush, "warning" => WarningBrush, _ => CacheMutedBrush };
    // The short card form of the cache line; the full estimate is in the tooltip.
    public string CacheChip => Cache.Length == 0 ? "" : CacheTone == "ready" ? "캐시 있음" : CacheTone == "warning" ? "캐시 없음" : "";
    public Visibility CacheChipVisibility => CacheChip.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Brush CacheChipBrush => CacheTone == "ready" ? ReadyBrush : MutedBrush;
    public Brush StatusBrush => StatusTone switch { "ready" => ReadyBrush, "warning" => WarningBrush, _ => MutedBrush };
    public Brush StatusPillBrush => StatusTone switch { "ready" => ReadyPill, "warning" => WarningPill, _ => MutedPill };
    // The pill shows a transient state ("여는 중") in place of the steady status.
    public string Pill => Transient.Length > 0 ? Transient : Status;
    public Brush PillBrush => Transient.Length > 0 ? TransientBrush : StatusBrush;
    public Brush PillBackground => Transient.Length > 0 ? TransientPill : StatusPillBrush;
    public Brush QuotaBrush => Remaining <= 10 ? WarningBrush : AccentBrush;
    public Brush NoticeBrush => NoticeTone == "warning" ? WarningBrush : CacheMutedBrush;
    public Brush NoticePillBrush => NoticeTone == "warning" ? WarningPill : MutedPill;
    public string ProviderLabel => ProfileBadge.ProviderLabel(Provider);
    // One chip for provider and model: "GPT", "Claude · Opus 5.5", "API · Example Model".
    public string ProviderModel => Model.Length == 0 || Provider == "gpt" ? ProviderLabel : ProviderLabel + " · " + ProfileBadge.Clip(Model, 22);
    public Brush TintBrush => WorkspaceAppearance.Tint(Provider).Fill;
    public Brush TintTextBrush => WorkspaceAppearance.Tint(Provider).Text;

    // Rail status dot: attention first, then a transient state, then running
    // (solid) or ready (hollow).
    private bool Attention => StatusTone == "warning" || Warning.Length > 0;
    public Brush DotBrush => Attention ? WarningBrush : Transient.Length > 0 ? TransientBrush : StatusTone == "ready" ? ReadyBrush : DotMutedBrush;
    public Brush DotFill => Attention || Transient.Length > 0 || State != "ready" ? DotBrush : Brushes.Transparent;

    // Avatar rings in a 48-DIP box: outer = weekly (or the only known window),
    // inner = the 5-hour window when both are known. The disc shrinks to fit.
    private UsageMeter? OuterMeter => Primary is { HasValue: true } ? Primary : Secondary is { HasValue: true } ? Secondary : null;
    private UsageMeter? InnerMeter => Primary is { HasValue: true } && Secondary is { HasValue: true } ? Secondary : null;
    public int RingCount => OuterMeter is null ? 0 : InnerMeter is null ? 1 : 2;
    public Visibility RingVisibility => OuterMeter is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility InnerRingVisibility => InnerMeter is null ? Visibility.Collapsed : Visibility.Visible;
    public Geometry RingArc => OuterMeter is { } meter ? Arc(meter.Remaining, 24, 22.5) : Geometry.Empty;
    public Brush RingBrush => OuterMeter?.RingBrush ?? FaintBrush;
    public Geometry InnerRingArc => InnerMeter is { } meter ? Arc(meter.Remaining, 24, 18.25) : Geometry.Empty;
    public Brush InnerRingBrush => InnerMeter?.RingBrush ?? FaintBrush;
    public double DiscSize => RingCount switch { 0 => 44, 1 => 37, _ => 31.5 };

    public string UsageSummary => Primary is null && Secondary is null
        ? External && Provider != "claude" ? Model : ""
        : string.Join(" · ", new[] { Primary, Secondary }.OfType<UsageMeter>().Select(meter => $"{meter.Short} {meter.Value}"));
    public string AccessibleName => string.Join(", ", new[] { Name, Pill, ProviderModel, UsageSummary, Warning }.Where(value => value.Length > 0));
    public string DetailHint => string.Join("\n", new[] { Name, Status, Account, ClaudeFiveHour?.Detail, ClaudeWeekly?.Detail, UsageHint, QuotaLabel + " " + QuotaText, ResetText, RedeemText, Freshness, Notice, Cache }.Where(v => !string.IsNullOrWhiteSpace(v)));
    // Tooltip-only lines.
    public string SubscriptionText => Account.Length > 0 ? "구독 · " + Account : "";
    public Visibility SubscriptionVisibility => Account.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public string RedeemLine => string.Join(" · ", new[] { RedeemText, Freshness }.Where(value => value.Length > 0));
    public Visibility RedeemLineVisibility => RedeemLine.Length > 0 && !External ? Visibility.Visible : Visibility.Collapsed;
    public string NoticeDetail => WarningDetail.Length > 0 ? WarningDetail : Notice;
    public Visibility NoticeDetailVisibility => NoticeDetail.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    public Brush NoticeDetailBrush => Warning.Length > 0 || NoticeTone == "warning" ? WarningBrush : MutedBrush;

    internal static readonly Brush ReadyBrush = Brush("#77C6A0"), WarningBrush = Brush("#E5B773"), CriticalBrush = Brush("#E57B73"),
        MutedBrush = Brush("#9BA6B8"), AccentBrush = Brush("#94A9F8"), InnerBrush = Brush("#77C6A0"), CacheMutedBrush = Brush("#8E9BB2"),
        FaintBrush = Brush("#8E9BB2"), StaleBrush = Brush("#6F7A8E"), TransientBrush = Brush("#A9B8DC"),
        ReadyPill = Brush("#1E3A2E"), WarningPill = Brush("#3A3020"), MutedPill = Brush("#2A2E37"), TransientPill = Brush("#262C3B"),
        DotMutedBrush = Brush("#6F7A8E");
    private static Brush Brush(string color) { var brush = (SolidColorBrush)new BrushConverter().ConvertFromString(color)!; brush.Freeze(); return brush; }
    internal static Brush QuotaTone(double remaining, bool weekly = true) =>
        remaining < 10 ? CriticalBrush : remaining < 25 ? WarningBrush : weekly ? AccentBrush : InnerBrush;

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

    // noticeTone: "warning" puts noticeShort on the single warning chip,
    // "transient" puts it on the status pill; the full notice stays in the tooltip.
    internal static ProfileCardData Create(JsonElement profile, string fallback, string notice = "", string cache = "", string cacheTone = "",
        string noticeTone = "", string noticeShort = "")
    {
        var name = profile.S("alias", fallback.Split('\n')[0]);
        var provider = ProfileBadge.Provider(profile);
        var (ssh, sshDetail) = SshHosts(profile);
        notice = notice.Trim();
        var warning = noticeTone == "warning" ? noticeShort : "";
        var warningDetail = warning.Length > 0 ? notice : "";
        var transient = noticeTone == "transient" ? noticeShort : "";
        if (ClaudeProfilePresentation.IsClaude(profile))
        {
            var claude = ClaudeProfilePresentation.Status(profile);
            var claudeUsage = ClaudeUsagePresentation.Read(profile);
            // A failed or old usage read is one short chip; its explanation is the tooltip.
            var usageProblem = profile.Get("usage").S("provider") == "claude_code"
                && (claudeUsage.FiveHour.Stale || claudeUsage.Weekly.Stale || profile.Get("usage").Get("error").ValueKind == JsonValueKind.Object);
            if (warning.Length == 0 && usageProblem) { warning = "사용량 확인 필요"; warningDetail = claudeUsage.Hint; }
            return new(name, ClaudeProfilePresentation.Label(claude), ClaudeProfilePresentation.Tone(claude),
                "", "", 0, false, "", "", ClaudeProfilePresentation.Model(profile), true, "", notice,
                ProviderBadge: "Claude", Account: ClaudeProfilePresentation.Account(claude), UsageHint: claudeUsage.Hint,
                ClaudeFiveHour: claudeUsage.FiveHour, ClaudeWeekly: claudeUsage.Weekly,
                State: profile.S("status"), Provider: provider, Short: ProfileBadge.Short(name), NoticeTone: noticeTone,
                Primary: UsageMeter.From(claudeUsage.Weekly, weekly: true), Secondary: UsageMeter.From(claudeUsage.FiveHour, weekly: false),
                Ssh: ssh, SshDetail: sshDetail, Warning: warning, WarningDetail: warningDetail, Transient: transient);
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
        var stale = freshness.Length > 0 && !usage.B("refreshing");
        var quotaLabel = external || profile.ValueKind != JsonValueKind.Object ? "" : hasQuota ? window.S("label", window.S("name", "사용량")) + " 남음" : "사용량";
        var quotaText = external ? "" : hasQuota ? $"{Math.Clamp(number, 0, 100):0}%" : "확인 안 됨";
        UsageMeter? primary = null, secondary = null;
        if (quotaLabel.Length > 0)
        {
            static bool IsWeekly(JsonElement w) => w.S("label", w.S("name")) == "주간";
            primary = new(quotaLabel, quotaText + (hasQuota && stale ? " · " + freshness : ""), hasQuota ? Math.Clamp(number, 0, 100) : 0, hasQuota,
                hasQuota ? resetText : "", stale, IsWeekly(window) || !hasQuota);
            var other = windows.FirstOrDefault(w => w.ValueKind == JsonValueKind.Object
                && w.S("label", w.S("name")) != window.S("label", window.S("name")));
            if (other.ValueKind == JsonValueKind.Object)
            {
                var value = Percent(other);
                secondary = new(other.S("label", other.S("name", "사용량")) + " 남음",
                    double.IsFinite(value) ? $"{Math.Clamp(value, 0, 100):0}%" + (stale ? " · " + freshness : "") : "확인 안 됨",
                    double.IsFinite(value) ? Math.Clamp(value, 0, 100) : 0, double.IsFinite(value), Reset(other) ?? "", stale, IsWeekly(other));
            }
        }
        return new(name, status, tone, quotaLabel, quotaText,
            hasQuota ? Math.Clamp(number, 0, 100) : 0, hasQuota,
            external ? "" : resetText, external ? "" : redeem,
            external ? profile.S("external_model_name", "외부 API 모델") : "", external,
            external ? "" : freshness, notice, cache.Trim(), cacheTone,
            ProviderBadge: LocalModelPresentation.IsLocalProfile(profile) ? "로컬" : "API",
            State: profile.S("status"), Provider: provider, Short: ProfileBadge.Short(name), NoticeTone: noticeTone,
            Primary: primary, Secondary: secondary, Ssh: ssh, SshDetail: sshDetail,
            Warning: warning, WarningDetail: warningDetail, Transient: transient);
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

    // Prepared SSH hosts of this profile, for the tooltip only.
    private static (string Chip, string Detail) SshHosts(JsonElement profile)
    {
        var hosts = profile.Arr("remote_bindings").Where(binding => binding.B("prepared"))
            .Select(binding => binding.S("alias")).Where(alias => alias.Length > 0).Distinct().ToArray();
        if (hosts.Length == 0) return ("", "");
        return ("SSH · " + ProfileBadge.Clip(hosts[0], 14) + (hosts.Length > 1 ? $" +{hosts.Length - 1}" : ""),
            "SSH 연결 · " + string.Join(", ", hosts));
    }
}
