using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal sealed class PersonalSkillsWindow : Window
{
    private readonly Func<string, object, Task<JsonElement>> request;
    private readonly Func<string, bool> confirm;
    private readonly TextBox search = new() { Name = "SkillSearch", Margin = new Thickness(0, 8, 0, 10), Padding = new Thickness(10, 8, 10, 8) };
    private readonly TextBlock count = new() { Foreground = Muted, Margin = new Thickness(0, 0, 0, 10) };
    private readonly TextBlock feedback = new() { TextWrapping = TextWrapping.Wrap, Foreground = Muted, Margin = new Thickness(0, 12, 0, 0) };
    private readonly StackPanel cards = new();
    private readonly StackPanel deleted = new();
    private readonly Expander restore;
    private readonly Button refresh;
    private JsonElement data;
    private bool busy;
    private static readonly SolidColorBrush Muted = new(Color.FromRgb(159, 173, 194));

    internal PersonalSkillsWindow(Func<string, object, Task<JsonElement>> request, Func<string, bool>? confirm = null)
    {
        this.request = request;
        this.confirm = confirm ?? (name => MessageBox.Show(this,
            $"‘{name}’ 스킬을 공통 목록에서 삭제할까요? 본앱과 모든 프로필에 적용됩니다. 아래 ‘삭제한 스킬’에서 복구할 수 있습니다.",
            "개인 스킬 삭제", MessageBoxButton.YesNo, MessageBoxImage.Question) == MessageBoxResult.Yes);
        Title = "공통 개인 스킬"; Width = 760; Height = 760; MinWidth = 560; MinHeight = 480;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        Background = new SolidColorBrush(Color.FromRgb(23, 25, 30));
        var layout = new DockPanel { Margin = new Thickness(24) };
        var header = new StackPanel();
        var titleRow = new DockPanel();
        refresh = new Button { Content = "새로고침", Margin = new Thickness(12, 0, 0, 0) };
        refresh.Click += async (_, _) => await LoadAsync();
        DockPanel.SetDock(refresh, Dock.Right); titleRow.Children.Add(refresh);
        titleRow.Children.Add(new TextBlock { Text = "공통 개인 스킬", FontSize = 23, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        header.Children.Add(titleRow);
        header.Children.Add(new TextBlock { Text = "본앱 · 모든 프로필이 같은 설정을 사용합니다.", Foreground = Muted, Margin = new Thickness(0, 10, 0, 4) });
        header.Children.Add(new TextBlock { Text = "어느 앱에서 바꿔도 함께 반영됩니다. 기본 제공·플러그인·프로젝트 스킬은 이 목록에 포함하지 않습니다.", Foreground = Muted, TextWrapping = TextWrapping.Wrap, FontSize = 12 });
        header.Children.Add(new TextBlock { Text = "지원하는 스킬은 SSH 작업에서도 사용할 수 있습니다.", Foreground = Muted, TextWrapping = TextWrapping.Wrap, FontSize = 12, Margin = new Thickness(0, 4, 0, 0) });
        header.Children.Add(new TextBlock { Text = "스킬 검색", Margin = new Thickness(0, 18, 0, 0) });
        header.Children.Add(search); header.Children.Add(count);
        DockPanel.SetDock(header, Dock.Top); layout.Children.Add(header);
        var footer = new StackPanel();
        restore = new Expander { Header = "삭제한 스킬", Content = new ScrollViewer { Content = deleted, MaxHeight = 150, VerticalScrollBarVisibility = ScrollBarVisibility.Auto } };
        footer.Children.Add(restore); footer.Children.Add(feedback);
        DockPanel.SetDock(footer, Dock.Bottom); layout.Children.Add(footer);
        layout.Children.Add(new ScrollViewer { Content = cards, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled });
        Content = layout;
        search.TextChanged += (_, _) => Render();
    }

    internal Task LoadAsync() => RunAsync("skills.personal.list", new { });

    private async Task RunAsync(string command, object args)
    {
        if (busy) return;
        busy = true; cards.IsEnabled = deleted.IsEnabled = refresh.IsEnabled = false;
        feedback.Text = "공통 설정을 확인하고 있습니다…";
        try
        {
            data = await request(command, args);
            Render();
            var errors = data.Get("sync").Arr("errors").ToArray();
            feedback.Foreground = errors.Length == 0 ? Muted : Brushes.Orange;
            feedback.Text = errors.Length == 0
                ? $"본앱 포함 {data.Get("sync").N("applied")}개에 저장됨 · 열린 앱의 목록은 다시 열 때 반영될 수 있습니다."
                : "일부 적용 대기 · " + string.Join(" / ", errors.Select(e => e.S("profile") + ": " + e.S("message")));
        }
        catch (Exception error) { feedback.Foreground = Brushes.Orange; feedback.Text = "변경을 완료하지 못했습니다 · " + error.Message; }
        finally { busy = false; cards.IsEnabled = deleted.IsEnabled = refresh.IsEnabled = true; }
    }

    private void Render()
    {
        if (data.ValueKind != JsonValueKind.Object) return;
        cards.Children.Clear(); deleted.Children.Clear();
        var all = data.Arr("skills").ToArray();
        count.Text = $"사용 중 {all.Count(s => s.B("enabled"))}개 / 전체 {all.Length}개";
        var query = search.Text.Trim();
        var visible = all.Where(s => query.Length == 0 || (s.S("name") + " " + s.S("description")).Contains(query, StringComparison.OrdinalIgnoreCase));
        foreach (var skill in visible.OrderByDescending(s => s.B("enabled")).ThenBy(s => s.S("name")))
        {
            string id = skill.S("id"), name = skill.S("name"); bool enabled = skill.B("enabled");
            var row = new Grid { Margin = new Thickness(14) };
            row.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            row.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition());
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var label = new StackPanel { Margin = new Thickness(0, 0, 16, 0), ToolTip = skill.S("path") };
            label.Children.Add(new TextBlock { Text = name, FontSize = 15, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap });
            label.Children.Add(new TextBlock { Text = skill.S("description"), FontSize = 12, Foreground = Muted, TextWrapping = TextWrapping.Wrap, MaxHeight = 38, Margin = new Thickness(0, 5, 0, 0) });
            row.Children.Add(label);
            var toggle = new Button { Name = "ToggleSkill", Tag = id, Content = enabled ? "사용 중" : "꺼짐", Width = 76, Height = 34, Padding = new Thickness(0),
                HorizontalContentAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0),
                Background = new SolidColorBrush(enabled ? Color.FromRgb(44, 64, 96) : Color.FromRgb(40, 44, 52)), ToolTip = enabled ? name + " 사용 끄기" : name + " 사용 켜기" };
            toggle.Click += async (_, _) => await RunAsync("skills.personal.set", new { skill_id = id, enabled = !enabled });
            Grid.SetColumn(toggle, 1); row.Children.Add(toggle);
            var remove = new Button { Name = "DeleteSkill", Tag = id, Content = "삭제", Height = 34, Padding = new Thickness(10, 0, 10, 0), Margin = new Thickness(8, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center, Background = Brushes.Transparent };
            remove.Click += async (_, _) => { if (confirm(name)) await RunAsync("skills.personal.delete", new { skill_id = id }); };
            Grid.SetColumn(remove, 2); row.Children.Add(remove);
            var bridge = skill.Get("bridge");
            bool bridgeSupported = bridge.B("supported"), bridgeEnabled = bridge.B("enabled");
            var bridgeRow = new Grid { Margin = new Thickness(0, 12, 0, 0) };
            bridgeRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            bridgeRow.ColumnDefinitions.Add(new ColumnDefinition());
            var bridgeToggle = new Button { Name = "ToggleSkillBridge", Tag = id, Content = bridgeEnabled ? "SSH에서 사용 중" : "SSH에서 사용", Width = 128, Height = 32,
                Padding = new Thickness(0), Margin = new Thickness(0, 0, 12, 0), IsEnabled = bridgeSupported, VerticalAlignment = VerticalAlignment.Top,
                HorizontalContentAlignment = HorizontalAlignment.Center, VerticalContentAlignment = VerticalAlignment.Center,
                Background = new SolidColorBrush(bridgeEnabled ? Color.FromRgb(44, 64, 96) : Color.FromRgb(40, 44, 52)),
                ToolTip = bridgeSupported ? name + (bridgeEnabled ? " SSH 사용 끄기" : " SSH 사용 켜기") : "이 스킬은 SSH 사용을 지원하지 않습니다." };
            bridgeToggle.Click += async (_, _) => await RunAsync("skills.bridge.set", new { id, enabled = !bridgeEnabled });
            bridgeRow.Children.Add(bridgeToggle);
            var bridgeStatus = new TextBlock { Name = "SkillBridgeStatus", Tag = id, Text = BridgeMessage(bridge), Foreground = Muted,
                FontSize = 12, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
            Grid.SetColumn(bridgeStatus, 1); bridgeRow.Children.Add(bridgeStatus);
            Grid.SetRow(bridgeRow, 1); Grid.SetColumnSpan(bridgeRow, 3); row.Children.Add(bridgeRow);
            cards.Children.Add(new Border { Child = row, CornerRadius = new CornerRadius(8), BorderThickness = new Thickness(1), BorderBrush = new SolidColorBrush(Color.FromRgb(49, 56, 68)), Background = new SolidColorBrush(Color.FromRgb(28, 32, 39)), Margin = new Thickness(0, 0, 8, 8) });
        }
        if (cards.Children.Count == 0) cards.Children.Add(new TextBlock { Text = "표시할 개인 스킬이 없습니다.", Margin = new Thickness(4, 18, 0, 0), Foreground = Muted });
        foreach (var entry in data.Arr("deleted"))
        {
            var id = entry.S("id");
            var button = new Button { Name = "RestoreSkill", Tag = id, Content = entry.S("name") + " · 복구" };
            button.Click += async (_, _) => await RunAsync("skills.personal.restore", new { deleted_id = id }); deleted.Children.Add(button);
        }
        restore.Header = $"삭제한 스킬 {deleted.Children.Count}개";
    }

    private static string BridgeMessage(JsonElement bridge)
    {
        var message = bridge.S("message");
        if (message.Length > 0) return message;
        if (!bridge.B("supported")) return "SSH 사용을 지원하지 않는 스킬입니다.";
        if (!bridge.B("enabled")) return "SSH에서 사용하지 않습니다.";
        return bridge.S("status") switch
        {
            "ready" or "connected" => "SSH에서 사용할 준비가 되었습니다.",
            "error" or "unavailable" => "SSH 사용 상태를 확인해 주세요.",
            _ => "SSH 연결을 기다리고 있습니다."
        };
    }
}
