using System.Globalization;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

internal static partial class Dialogs
{
    private static readonly string[] EffortNames = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
    private static string EffortLabel(string value) => value switch
    {
        "none" => "끄기 · none", "low" => "낮음 · low", "medium" => "중간 · medium",
        "high" => "높음 · high", "max" => "최대 · max", _ => value
    };

    public static Dictionary<string, object>? ExternalModelSettings(Window owner, JsonElement model, JsonElement saved = default, string? profileAlias = null)
    {
        var local = LocalModelPresentation.IsLocalModel(model);
        var preset = model.Get("local_preset");
        var fullContext = local && preset.ValueKind == JsonValueKind.Object;
        var window = Create(owner, local ? "로컬 모델 기본 설정" : "외부 모델 기본 설정", 600, 720); var body = Body(window);
        body.Children.Add(new TextBlock { Text = model.S("display_name", model.S("wire_model_id")), FontSize = 22, Margin = new Thickness(0, 0, 0, 16) });
        if (profileAlias is not null) body.Children.Add(Note("설정 대상 프로필 · " + profileAlias));
        body.Children.Add(Note("이 프로필에서 직접 답변하는 모델의 기본값입니다. 현재 작업의 추론 강도를 표시하는 화면은 아닙니다."));
        var defaults = model.Get("settings_defaults");
        string Value(string key) => saved.Get(key).ValueKind is JsonValueKind.Undefined or JsonValueKind.Null ? defaults.Get(key).ToString() : saved.Get(key).ToString();
        var efforts = fullContext ? LocalModelPresentation.PresetEfforts(preset) : model.Arr("supported_reasoning_efforts").Select(x => x.GetString()!).ToArray();
        if (efforts.Length == 0) efforts = [model.S("reasoning_effort", "high")];
        var selectedEffort = Value("reasoning_effort");
        if (fullContext && !efforts.Contains(selectedEffort)) selectedEffort = LocalModelPresentation.NativeEffort(preset);
        var effort = Choices(body, "기본 추론 강도", efforts.Select(v => new Choice(v,
            local ? LocalModelPresentation.EffortLabel(v, preset) : EffortLabel(v))), selectedEffort);
        body.Children.Add(Note("채팅 입력창에서 강도를 직접 선택하면 해당 요청에 우선 적용됩니다. 채팅에서 바꾼 값이 이 기본값을 변경하지는 않습니다."));
        int maximum = fullContext ? LocalModelPresentation.MaximumContext(preset)
            : defaults.Get("context_window").ValueKind == JsonValueKind.Number && defaults.Get("context_window").TryGetInt32(out var limit) ? limit : 32768;
        var context = Field(body, fullContext ? "프리셋 최대 컨텍스트 · 토큰" : "컨텍스트 크기 · 토큰", fullContext ? maximum.ToString(CultureInfo.InvariantCulture) : Value("context_window"));
        context.IsReadOnly = fullContext;
        if (fullContext)
        {
            var specification = Note(LocalModelPresentation.PresetDetail(preset));
            specification.ToolTip = LocalModelPresentation.PresetReferences(preset);
            body.Children.Add(specification);
        }
        var compact = Field(body, "자동 압축 시작 · 컨텍스트의 %", Value("auto_compact_percent"));
        var preview = Note(""); body.Children.Add(preview);
        bool Read(out int tokens, out int percent)
        {
            percent = 0;
            return LocalModelPresentation.ReadContext(context.Text, maximum, fullContext, out tokens)
                && int.TryParse(compact.Text, out percent) && percent >= 10 && percent <= 90;
        }
        void Update()
        {
            preview.Text = Read(out int tokens, out int percent)
                ? $"약 {(long)tokens * percent / 100:N0} 토큰에서 자동 압축\n모델 한도 {maximum:N0} 토큰 · 선택한 컨텍스트 {tokens:N0} 토큰"
                : $"컨텍스트 4,096~{maximum:N0} 토큰, 자동 압축 10~90%를 입력하세요.";
        }
        context.TextChanged += (_, _) => Update(); compact.TextChanged += (_, _) => Update(); Update();
        body.Children.Add(Note("85%처럼 기준을 낮추면 더 일찍 압축합니다. Codex 런타임 상한은 90%이며 실제 압축은 요청 경계에서 수행됩니다. 압축 기준은 모델의 컨텍스트 한도를 낮추지 않습니다."));
        Dictionary<string, object>? result = null;
        Button(body, "프로필 기본값 사용", () =>
        {
            if (!Read(out int tokens, out int percent)) { Update(); return; }
            result = new() { ["reasoning_effort"] = ((Choice)effort.SelectedItem).Id, ["context_window"] = tokens, ["auto_compact_percent"] = percent };
            window.DialogResult = true;
        });
        Button(body, "취소", window.Close); window.ShowDialog(); return result;
    }
}
