using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// One task shortcut: which account opens it, whether that task is working or
// waiting right now, and how much of the account's usage is left (a ring).
// Record equality covers only the stored values, so a state poll re-renders a
// card only when one of them changed; brushes and geometry are derived.
internal sealed record ShortcutCardData(string Title, string Account, string AccountShort, string Host,
    string State, string StateText, double Outer, double Inner, string OuterLabel, string InnerLabel,
    bool External, string Agent, string Detail)
{
    public Visibility AgentVisibility => Agent.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
    // Ring geometry in a 40-DIP box: outer track radius 17.5, inner 12.5.
    private const double Center = 20, OuterRadius = 17.5, InnerRadius = 12.5;

    public Geometry OuterArc => Arc(Outer, OuterRadius);
    public Geometry InnerArc => Arc(Inner, InnerRadius);
    public Brush OuterBrush => QuotaBrush(Outer, Plenty);
    public Brush InnerBrush => QuotaBrush(Inner, PlentyInner);
    public Visibility InnerVisibility => double.IsFinite(Inner) ? Visibility.Visible : Visibility.Collapsed;
    public Brush StateBrush => State switch
    {
        "working" => Working, "waiting_approval" or "waiting_input" => Waiting, _ => Muted
    };
    public Brush PillBrush => State switch
    {
        "working" => WorkingPill, "waiting_approval" or "waiting_input" => WaitingPill, _ => MutedPill
    };

    private static readonly Brush Working = Freeze("#77C6A0"), Waiting = Freeze("#E5B773"), Muted = Freeze("#8E9BB2"),
        WorkingPill = Freeze("#1E3A2E"), WaitingPill = Freeze("#3A3020"), MutedPill = Freeze("#262B34"),
        Plenty = Freeze("#94A9F8"), PlentyInner = Freeze("#77C6A0"), Low = Freeze("#E5B773"),
        Critical = Freeze("#E57B73"), Api = Freeze("#6F7A8E");

    private static Brush Freeze(string color)
    {
        var brush = (SolidColorBrush)new BrushConverter().ConvertFromString(color)!;
        brush.Freeze();
        return brush;
    }

    private Brush QuotaBrush(double remaining, Brush plenty) => External || !double.IsFinite(remaining) ? Api
        : remaining < 10 ? Critical : remaining < 30 ? Low : plenty;

    private static Geometry Arc(double percent, double radius)
    {
        if (!double.IsFinite(percent) || percent <= 0.5) return Geometry.Empty;
        percent = Math.Min(percent, 99.9);
        double angle = percent / 100 * 2 * Math.PI;
        double x = Center + radius * Math.Sin(angle), y = Center - radius * Math.Cos(angle);
        var text = string.Create(CultureInfo.InvariantCulture,
            $"M {Center},{Center - radius} A {radius},{radius} 0 {(percent > 50 ? 1 : 0)} 1 {x:0.###},{y:0.###}");
        var geometry = Geometry.Parse(text);
        geometry.Freeze();
        return geometry;
    }

    internal static Dictionary<string, (string State, string Alias)> Activity(IEnumerable<JsonElement> profiles)
    {
        // Strongest state wins when two accounts report the same task.
        static int Rank(string state) => state switch { "waiting_approval" => 0, "waiting_input" => 1, "working" => 2, _ => 3 };
        var result = new Dictionary<string, (string, string)>(StringComparer.Ordinal);
        foreach (var profile in profiles)
        {
            var activity = profile.Get("thread_activity");
            if (activity.ValueKind != JsonValueKind.Object) continue;
            foreach (var entry in activity.EnumerateObject())
            {
                var state = entry.Value.ValueKind == JsonValueKind.String ? entry.Value.GetString() ?? "" : "";
                if (Rank(state) == 3) continue;
                if (!result.TryGetValue(entry.Name, out var current) || Rank(state) < Rank(current.Item1))
                    result[entry.Name] = (state, profile.S("alias"));
            }
        }
        return result;
    }

    internal static ShortcutCardData Create(JsonElement shortcut, JsonElement owner,
        IReadOnlyDictionary<string, (string State, string Alias)> activity, JsonElement[] models)
    {
        var alias = owner.ValueKind == JsonValueKind.Object ? owner.S("alias") : "";
        var claude = ClaudeProfilePresentation.IsClaude(owner);
        var external = claude || owner.S("auth_mode") == "external";
        var host = shortcut.S("host_id", "local");
        host = host == "local" ? "Windows" : host.StartsWith("ssh:", StringComparison.Ordinal) ? host[4..] : host;
        var observed = activity.TryGetValue(shortcut.S("thread_id"), out var live);
        var state = observed ? live.State : alias == "" ? "unassigned" : owner.S("status") == "running" ? "idle" : "closed";
        var stateText = state switch
        {
            "working" => "작업 중", "waiting_approval" => "승인 대기", "waiting_input" => "입력 대기",
            "idle" => "대기", "closed" => "계정 닫힘", _ => "계정 지정 필요"
        };
        var (outer, outerLabel, inner, innerLabel) = Quota(owner, external);
        var claudeUsage = claude ? ClaudeUsagePresentation.Read(owner) : null;
        if (claudeUsage is not null)
        {
            outer = claudeUsage.Weekly.HasValue && !claudeUsage.Weekly.Stale ? claudeUsage.Weekly.Remaining : double.NaN;
            inner = claudeUsage.FiveHour.HasValue && !claudeUsage.FiveHour.Stale ? claudeUsage.FiveHour.Remaining : double.NaN;
            outerLabel = "주간"; innerLabel = "5시간";
        }
        var runner = observed && live.Alias.Length > 0 && live.Alias != alias ? $" · {live.Alias}" : "";
        var usage = claudeUsage is not null ? claudeUsage.Summary : LocalModelPresentation.IsLocalProfile(owner) ? "로컬 서버 · 구독 사용량 해당 없음" : external ? "외부 API · 사용량 한도 없음"
            : double.IsFinite(outer) ? $"{outerLabel} {outer:0}% 남음" + (double.IsFinite(inner) ? $" · {innerLabel} {inner:0}% 남음" : "")
            : "사용량 확인 안 됨";
        var where = runner.Length > 0 ? $" ({live.Alias} 계정에서 실행 중)" : "";
        var (agent, agentDetail) = Agents(owner, external, models);
        var detail = $"{shortcut.S("alias", "이름 없는 작업")}\n{(alias == "" ? "계정 지정 필요" : alias + " 계정")} · {host}\n" +
                     $"{stateText}{where}\n{usage}\n{agentDetail}\n누르면 이 계정에서 열립니다. ⋯ 버튼으로 계정 이동·별칭 변경·링크 삭제.";
        return new(shortcut.S("alias", "이름 없는 작업"), alias == "" ? "계정 지정 필요" : alias,
            Short(alias), host, state, stateText + runner, outer, inner, outerLabel, innerLabel, external && !claude, agent, detail);
    }

    // Compact chip: the external model an API profile runs, or the saved
    // subagent models of a Codex account ("↳ deepseek-flash +1", "… 전용").
    private static (string Chip, string Detail) Agents(JsonElement owner, bool external, JsonElement[] models)
    {
        if (ClaudeProfilePresentation.IsClaude(owner)) return ("Claude · " + ClaudeProfilePresentation.Model(owner), ClaudeProfilePresentation.Detail(owner));
        if (external)
        {
            var model = owner.S("external_model_name", "외부 API");
            var local = LocalModelPresentation.IsLocalProfile(owner);
            return ((local ? "로컬 · " : "API · ") + Clip(model), (local ? "로컬 모델 프로필 · " : "외부 API 프로필 · ") + model);
        }
        var policy = owner.Get("policy");
        if (!policy.B("enabled")) return ("", "하위 에이전트 사용 안 함");
        var names = policy.Arr("model_ids").Select(id => id.ValueKind == JsonValueKind.String ? id.GetString() ?? "" : "")
            .Select(id => models.FirstOrDefault(m => m.S("id") == id) is { ValueKind: JsonValueKind.Object } m
                ? m.S("display_name", m.S("wire_model_id", "외부 모델")) : "외부 모델").ToArray();
        if (names.Length == 0) return ("", "하위 에이전트 · 모델 미선택");
        var only = policy.S("selection_mode", "automatic") == "external_only";
        var chip = "↳ " + Clip(names[0]) + (names.Length > 1 ? $" +{names.Length - 1}" : "") + (only ? " 전용" : "");
        return (chip, "하위 에이전트 · " + string.Join(", ", names) + (only ? " (외부 모델 전용)" : " (GPT와 함께 선택)"));
    }

    private static string Clip(string text) => text.Length <= 16 ? text : text[..15] + "…";

    private static string Short(string alias)
    {
        if (alias.Length == 0) return "?";
        // Three narrow characters fit inside the ring; wide (e.g. Hangul) ones only two.
        var limit = alias.Any(c => c > 0x2E7F) ? 2 : 3;
        var elements = StringInfo.GetTextElementEnumerator(alias);
        var text = "";
        while (elements.MoveNext() && new StringInfo(text).LengthInTextElements < limit) text += elements.GetTextElement();
        return text;
    }

    private static (double, string, double, string) Quota(JsonElement owner, bool external)
    {
        if (external) return (100, "", double.NaN, "");
        var windows = owner.Get("usage").Arr("windows").ToArray();
        static double Remaining(JsonElement window)
        {
            var value = window.Get("remaining_percent");
            if (value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var remaining)) return Math.Clamp(remaining, 0, 100);
            value = window.Get("used_percent");
            return value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var used) ? Math.Clamp(100 - used, 0, 100) : double.NaN;
        }
        static string Label(JsonElement window) => window.S("label", window.S("name", "사용량"));
        // The weekly window is the outer ring; a shorter window, when reported, is the inner one.
        if (windows.Length == 0) return (double.NaN, "", double.NaN, "");
        var index = Math.Max(0, Array.FindIndex(windows, w => Label(w) == "주간"));
        var primary = windows[index];
        var otherIndex = Array.FindIndex(windows, w => w.ValueKind == JsonValueKind.Object && Label(w) != Label(primary));
        return (Remaining(primary), Label(primary),
                otherIndex >= 0 && otherIndex != index ? Remaining(windows[otherIndex]) : double.NaN,
                otherIndex >= 0 && otherIndex != index ? Label(windows[otherIndex]) : "");
    }
}
