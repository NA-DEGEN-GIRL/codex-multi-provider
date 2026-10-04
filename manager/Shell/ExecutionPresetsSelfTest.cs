using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class ExecutionPresetsSelfTest
{
    private const string OwnerId = "11111111-1111-4111-8111-111111111111";
    private const string ClaudeId = "22222222-2222-4222-8222-222222222222";
    private const string ModelId = "33333333-3333-4333-8333-333333333333";
    private const string PresetId = "44444444-4444-4444-8444-444444444444";

    internal static async Task RunAsync(string report)
    {
        var checks = new List<string>();
        void Require(bool value, string message)
        {
            if (!value) throw new InvalidOperationException(message);
            checks.Add(message);
        }
        var calls = new List<(string Command, JsonElement Input)>();
        var failures = new List<string>();
        var history = new Dictionary<long, JsonElement>();
        JsonElement? selectedDefault = null;
        bool deleted = false;
        var saved = JsonSerializer.SerializeToElement(new
        {
            id = PresetId, profile_id = OwnerId, name = "혼합 계정 검토", revision = 1,
            main = new { },
            roles = new object[] {
                new { name = "구현 담당", kind = "codex", profile_id = OwnerId, model = "gpt-6-astra", effort = "high", account_binding = "fixture-native-binding" },
                new { name = "검토 담당", kind = "claude_code", profile_id = ClaudeId, model = "claude-opus-5-5", effort = "ultracode", account_binding = "fixture-claude-binding" },
                new { name = "API 점검", kind = "external", model_id = ModelId, provider_id = "fixture-provider", model = "fixture-http", effort = "low", model_revision = 3, provider_revision = 5 }
            }
        });
        history[1] = saved;

        JsonElement Registry() => JsonSerializer.SerializeToElement(new
        {
            profile_id = OwnerId, presets = deleted ? Array.Empty<JsonElement>() : new[] { saved },
            @default = selectedDefault,
            accounts = new object[] {
                new { id = OwnerId, alias = "계정 06", kind = "codex", logged_in = true, auth_state = "signed_in",
                    models = new[] { new { id = "gpt-6-astra", name = "GPT Astra", efforts = new[] { "low", "high", "max" } } } },
                new { id = ClaudeId, alias = "Claude 검토 계정", kind = "claude_code", logged_in = true, auth_state = "ready",
                    models = new[] { new { id = "claude-opus-5-5", name = "Opus 5.5", efforts = new[] { "high", "ultracode" } } } }
            },
            models = new[] { new { id = ModelId, name = "등록 API 모델", model = "fixture-http", effort = "low", kind = "external", verified = true, credentials_ready = true } }
        });

        async Task<JsonElement> Request(string command, object? args)
        {
            // Preserve a real async boundary so the dialog must survive its
            // disabled/loading state and a second registry read after writes.
            await Task.Yield();
            var input = JsonSerializer.SerializeToElement(args);
            calls.Add((command, input));
            if (input.S("profile_id") != OwnerId) failures.Add("Request crossed the owner account boundary.");
            switch (command)
            {
                case "presets.list": return Registry();
                case "presets.save":
                    var payload = input.Get("preset");
                    if (payload.S("id") != PresetId || input.N("expected_revision") != saved.N("revision"))
                        failures.Add("Save lost the preset identity or optimistic revision.");
                    if (payload.Get("main").ValueKind != JsonValueKind.Object || payload.Get("main").EnumerateObject().Any())
                        failures.Add("Editing the role composition overwrote the task model/effort defaults.");
                    foreach (var role in payload.Arr("roles"))
                    {
                        var expected = role.S("model_id") == "" ? new[] { "name", "profile_id", "model", "effort" } : new[] { "name", "model_id" };
                        if (!role.EnumerateObject().Select(p => p.Name).Order().SequenceEqual(expected.Order()))
                            failures.Add("Save submitted normalized/internal role metadata instead of public inputs.");
                    }
                    var next = saved.N("revision") + 1;
                    var normalized = payload.Arr("roles").Select(role =>
                    {
                        var value = JsonSerializer.Deserialize<Dictionary<string, object>>(role.GetRawText())!;
                        if (role.S("model_id") != "")
                        {
                            value["kind"] = "external"; value["model_revision"] = 3; value["provider_revision"] = 5;
                            value["provider_id"] = "fixture-provider"; value["model"] = "fixture-http"; value["effort"] = "low";
                        }
                        else
                        {
                            value["kind"] = role.S("profile_id") == ClaudeId ? "claude_code" : "codex";
                            value["account_binding"] = "fixture-binding-added-by-registry";
                        }
                        return value;
                    }).ToArray();
                    saved = JsonSerializer.SerializeToElement(new { id = PresetId, profile_id = OwnerId, name = payload.S("name"), revision = next, main = new { }, roles = normalized });
                    history[next] = saved;
                    return saved;
                case "presets.default":
                    selectedDefault = input.Get("preset_id").ValueKind == JsonValueKind.Null ? null : JsonSerializer.SerializeToElement(new { preset_id = input.S("preset_id"), revision = input.N("revision") });
                    return JsonSerializer.SerializeToElement(new { profile_id = OwnerId, @default = selectedDefault });
                case "presets.delete":
                    if (input.S("preset_id") != PresetId || input.N("expected_revision") != saved.N("revision"))
                        failures.Add("Delete lost the selected identity or revision.");
                    deleted = true;
                    return JsonSerializer.SerializeToElement(new { deleted = true, id = PresetId, revision = saved.N("revision") });
                default:
                    failures.Add("Unexpected production/lifecycle request: " + command);
                    return JsonSerializer.SerializeToElement(new { });
            }
        }

        // This fixture owns its Application process. Hide every fixture dialog
        // before first render, including the nested role editor.
        var previousStyle = Application.Current.Resources[typeof(Window)] as Style;
        var hiddenStyle = new Style(typeof(Window), previousStyle);
        hiddenStyle.Setters.Add(new Setter(UIElement.OpacityProperty, 0.0));
        hiddenStyle.Setters.Add(new Setter(Window.ShowActivatedProperty, false));
        Application.Current.Resources[typeof(Window)] = hiddenStyle;
        var owner = new Window { Title = "Execution preset fixture", Width = 800, Height = 900,
            Left = -28000, Top = -28000, WindowStartupLocation = WindowStartupLocation.Manual,
            ShowInTaskbar = false, ShowActivated = false, Opacity = 0 };
        Exception? driverError = null;
        Window? dialog = null;
        try
        {
            owner.Show();
            async Task DriveAsync()
            {
                try
                {
                    dialog = await WaitWindowAsync("실행 프리셋");
                    dialog.UpdateLayout();
                    ListBox Presets() => Descendants(dialog).OfType<ListBox>().Single(l => l.Height == 155);
                    ListBox Roles() => Descendants(dialog).OfType<ListBox>().Single(l => l.Height == 130);
                    TextBox Name() => Descendants(dialog).OfType<TextBox>().Single();
                    Presets().SelectedIndex = 0;
                    dialog.UpdateLayout();
                    var roleLabels = Roles().Items.OfType<Choice>().Select(c => c.ToString()).ToArray();
                    Require(roleLabels.Length == 3 && roleLabels.Any(s => s.Contains("계정 06")) && roleLabels.Any(s => s.Contains("Claude 검토 계정")) && roleLabels.Any(s => s.Contains("등록 API 모델")),
                        "Saved normalized GPT, Claude and API roles open with purpose and account/model labels.");
                    Require(Name().Text == "혼합 계정 검토", "Selecting a saved preset opens its existing name.");
                    Name().Text = "이름을 바꾼 혼합 조합";
                    Click(dialog, "프리셋 저장");
                    await WaitUntilAsync(() => saved.N("revision") == 2 && Name().Text == saved.S("name") && Ready(dialog, "프리셋 저장"));
                    Require(failures.Count == 0 && saved.Arr("roles").Count() == 3 && saved.S("name") == "이름을 바꾼 혼합 조합",
                        "Rename/save projects only public role inputs and preserves every account/model/effort.");
                    Require(history[1].S("name") == "혼합 계정 검토", "Saving an edit leaves the earlier immutable fixture revision intact.");

                    Click(dialog, "새 작업 기본으로");
                    await WaitUntilAsync(() => selectedDefault is not null && Ready(dialog, "새 작업 기본으로"));
                    Require(selectedDefault!.Value.S("preset_id") == PresetId && selectedDefault.Value.N("revision") == 2,
                        "Default action selects the saved preset's exact revision.");
                    Click(dialog, "기본값 해제");
                    await WaitUntilAsync(() => selectedDefault is null && Ready(dialog, "기본값 해제"));
                    Require(calls.Last(c => c.Command == "presets.default").Input.Get("preset_id").ValueKind == JsonValueKind.Null,
                        "Clearing the default sends an explicit null selection.");
                    Presets().SelectedIndex = 0;

                    async Task AddRoleAsync(bool api)
                    {
                        Exception? editorError = null;
                        var driveEditor = owner.Dispatcher.InvokeAsync(async () =>
                        {
                            Window? editor = null;
                            try
                            {
                                editor = await WaitWindowAsync("하위 에이전트 추가");
                                editor.UpdateLayout();
                                var purpose = Descendants(editor).OfType<TextBox>().Single();
                                purpose.Text = "   ";
                                Click(editor, "추가");
                                Require(editor.IsVisible, "The role editor keeps a blank purpose open instead of adding an unnamed role.");
                                purpose.Text = api ? "  API 결과 확인  " : "  한국어 코드 검토  ";
                                var choices = Descendants(editor).OfType<ComboBox>().ToArray();
                                choices[0].SelectedItem = choices[0].Items.OfType<Choice>().Single(c => c.Id == (api ? "model:" + ModelId : "account:" + ClaudeId));
                                if (api)
                                    Require(!choices[1].IsEnabled && !choices[2].IsEnabled, "Registered API roles retain registry model/effort settings.");
                                else
                                {
                                    choices[1].SelectedItem = choices[1].Items.OfType<Choice>().Single(c => c.Id == "claude-opus-5-5");
                                    choices[2].SelectedItem = choices[2].Items.OfType<Choice>().Single(c => c.Id == "ultracode");
                                }
                                Click(editor, "추가");
                            }
                            catch (Exception error) { editorError = error; }
                            finally { editor?.Close(); }
                        }, DispatcherPriority.Background).Task.Unwrap();
                        Click(dialog, "하위 에이전트 추가");
                        await driveEditor;
                        if (editorError is not null) throw editorError;
                        dialog.UpdateLayout();
                    }
                    await AddRoleAsync(false);
                    await AddRoleAsync(true);
                    Require(Roles().Items.Count == 5 && Roles().Items.OfType<Choice>().Any(c => c.ToString().Contains("한국어 코드 검토")),
                        "Korean role-purpose input is trimmed and displayed beside the selected account.");
                    Click(dialog, "프리셋 저장");
                    await WaitUntilAsync(() => saved.N("revision") == 3 && Ready(dialog, "프리셋 저장"));
                    var lastSave = calls.Last(c => c.Command == "presets.save").Input.Get("preset");
                    var claude = lastSave.Arr("roles").Single(r => r.S("name") == "한국어 코드 검토");
                    var apiRole = lastSave.Arr("roles").Single(r => r.S("name") == "API 결과 확인");
                    Require(claude.S("profile_id") == ClaudeId && claude.S("model") == "claude-opus-5-5" && claude.S("effort") == "ultracode" && apiRole.S("model_id") == ModelId && failures.Count == 0,
                        "Saving newly named roles preserves explicit Claude account/Opus 5.5/UltraCode and the API model reference.");
                    Click(dialog, "삭제");
                    await WaitUntilAsync(() => deleted && Presets().Items.Count == 0 && Ready(dialog, "삭제"));
                    Require(history.Count == 3 && Name().Text == "새 조합" && calls.Last(c => c.Command == "presets.delete").Input.N("expected_revision") == 3,
                        "Delete uses the current revision and resets the editor while the fixture keeps revision history.");
                    Require(failures.Count == 0 && calls.All(c => c.Command is "presets.list" or "presets.save" or "presets.default" or "presets.delete"),
                        "All requests stay within the fake preset registry; no service, account login, model call or lifecycle action occurs.");
                }
                catch (Exception error) { driverError = error; }
                finally { dialog?.Close(); }
            }
            var driver = owner.Dispatcher.InvokeAsync(DriveAsync, DispatcherPriority.Background).Task.Unwrap();
            await Dialogs.ExecutionPresetsAsync(owner, OwnerId, Request);
            await driver;
            if (driverError is not null) throw driverError;
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, checks,
                requests = calls.Select(c => c.Command), isolation = "Hidden fixture windows and an in-memory registry only; no production services or accounts." },
                new JsonSerializerOptions { WriteIndented = true }));
        }
        finally
        {
            foreach (var window in Application.Current.Windows.OfType<Window>().Where(w => w != owner).ToArray()) window.Close();
            owner.Close();
            if (previousStyle is not null) Application.Current.Resources[typeof(Window)] = previousStyle;
            else Application.Current.Resources.Remove(typeof(Window));
        }
    }

    private static bool Ready(Window window, string text) => Descendants(window).OfType<Button>().Any(b => Equals(b.Content, text) && b.IsEnabled);
    private static void Click(Window window, string text) => Descendants(window).OfType<Button>().Single(b => Equals(b.Content, text))
        .RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
    private static async Task<Window> WaitWindowAsync(string title)
    {
        Window? found = null;
        await WaitUntilAsync(() => (found = Application.Current.Windows.OfType<Window>().FirstOrDefault(w => w.Title == title && w.IsLoaded)) is not null);
        return found!;
    }
    private static async Task WaitUntilAsync(Func<bool> ready)
    {
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (!ready())
        {
            if (DateTime.UtcNow >= deadline) throw new TimeoutException("Preset fixture did not settle before its deadline.");
            await Task.Delay(10);
        }
    }
    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i); yield return child;
            foreach (var next in Descendants(child)) yield return next;
        }
    }
}
