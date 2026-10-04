using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

public sealed partial class MainWindow
{
    private Button? _presetButton;
    private long _presetRead;

    private async Task RefreshTaskPresetAsync()
    {
        var ticket = ++_presetRead;
        var profile = _selectedProfile;
        var task = _selectedTask;
        if (_presetButton is null) return;
        _presetButton.Content = "실행 프리셋";
        _presetButton.ToolTip = "이 작업의 모델·하위 에이전트 조합 선택";
        if (profile is null || task is null || _viewingCatalog) return;
        try
        {
            var status = await Request("presets.status", new { profile_id = profile, host_id = task.Task.HostId, thread_id = task.Task.ThreadId });
            if (ticket != _presetRead || profile != _selectedProfile || task != _selectedTask) return;
            var preset = status.Get("preset");
            _presetButton.Content = new TextBlock { MaxWidth = 210, TextTrimming = TextTrimming.CharacterEllipsis,
                Text = preset.S("name") is { Length: > 0 } name ? "프리셋 · " + name : "프리셋 · 기본 설정" };
            _presetButton.ToolTip = status.Message("다음 실행에 사용할 설정입니다.") +
                (status.B("confirmation_pending") ? "\n실행기 적용 확인 대기 중" : "") +
                (preset.S("name") == "" ? "" : $"\n{preset.S("name")} · 설정 {preset.N("revision")}");
        }
        catch { if (ticket == _presetRead) _presetButton.ToolTip = "프리셋 상태를 확인하지 못했습니다. 눌러서 다시 확인하세요."; }
    }

    private async Task ManagePresetsAsync(string? profileId = null)
    {
        var id = profileId ?? RequireProfile();
        await Dialogs.ExecutionPresetsAsync(this, id, Request);
        await RefreshTaskPresetAsync();
    }

    private async Task ChooseTaskPresetAsync()
    {
        var profile = RequireProfile();
        var task = _selectedTask;
        if (task is null) { await ManagePresetsAsync(profile); return; }
        var registry = await Request("presets.list", new { profile_id = profile });
        var choices = new List<Choice> { new("default", "프로필 기본 설정 · 저장된 프리셋 해제") };
        choices.AddRange(registry.Arr("presets").Select(p => new Choice(p.S("id"), p.S("name") +
            $" · 하위 에이전트 {p.Arr("roles").Count()}개", p)));
        choices.Add(new("manage", "프리셋 만들기 · 편집"));
        var selected = Dialogs.Select(this, "이 작업의 실행 프리셋", task.Title +
            "\n선택한 설정은 다음 실행부터 사용합니다. 진행 중인 응답과 다른 작업은 유지됩니다.", choices);
        if (selected is null) return;
        if (selected.Id == "manage") { await ManagePresetsAsync(profile); return; }
        var result = await Request("presets.select", new { profile_id = profile, host_id = task.Task.HostId,
            thread_id = task.Task.ThreadId, preset_id = selected.Id == "default" ? null : selected.Id,
            revision = selected.Id == "default" ? (long?)null : selected.Data.N("revision") });
        SetStatus(result.Message("이 작업의 실행 프리셋을 저장했습니다."), result.B("runtime_prepare_required"));
        await RefreshTaskPresetAsync();
    }
}

internal static partial class Dialogs
{
    internal static async Task ExecutionPresetsAsync(Window owner, string profileId, Func<string, object?, Task<JsonElement>> request)
    {
        var registry = await request("presets.list", new { profile_id = profileId });
        var window = Create(owner, "실행 프리셋", 700, 840);
        var body = Body(window);
        body.Children.Add(Note("같은 계정에서 사용할 모델·하위 에이전트 조합을 저장합니다. 작업마다 선택할 수 있으며, 저장된 설정을 편집해도 기존 작업에 연결한 설정 버전은 유지됩니다."));
        var presets = new ListBox { Height = 155 }; body.Children.Add(presets);
        var status = Note(""); body.Children.Add(status);
        var actions = new WrapPanel(); body.Children.Add(actions);
        var editor = new StackPanel(); body.Children.Add(editor);
        string? editing = null;
        long revision = 0;
        var roles = new List<Dictionary<string, object>>();
        TextBox? name = null;
        ListBox? roleList = null;

        void Fill(string? id = null)
        {
            presets.ItemsSource = registry.Arr("presets").Select(p => new Choice(p.S("id"), p.S("name") +
                (registry.Get("default").S("preset_id") == p.S("id") ? " · 새 작업 기본" : "") +
                $" · 하위 에이전트 {p.Arr("roles").Count()}개", p)).ToArray();
            presets.SelectedItem = presets.Items.OfType<Choice>().FirstOrDefault(p => p.Id == id);
        }
        void ShowRoles()
        {
            if (roleList is null) return;
            roleList.ItemsSource = roles.Select((r, i) => new Choice(i.ToString(), RoleLabel(r, registry))).ToArray();
        }
        void Edit(JsonElement saved = default)
        {
            editing = saved.S("id") is { Length: > 0 } id ? id : null;
            revision = saved.N("revision");
            editor.Children.Clear(); roles.Clear();
            foreach (var role in saved.Arr("roles")) roles.Add(ExecutionPresetRoleInput(role));
            name = Field(editor, "프리셋 이름", saved.S("name", "새 조합"));
            editor.Children.Add(Note("주 에이전트의 모델·추론 강도는 대화 입력창에서 선택합니다. 아래에는 이 조합에서 사용할 하위 에이전트를 지정하세요."));
            roleList = new ListBox { Height = 130 }; editor.Children.Add(roleList); ShowRoles();
            var row = new WrapPanel(); editor.Children.Add(row);
            Button(row, "하위 에이전트 추가", () =>
            {
                var role = ExecutionPresetRole(window, registry, roles.Count + 1);
                if (role is not null) { roles.Add(role); ShowRoles(); }
            });
            Button(row, "선택 제거", () =>
            {
                if (roleList.SelectedItem is Choice choice && int.TryParse(choice.Id, out var i))
                { roles.RemoveAt(i); ShowRoles(); }
            });
            AsyncButton(window, editor, "프리셋 저장", async () =>
            {
                var result = await request("presets.save", new { profile_id = profileId, expected_revision = editing is null ? (long?)null : revision,
                    preset = new { id = editing, name = name.Text.Trim(), main = saved.Get("main").ValueKind == JsonValueKind.Object
                        ? saved.Get("main") : JsonSerializer.SerializeToElement(new { }), roles = roles.ToArray() } });
                registry = await request("presets.list", new { profile_id = profileId });
                Fill(result.S("id")); Edit(result); status.Text = result.Message("저장했습니다. 작업 상단의 실행 프리셋에서 선택할 수 있습니다.");
            });
        }
        Button(actions, "새 프리셋", () => { presets.SelectedItem = null; Edit(); });
        AsyncButton(window, actions, "새 작업 기본으로", async () =>
        {
            if (presets.SelectedItem is not Choice selected) return;
            var result = await request("presets.default", new { profile_id = profileId, preset_id = selected.Id, revision = selected.Data.N("revision") });
            registry = await request("presets.list", new { profile_id = profileId }); Fill(selected.Id); status.Text = result.Message("새 작업에서 사용할 기본 프리셋을 저장했습니다.");
        });
        AsyncButton(window, actions, "기본값 해제", async () =>
        {
            await request("presets.default", new { profile_id = profileId, preset_id = (string?)null });
            registry = await request("presets.list", new { profile_id = profileId }); Fill(); status.Text = "새 작업은 프로필 기본 설정으로 시작합니다.";
        });
        AsyncButton(window, actions, "삭제", async () =>
        {
            if (presets.SelectedItem is not Choice selected) return;
            await request("presets.delete", new { profile_id = profileId, preset_id = selected.Id, expected_revision = selected.Data.N("revision") });
            registry = await request("presets.list", new { profile_id = profileId }); Fill(); Edit(); status.Text = "선택 목록에서 삭제했습니다. 기존 작업의 설정 버전은 보존됩니다.";
        });
        presets.SelectionChanged += (_, _) => { if (presets.SelectedItem is Choice selected) Edit(selected.Data); };
        Fill(); Edit(); window.ShowDialog();
    }

    internal static Dictionary<string, object> ExecutionPresetRoleInput(JsonElement role)
    {
        var result = new Dictionary<string, object> { ["name"] = role.S("name") };
        if (role.S("model_id") is { Length: > 0 } modelId) result["model_id"] = modelId;
        else
        {
            result["profile_id"] = role.S("profile_id");
            result["model"] = role.S("model");
            result["effort"] = role.S("effort");
        }
        return result;
    }

    private static string RoleLabel(Dictionary<string, object> role, JsonElement registry)
    {
        var item = JsonSerializer.SerializeToElement(role);
        if (item.S("model_id") is { Length: > 0 } modelId)
            return item.S("name") + " · " + registry.Arr("models").FirstOrDefault(m => m.S("id") == modelId).S("name", modelId);
        return item.S("name") + " · " + registry.Arr("accounts").FirstOrDefault(p => p.S("id") == item.S("profile_id")).S("alias", "계정") +
            " · " + item.S("model") + " · " + item.S("effort");
    }

    private static Dictionary<string, object>? ExecutionPresetRole(Window owner, JsonElement registry, int ordinal)
    {
        var window = Create(owner, "하위 에이전트 추가", 550, 650); var body = Body(window);
        body.Children.Add(Note("하위 에이전트가 사용할 계정 또는 등록된 API·로컬 모델을 선택하세요."));
        var purpose = Field(body, "역할 이름 · 예: 코드 검토, UI 구현", "하위 에이전트 " + ordinal);
        var targets = registry.Arr("accounts").Select(p => new Choice("account:" + p.S("id"), p.S("alias"), p)).ToList();
        targets.AddRange(registry.Arr("models").Select(m => new Choice("model:" + m.S("id"), "API / 로컬 · " + m.S("name"), m)));
        var target = Choices(body, "실행 계정 / 모델", targets);
        var model = Choices(body, "모델", Array.Empty<Choice>());
        var effort = Choices(body, "추론 강도", Array.Empty<Choice>());
        void ModelChanged()
        {
            var choice = model.SelectedItem as Choice;
            var values = choice?.Data.Arr("efforts").Select(v => v.GetString()!).ToArray() ?? [];
            effort.ItemsSource = values.Select(v => new Choice(v, v)).ToArray();
            effort.SelectedItem = effort.Items.OfType<Choice>().FirstOrDefault(x => x.Id == "max") ?? effort.Items.OfType<Choice>().LastOrDefault();
        }
        void TargetChanged()
        {
            if (target.SelectedItem is not Choice choice) return;
            bool account = choice.Id.StartsWith("account:", StringComparison.Ordinal);
            model.IsEnabled = effort.IsEnabled = account;
            model.ItemsSource = account ? choice.Data.Arr("models").Select(m => new Choice(m.S("id"), m.S("name", m.S("id")), m)).ToArray() : [];
            model.SelectedIndex = model.Items.Count > 0 ? 0 : -1;
            ModelChanged();
        }
        model.SelectionChanged += (_, _) => ModelChanged(); target.SelectionChanged += (_, _) => TargetChanged(); TargetChanged();
        Dictionary<string, object>? result = null;
        Button(body, "추가", () =>
        {
            if (target.SelectedItem is not Choice selected) return;
            if (string.IsNullOrWhiteSpace(purpose.Text)) { purpose.Focus(); return; }
            result = new() { ["name"] = purpose.Text.Trim() };
            if (selected.Id.StartsWith("model:", StringComparison.Ordinal)) result["model_id"] = selected.Data.S("id");
            else
            {
                if (model.SelectedItem is not Choice m || effort.SelectedItem is not Choice e) return;
                result["profile_id"] = selected.Data.S("id"); result["model"] = m.Id; result["effort"] = e.Id;
            }
            window.DialogResult = true;
        });
        window.ShowDialog(); return result;
    }
}
