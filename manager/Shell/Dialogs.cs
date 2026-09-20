using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static partial class Dialogs
{
    private static Window Create(Window owner, string title, double width = 580, double height = 540) => new()
    {
        Owner = owner, Title = title, Width = width, Height = height, MinWidth = Math.Min(width, 440), MinHeight = 300,
        WindowStartupLocation = WindowStartupLocation.CenterOwner, ShowInTaskbar = false
    };
    private static StackPanel Body(Window window)
    {
        var body = new StackPanel { Margin = new Thickness(24) };
        window.Content = new ScrollViewer { Content = body, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        return body;
    }
    private static TextBlock Note(string text) => new() { Text = text, TextWrapping = TextWrapping.Wrap, Foreground = new SolidColorBrush(Color.FromRgb(166, 176, 192)), Margin = new Thickness(0, 0, 0, 14) };
    private static TextBox Field(Panel body, string label, string value = "")
    {
        body.Children.Add(new TextBlock { Text = label });
        var input = new TextBox { Text = value }; body.Children.Add(input); return input;
    }
    private static ComboBox Choices(Panel body, string label, IEnumerable<Choice> choices, string? selected = null)
    {
        body.Children.Add(new TextBlock { Text = label }); var combo = new ComboBox();
        foreach (var choice in choices) combo.Items.Add(choice);
        combo.SelectedItem = combo.Items.OfType<Choice>().FirstOrDefault(c => c.Id == selected) ?? combo.Items.OfType<Choice>().FirstOrDefault();
        body.Children.Add(combo); return combo;
    }
    private static Button Button(Panel body, string label, Action action)
    {
        var button = new Button { Content = label }; button.Click += (_, _) => action(); body.Children.Add(button); return button;
    }
    private static Button AsyncButton(Window window, Panel body, string label, Func<Task> action)
    {
        var button = new Button { Content = label };
        button.Click += async (_, _) => { button.IsEnabled = false; button.Content = label + " · 처리 중…"; try { await action(); } catch (Exception ex) { if (window.IsVisible) MessageBox.Show(window, ex.Message, "확인 필요", MessageBoxButton.OK, MessageBoxImage.Information); } finally { button.IsEnabled = true; button.Content = label; } };
        body.Children.Add(button); return button;
    }

    public static string? Prompt(Window owner, string title, string label, string value = "")
    {
        var window = Create(owner, title, 490, 250); var body = Body(window); var input = Field(body, label, value); string? result = null;
        var save = Button(body, "저장", () => { if (string.IsNullOrWhiteSpace(input.Text)) { input.Focus(); return; } result = input.Text.Trim(); window.DialogResult = true; });
        save.IsDefault = true; window.Loaded += (_, _) => { input.Focus(); input.SelectAll(); }; window.ShowDialog(); return result;
    }
    public static Choice? Select(Window owner, string title, string description, IEnumerable<Choice> choices)
    {
        var window = Create(owner, title, 560, 520); var body = Body(window); body.Children.Add(Note(description));
        var list = new ListBox { Height = 310 }; foreach (var choice in choices) list.Items.Add(choice); body.Children.Add(list);
        if (list.Items.Count == 0) body.Children.Add(Note("선택할 항목이 없습니다. 계정 또는 프로필을 먼저 준비하세요."));
        Choice? result = null; Button(body, "선택", () => { if (list.SelectedItem is not Choice selected) return; result = selected; window.DialogResult = true; });
        window.ShowDialog(); return result;
    }
    public static object? Shortcut(Window owner, IEnumerable<JsonElement> profiles, JsonElement catalog, string? selectedProfile, JsonElement captured = default, Func<string, string?, Task<JsonElement>>? refreshCatalog = null)
    {
        var window = Create(owner, "작업 바로가기 연결", 720, 790); var body = Body(window);
        body.Children.Add(Note("대화를 열 계정과 작업 별칭을 연결합니다. 대화의 원본 위치와 실제 기록은 그대로 유지합니다."));
        var target = Choices(body, "이 작업을 열 프로필", profiles.Select(p => new Choice(p.S("id"), p.S("alias"))), selectedProfile);
        var search = Field(body, "등록된 대화에서 찾기");
        static Choice[] ReadChoices(JsonElement value)
        {
            var records = value.ValueKind == JsonValueKind.Array ? value.Items() : value.Arr("threads").Concat(value.Arr("items")).Concat(value.Arr("conversations"));
            return records.Select(t => new Choice(t.S("thread_id", t.S("id")), t.S("title", t.S("name", t.S("thread_id", t.S("id")))) + "\n" + t.S("host_id", "local") + " · " + t.S("source_alias", t.S("source_store_id")) + (t.B("catalog_stale") ? " · 이전 목록" : ""), t)).ToArray();
        }
        static string Key(Choice value) => value.Id + "\0" + value.Data.S("host_id") + "\0" + value.Data.S("source_store_id");
        var choices = ReadChoices(catalog);
        var list = new ListBox { Height = 230 }; body.Children.Add(list);
        VirtualizingPanel.SetIsVirtualizing(list, true);
        VirtualizingPanel.SetVirtualizationMode(list, VirtualizationMode.Recycling);
        var updating = false;
        void Filter()
        {
            var selected = list.SelectedItem is Choice old ? Key(old) : null;
            updating = true;
            try
            {
                list.ItemsSource = refreshCatalog is null ? choices.Where(x => x.Label.Contains(search.Text, StringComparison.OrdinalIgnoreCase)).ToArray() : choices;
                list.SelectedItem = list.Items.OfType<Choice>().FirstOrDefault(x => Key(x) == selected);
            }
            finally { updating = false; }
        }
        Filter();
        var pages = new StackPanel { Orientation = Orientation.Horizontal }; body.Children.Add(pages);
        var previous = new Button { Content = "이전 페이지", IsEnabled = false };
        var nextPage = new Button { Content = "다음 페이지", IsEnabled = false };
        pages.Children.Add(previous); pages.Children.Add(nextPage);
        var history = new Stack<string?>();
        string? currentCursor = null;
        string? nextCursor = catalog.S("next_cursor") is { Length: > 0 } initialCursor ? initialCursor : null;
        var progress = Note(""); body.Children.Add(progress);
        void ShowProgress(JsonElement value)
        {
            var hosts = value.Arr("remote_hosts").ToArray();
            var total = value.Get("total_count").ValueKind == JsonValueKind.Number ? value.N("total_count") : choices.Length;
            var matching = value.Get("matching_count").ValueKind == JsonValueKind.Number ? value.N("matching_count") : choices.Length;
            progress.Text = total == 0 ? "아직 색인된 대화가 없습니다. 목록은 자동으로 갱신됩니다." :
                $"전체 {total:N0}개 · 검색 결과 {matching:N0}개 · {history.Count + 1}페이지 · 자동 갱신 중";
            if (hosts.Any(x => x.B("refreshing"))) progress.Text += " · SSH 확인 중";
            if (hosts.Any(x => x.B("stale") && x.S("observed_at") != "")) progress.Text += " · 일부 SSH는 이전 목록";
            if (value.B("possibly_truncated")) progress.Text += " · 대화가 많아 일부만 표시";
        }
        ShowProgress(catalog);
        nextPage.IsEnabled = refreshCatalog is not null && nextCursor is not null;
        var alias = Field(body, "작업 별칭", captured.S("title"));
        var details = new StackPanel();
        var thread = Field(details, "대화 ID (thread.id)", captured.S("thread_id"));
        var host = Field(details, "호스트 ID", captured.S("host_id", "local"));
        var source = Field(details, "원본 저장소 ID", captured.S("source_store_id"));
        var expander = new Expander { Header = "연결 상세", Content = details, IsExpanded = choices.Length == 0, Foreground = Brushes.LightGray, Margin = new Thickness(0, 8, 0, 8) }; body.Children.Add(expander);
        list.SelectionChanged += (_, _) => { if (updating || list.SelectedItem is not Choice choice) return; thread.Text = choice.Id; host.Text = choice.Data.S("host_id", "local"); source.Text = choice.Data.S("source_store_id"); alias.Text = choice.Data.S("title", choice.Data.S("name", "작업")); };
        var closed = false; var requests = 0; var revision = 0; var loadedQuery = "";
        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
        var searchTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(300) };
        async Task LoadPage(string? cursor, string action)
        {
            if (closed || refreshCatalog is null) return;
            var requestedRevision = ++revision;
            var requestedQuery = search.Text;
            requests++; previous.IsEnabled = false; nextPage.IsEnabled = false;
            try
            {
                var latest = await refreshCatalog(requestedQuery, cursor);
                if (closed || requestedRevision != revision) return;
                if (action == "next") history.Push(currentCursor);
                else if (action == "previous" && history.Count > 0) history.Pop();
                else if (action == "search") history.Clear();
                currentCursor = cursor;
                loadedQuery = requestedQuery;
                nextCursor = latest.S("next_cursor") is { Length: > 0 } more ? more : null;
                var next = ReadChoices(latest);
                var changed = !choices.Select(x => (Key(x), x.Label)).SequenceEqual(next.Select(x => (Key(x), x.Label)));
                choices = next;
                if (changed) Filter();
                ShowProgress(latest);
            }
            catch (Exception) { if (!closed && requestedRevision == revision) progress.Text = "목록을 갱신하지 못했습니다. 이전 목록을 유지합니다. 검색어를 바꾸면 첫 페이지에서 다시 확인합니다."; }
            finally
            {
                requests--;
                if (!closed && requests == 0)
                {
                    previous.IsEnabled = !searchTimer.IsEnabled && loadedQuery == search.Text && history.Count > 0;
                    nextPage.IsEnabled = !searchTimer.IsEnabled && loadedQuery == search.Text && nextCursor is not null;
                }
            }
        }
        previous.Click += async (_, _) => { if (history.Count > 0) await LoadPage(history.Peek(), "previous"); };
        nextPage.Click += async (_, _) => { if (nextCursor is not null) await LoadPage(nextCursor, "next"); };
        searchTimer.Tick += async (_, _) => { searchTimer.Stop(); await LoadPage(null, "search"); };
        search.TextChanged += (_, _) =>
        {
            if (refreshCatalog is null) { Filter(); return; }
            revision++; searchTimer.Stop(); searchTimer.Start();
            previous.IsEnabled = false; nextPage.IsEnabled = false;
        };
        timer.Tick += async (_, _) =>
        {
            if (!closed && requests == 0 && !searchTimer.IsEnabled)
                await LoadPage(loadedQuery == search.Text ? currentCursor : null, loadedQuery == search.Text ? "refresh" : "search");
        };
        window.Loaded += (_, _) => { if (refreshCatalog is not null) timer.Start(); };
        window.Closed += (_, _) => { closed = true; revision++; timer.Stop(); searchTimer.Stop(); };
        object? result = null;
        Button(body, "바로가기 저장", () =>
        {
            if (target.SelectedItem is not Choice profile || string.IsNullOrWhiteSpace(alias.Text) || string.IsNullOrWhiteSpace(thread.Text) || string.IsNullOrWhiteSpace(host.Text) || string.IsNullOrWhiteSpace(source.Text)) { MessageBox.Show(window, "프로필, 별칭, 대화 ID, 원본 출처를 모두 지정하세요."); return; }
            result = new { alias = alias.Text.Trim(), profile_id = profile.Id, thread_id = thread.Text.Trim(), host_id = host.Text.Trim(), source_store_id = source.Text.Trim() };
            window.DialogResult = true;
        }); window.ShowDialog(); return result;
    }

    public static Task ProvidersAsync(Window owner, JsonElement profile, JsonElement registry, Func<string, object?, Task<JsonElement>> request)
    {
        var window = Create(owner, "하위 에이전트 · API 공급자", 780, 860); var body = Body(window);
        body.Children.Add(Note("아래 선택은 하위 에이전트용입니다. 현재 프로필의 주 모델은 바뀌지 않습니다. 외부 모델이 직접 작업하게 하려면 프로필 +에서 ‘외부 API 모델’을 추가하세요."));
        var profileId = profile.S("id");
        body.Children.Add(new TextBlock { Text = profileId == "" ? "프로필을 선택하면 모델 조합을 저장할 수 있습니다." : "설정 대상 프로필 · " + profile.S("alias"), FontSize = 19, Margin = new Thickness(0, 0, 0, 12) });
        var enabled = new CheckBox { Content = "외부 하위 에이전트 사용", IsChecked = profile.Get("policy").B("enabled"), IsEnabled = profileId != "" }; body.Children.Add(enabled);
        var selectionMode = Choices(body, "하위 에이전트 모델 선택", [new("automatic", "자동 선택 · GPT와 선택한 외부 모델"), new("external_only", "외부 모델만 · 아래에서 선택한 모델 사용")], profile.Get("policy").S("selection_mode", "automatic"));
        selectionMode.IsEnabled = enabled.IsChecked == true;
        enabled.Checked += (_, _) => selectionMode.IsEnabled = true;
        enabled.Unchecked += (_, _) => { selectionMode.SelectedIndex = 0; selectionMode.IsEnabled = false; };
        body.Children.Add(Note("여러 모델을 선택할 수 있습니다. 작업을 맡길 때 적합한 모델을 고르며, 외부 모델만 모드에서는 GPT 하위 에이전트를 실행하지 않습니다."));
        var selected = profile.Get("policy").Arr("model_ids").Select(x => x.GetString()).ToHashSet();
        var models = new StackPanel(); body.Children.Add(models); var modelChecks = new List<(string Id, CheckBox Box)>();
        var verifyModels = new ComboBox { Margin = new Thickness(0, 8, 0, 8) };
        void RenderModels(JsonElement data)
        {
            if (modelChecks.Count > 0) { selected.Clear(); foreach (var (id, box) in modelChecks) if (box.IsChecked == true) selected.Add(id); }
            models.Children.Clear(); modelChecks.Clear(); verifyModels.Items.Clear();
            foreach (var model in data.Arr("models"))
            {
                var verified = model.B("verified") || model.Get("capabilities").B("verified");
                var forced = model.S("forced_reasoning_effort");
                var text = model.S("display_name", model.S("name", model.S("wire_model_id", model.S("model")))) + (forced != "" ? " · " + forced + " 고정" : " · " + model.S("reasoning_effort", model.S("reasoning", "기본 추론"))) + (verified ? "" : " · 연결 검증 필요");
                var box = new CheckBox { Content = text, IsChecked = selected.Contains(model.S("id")), IsEnabled = verified && profileId != "", ToolTip = model.S("wire_model_id", model.S("model")) };
                models.Children.Add(box); modelChecks.Add((model.S("id"), box));
                verifyModels.Items.Add(new Choice(model.S("id"), model.S("display_name", model.S("wire_model_id", model.S("model"))), model));
            }
            if (verifyModels.Items.Count > 0) verifyModels.SelectedIndex = 0;
        }
        RenderModels(registry);
        var policyStatus = Note(""); body.Children.Add(policyStatus);
        AsyncButton(window, body, "저장하고 작업 종료 후 자동 적용", async () =>
        {
            if (profileId == "") throw new InvalidOperationException("먼저 사용할 프로필을 선택하세요.");
            var result = await request("policy.set", new { profile_id = profileId, enabled = enabled.IsChecked == true, selection_mode = enabled.IsChecked == true ? (selectionMode.SelectedItem as Choice)?.Id ?? "automatic" : "automatic", model_ids = modelChecks.Where(x => x.Box.IsChecked == true).Select(x => x.Id).ToArray() });
            policyStatus.Text = result.Message("설정을 저장했습니다. 닫힌 프로필은 다음 실행부터 적용됩니다.");
        }).IsEnabled = profileId != "";
        body.Children.Add(new Separator { Margin = new Thickness(0, 18, 0, 18) });
        body.Children.Add(new TextBlock { Text = "API 공급자 · 하위 에이전트와 외부 프로필 공용", FontSize = 19 });
        var providers = Choices(body, "연결", registry.Arr("providers").Select(p => new Choice(p.S("id"), p.S("name") + (p.B("key_saved") ? " · 키 저장됨" : " · 키 필요"), p)));
        AsyncButton(window, body, "공급자 또는 모델 추가", async () =>
        {
            var form = ProviderForm(window, providers.SelectedItem as Choice); if (form is null) return;
            var result = await request("providers.save", form);
            registry = await request("providers.list", null); RenderModels(registry);
            providers.Items.Clear(); foreach (var p in registry.Arr("providers")) providers.Items.Add(new Choice(p.S("id"), p.S("name") + (p.B("key_saved") ? " · 키 저장됨" : " · 키 필요"), p));
            if (providers.Items.Count > 0) providers.SelectedIndex = 0;
            policyStatus.Text = result.Message("연결을 등록했습니다. 새 모델은 연결 검증 후 자동 선택할 수 있습니다.");
        });
        AsyncButton(window, body, "선택한 공급자의 API 키 저장", async () =>
        {
            if (providers.SelectedItem is not Choice provider) throw new InvalidOperationException("공급자를 선택하세요.");
            var key = Key(window, provider.Label); if (key is null) return;
            try
            {
                await request("providers.key", new { provider_id = provider.Id, key }); policyStatus.Text = "API 키를 Windows 사용자 보호 저장소에 저장했습니다.";
                registry = await request("providers.list", null);
                providers.Items.Clear(); foreach (var p in registry.Arr("providers")) providers.Items.Add(new Choice(p.S("id"), p.S("name") + (p.B("key_saved") ? " · 키 저장됨" : " · 키 필요"), p));
                providers.SelectedItem = providers.Items.OfType<Choice>().FirstOrDefault(p => p.Id == provider.Id);
            }
            finally { key = null; }
        });
        body.Children.Add(new TextBlock { Text = "연결을 시험할 모델", Margin = new Thickness(0, 10, 0, 0) });
        body.Children.Add(verifyModels);
        AsyncButton(window, body, "선택한 모델 기본값 수정", async () =>
        {
            if (verifyModels.SelectedItem is not Choice selectedModel) return;
            var provider = registry.Arr("providers").First(p => p.S("id") == selectedModel.Data.S("provider_id"));
            var form = ProviderForm(window, new Choice(provider.S("id"), provider.S("name"), provider), selectedModel.Data);
            if (form is null) return;
            await request("providers.save", form);
            registry = await request("providers.list", null); RenderModels(registry);
            policyStatus.Text = "모델 기본값을 저장했습니다. 프로필별 설정은 프로필 메뉴의 ‘외부 모델 설정’에서 수정하세요.";
        });
        AsyncButton(window, body, "모델 연결 시험", async () =>
        {
            if (verifyModels.SelectedItem is not Choice model) throw new InvalidOperationException("시험할 모델을 선택하세요.");
            policyStatus.Text = "모델 API와 기본 도구 호출을 시험하고 있습니다…";
            var result = await request("providers.verify", new { model_id = model.Id });
            registry = await request("providers.list", null); RenderModels(registry);
            policyStatus.Text = result.Message("모델 연결 시험을 마쳤습니다.");
        });
        body.Children.Add(Note("연결 시험은 저장한 키로 작은 API 요청을 실제 전송하므로 공급자의 사용량이 발생할 수 있습니다."));
        body.Children.Add(Note("API 키는 명령행이나 일반 설정 파일에 넣지 않습니다. 새 API 형식은 해당 어댑터의 지원이 필요하며, 등록만으로 호출 성공을 보장하지 않습니다."));
        Button(body, "닫기", window.Close); window.ShowDialog(); return Task.CompletedTask;
    }
    private static object? ProviderForm(Window owner, Choice? existing, JsonElement modelData = default)
    {
        var window = Create(owner, "공급자 · 모델 등록", 630, 790); var body = Body(window);
        var useExisting = new CheckBox { Content = existing is null ? "기존 공급자 없음" : "기존 공급자에 모델 추가 · " + existing.Label, IsChecked = existing is not null, IsEnabled = existing is not null }; body.Children.Add(useExisting);
        var name = Field(body, "공급자 이름", existing?.Data.S("name") ?? "");
        var url = Field(body, "API 기본 주소 (HTTPS)", existing?.Data.S("base_url") ?? "");
        var protocol = Choices(body, "API 형식", [new("responses", "OpenAI Responses"), new("chat_completions", "Chat Completions · 어댑터 지원 확인 필요"), new("anthropic_messages", "Anthropic Messages · 어댑터 지원 확인 필요")], existing?.Data.S("protocol", "responses"));
        var model = Field(body, "공급자가 제공한 정확한 모델 ID", modelData.S("wire_model_id"));
        var display = Field(body, "목록에 표시할 모델 이름", modelData.S("display_name"));
        var reasoning = Choices(body, "기본 추론 강도", EffortNames.Select(v => new Choice(v, EffortLabel(v))), modelData.S("reasoning_effort", "high"));
        var supported = Field(body, "지원하는 강도 · 쉼표로 구분", string.Join(",", modelData.Arr("supported_reasoning_efforts").Select(x => x.GetString())));
        var context = Field(body, "모델 컨텍스트 한도 · 토큰", modelData.Get("settings_defaults").Get("context_window").ToString());
        var compact = Field(body, "자동 압축 기본 비율 · 10~90%", modelData.Get("settings_defaults").Get("auto_compact_percent").ToString());
        if (compact.Text == "") compact.Text = "90";
        body.Children.Add(Note("DeepSeek Flash / V4는 none·low·high·max, 1,048,576 토큰을 기본으로 사용합니다. 다른 모델은 공급자의 지원값을 입력하세요. 빈 지원 목록은 선택한 강도만 사용합니다."));
        object? result = null;
        Button(body, "등록", () =>
        {
            if (string.IsNullOrWhiteSpace(name.Text) || string.IsNullOrWhiteSpace(url.Text) || string.IsNullOrWhiteSpace(model.Text)) { MessageBox.Show(window, "공급자 이름, API 주소, 정확한 모델 ID를 입력하세요."); return; }
            var provider = new Dictionary<string, object?> { ["name"] = name.Text.Trim(), ["base_url"] = url.Text.Trim(), ["protocol"] = (protocol.SelectedItem as Choice)?.Id ?? "responses" };
            if (useExisting.IsChecked == true && existing is not null) provider["id"] = existing.Id;
            var cap = new Dictionary<string, object>();
            var isDeepSeek = model.Text.StartsWith("deepseek-flash", StringComparison.OrdinalIgnoreCase) || model.Text.StartsWith("deepseek-v4-", StringComparison.OrdinalIgnoreCase);
            if (!int.TryParse(string.IsNullOrWhiteSpace(context.Text) ? (isDeepSeek ? "1048576" : "32768") : context.Text.Replace(",", ""), out var tokens) || tokens < 4096 || tokens > 10000000 || !int.TryParse(compact.Text, out var percent) || percent < 10 || percent > 90)
            { MessageBox.Show(window, "컨텍스트 토큰 수와 압축 비율(10~90)을 확인하세요."); return; }
            cap["context_window"] = tokens;
            if (!string.IsNullOrWhiteSpace(supported.Text)) cap["reasoning_efforts"] = supported.Text.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            var settings = new Dictionary<string, object> { ["wire_model_id"] = model.Text.Trim(), ["display_name"] = string.IsNullOrWhiteSpace(display.Text) ? model.Text.Trim() : display.Text.Trim(), ["reasoning_effort"] = (reasoning.SelectedItem as Choice)?.Id ?? "high", ["capabilities"] = cap, ["auto_compact_percent"] = percent };
            if (modelData.S("id") != "") { settings["id"] = modelData.S("id"); provider["id"] = existing!.Id; }
            result = new { provider, model = settings }; window.DialogResult = true;
        }); window.ShowDialog(); return result;
    }
    private static string? Key(Window owner, string name)
    {
        var window = Create(owner, "API 키 저장", 560, 310); var body = Body(window); body.Children.Add(Note(name + "에 사용할 API 키를 입력하세요. 입력값은 화면·명령행·진행 내역에 표시하지 않습니다."));
        var input = new PasswordBox(); body.Children.Add(input); string? result = null;
        Button(body, "안전하게 저장", () => { if (string.IsNullOrWhiteSpace(input.Password)) return; result = input.Password; input.Clear(); window.DialogResult = true; }); window.Loaded += (_, _) => input.Focus(); window.ShowDialog(); input.Clear(); return result;
    }

    public static Task RemoteAsync(Window owner, string profileId, JsonElement data, Func<string, object?, Task<JsonElement>> request)
    {
        var window = new RemoteUpdatesWindow(profileId, data, request) { Owner = owner };
        window.Loaded += async (_, _) => await window.LoadAsync();
        window.ShowDialog();
        return Task.CompletedTask;
    }
    internal static string ResultSummary(JsonElement result)
    {
        var lines = new List<string> { result.Message() };
        foreach (var field in new[] { "blockers", "remote_pending", "restore_failures" })
        {
            foreach (var item in result.Arr(field))
            {
                var description = item.ValueKind == JsonValueKind.String ? item.GetString()! : item.S("message", item.S("reason", item.S("host_id", item.S("alias", "연결 상태 확인 필요"))));
                if (!lines.Contains(description)) lines.Add("• " + description);
            }
        }
        return string.Join("\n\n", lines);
    }
    public static bool Update(Window owner, JsonElement data)
    {
        var window = Create(owner, "Codex 업데이트", 640, 530); var body = Body(window);
        body.Children.Add(new TextBlock { Text = "공식 Codex 앱 업데이트", FontSize = 22, Margin = new Thickness(0, 0, 0, 18) });
        body.Children.Add(Note(data.Message("설치된 앱 상태를 확인했습니다.")));
        if (data.Get("installed").S("version") != "") body.Children.Add(Note("현재 설치 · " + data.Get("installed").S("version")));
        if (data.Get("latest").S("version") != "") body.Children.Add(Note("최신 공식 버전 · " + data.Get("latest").S("version")));
        foreach (var key in new[] { "installed_version", "current_version", "available_version", "channel", "status" }) if (data.S(key) != "") body.Children.Add(Note(key + " · " + data.S(key)));
        body.Children.Add(Note("업데이트는 진행 중인 작업을 정리할 수 있을 때 수행합니다. 다른 계정이나 원래 앱의 작업이 실행 중이면 대기 상태와 이유를 표시합니다."));
        foreach (var blocker in data.Arr("blockers")) body.Children.Add(Note(blocker.ValueKind == JsonValueKind.String ? blocker.GetString()! : blocker.Message()));
        bool result = false;
        if (UpdatePresentation.CanInstall(data))
            Button(body, data.S("status") is "recovery_required" or "failed_restore" ? "이전 업데이트 확인 계속" : "모든 프로필에 앱 업데이트 적용",
                () => { result = true; window.DialogResult = true; });
        Button(body, "닫기", window.Close); window.ShowDialog(); return result;
    }
    public static string? ProfileUpdates(Window owner, JsonElement state)
    {
        var window = Create(owner, "전체 프로필 업데이트", 640, 580); var body = Body(window);
        body.Children.Add(new TextBlock { Text = "모든 계정에 최신 버전 적용", FontSize = 22, Margin = new Thickness(0, 0, 0, 18) });
        body.Children.Add(Note("꺼진 프로필은 다음 실행부터 최신 버전을 사용합니다. 실행 중인 프로필은 작업이 끝나면 적용합니다."));
        var startup = state.Get("startup_updates");
        foreach (var profile in startup.Arr("profiles"))
            body.Children.Add(Note(profile.S("alias") + " · " + UpdatePresentation.ProfileState(profile) +
                (profile.S("message") == "" ? "" : "\n" + profile.S("message"))));
        string? action = null;
        if (!startup.B("worker_active"))
        {
            Button(body, "모든 프로필 다시 확인·적용", () => { action = "retry"; window.DialogResult = true; });
            var legacy = state.Arr("profiles").Where(p => p.Get("restart").S("phase") == "attention" &&
                p.Get("restart").S("code") == "runtime_state_unavailable").ToArray();
            if (legacy.Length > 0)
            {
                body.Children.Add(Note("구버전 정리 대상: " + string.Join(", ", legacy.Select(p => p.S("alias"))) +
                    "\n이 프로필들은 실행 상태 기록이 불완전합니다. 아래 버튼은 해당 관리용 Codex를 종료합니다. 진행 중인 작업이나 보내지 않은 입력이 중단될 수 있습니다. 원래 Codex 앱은 유지됩니다."));
                Button(body, "위 구버전들 종료 후 최신으로 열기", () => { action = "legacy"; window.DialogResult = true; });
            }
        }
        Button(body, "닫기", window.Close); window.ShowDialog(); return action;
    }
    public static void ShowEvents(Window owner, IEnumerable<string> events)
    {
        var window = Create(owner, "관리 앱 진행 내역", 780, 580); var body = Body(window);
        body.Children.Add(Note("프로필·바로가기·연결 작업의 처리 결과입니다. 실제 코딩 작업의 진행은 오른쪽 원본 Codex에서 확인합니다."));
        body.Children.Add(new TextBox { IsReadOnly = true, Text = string.Join(Environment.NewLine, events), AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, Height = 390, VerticalScrollBarVisibility = ScrollBarVisibility.Auto }); Button(body, "닫기", window.Close); window.ShowDialog();
    }
}
