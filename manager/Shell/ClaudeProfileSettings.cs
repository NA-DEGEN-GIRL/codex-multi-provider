using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

internal sealed record ClaudeSettingsEditor(ComboBox Model, ComboBox Effort, ComboBox ContextMode, TextBox Context,
    ComboBox CompactMode, TextBox Compact, TextBlock Preview, Func<Dictionary<string, object>?> Read);

internal static partial class Dialogs
{
    internal static bool ReadClaudeContext(string contextMode, string context, string compactMode, string compact,
        out int? tokens, out int? percent)
    {
        tokens = null; percent = null;
        if (contextMode is not ("auto" or "custom") || compactMode is not ("auto" or "custom")) return false;
        if (contextMode == "custom")
        {
            if (!int.TryParse(context.Replace(",", ""), NumberStyles.Integer, CultureInfo.InvariantCulture, out var value)
                || value is < 100000 or > 1000000) return false;
            tokens = value;
        }
        if (compactMode == "custom")
        {
            if (!int.TryParse(compact, NumberStyles.Integer, CultureInfo.InvariantCulture, out var value)
                || value is < 50 or > 95) return false;
            percent = value;
            if ((long)(tokens ?? 1000000) * value / 100 < 100000) return false;
        }
        return true;
    }

    internal static ClaudeSettingsEditor AddClaudeSettingsEditor(Panel body, JsonElement saved)
    {
        var model = Choices(body, "기본 모델", [new("opus", "Opus · 현재 기본 버전"), new("sonnet", "Sonnet · 현재 기본 버전"),
            new("fable", "Fable · 현재 기본 버전"), new("claude-opus-5-5", "Opus 5.5")], saved.S("model", "opus"));
        var effort = Choices(body, "기본 추론 강도", new[] { "low", "medium", "high", "xhigh", "max", "ultracode" }
            .Select(value => new Choice(value, value switch
            {
                "xhigh" => "매우 높음 · xhigh", "ultracode" => "UltraCode · xhigh + 동적 작업 흐름", _ => EffortLabel(value)
            })), saved.S("reasoning_effort", saved.S("effort", "high")));
        body.Children.Add(Note("이 값은 프로필의 기본값입니다. 저장 후 프로필을 다시 열면 작업 입력창에서 모델과 추론 강도를 바꿀 수 있습니다. 작업에서 선택한 값은 이 기본값을 변경하지 않습니다."));
        body.Children.Add(Note("UltraCode를 사용하려면 추론 강도에서 선택하세요. xhigh 추론과 동적 작업 흐름을 함께 사용하며, 실제 지원은 모델·계정·조직 정책에 따릅니다."));
        static string Mode(JsonElement value) => value.ValueKind is JsonValueKind.Undefined or JsonValueKind.Null ? "auto" : "custom";
        var contextMode = Choices(body, "컨텍스트", [new("auto", "자동 · Claude 기본"), new("custom", "직접 설정")], Mode(saved.Get("context_window")));
        var context = Field(body, "직접 설정할 컨텍스트 · 토큰", saved.S("context_window", "1000000"));
        var compactMode = Choices(body, "자동 압축 시작", [new("auto", "자동 · Claude 기본"), new("custom", "직접 설정")], Mode(saved.Get("auto_compact_percent")));
        var compact = Field(body, "직접 설정할 압축 시작 · 컨텍스트의 %", saved.S("auto_compact_percent", "85"));
        var preview = Note(""); body.Children.Add(preview);
        string Selected(ComboBox combo) => ((Choice)combo.SelectedItem).Id;
        void Update()
        {
            context.IsEnabled = Selected(contextMode) == "custom";
            compact.IsEnabled = Selected(compactMode) == "custom";
            if (!ReadClaudeContext(Selected(contextMode), context.Text, Selected(compactMode), compact.Text, out var tokens, out var percent))
            {
                preview.Text = tokens is >= 100000 and <= 1000000 && percent is >= 50 and <= 95
                    ? $"현재 압축 비율에서는 컨텍스트를 {(int)Math.Ceiling(10000000d / percent.Value):N0} 토큰 이상으로 설정하세요."
                    : "직접 설정: 컨텍스트 100,000~1,000,000 토큰, 자동 압축 50~95%를 입력하세요.";
                return;
            }
            var contextText = tokens is { } custom ? $"컨텍스트 {custom:N0} 토큰"
                : "컨텍스트는 Claude 기본값 · 선택한 모델 기준 최대 1,000,000 토큰";
            var compactText = percent is { } selected
                ? $"약 {(long)(tokens ?? 1000000) * selected / 100:N0} 토큰에서 자동 압축" + (tokens is null ? " · 현재 기본 컨텍스트 1,000,000 토큰 기준" : "")
                : "자동 압축은 Claude의 기본 시점에 따릅니다. 고정 비율을 적용하지 않습니다.";
            preview.Text = contextText + "\n" + compactText + "\n실제 컨텍스트는 모델과 계정 한도에 따릅니다.";
        }
        contextMode.SelectionChanged += (_, _) => Update(); compactMode.SelectionChanged += (_, _) => Update();
        context.TextChanged += (_, _) => Update(); compact.TextChanged += (_, _) => Update(); Update();
        Dictionary<string, object>? Read()
        {
            if (!ReadClaudeContext(Selected(contextMode), context.Text, Selected(compactMode), compact.Text, out var tokens, out var percent))
            { Update(); if (context.IsEnabled) context.Focus(); else compact.Focus(); return null; }
            // JSON null means use the native CLI default, independently for each setting.
            return new() { ["model"] = Selected(model), ["reasoning_effort"] = Selected(effort),
                ["context_window"] = tokens.HasValue ? tokens.Value : null!, ["auto_compact_percent"] = percent.HasValue ? percent.Value : null! };
        }
        return new(model, effort, contextMode, context, compactMode, compact, preview, Read);
    }

    internal static Dictionary<string, object>? ClaudeProfileSettings(Window owner, JsonElement profile = default,
        Func<string, Task<JsonElement>>? authenticate = null, Func<string, object, Task<JsonElement>>? request = null)
    {
        var window = Create(owner, "Claude 프로필 · 로그인과 기본 설정", 610, 840);
        var body = Body(window);
        body.Children.Add(new TextBlock { Text = profile.S("alias", "Claude 프로필"), FontSize = 22, Margin = new Thickness(0, 0, 0, 16) });
        body.Children.Add(Note("이 PC에 설치된 Claude Code CLI로 실행합니다. 로그인은 Claude Code가 여는 콘솔과 브라우저에서 완료합니다."));
        ClaudeLongLivedTokenPanel? longLived = null;
        if (authenticate is not null)
        {
            var status = Note(""); body.Children.Add(status);
            void Present(JsonElement value)
            {
                status.Text = string.Join("\n", new[] { ClaudeProfilePresentation.Label(value), ClaudeProfilePresentation.Account(value),
                    value.Message(""), value.S("cli_version") is { Length: > 0 } version ? "Claude Code CLI " + version : "" }.Where(value => value.Length > 0));
                longLived?.UpdateLogin(value);
            }
            Present(ClaudeProfilePresentation.Status(profile));
            var actions = new StackPanel { Orientation = Orientation.Horizontal }; body.Children.Add(actions);
            AsyncButton(window, actions, "Claude 로그인", async () => Present(await authenticate("claude.login")));
            AsyncButton(window, actions, "로그인 상태 확인", async () => Present(await authenticate("claude.status")));
            var usage = Note(ClaudeUsagePresentation.Read(profile).Summary); body.Children.Add(usage);
            var setup = AsyncButton(window, body, "Claude 첫 실행 설정", async () =>
                status.Text = (await authenticate("claude.setup")).Message("공식 Claude Code 창에서 첫 설정을 마친 후 사용량을 새로 확인하세요."));
            setup.ToolTip = "공식 Claude Code 창에서 첫 설정을 마친 후 ‘사용량 새로 확인’을 누르세요.";
            setup.Visibility = profile.Get("usage").Get("error").S("code") == "onboarding_required" ? Visibility.Visible : Visibility.Collapsed;
            AsyncButton(window, body, "사용량 새로 확인", async () =>
            {
                var updated = await authenticate("claude.usage");
                usage.Text = ClaudeUsagePresentation.Read(JsonSerializer.SerializeToElement(new { usage = updated })).Summary;
                setup.Visibility = updated.Get("error").S("code") == "onboarding_required" ? Visibility.Visible : Visibility.Collapsed;
            });
            if (request is not null) longLived = new ClaudeLongLivedTokenPanel(window, body, profile, request);
        }
        var fields = AddClaudeSettingsEditor(body, profile.Get("claude_settings"));
        body.Children.Add(Note("같은 계정에서 Claude 모델·추론 강도를 바꿔도 기존 세션을 이어갑니다. 계정마다 세션은 분리되며, 다른 제공자로 전환할 때는 공유 기록과 요약을 전달합니다. 계정 사이의 서버 캐시는 공유되지 않습니다."));
        body.Children.Add(Note("압축 후 다른 계정·모델에 넘길 공통 요약을 만들 때 Claude 사용량이 추가될 수 있습니다."));
        Dictionary<string, object>? result = null;
        Button(body, authenticate is null ? "이 설정으로 계속" : "기본 설정 저장", () =>
        {
            if (longLived is not null && !longLived.ConfirmDiscard()) return;
            result = fields.Read();
            if (result is not null) window.DialogResult = true;
        });
        Button(body, "닫기", () => { if (longLived is null || longLived.ConfirmDiscard()) window.Close(); });
        // The title bar close button gets the same unsaved-token check.
        window.Closing += (_, e) => { if (longLived is not null && !longLived.ConfirmDiscard()) e.Cancel = true; };
        window.ShowDialog();
        longLived?.Token.Clear();
        return result;
    }
}
