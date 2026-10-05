using System.Globalization;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// Short identity for a profile: the avatar label in the rail and shortcut rings
// ("01", "Cla", "개인"), the badge on a task card ("01 개인 개발") and the
// provider family that selects the avatar tint.
internal static class ProfileBadge
{
    internal static string Provider(JsonElement profile) =>
        ClaudeProfilePresentation.IsClaude(profile) ? "claude"
        : profile.S("auth_mode") == "external" ? LocalModelPresentation.IsLocalProfile(profile) ? "local" : "api"
        : "gpt";

    internal static string ProviderLabel(string provider) => provider switch
    {
        "claude" => "Claude", "api" => "API", "local" => "로컬", _ => "GPT"
    };

    // A leading profile number wins; otherwise the first word, two wide
    // (Hangul) or three narrow characters, so the label fits a 34-DIP circle.
    internal static string Short(string alias)
    {
        alias = alias.Trim();
        if (alias.Length == 0) return "?";
        var digits = new string(alias.TakeWhile(char.IsAsciiDigit).Take(3).ToArray());
        if (digits.Length > 0) return digits;
        var word = Segments(alias).FirstOrDefault() ?? alias;
        word = word.Split(' ', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault() ?? word;
        var limit = word.Any(c => c > 0x2E7F) ? 2 : 3;
        var elements = StringInfo.GetTextElementEnumerator(word);
        var text = "";
        while (elements.MoveNext() && new StringInfo(text).LengthInTextElements < limit) text += elements.GetTextElement();
        return text.Length == 0 ? "?" : char.ToUpper(text[0], CultureInfo.CurrentCulture) + text[1..];
    }

    // "01 · 개인 개발 · 설명" -> "01 개인 개발"; other names are clipped whole.
    internal static string Name(string alias)
    {
        alias = alias.Trim();
        if (alias.Length == 0) return "계정 지정 필요";
        var parts = Segments(alias).ToArray();
        var text = parts.Length > 1 && parts[0].All(char.IsAsciiDigit) ? parts[0] + " " + parts[1] : parts.FirstOrDefault() ?? alias;
        return Clip(text, 14);
    }

    internal static string Clip(string text, int limit) => new StringInfo(text).LengthInTextElements <= limit
        ? text : new StringInfo(text).SubstringByTextElements(0, limit - 1).TrimEnd() + "…";

    private static IEnumerable<string> Segments(string alias) => alias
        .Split(['·', '|', '/'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
        .Where(part => part.Length > 0);
}
