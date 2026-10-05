using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Shapes;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

// Left side of the workspace: a profile rail that expands into cards, the task
// shortcut column beside it (an overlay when the window is narrow), and the
// settings flyout. The embedded Codex view is a separate native window above
// the manager, so anything drawn over it must be a Popup.
public sealed partial class MainWindow
{
    private const double RailWidth = 68, ExpandedProfileWidth = 304, ShortcutSplitWidth = 6, OverlayMaxWidth = 360;
    // The workspace keeps at least this width beside the docked shortcut
    // column; below it the column becomes an overlay instead of squeezing Codex.
    internal const double MinWorkspaceWidth = 760;
    private readonly Grid _layout = new() { Name = "WorkspaceLayout" };
    private readonly ColumnDefinition _profileColumn = new() { Width = new GridLength(RailWidth) };
    private readonly ColumnDefinition _shortcutColumn = new() { Width = new GridLength(SidebarLayout.DefaultShortcutWidth) };
    private readonly ColumnDefinition _shortcutSplitColumn = new() { Width = new GridLength(ShortcutSplitWidth) };
    private SidebarLayout _sidebarLayout = null!;
    private bool _profilesExpanded, _shortcutsDocked = true;
    private readonly Border _profilePanel = new() { Name = "ProfilePanel", Background = WorkspaceAppearance.Rail,
        BorderBrush = WorkspaceAppearance.Divider, BorderThickness = new Thickness(0, 0, 1, 0) };
    private readonly DockPanel _profileHeader = new() { Margin = new Thickness(16, 14, 10, 8) };
    private readonly Border _profileTools = new() { Name = "ProfileTools", BorderBrush = WorkspaceAppearance.Divider };
    private readonly Button _profilePanelToggle = new();
    private readonly Button _shortcutOverlayToggle = new();
    private readonly Button _settingsButton = new();
    private readonly Ellipse _settingsBadge = new() { Name = "WorkspaceSettingsBadge", Width = 9, Height = 9, StrokeThickness = 1.5,
        Stroke = WorkspaceAppearance.Rail, HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Top,
        Visibility = Visibility.Collapsed, IsHitTestVisible = false };
    private Button _addProfile = null!, _allRecords = null!, _exitWorkspace = null!;
    private readonly Border _shortcutPanel = new() { Name = "ShortcutPanel", Background = WorkspaceAppearance.Surface };
    private readonly TextBox _shortcutFilter = new() { Name = "ShortcutFilter" };
    private readonly Border _shortcutFilterFrame = new();
    private readonly TextBlock _shortcutCount = new() { Name = "ShortcutCount", FontSize = 12, Foreground = WorkspaceAppearance.Faint,
        VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
    private readonly TextBlock _shortcutEmpty = new() { Name = "ShortcutEmpty", FontSize = 12, Foreground = WorkspaceAppearance.Faint,
        TextAlignment = TextAlignment.Center, TextWrapping = TextWrapping.Wrap, LineHeight = 20, Margin = new Thickness(24, 32, 24, 0),
        HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Top, Visibility = Visibility.Collapsed };
    private readonly Button _shortcutOverlayClose = new();
    private readonly Border _overlayFrame = new() { Name = "ShortcutOverlayFrame", Background = WorkspaceAppearance.Surface,
        BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(0, 0, 1, 0) };
    private readonly Popup _shortcutOverlay = new() { Name = "ShortcutOverlay", StaysOpen = false, AllowsTransparency = true,
        Placement = PlacementMode.Relative, PopupAnimation = PopupAnimation.None };
    private readonly Popup _settingsFlyout = new() { Name = "SettingsFlyout", StaysOpen = false, AllowsTransparency = true,
        Placement = PlacementMode.Right, PopupAnimation = PopupAnimation.None };
    private readonly ScrollViewer _settingsScroll = new() { Name = "SettingsFlyoutScroll", VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
    private readonly ContentControl _profileSummary = new() { Name = "ProfileSummaryHost", Focusable = false, IsTabStop = false,
        Visibility = Visibility.Collapsed };
    private readonly Border _updateBanner = new() { Name = "WorkspaceUpdateBanner", CornerRadius = new CornerRadius(6),
        Padding = new Thickness(10, 6, 6, 6), Margin = new Thickness(0, 10, 0, 0), Visibility = Visibility.Collapsed, Cursor = Cursors.Hand };
    private readonly TextBlock _updateBannerTitle = new() { FontSize = 12, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _updateBannerDetail = new() { FontSize = 12, Foreground = WorkspaceAppearance.Muted, VerticalAlignment = VerticalAlignment.Center,
        TextTrimming = TextTrimming.CharacterEllipsis, Margin = new Thickness(10, 0, 0, 0) };

    // Columns: profiles (rail or cards) | shortcuts | divider | workspace.
    private void BuildSidebar(Button profileEmail)
    {
        _layout.ColumnDefinitions.Add(_profileColumn);
        _layout.ColumnDefinitions.Add(_shortcutColumn);
        _layout.ColumnDefinitions.Add(_shortcutSplitColumn);
        _layout.ColumnDefinitions.Add(new ColumnDefinition { MinWidth = 420 });
        _sidebarLayout = new SidebarLayout(_root, _shortcutColumn, Log);
        _sidebarLayout.ShortcutWidthChanged += ArrangeSidebar;
        Grid.SetColumn(_sidebarLayout.Divider, 2);
        _layout.Children.Add(_sidebarLayout.Divider);

        // Profile panel: header (expanded only), list, tools.
        _profilePanel.Child = new Grid();
        var panel = (Grid)_profilePanel.Child;
        panel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        panel.RowDefinitions.Add(new RowDefinition());
        panel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var title = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        title.Children.Add(new TextBlock { Text = "프로필", FontSize = 13, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        _profileCount.FontSize = 12; _profileCount.Foreground = WorkspaceAppearance.Faint; _profileCount.Margin = new Thickness(8, 0, 0, 0);
        title.Children.Add(_profileCount);
        profileEmail.Foreground = WorkspaceAppearance.Muted; profileEmail.FontSize = 12;
        DockPanel.SetDock(profileEmail, Dock.Right);
        _profileHeader.Children.Add(profileEmail);
        _profileHeader.Children.Add(title);
        panel.Children.Add(_profileHeader);
        _profiles.Background = Brushes.Transparent;
        _profiles.BorderThickness = new Thickness(0);
        AutomationProperties.SetName(_profiles, "프로필 목록");
        Grid.SetRow(_profiles, 1); panel.Children.Add(_profiles);
        _addProfile = WorkspaceAppearance.Glyph(Action("＋", AddProfileAsync), "AddProfile", WorkspaceAppearance.GlyphAdd,
            "ChatGPT · Claude · 로컬 모델 · 외부 API 프로필 추가", 40);
        _allRecords = WorkspaceAppearance.Glyph(Action("전체 기록", ShowCatalogAsync), "AllRecords", WorkspaceAppearance.GlyphHistory,
            "전체 기록 · 대표 계정으로 전체 작업 기록 보기", 40);
        WorkspaceAppearance.Glyph(_settingsButton, "WorkspaceSettings", WorkspaceAppearance.GlyphSettings, "설정 및 관리", 40);
        var settingsGlyph = new Grid { Width = 22, Height = 22 };
        settingsGlyph.Children.Add(new TextBlock { Text = WorkspaceAppearance.GlyphSettings, FontFamily = WorkspaceAppearance.Icons, FontSize = 16,
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center });
        settingsGlyph.Children.Add(_settingsBadge);
        _settingsButton.Content = settingsGlyph;
        _settingsButton.Click += (_, _) => ToggleSettingsFlyout();
        WorkspaceAppearance.Glyph(_profilePanelToggle, "ToggleProfilePanel", WorkspaceAppearance.GlyphOpenPane, "프로필 펼쳐 보기", 40);
        _profilePanelToggle.Click += (_, _) => SetProfilesExpanded(!_profilesExpanded, save: true);
        WorkspaceAppearance.Glyph(_shortcutOverlayToggle, "ShortcutOverlayToggle", WorkspaceAppearance.GlyphList, "작업 바로가기 열기", 40);
        _shortcutOverlayToggle.Click += (_, _) => ToggleShortcutOverlay();
        _shortcutOverlayToggle.Visibility = Visibility.Collapsed;
        _exitWorkspace = WorkspaceAppearance.Glyph(Action("완전 종료…", ExitWorkspaceAsync,
            "관리 중인 모든 Codex 작업을 끝내고 종료합니다. 제목줄의 X는 작업을 유지한 채 창만 닫습니다."), "ExitWorkspace",
            WorkspaceAppearance.GlyphPower, "완전 종료… · 관리 중인 모든 Codex 작업을 끝내고 종료합니다. 제목줄의 X는 작업을 유지한 채 창만 닫습니다.", 40);
        Grid.SetRow(_profileTools, 2); panel.Children.Add(_profileTools);
        Grid.SetColumn(_profilePanel, 0); _layout.Children.Add(_profilePanel);

        BuildShortcutPanel();
        _overlayFrame.Effect = new DropShadowEffect { BlurRadius = 18, ShadowDepth = 0, Opacity = 0.55, Color = Colors.Black };
        _overlayFrame.Margin = new Thickness(0, 0, 18, 0);
        _shortcutOverlay.Child = _overlayFrame;
        _shortcutOverlay.PlacementTarget = _layout;
        // Clicking the toggle while open must only close the overlay, not reopen it.
        _shortcutOverlay.Opened += (_, _) => { _shortcutOverlayToggle.IsHitTestVisible = false; WorkspaceAppearance.Active(_shortcutOverlayToggle, true); };
        _shortcutOverlay.Closed += (_, _) => { _shortcutOverlayToggle.IsHitTestVisible = true; WorkspaceAppearance.Rest(_shortcutOverlayToggle); };
        _overlayFrame.PreviewKeyDown += (_, e) =>
        {
            if (e.Key != Key.Escape || _shortcutsDocked) return;
            e.Handled = true; CloseShortcutOverlay(); _shortcutOverlayToggle.Focus();
        };
        _layout.Children.Add(_shortcutOverlay);
        _layout.Children.Add(_settingsFlyout);
        _layout.SizeChanged += (_, _) =>
        {
            ArrangeSidebar();
            if (_shortcutOverlay.IsOpen) _overlayFrame.Height = Math.Max(240, _layout.ActualHeight);
        };
        // Popups do not follow the window, and a popup that lost its mouse
        // capture to a context menu no longer closes on an outside click.
        PreviewMouseDown += (_, e) =>
        {
            if (!_shortcutOverlay.IsOpen || _overlayFrame.IsMouseOver || InContextMenu(e.OriginalSource)) return;
            CloseShortcutOverlay();
        };
        Deactivated += (_, _) => { CloseShortcutOverlay(); _settingsFlyout.IsOpen = false; };
        LocationChanged += (_, _) => { CloseShortcutOverlay(); _settingsFlyout.IsOpen = false; };
        PreviewKeyDown += (_, e) =>
        {
            if (e.Key != Key.F || Keyboard.Modifiers != ModifierKeys.Control) return;
            e.Handled = true;
            if (_shortcutsDocked) { _shortcutFilter.Focus(); _shortcutFilter.SelectAll(); }
            else if (!_shortcutOverlay.IsOpen) ToggleShortcutOverlay();
        };
        SetProfilesExpanded(_sidebarLayout.ProfilesExpanded, save: false);
    }

    private void BuildShortcutPanel()
    {
        var grid = new Grid();
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition());
        var header = new DockPanel { Margin = new Thickness(16, 14, 8, 8) };
        var actions = new StackPanel { Orientation = Orientation.Horizontal };
        actions.Children.Add(WorkspaceAppearance.Glyph(Action("＋", AddShortcutAsync), "AddShortcut", WorkspaceAppearance.GlyphAdd, "작업 찾아서 바로가기 추가"));
        actions.Children.Add(WorkspaceAppearance.Glyph(MenuButton("바로가기 관리", ("지금 열린 작업 추가", CaptureShortcutAsync), ("삭제한 링크 복구", UndoShortcutAsync)),
            "ShortcutActions", WorkspaceAppearance.GlyphMore, "바로가기 관리"));
        WorkspaceAppearance.Glyph(_shortcutOverlayClose, "CloseShortcutOverlay", WorkspaceAppearance.GlyphClose, "작업 바로가기 닫기");
        _shortcutOverlayClose.Click += (_, _) => { CloseShortcutOverlay(); _shortcutOverlayToggle.Focus(); };
        _shortcutOverlayClose.Visibility = Visibility.Collapsed;
        actions.Children.Add(_shortcutOverlayClose);
        foreach (Button button in actions.Children) button.Margin = new Thickness(2, 0, 0, 0);
        DockPanel.SetDock(actions, Dock.Right); header.Children.Add(actions);
        var title = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center,
            ToolTip = "모든 프로필에서 함께 사용하는 작업 링크입니다. 누르면 지정한 계정에서 열립니다." };
        title.Children.Add(new TextBlock { Text = "작업 바로가기", FontSize = 13, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        title.Children.Add(_shortcutCount);
        header.Children.Add(title);
        grid.Children.Add(header);

        // Filter by task title, account name or SSH host.
        var search = new Grid();
        search.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        search.ColumnDefinitions.Add(new ColumnDefinition());
        search.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        search.Children.Add(new TextBlock { Text = WorkspaceAppearance.GlyphSearch, FontFamily = WorkspaceAppearance.Icons, FontSize = 12,
            Foreground = WorkspaceAppearance.Faint, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(10, 0, 0, 0) });
        var placeholder = new TextBlock { Text = "작업 · 프로필 검색", FontSize = 13, Foreground = WorkspaceAppearance.Faint,
            VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0), IsHitTestVisible = false };
        Grid.SetColumn(placeholder, 1); search.Children.Add(placeholder);
        _shortcutFilter.Background = Brushes.Transparent; _shortcutFilter.BorderThickness = new Thickness(0);
        _shortcutFilter.Padding = new Thickness(6, 7, 4, 7); _shortcutFilter.Margin = new Thickness(0); _shortcutFilter.FontSize = 13;
        _shortcutFilter.VerticalContentAlignment = VerticalAlignment.Center;
        AutomationProperties.SetName(_shortcutFilter, "작업 바로가기 검색");
        _shortcutFilter.ToolTip = "작업 제목, 프로필 이름, SSH 호스트로 찾기 · Ctrl+F로 이동 · Esc로 지우기";
        Grid.SetColumn(_shortcutFilter, 1); search.Children.Add(_shortcutFilter);
        var clear = WorkspaceAppearance.Glyph(new Button(), "ClearShortcutFilter", WorkspaceAppearance.GlyphClose, "검색어 지우기", 28);
        clear.FontSize = 10; clear.Visibility = Visibility.Collapsed; clear.Margin = new Thickness(0, 0, 2, 0);
        clear.Click += (_, _) => { _shortcutFilter.Clear(); _shortcutFilter.Focus(); };
        Grid.SetColumn(clear, 2); search.Children.Add(clear);
        _shortcutFilter.TextChanged += (_, _) =>
        {
            var empty = _shortcutFilter.Text.Length == 0;
            placeholder.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
            clear.Visibility = empty ? Visibility.Collapsed : Visibility.Visible;
            if (_rendering) return;
            _rendering = true;
            try { RenderShortcuts(); }
            finally { _rendering = false; }
        };
        _shortcutFilter.PreviewKeyDown += (_, e) =>
        {
            if (e.Key == Key.Escape && _shortcutFilter.Text.Length > 0) { _shortcutFilter.Clear(); e.Handled = true; }
            else if (e.Key == Key.Down && _shortcuts.Items.Count > 0)
            {
                // Move into the list without opening anything; Enter opens the card.
                var target = _shortcuts.SelectedItem ?? _shortcuts.Items[0];
                _shortcuts.ScrollIntoView(target);
                _shortcuts.UpdateLayout();
                (_shortcuts.ItemContainerGenerator.ContainerFromItem(target) as ListBoxItem)?.Focus();
                e.Handled = true;
            }
        };
        _shortcutFilterFrame.Child = search;
        _shortcutFilterFrame.Background = WorkspaceAppearance.Canvas;
        _shortcutFilterFrame.BorderBrush = WorkspaceAppearance.Divider;
        _shortcutFilterFrame.BorderThickness = new Thickness(1);
        _shortcutFilterFrame.CornerRadius = new CornerRadius(6);
        _shortcutFilterFrame.Margin = new Thickness(12, 0, 12, 8);
        _shortcutFilter.GotKeyboardFocus += (_, _) => _shortcutFilterFrame.BorderBrush = WorkspaceAppearance.Accent;
        _shortcutFilter.LostKeyboardFocus += (_, _) => _shortcutFilterFrame.BorderBrush = WorkspaceAppearance.Divider;
        Grid.SetRow(_shortcutFilterFrame, 1); grid.Children.Add(_shortcutFilterFrame);

        _shortcuts.Background = Brushes.Transparent;
        _shortcuts.BorderThickness = new Thickness(0);
        AutomationProperties.SetName(_shortcuts, "작업 바로가기 목록");
        Grid.SetRow(_shortcuts, 2); grid.Children.Add(_shortcuts);
        Grid.SetRow(_shortcutEmpty, 2); grid.Children.Add(_shortcutEmpty);
        _shortcutPanel.Child = grid;
        Grid.SetColumn(_shortcutPanel, 1); _layout.Children.Add(_shortcutPanel);
    }

    // Rail: a centred column of 40-DIP icons. Expanded: one row of the same buttons.
    private void ArrangeProfileTools(bool expanded)
    {
        if (_profileTools.Child is Panel previous) previous.Children.Clear();
        static Border Separator() => new() { Background = WorkspaceAppearance.Divider, Width = 28, Height = 1, Margin = new Thickness(0, 6, 0, 6) };
        if (expanded)
        {
            var row = new DockPanel { Margin = new Thickness(8, 6, 8, 8), LastChildFill = false };
            foreach (var button in new[] { _addProfile, _shortcutOverlayToggle, _allRecords, _settingsButton, _profilePanelToggle })
            { DockPanel.SetDock(button, Dock.Left); button.Margin = new Thickness(0, 0, 2, 0); row.Children.Add(button); }
            DockPanel.SetDock(_exitWorkspace, Dock.Right); _exitWorkspace.Margin = new Thickness(0); row.Children.Add(_exitWorkspace);
            _profileTools.BorderThickness = new Thickness(0, 1, 0, 0);
            _profileTools.Child = row;
        }
        else
        {
            var column = new StackPanel { Margin = new Thickness(0, 4, 0, 10), HorizontalAlignment = HorizontalAlignment.Center };
            column.Children.Add(_addProfile);
            column.Children.Add(Separator());
            foreach (var button in new[] { _shortcutOverlayToggle, _allRecords, _settingsButton, _profilePanelToggle }) column.Children.Add(button);
            column.Children.Add(Separator());
            column.Children.Add(_exitWorkspace);
            foreach (var button in column.Children.OfType<Button>()) button.Margin = new Thickness(0, 2, 0, 2);
            _profileTools.BorderThickness = new Thickness(0);
            _profileTools.Child = column;
        }
    }

    private void SetProfilesExpanded(bool expanded, bool save)
    {
        if (save) _sidebarLayout.SetProfilesExpanded(expanded);
        _profilesExpanded = expanded;
        _profileColumn.Width = new GridLength(expanded ? ExpandedProfileWidth : RailWidth);
        _profileHeader.Visibility = expanded ? Visibility.Visible : Visibility.Collapsed;
        _profiles.ItemTemplate = expanded ? ProfileCards.Create() : ProfileCards.Rail();
        _profiles.ItemContainerStyle = expanded ? ProfileCards.ProfileContainerStyle() : ProfileCards.RailContainerStyle();
        _profiles.Margin = expanded ? new Thickness(8, 0, 8, 8) : new Thickness(0, 10, 0, 6);
        ScrollViewer.SetVerticalScrollBarVisibility(_profiles, expanded ? ScrollBarVisibility.Auto : ScrollBarVisibility.Hidden);
        ArrangeProfileTools(expanded);
        var tip = expanded ? "프로필 접기 · 아이콘으로 보기" : "프로필 펼쳐 보기 · 사용량과 상태를 카드로 보기";
        _profilePanelToggle.Content = expanded ? WorkspaceAppearance.GlyphClosePane : WorkspaceAppearance.GlyphOpenPane;
        _profilePanelToggle.ToolTip = tip;
        AutomationProperties.SetName(_profilePanelToggle, tip);
        AutomationProperties.SetItemStatus(_profilePanelToggle, expanded ? "펼침" : "접힘");
        ArrangeSidebar();
    }

    // Docks the shortcut column while the workspace keeps MinWorkspaceWidth;
    // otherwise the column becomes an overlay opened from the rail.
    private void ArrangeSidebar()
    {
        var available = _layout.ActualWidth > 0 ? _layout.ActualWidth : double.IsFinite(Width) ? Width : 1440;
        var profileWidth = _profilesExpanded ? ExpandedProfileWidth : RailWidth;
        var room = available - profileWidth - ShortcutSplitWidth - MinWorkspaceWidth;
        var docked = room >= SidebarLayout.MinShortcutWidth;
        if (docked)
        {
            var max = Math.Min(SidebarLayout.MaxShortcutWidth, room);
            _shortcutColumn.MinWidth = SidebarLayout.MinShortcutWidth;
            _shortcutColumn.MaxWidth = max;
            _shortcutColumn.Width = new GridLength(Math.Clamp(_sidebarLayout.ShortcutWidth, SidebarLayout.MinShortcutWidth, max));
            _shortcutSplitColumn.Width = new GridLength(ShortcutSplitWidth);
        }
        else
        {
            _shortcutColumn.MinWidth = 0;
            _shortcutColumn.MaxWidth = double.PositiveInfinity;
            _shortcutColumn.Width = new GridLength(0);
            _shortcutSplitColumn.Width = new GridLength(0);
        }
        if (docked != _shortcutsDocked) DockShortcuts(docked);
    }

    private void DockShortcuts(bool docked)
    {
        _shortcutsDocked = docked;
        _shortcutOverlay.IsOpen = false;
        if (docked)
        {
            _overlayFrame.Child = null;
            if (_shortcutPanel.Parent is null) { Grid.SetColumn(_shortcutPanel, 1); _layout.Children.Add(_shortcutPanel); }
        }
        else
        {
            _layout.Children.Remove(_shortcutPanel);
            _overlayFrame.Child = _shortcutPanel;
        }
        _shortcutOverlayToggle.Visibility = docked ? Visibility.Collapsed : Visibility.Visible;
        _shortcutOverlayClose.Visibility = docked ? Visibility.Collapsed : Visibility.Visible;
        _sidebarLayout.Divider.Visibility = docked ? Visibility.Visible : Visibility.Collapsed;
        Log(docked ? "작업 바로가기 · 창 너비가 충분해 옆에 고정" : "작업 바로가기 · 좁은 창이라 레일 버튼으로 열기");
    }

    private void ToggleShortcutOverlay()
    {
        if (_shortcutsDocked) return;
        if (_shortcutOverlay.IsOpen) { CloseShortcutOverlay(); return; }
        var width = Math.Clamp(_sidebarLayout.ShortcutWidth, SidebarLayout.MinShortcutWidth, OverlayMaxWidth);
        _overlayFrame.Width = width;
        _overlayFrame.Height = Math.Max(240, _layout.ActualHeight);
        _shortcutOverlay.HorizontalOffset = _profileColumn.ActualWidth;
        _shortcutOverlay.VerticalOffset = 0;
        _shortcutOverlay.IsOpen = true;
        Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => _shortcutFilter.Focus()));
    }

    private void CloseShortcutOverlay() { if (_shortcutOverlay.IsOpen) _shortcutOverlay.IsOpen = false; }

    private static bool InContextMenu(object source)
    {
        for (var node = source as DependencyObject; node is not null;
             node = node is Visual ? VisualTreeHelper.GetParent(node) : LogicalTreeHelper.GetParent(node))
            if (node is ContextMenu) return true;
        return false;
    }

    // Settings and maintenance actions, plus the manager update notice. The
    // flyout opens beside the rail with its bottom at the settings button.
    private void BuildSettingsFlyout(StackPanel settings, FrameworkElement executionMode, Button version)
    {
        var body = new StackPanel { Margin = new Thickness(14, 12, 14, 12) };
        body.Children.Add(new TextBlock { Text = "설정 및 관리", FontSize = 15, FontWeight = FontWeights.SemiBold, Margin = new Thickness(2, 0, 0, 8) });
        body.Children.Add(_managerUpdates);
        body.Children.Add(settings);
        body.Children.Add(new Border { Height = 1, Background = WorkspaceAppearance.Divider, Margin = new Thickness(0, 8, 0, 6) });
        body.Children.Add(executionMode);
        body.Children.Add(version);
        _settingsScroll.Content = body;
        var frame = new Border { Name = "SettingsFlyoutPanel", Width = 340, Background = WorkspaceAppearance.Surface,
            BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8),
            Child = _settingsScroll, Margin = new Thickness(0, 0, 18, 18),
            Effect = new DropShadowEffect { BlurRadius = 18, ShadowDepth = 0, Opacity = 0.55, Color = Colors.Black } };
        frame.PreviewKeyDown += (_, e) =>
        {
            if (e.Key != Key.Escape) return;
            e.Handled = true; _settingsFlyout.IsOpen = false; _settingsButton.Focus();
        };
        _settingsFlyout.Child = frame;
        _settingsFlyout.PlacementTarget = _settingsButton;
        _settingsFlyout.Opened += (_, _) => { _settingsButton.IsHitTestVisible = false; AutomationProperties.SetItemStatus(_settingsButton, "열림"); };
        _settingsFlyout.Closed += (_, _) => { _settingsButton.IsHitTestVisible = true; UpdateAttention(); };
    }

    private void ToggleSettingsFlyout()
    {
        if (_settingsFlyout.IsOpen) { _settingsFlyout.IsOpen = false; return; }
        var frame = (FrameworkElement)_settingsFlyout.Child;
        _settingsScroll.MaxHeight = Math.Max(240, (_layout.ActualHeight > 0 ? _layout.ActualHeight : 800) - 24);
        frame.Measure(new Size(frame.Width + frame.Margin.Left + frame.Margin.Right, double.PositiveInfinity));
        var height = frame.DesiredSize.Height - frame.Margin.Bottom;
        // Right of the rail, bottom-aligned with the button; WPF keeps it on screen.
        var railRight = _profilePanel.ActualWidth - (_settingsButton.TranslatePoint(new Point(_settingsButton.ActualWidth, 0), _profilePanel).X);
        _settingsFlyout.HorizontalOffset = Math.Max(0, railRight) + 8;
        _settingsFlyout.VerticalOffset = _settingsButton.ActualHeight - height;
        _settingsFlyout.IsOpen = true;
        if (Keyboard.FocusedElement == _settingsButton)
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => frame.MoveFocus(new TraversalRequest(FocusNavigationDirection.First))));
    }

    private Button SettingsAction(string text, Func<Task> action, string? tip = null)
    {
        // Close the flyout first: several actions open a modal dialog.
        var button = Action(text, () => { _settingsFlyout.IsOpen = false; return action(); }, tip);
        button.Background = Brushes.Transparent; button.BorderBrush = Brushes.Transparent;
        button.Margin = new Thickness(0, 1, 0, 1); button.Padding = new Thickness(10, 7, 10, 7);
        button.HorizontalAlignment = HorizontalAlignment.Stretch;
        return button;
    }

    private static TextBlock SettingsSection(string text) => new()
    {
        Text = text, FontSize = 12, FontWeight = FontWeights.SemiBold, Foreground = WorkspaceAppearance.Faint,
        Margin = new Thickness(10, 12, 0, 4)
    };

    // The header banner and settings badge for update states that need the user.
    private void BuildUpdateBanner()
    {
        var row = new DockPanel();
        var details = WorkspaceAppearance.Tool(new Button { Content = "자세히", ToolTip = "설정 및 관리에서 업데이트 상태 보기" }, "WorkspaceUpdateBannerDetails", quiet: true);
        details.Height = 26; details.Padding = new Thickness(8, 0, 8, 0);
        details.Click += (_, _) => { if (!_settingsFlyout.IsOpen) ToggleSettingsFlyout(); };
        DockPanel.SetDock(details, Dock.Right); row.Children.Add(details);
        var dot = new Ellipse { Width = 8, Height = 8, Margin = new Thickness(0, 0, 8, 0), VerticalAlignment = VerticalAlignment.Center };
        dot.SetBinding(Shape.FillProperty, new System.Windows.Data.Binding(nameof(TextBlock.Foreground)) { Source = _updateBannerTitle });
        row.Children.Add(dot);
        row.Children.Add(_updateBannerTitle);
        row.Children.Add(_updateBannerDetail);
        _updateBanner.Child = row;
        _updateBanner.MouseLeftButtonUp += (_, e) => { if (e.OriginalSource is not Button && !_settingsFlyout.IsOpen) ToggleSettingsFlyout(); };
        _managerUpdates.NoticeChanged += _ => UpdateAttention();
    }

    private void UpdateAttention()
    {
        var notice = _managerUpdates.Notice;
        var update = _state.Get("updates");
        var reasons = new List<string>();
        var warning = false;
        if (ManagerUpdatePanel.NeedsAttention(notice) || notice.State == ManagerUpdateState.Unknown)
        {
            reasons.Add(notice.Title);
            warning |= notice.State is not ManagerUpdateState.Ready;
        }
        if (update.B("worker_active")) reasons.Add("Codex 업데이트 진행 중");
        else if (update.S("status") is "available" or "update_available") reasons.Add("Codex 업데이트 설치 가능");
        else if (update.S("status") is "recovery_required" or "failed_restore") { reasons.Add("Codex 업데이트 복구 필요"); warning = true; }
        if (_state.Get("startup_updates").S("state") == "attention") { reasons.Add("전체 프로필 업데이트 확인 필요"); warning = true; }
        _settingsBadge.Visibility = reasons.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        _settingsBadge.Fill = warning ? WorkspaceAppearance.Warning : WorkspaceAppearance.Ready;
        var tip = reasons.Count == 0 ? "설정 및 관리" : "설정 및 관리\n" + string.Join("\n", reasons);
        _settingsButton.ToolTip = tip;
        AutomationProperties.SetItemStatus(_settingsButton, string.Join(", ", reasons));
        var banner = ManagerUpdatePanel.NeedsAttention(notice);
        _updateBanner.Visibility = banner ? Visibility.Visible : Visibility.Collapsed;
        if (!banner) return;
        var ready = notice.State == ManagerUpdateState.Ready;
        _updateBanner.Background = WorkspaceAppearance.Color(ready ? "#1C2E27" : "#2E2A20");
        _updateBannerTitle.Text = notice.Title;
        _updateBannerTitle.Foreground = ready ? WorkspaceAppearance.Ready : WorkspaceAppearance.Warning;
        _updateBannerDetail.Text = notice.Detail;
        _updateBanner.ToolTip = notice.Title + "\n" + notice.Detail;
        AutomationProperties.SetName(_updateBanner, notice.Title + " · " + notice.Detail);
    }

    private void RenderShortcuts()
    {
        var shortcuts = _state.Arr("shortcuts").ToArray();
        var owners = _state.Arr("profiles").ToArray();
        var activity = ShortcutCardData.Activity(owners);
        var models = _state.Arr("models").ToArray();
        var now = DateTimeOffset.Now;
        var query = _shortcutFilter.Text.Trim();
        var items = shortcuts.Select(s =>
        {
            var owner = owners.FirstOrDefault(p => p.S("id") == s.S("profile_id"));
            return new Choice(s.S("id"), s.S("alias", "이름 없는 작업") + "\n" + owner.S("alias", "계정 지정 필요") +
                " 계정에서 열기 · " + (s.S("host_id", "local") == "local" ? "Windows" : s.S("host_id")), s)
                { Shortcut = ShortcutCardData.Create(s, owner, activity, models, now) };
        }).Where(choice => MatchesShortcut(choice, query)).ToArray();
        Fill(_shortcuts, items, SelectedShortcutId(shortcuts));
        _shortcutCount.Text = query.Length == 0 ? shortcuts.Length.ToString() : $"{items.Length} / {shortcuts.Length}";
        _shortcutEmpty.Text = shortcuts.Length == 0 ? "작업 바로가기가 없습니다.\n＋ 버튼으로 자주 여는 작업을 추가하세요."
            : items.Length == 0 ? "검색 결과가 없습니다." : "";
        _shortcutEmpty.Visibility = items.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        var tip = $"작업 바로가기 열기 · {shortcuts.Length}개";
        _shortcutOverlayToggle.ToolTip = tip;
        AutomationProperties.SetName(_shortcutOverlayToggle, tip);
    }

    private static bool MatchesShortcut(Choice choice, string query) => query.Length == 0 ||
        new[] { choice.Shortcut?.Title, choice.Shortcut?.Account, choice.Shortcut?.ProfileName, choice.Shortcut?.Host }
            .Any(text => text?.Contains(query, StringComparison.CurrentCultureIgnoreCase) == true);

    // The selected profile's card in the header; the catalog and "no profile"
    // states keep the plain identity line instead.
    private void UpdateProfileSummary()
    {
        var choice = _viewingCatalog || _selectedProfile is null ? null
            : _profiles.Items.OfType<Choice>().FirstOrDefault(c => c.Id == _selectedProfile);
        if (!ReferenceEquals(_profileSummary.Content, choice)) _profileSummary.Content = choice;
        _profileSummary.Visibility = choice is null ? Visibility.Collapsed : Visibility.Visible;
        _identity.Visibility = choice is null ? Visibility.Visible : Visibility.Collapsed;
        AutomationProperties.SetName(_profileSummary, choice?.Card.AccessibleName ?? "");
    }

    // A press released in place on an avatar or card (ListOrdering).
    private async Task ProfileClickedAsync(Choice choice, int timestamp)
    {
        if (timestamp != 0) _responsiveness?.Record("profile_click", new { queue_ms = unchecked((uint)(Environment.TickCount - timestamp)) });
        Log($"프로필 클릭 수신 · {choice.Data.S("alias")} · {choice.Id}");
        var already = (_profiles.SelectedItem as Choice)?.Id == choice.Id;
        if (!already) _profiles.SelectedItem = choice; // SelectionChanged opens it.
        if (_profiles.ItemContainerGenerator.ContainerFromItem(_profiles.SelectedItem) is ListBoxItem item) item.Focus();
        if (already) await Safe(() => ShowProfileAsync(choice.Id));
    }
}
