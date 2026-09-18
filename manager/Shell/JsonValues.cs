using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class JsonValues
{
    public static JsonElement Get(this JsonElement value, string name) => value.ValueKind == JsonValueKind.Object && value.TryGetProperty(name, out var result) ? result : default;
    public static string S(this JsonElement value, string name, string fallback = "")
    {
        var item = value.Get(name);
        return item.ValueKind == JsonValueKind.String ? item.GetString() ?? fallback : item.ValueKind == JsonValueKind.Number ? item.ToString() : fallback;
    }
    public static bool B(this JsonElement value, string name) => value.Get(name).ValueKind == JsonValueKind.True;
    public static long N(this JsonElement value, string name) => long.TryParse(value.S(name), out var number) ? number : 0;
    public static IEnumerable<JsonElement> Arr(this JsonElement value, string name) => value.Get(name).Items();
    public static IEnumerable<JsonElement> Items(this JsonElement value) => value.ValueKind == JsonValueKind.Array ? value.EnumerateArray().ToArray() : [];
    public static string Message(this JsonElement value, string fallback = "완료했습니다.") => value.S("message", value.S("summary", fallback));
}

internal sealed record Choice(string Id, string Label, JsonElement Data = default)
{
    public override string ToString() => Label;
    public string AgentBadge => ProfileAgentPresentation.Badge(Data);
    public string AgentHint => ProfileAgentPresentation.Hint(Data);
    public System.Windows.Visibility AgentBadgeVisibility => AgentBadge.Length > 0 ? System.Windows.Visibility.Visible : System.Windows.Visibility.Collapsed;
    public string Hint
    {
        get
        {
            if (Data.S("thread_id") != "") return "작업 이름을 누르면 표시된 계정에서 열립니다.\n계정 이동·별칭 변경·링크 삭제는 아래 버튼을 사용하세요.";
            var usage = Data.Get("usage");
            var observed = usage.S("observed_at");
            if (DateTimeOffset.TryParse(observed, out var time)) return $"사용량 마지막 확인: {time.ToLocalTime():yyyy-MM-dd HH:mm:ss}\n{Data.S("status_message")}";
            return Data.S("status_message", Data.S("source_store_id", Label));
        }
    }
}
