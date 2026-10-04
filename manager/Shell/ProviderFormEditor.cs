using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

internal static partial class Dialogs
{
    // Built separately from the modal window so defaults and outgoing values
    // can be checked without contacting a server or displaying a dialog.
    internal sealed class ProviderEditor
    {
        internal readonly CheckBox UseExisting;
        internal readonly ComboBox Deployment, Presets, Auth, Scope, Protocol, Effort;
        internal readonly TextBox Name, Url, ModelId, Display, Supported, Context, Compact;
        private readonly Choice? _existing;
        private readonly JsonElement _model;
        private readonly StackPanel _presetFields = new();
        private readonly TextBlock _detail;
        private bool _updating;
        internal JsonElement SelectedPreset => Local && Presets.SelectedItem is Choice choice ? choice.Data : default;
        private bool Local => (Deployment.SelectedItem as Choice)?.Id == "local";

        internal ProviderEditor(Panel body, Choice? existing, JsonElement model, JsonElement registry, bool localFirst = false)
        {
            _existing = existing; _model = model;
            UseExisting = new CheckBox { Content = existing is null ? "새 공급자 등록" : "기존 공급자에 모델 추가 · " + existing.Label,
                IsChecked = existing is not null, IsEnabled = existing is not null && model.S("id") == "" };
            body.Children.Add(UseExisting);
            Deployment = Choices(body, "연결 위치", [new("cloud", "클라우드 API"), new("local", "로컬 모델 서버")],
                existing?.Data.S("deployment", "cloud") ?? (localFirst ? "local" : "cloud"));
            body.Children.Add(_presetFields);
            var presets = registry.Arr("local_presets").Select(preset => new Choice(preset.S("id"), preset.S("display_name"), preset)).ToArray();
            Presets = Choices(_presetFields, "로컬 모델 프리셋", new[] { new Choice("", "직접 입력") }.Concat(presets),
                model.S("local_preset_id", Local ? presets.FirstOrDefault()?.Id ?? "" : ""));
            _detail = Note(""); _presetFields.Children.Add(_detail);
            Name = Field(body, "공급자 이름", existing?.Data.S("name") ?? "");
            Url = Field(body, "서버 API 기본 주소 · 로컬 HTTP/HTTPS, 클라우드 HTTPS", existing?.Data.S("base_url") ?? "");
            Url.ToolTip = "실행 중인 서버의 API 기본 주소를 직접 입력하세요. 서버를 자동 설치하거나 시작하지 않습니다.";
            Protocol = Choices(body, "API 형식", [new("responses", "OpenAI Responses"),
                new("chat_completions", "Chat Completions · 아직 지원 안 됨"), new("anthropic_messages", "Anthropic Messages · 아직 지원 안 됨")],
                existing?.Data.S("protocol", "responses"));
            Auth = Choices(body, "인증", [new("none", "API 키 없이 연결"), new("api_key", "API 키 사용")],
                existing?.Data.S("auth_type", "api_key") ?? (Local ? "none" : "api_key"));
            Scope = Choices(body, "사용할 실행 위치", [new("local", "이 Windows에서만 사용"), new("all_hosts", "Windows와 SSH 모두 · 각 호스트에서 서버에 연결 가능")],
                existing?.Data.S("execution_scope", Local ? "local" : "all_hosts") ?? (Local ? "local" : "all_hosts"));
            body.Children.Add(Note("Windows 전용 연결은 SSH 작업에 전달하지 않습니다. 모든 호스트를 선택하려면 각 SSH 호스트에서도 같은 주소로 연결할 수 있어야 합니다."));
            ModelId = Field(body, "서버가 실제 제공하는 정확한 모델 ID", model.S("wire_model_id"));
            Display = Field(body, "목록에 표시할 모델 이름", model.S("display_name"));
            Effort = Choices(body, "기본 추론 강도", EffortNames.Select(value => new Choice(value, EffortLabel(value))), model.S("reasoning_effort", "high"));
            Supported = Field(body, "지원하는 강도 · 쉼표로 구분", string.Join(",", model.Arr("supported_reasoning_efforts").Select(value => value.GetString())));
            Context = Field(body, "모델 컨텍스트 한도 · 토큰", model.Get("settings_defaults").Get("context_window").ToString());
            Compact = Field(body, "자동 압축 기본 비율 · 10~90%", model.Get("settings_defaults").Get("auto_compact_percent").ToString());
            if (Compact.Text == "") Compact.Text = "90";
            Deployment.SelectionChanged += (_, _) => UpdateConnection(changed: true);
            UseExisting.Checked += (_, _) => UpdateConnection(changed: false);
            UseExisting.Unchecked += (_, _) => UpdateConnection(changed: false);
            Presets.SelectionChanged += (_, _) => ApplyPreset(initial: false);
            UpdateConnection(changed: false);
            ApplyPreset(initial: true);
        }

        private void UpdateConnection(bool changed)
        {
            if (_updating) return;
            bool wasLocal = Local;
            _updating = true;
            try
            {
                if (UseExisting.IsChecked == true && _existing is not null)
                    Deployment.SelectedItem = Deployment.Items.OfType<Choice>().First(choice => choice.Id == _existing.Data.S("deployment", "cloud"));
                Deployment.IsEnabled = UseExisting.IsChecked != true;
                _presetFields.Visibility = Local ? Visibility.Visible : Visibility.Collapsed;
                Auth.IsEnabled = Local;
                Scope.IsEnabled = Local;
                if (changed || !Local)
                {
                    Auth.SelectedItem = Auth.Items.OfType<Choice>().First(choice => choice.Id == (Local ? "none" : "api_key"));
                    Scope.SelectedItem = Scope.Items.OfType<Choice>().First(choice => choice.Id == (Local ? "local" : "all_hosts"));
                }
                if (changed && Local) Protocol.SelectedIndex = 0;
            }
            finally { _updating = false; }
            if (changed || wasLocal != Local) ApplyPreset(initial: false);
        }

        private void ApplyPreset(bool initial)
        {
            if (_updating) return;
            var preset = SelectedPreset;
            bool locked = preset.ValueKind == JsonValueKind.Object;
            Context.IsReadOnly = locked;
            Supported.IsReadOnly = locked;
            _detail.Text = LocalModelPresentation.PresetDetail(preset);
            _detail.ToolTip = LocalModelPresentation.PresetReferences(preset);
            if (locked)
            {
                Context.Text = LocalModelPresentation.MaximumContext(preset).ToString();
                Supported.Text = string.Join(",", LocalModelPresentation.PresetEfforts(preset));
                Effort.Items.Clear();
                foreach (var value in LocalModelPresentation.PresetEfforts(preset))
                    Effort.Items.Add(new Choice(value, LocalModelPresentation.EffortLabel(value, preset)));
                var savedEffort = initial ? _model.S("reasoning_effort") : "";
                Effort.SelectedItem = Effort.Items.OfType<Choice>().FirstOrDefault(choice => choice.Id == savedEffort)
                    ?? Effort.Items.OfType<Choice>().FirstOrDefault(choice => choice.Id == LocalModelPresentation.NativeEffort(preset));
                if (!initial || ModelId.Text == "") ModelId.Text = preset.S("model_id");
                if (!initial || Display.Text == "") Display.Text = preset.S("display_name");
                if (Name.Text == "") Name.Text = preset.S("display_name") + " 로컬 서버";
            }
            else
            {
                var selected = Local ? initial && _model.S("id") != "" ? _model.S("reasoning_effort", "max") : "max"
                    : (Effort.SelectedItem as Choice)?.Id ?? _model.S("reasoning_effort", "high");
                Effort.Items.Clear();
                foreach (var value in EffortNames) Effort.Items.Add(new Choice(value, EffortLabel(value)));
                Effort.SelectedItem = Effort.Items.OfType<Choice>().FirstOrDefault(choice => choice.Id == selected) ?? Effort.Items[4];
            }
        }

        internal bool TryBuild(out object? result, out string error)
        {
            result = null; error = "";
            if (string.IsNullOrWhiteSpace(Name.Text) || string.IsNullOrWhiteSpace(Url.Text) || string.IsNullOrWhiteSpace(ModelId.Text))
            { error = "공급자 이름, 서버 주소, 실제 모델 ID를 입력하세요."; return false; }
            var preset = SelectedPreset;
            var locked = preset.ValueKind == JsonValueKind.Object;
            if (_model.S("local_preset_id") != "" && !locked)
            { error = "기존 모델의 프리셋은 지울 수 없습니다. 직접 입력하려면 새 모델 연결을 등록하세요."; return false; }
            if (Local && !locked && string.IsNullOrWhiteSpace(Context.Text))
            { error = "로컬 서버가 지원하는 컨텍스트 한도를 직접 입력하세요."; return false; }
            var isDeepSeek = ModelId.Text.StartsWith("deepseek-flash", StringComparison.OrdinalIgnoreCase) || ModelId.Text.StartsWith("deepseek-v4-", StringComparison.OrdinalIgnoreCase);
            var maximum = locked ? LocalModelPresentation.MaximumContext(preset) : 10000000;
            var contextText = string.IsNullOrWhiteSpace(Context.Text) ? (isDeepSeek ? "1048576" : "32768") : Context.Text;
            if (!LocalModelPresentation.ReadContext(contextText, maximum, locked, out var tokens)
                || !int.TryParse(Compact.Text, out var percent) || percent < 10 || percent > 90)
            { error = locked ? $"프리셋 최대 컨텍스트 {maximum:N0} 토큰과 압축 비율(10~90)을 확인하세요." : "컨텍스트 토큰 수와 압축 비율(10~90)을 확인하세요."; return false; }
            if (Effort.SelectedItem is not Choice effort)
            { error = "모델이 지원하는 추론 강도를 선택하세요."; return false; }
            var provider = new Dictionary<string, object?> { ["name"] = Name.Text.Trim(), ["base_url"] = Url.Text.Trim(),
                ["protocol"] = (Protocol.SelectedItem as Choice)?.Id ?? "responses", ["deployment"] = Local ? "local" : "cloud",
                ["auth_type"] = Local ? (Auth.SelectedItem as Choice)?.Id ?? "none" : "api_key",
                ["execution_scope"] = Local ? (Scope.SelectedItem as Choice)?.Id ?? "local" : "all_hosts" };
            if (UseExisting.IsChecked == true && _existing is not null) provider["id"] = _existing.Id;
            var capabilities = new Dictionary<string, object> { ["context_window"] = tokens };
            if (locked) capabilities["reasoning_efforts"] = LocalModelPresentation.PresetEfforts(preset);
            else if (!string.IsNullOrWhiteSpace(Supported.Text)) capabilities["reasoning_efforts"] = Supported.Text.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            var settings = new Dictionary<string, object> { ["wire_model_id"] = ModelId.Text.Trim(),
                ["display_name"] = string.IsNullOrWhiteSpace(Display.Text) ? ModelId.Text.Trim() : Display.Text.Trim(),
                ["reasoning_effort"] = effort.Id, ["capabilities"] = capabilities, ["auto_compact_percent"] = percent };
            if (locked) settings["local_preset_id"] = preset.S("id");
            if (_model.S("id") != "") { settings["id"] = _model.S("id"); provider["id"] = _existing!.Id; }
            result = new { provider, model = settings };
            return true;
        }
    }
}
