using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class ClaudeProfileSelfTest
{
    internal static async Task RunAsync(string report)
    {
        static void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
        var checks = new List<string>();
        static JsonElement Profile(string state, bool loggedIn = false, object? usageData = null) => JsonSerializer.SerializeToElement(new
        {
            id = "claude-fixture", alias = "Claude 계정", auth_mode = "claude_code", status = "running", runtime_channel = "managed",
            claude_settings = new { model = "sonnet", reasoning_effort = "high", context_window = 200000, auto_compact_percent = 85 },
            claude_status = new { state, logged_in = loggedIn, masked_email = "c***@example.test", cli_version = "fixture" },
            login_health = new { blocks_launch = true, reason = "expired_gpt" },
            usage = usageData ?? new { windows = new[] { new { label = "주간", remaining_percent = 42 } }, reset_credits = new { available = 3 } }
        });
        var profile = Profile("signed_in", true);
        var card = ProfileCardData.Create(profile, "fixture", cache: "첫 요청 ≈ 38 크레딧");
        Require(card.ProviderBadge == "Claude" && card.Model == "Sonnet" && card.Status == "로그인됨" && card.Account == ""
            && !card.DetailHint.Contains('@'), "Claude card did not identify its model and CLI login while hiding email by default.");
        Require(!card.HasQuota && card.NativeVisibility == Visibility.Collapsed && card.QuotaText == "" && card.RedeemText == "" && card.Cache == "",
            "Claude inherited GPT quota, reset credits, or cache-cost estimates.");
        Require(!ProfileLoginPresentation.NeedsLogin(profile) && !ProfileLoginPresentation.ShowRecovery(Profile("signed_out"))
            && !ProfileLoginPresentation.RequiresRestartForLogin(profile, default), "Claude entered ChatGPT login recovery or restart.");
        Require(ProfileCardData.Create(Profile("cli_missing"), "fixture").Status == "CLI 설치 필요"
            && ProfileCardData.Create(Profile("blocked", true), "fixture").Status == "사용 중지됨"
            && ProfileLoginPresentation.NeedsLogin(Profile("signed_out")), "CLI, blocked, and signed-out statuses were conflated.");
        checks.Add("Claude cards use CLI status and hide email by default; stale GPT login, quotas, credits and cache prices are ignored.");
        var shortcut = ShortcutCardData.Create(JsonSerializer.SerializeToElement(new { alias = "작업", thread_id = "task" }), profile,
            new Dictionary<string, (string State, string Alias)>(), []);
        Require(shortcut.Detail.Contains(ClaudeProfilePresentation.UsageHint) && !shortcut.Detail.Contains("42%"), "Task shortcuts showed GPT quota for Claude.");
        Require(card.ClaudeFiveHour is { HasValue: false, Text: "미확인" } && card.ClaudeWeekly is { HasValue: false, Text: "미확인" },
            "Missing Claude usage was rendered as exhausted or inherited GPT values.");
        static object Usage(string freshness = "live") => new
        {
            provider = "claude_code", freshness, observed_at = "2026-09-29T01:00:00Z",
            windows = new[] { new { key = "five_hour", remaining_percent = 73.5, resets_at = 1790654400L },
                new { key = "seven_day", remaining_percent = 0.0, resets_at = 1791259200L } }
        };
        profile = Profile("signed_in", true, Usage());
        card = ProfileCardData.Create(profile, "fixture");
        Require(card.ClaudeFiveHour is { HasValue: true, Remaining: 73.5, Text: "73.5%" }
            && card.ClaudeWeekly is { HasValue: true, Remaining: 0, Text: "0%" }
            && card.ClaudeFiveHour.ResetText.Contains("초기화") && !card.ClaudeFiveHour.ResetText.Contains("미확인")
            && card.RedeemText == "" && card.Cache == "", "Live Claude windows or reset times were not presented independently.");
        var staleProfile = Profile("signed_in", true, Usage("stale"));
        var stale = ClaudeUsagePresentation.Read(staleProfile);
        Require(stale.FiveHour is { HasValue: true, Remaining: 73.5, Stale: true } && stale.FiveHour.Text.Contains("이전 값")
            && stale.Hint.Contains("이전 사용량"), "Stale Claude values lost their freshness label.");
        var partial = ClaudeUsagePresentation.Read(Profile("signed_in", true, new
        {
            provider = "claude_code", freshness = "live",
            windows = new[] { new { key = "five_hour", used_percent = 20.0, resets_at = (long?)null } }
        }));
        Require(partial.FiveHour.Remaining == 80 && partial.FiveHour.ResetText.Contains("미확인")
            && !partial.Weekly.HasValue && partial.Weekly.MeterVisibility == Visibility.Collapsed,
            "A missing subscription window or reset time became a fabricated value.");
        var bad = ClaudeUsagePresentation.Read(Profile("signed_in", true, new
        { provider = "claude_code", freshness = "live", windows = new[] { new { key = "five_hour", remaining_percent = -1 } } }));
        Require(!bad.FiveHour.HasValue, "Malformed quota was converted to exhausted quota.");
        const string providerReset = "Resets Oct 1, 10am (UTC)";
        var renderedReset = ClaudeUsagePresentation.Read(Profile("signed_in", true, new
        {
            provider = "claude_code", freshness = "live", windows = new[]
            { new { key = "five_hour", remaining_percent = 75, resets_at = (long?)null, reset_text = providerReset } }
        }));
        Require(renderedReset.FiveHour.ResetText == providerReset,
            "Provider-rendered reset text was discarded or converted into a guessed timestamp.");
        var mixedFreshness = ClaudeUsagePresentation.Read(Profile("signed_in", true, new
        {
            provider = "claude_code", freshness = "stale", windows = new[]
            {
                new { key = "five_hour", remaining_percent = 75, freshness = "live" },
                new { key = "seven_day", remaining_percent = 30, freshness = "stale" }
            }
        }));
        Require(!mixedFreshness.FiveHour.Stale && mixedFreshness.Weekly.Stale && mixedFreshness.Hint.Contains("일부"),
            "One old window incorrectly invalidated a freshly observed independent window.");
        shortcut = ShortcutCardData.Create(JsonSerializer.SerializeToElement(new { alias = "작업", thread_id = "task" }), profile,
            new Dictionary<string, (string State, string Alias)>(), []);
        Require(shortcut.Outer == 0 && shortcut.Inner == 73.5 && shortcut.Detail.Contains("73.5%") && shortcut.Detail.Contains("초기화"),
            "Claude task shortcuts did not use the actual subscription windows.");
        var staleShortcut = ShortcutCardData.Create(JsonSerializer.SerializeToElement(new { alias = "작업", thread_id = "task" }), staleProfile,
            new Dictionary<string, (string State, string Alias)>(), []);
        Require(double.IsNaN(staleShortcut.Outer) && double.IsNaN(staleShortcut.Inner) && staleShortcut.Detail.Contains("이전 값"),
            "Stale subscription quotas remained active task meters.");
        checks.Add("Two actual Claude usage windows and resets are shown; missing, exhausted, malformed and stale values remain distinct.");

        Require(Dialogs.ReadClaudeContext("custom", "200,000", "custom", "85", out var tokens, out var percent) && tokens == 200000 && percent == 85
            && !Dialogs.ReadClaudeContext("custom", "100000", "custom", "85", out _, out _)
            && !Dialogs.ReadClaudeContext("custom", "200000", "custom", "96", out _, out _)
            && !Dialogs.ReadClaudeContext("custom", "1000001", "auto", "", out _, out _)
            && Dialogs.ReadClaudeContext("auto", "", "auto", "", out tokens, out percent) && tokens is null && percent is null
            && Dialogs.ReadClaudeContext("custom", "100000", "auto", "", out tokens, out percent) && tokens == 100000 && percent is null,
            "Native context/compression defaults or custom bounds were not enforced.");
        var fields = Dialogs.AddClaudeSettingsEditor(new StackPanel(), default);
        static void Select(ComboBox combo, string id) => combo.SelectedItem = combo.Items.OfType<Choice>().Single(choice => choice.Id == id);
        Require(fields.Model.Items.OfType<Choice>().Select(choice => choice.Id).SequenceEqual(new[] { "opus", "sonnet", "fable", "claude-opus-5-5" }),
            "Claude model choices lost the aliases or explicit Opus 5.5 selection.");
        Require(!fields.Context.IsEnabled && !fields.Compact.IsEnabled
            && fields.Read() is { } defaults && defaults["context_window"] is null && defaults["auto_compact_percent"] is null
            && fields.Preview.Text.Contains("1,000,000") && fields.Preview.Text.Contains("고정 비율"),
            "Fresh settings did not select independent native defaults.");
        Select(fields.ContextMode, "custom"); Select(fields.CompactMode, "custom"); Select(fields.Effort, "ultracode");
        Select(fields.Model, "claude-opus-5-5");
        fields.Context.Text = "300,000"; fields.Compact.Text = "90";
        Require(fields.Context.IsEnabled && !fields.Context.IsReadOnly && fields.Compact.IsEnabled && !fields.Compact.IsReadOnly
            && fields.Read() is { } custom && Equals(custom["context_window"], 300000) && Equals(custom["auto_compact_percent"], 90)
            && Equals(custom["model"], "claude-opus-5-5") && Equals(custom["reasoning_effort"], "ultracode"),
            "Custom settings were not editable or Opus 5.5 with UltraCode did not round-trip.");
        Select(fields.ContextMode, "auto");
        Require(fields.Read() is { } mixed && mixed["context_window"] is null && Equals(mixed["auto_compact_percent"], 90),
            "Automatic context incorrectly removed a custom compression override.");
        Select(fields.ContextMode, "custom");
        Require(fields.Context.Text == "300,000", "Switching mode erased the user's custom context draft.");
        fields.Context.Text = "100000";
        Require(fields.Read() is null, "Invalid custom compaction threshold was accepted.");
        var savedFields = Dialogs.AddClaudeSettingsEditor(new StackPanel(), profile.Get("claude_settings"));
        Require(savedFields.Context.IsEnabled && savedFields.Compact.IsEnabled && savedFields.Read() is { } saved
            && Equals(saved["context_window"], 200000) && Equals(saved["auto_compact_percent"], 85),
            "Existing explicit context/compression overrides were not preserved.");
        foreach (var effort in new[] { "low", "medium", "high", "xhigh", "max", "ultracode" })
        {
            var versioned = Dialogs.AddClaudeSettingsEditor(new StackPanel(), JsonSerializer.SerializeToElement(new
                { model = "claude-opus-5-5", reasoning_effort = effort }));
            Require(versioned.Read() is { } preserved && Equals(preserved["model"], "claude-opus-5-5")
                && Equals(preserved["reasoning_effort"], effort), "Opening settings changed the saved versioned model or effort.");
            Select(versioned.Model, "sonnet");
            Require(versioned.Read() is { } switched && Equals(switched["reasoning_effort"], effort),
                "Changing the default model reset the user's saved effort.");
        }
        Require(ClaudeProfilePresentation.Model(JsonSerializer.SerializeToElement(new
            { claude_settings = new { model = "claude-opus-5-5" } })) == "Opus 5.5", "Explicit Opus 5.5 was not presented with its display name.");
        checks.Add("Native defaults and custom context values are preserved; Opus 5.5 and UltraCode round-trip without resetting saved effort.");

        var state = JsonSerializer.SerializeToElement(new { profiles = new[] { profile } });
        var commands = new List<string>();
        var root = Path.Combine(Path.GetTempPath(), "codex-claude-ui-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        var window = new MainWindow(root, fixture: true, fixtureRequest: (command, args) =>
        {
            commands.Add(command);
            return Task.FromResult(command switch { "state" => state, "claude.status" => profile.Get("claude_status"), "claude.usage" => profile.Get("usage"),
                _ => throw new InvalidOperationException("Unexpected request: " + command) });
        }) { FixtureRefreshesState = true, WindowState = WindowState.Normal, Width = 1440, Height = 960,
            Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        try
        {
            window.UseFixture(state);
            window.Show();
            await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            window.UpdateLayout();
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
            var refresh = typeof(MainWindow).GetMethod("RefreshLoginStatusAsync", flags, [typeof(string)])!;
            await (Task)refresh.Invoke(window, ["claude-fixture"])!;
            Require(commands.SequenceEqual(new[] { "claude.status", "claude.usage", "state" }), "Claude refresh called ChatGPT login or quota RPCs.");
            var account = (TextBlock)typeof(MainWindow).GetField("_accountState", flags)!.GetValue(window)!;
            var repair = (Button)typeof(MainWindow).GetField("_loginRepair", flags)!.GetValue(window)!;
            Require(account.Text.Contains("Claude Sonnet") && account.Text.Contains("로그인됨") && Equals(repair.Content, "Claude 로그인 · 설정"),
                "Selected-profile login controls presented Claude as GPT.");
            var menu = new ContextMenu();
            var subagents = new MenuItem { Header = "하위 에이전트 설정" }; menu.Items.Add(subagents);
            var presentation = typeof(MainWindow).GetMethod("SetClaudeProfileMenu", BindingFlags.Static | BindingFlags.NonPublic)!;
            presentation.Invoke(null, [menu, profile]);
            Require(subagents.Visibility == Visibility.Collapsed && ProfileAgentPresentation.Badge(profile) == "", "Claude exposed native subagent policy.");
            var providers = typeof(MainWindow).GetMethod("ShowProvidersAsync", flags)!;
            await (Task)providers.Invoke(window, ["claude-fixture"])!;
            Require(commands.SequenceEqual(new[] { "claude.status", "claude.usage", "state" }), "Claude opened native subagent provider policy.");
            static IEnumerable<DependencyObject> Descendants(DependencyObject value)
            {
                for (int index = 0; index < VisualTreeHelper.GetChildrenCount(value); index++)
                {
                    var child = VisualTreeHelper.GetChild(value, index); yield return child;
                    foreach (var descendant in Descendants(child)) yield return descendant;
                }
            }
            var labels = Descendants(window).OfType<TextBlock>().Where(block => block.IsVisible).Select(block => block.Text).ToArray();
            Require(labels.Contains("Claude · Sonnet") && !labels.Any(label => label.Contains('@'))
                && labels.Contains("5시간") && labels.Contains("주간") && labels.Contains("73.5%") && labels.Contains("0%"),
                "Claude card bindings did not hide email or render both usage windows.");
            var layout = (FrameworkElement)window.Content;
            var image = new RenderTargetBitmap((int)layout.ActualWidth, (int)layout.ActualHeight, 96, 96, PixelFormats.Pbgra32);
            image.Render(layout);
            var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(image));
            using (var output = File.Create(Path.ChangeExtension(report, ".png"))) png.Save(output);
            checks.Add("Selected-profile refresh calls claude.status, claude.usage and state; Claude login/settings stays available.");
            checks.Add("Rendered Claude cards show the model and both subscription windows; account email and native subagent policy are hidden.");
        }
        finally { window.Close(); }
        checks.AddRange(await ProfileEmailSelfTest.RunAsync());
        File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, checks }, new JsonSerializerOptions { WriteIndented = true }));
    }
}
