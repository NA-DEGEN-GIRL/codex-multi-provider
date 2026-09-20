using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Threading;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

public sealed class MainWindow : Window
{
    private readonly string _root;
    private ManagerClient? _client;
    private readonly Func<string, object?, Task<JsonElement>>? _fixtureRequest;
    private readonly TaskNotesPanel _notes;
    private TaskContextWatcher? _taskContext;
    private SelectedTask? _selectedTask;
    private readonly ColumnDefinition _notesColumn = new() { Width = new GridLength(340), MinWidth = 280, MaxWidth = 620 };
    private NotificationActivation? _notificationActivation;
    private WorkspaceNotifications? _workspaceNotifications;
    private WorkspaceActivation? _workspaceActivation;
    private string? _pendingNotification;
    private bool _initialized;
    private CancellationTokenSource? _notificationNavigation;
    private WindowState _lastVisibleState = WindowState.Maximized;
    private JsonElement _state;
    private readonly ListBox _profiles = new();
    private readonly TextBlock _profileCount = new() { Foreground = Muted, FontSize = 11, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(6, 0, 0, 0) };
    private readonly ProfileOrdering _profileOrdering;
    private readonly ListBox _shortcuts = new();
    private readonly TextBlock _status = new() { TextWrapping = TextWrapping.Wrap, TextTrimming = TextTrimming.CharacterEllipsis, FontSize = 11, LineHeight = 16, Foreground = Muted, MaxHeight = 32 };
    private readonly TextBlock _identity = new() { FontSize = 12, FontWeight = FontWeights.SemiBold, Foreground = WorkspaceAppearance.Accent, TextTrimming = TextTrimming.CharacterEllipsis };
    private readonly TextBlock _taskIdentity = new() { FontSize = 18, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 5, 0, 0), TextTrimming = TextTrimming.CharacterEllipsis };
    private readonly TextBlock _attention = new() { Foreground = Brushes.Orange, FontSize = 12, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 4, 0, 0), Visibility = Visibility.Collapsed };
    private readonly TextBlock _mode = new() { Foreground = Muted, Margin = new Thickness(0, 5, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _accountState = new() { Foreground = Muted, Margin = new Thickness(0, 5, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _runtimeVersion = new() { Foreground = Muted, Margin = new Thickness(0, 5, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _operation = new() { Foreground = Muted, Margin = new Thickness(0, 6, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap, Visibility = Visibility.Collapsed };
    private readonly TextBlock _empty = new() { Text = "왼쪽에서 프로필을 선택하세요.\n선택한 계정의 Codex가 여기에 열립니다.", TextWrapping = TextWrapping.Wrap, TextAlignment = TextAlignment.Center, LineHeight = 24, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Foreground = Muted, FontSize = 14, Margin = new Thickness(28) };
    private readonly Grid _clientSurface = new() { Background = new SolidColorBrush(Color.FromRgb(22, 24, 29)) };
    private readonly NativeHostDeck _hostDeck;
    private NativeWindowHost _host => _hostDeck.Current;
    private readonly Dictionary<NativeWindowHost, WindowIdentity> _attachedWindows = [];
    private readonly Dictionary<NativeWindowHost, WindowLaunchIdentity> _windowLaunches = [];
    private readonly Dictionary<string, WindowLaunchIdentity> _detachedProfiles = [];
    private readonly WindowAttachmentAttempts _attachAttempts = new();
    private readonly Button _updateButton;
    private readonly Button _loginRepair;
    private readonly Dictionary<string, string> _loginNotices = [];
    private readonly TextBlock _profileUpdateStatus = new() { TextWrapping = TextWrapping.Wrap, Foreground = Muted, FontSize = 12, Margin = new Thickness(0, 4, 0, 8) };
    private string? _lastUpdateNotice;
    private readonly TextBlock _updateStatus = new() { TextWrapping = TextWrapping.Wrap, FontSize = 12, Foreground = Muted,
        MaxHeight = 56, Margin = new Thickness(0, 0, 0, 6), Visibility = Visibility.Collapsed };
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(4) };
    private readonly DispatcherTimer _activityTimer = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly Dictionary<Guid, (string Label, DateTime Started)> _pendingActions = [];
    private readonly ProfileActionGate _profileActions = new();
    private bool _logFlushPending;
    private readonly List<string> _events = [];
    private readonly DiagnosticLog _diagnostics;
    private readonly ResponsivenessMonitor? _responsiveness;
    private readonly TextBlock _logFeedback = new() { Foreground = Muted, FontSize = 11, Margin = new Thickness(12, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
    private readonly TextBox _logText = new() { Name = "WorkspaceLogOutput", IsReadOnly = true, AcceptsReturn = true,
        FontFamily = new FontFamily("Consolas, Malgun Gothic"), FontSize = 12,
        VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto };
    private readonly Dictionary<string, long> _observedDiagnostics = [];
    private readonly Dictionary<string, string> _restartNotices = [];
    private readonly RpcLogFilter _rpcLogFilter = new();
    private DateTime _attachDeadline;
    private int? _profileRequestTicket;
    private WindowLaunchIdentity? _expectedWindowLaunch;
    private nint _closedWindow;
    private readonly Dictionary<nint, WindowIdentity> _parked = [];
    private readonly Dictionary<string, (JsonElement Result, DateTime CheckedAt)> _loginStatuses = [];
    private string? _selectedProfile;
    private WindowIdentity? _attached
    {
        get => _attachedWindows.GetValueOrDefault(_host);
        set { if (value is null) _attachedWindows.Remove(_host); else _attachedWindows[_host] = value; }
    }
    private bool _refreshing, _rendering, _closing, _embedRequested, _layoutRetryQueued;
    private int _navigation;
    private int _stateRevision;
    private string? _contextProfile, _contextShortcut;
    private (string ProfileId, string ThreadId, string HostId)? _expectedConversation;
    private string? _expectedCanonicalThread;
    private bool _profileAlreadySelected;
    private bool _viewingCatalog;
    private JsonElement _viewerProfile;
    private string? _viewerRepresentative;
    private static readonly SolidColorBrush Muted = new(Color.FromRgb(159, 167, 183));
    private sealed record WindowIdentity(nint Handle, int Pid, string Executable)
    {
        public string Marker { get; } = "Codex.ControlCenter.Parked." + Guid.NewGuid().ToString("N");
        public bool Marked { get; set; }
        public bool MatchesLifetime => Marked && NativeWindowInterop.IsWindow(Handle) && NativeWindowInterop.GetPropW(Handle, Marker) == 1;
        public void ClearMarker() { if (MatchesLifetime) NativeWindowInterop.RemovePropW(Handle, Marker); Marked = false; }
    }

    public MainWindow(string root, bool fixture = false, Func<string, object?, Task<JsonElement>>? fixtureRequest = null)
    {
        if (!fixture && fixtureRequest is not null) throw new ArgumentException("Request fixtures require an isolated fixture window.");
        _fixtureRequest = fixtureRequest;
        _root = Path.GetFullPath(root);
        _notes = new TaskNotesPanel(_root, async (command, args) =>
        {
            if (_fixtureRequest is not null) return await _fixtureRequest(command, args);
            if (_client is null) throw new InvalidOperationException("관리 서비스 연결을 기다려 주세요.");
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            return await _client.RequestAsync(command, args, timeout.Token);
        });
        _diagnostics = new DiagnosticLog(_root);
        if (!fixture) _workspaceNotifications = new WorkspaceNotifications(_root, Log);
        if (!fixture) _responsiveness = new ResponsivenessMonitor(_diagnostics.Path, _root, Dispatcher);
        _hostDeck = new NativeHostDeck(host =>
        {
            host.WindowStateDirectory = Path.Combine(_root, "work", "control-center", "window-hosts");
            host.Responsiveness = _responsiveness;
            host.Diagnostic += message => Log("창 연결 · " + message);
            host.AttachmentLost += (hwnd, parentChanged) => OnAttachmentLost(host, hwnd, parentChanged);
            host.ViewportRecoveryFailed += message =>
            {
                if (host != _host) return;
                _embedRequested = false;
                host.Visibility = Visibility.Collapsed;
                _empty.Visibility = Visibility.Visible;
                _empty.Text = message;
                SetStatus(message, true);
            };
            host.ViewportRecoveryCompleted += (_, _) =>
            {
                if (host == _host) SetStatus("Codex 표시 영역을 맞췄습니다.");
            };
            _clientSurface.Children.Insert(0, host);
        });
        _hostDeck.Select("");
        Title = WorkspaceBuild.Title;
        Background = new SolidColorBrush(Color.FromRgb(23, 25, 30));
        Foreground = new SolidColorBrush(Color.FromRgb(233, 236, 242));
        FontFamily = new FontFamily("Segoe UI, Malgun Gothic"); FontSize = 14;
        Width = 1440; Height = 920; MinWidth = 1024; MinHeight = 840;
        WindowState = WindowState.Maximized;
        StateChanged += (_, _) => { if (WindowState != WindowState.Minimized) _lastVisibleState = WindowState; };
        var layout = new Grid();
        layout.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(304) });
        layout.ColumnDefinitions.Add(new ColumnDefinition());
        var sidebarFrame = new Grid { Background = new SolidColorBrush(Color.FromRgb(32, 34, 40)) };
        sidebarFrame.RowDefinitions.Add(new RowDefinition());
        sidebarFrame.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var sidebar = new Grid { Background = new SolidColorBrush(Color.FromRgb(32, 34, 40)), Margin = new Thickness(0, 0, 1, 0) };
        sidebar.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        sidebar.RowDefinitions.Add(new RowDefinition { Height = new GridLength(3, GridUnitType.Star), MinHeight = 160 });
        sidebar.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        sidebar.RowDefinitions.Add(new RowDefinition { Height = new GridLength(4, GridUnitType.Star), MinHeight = 120 });
        var heading = new StackPanel { Margin = new Thickness(18, 22, 18, 8) };
        heading.Children.Add(new TextBlock { Text = "Codex 작업 공간", FontSize = 20, FontWeight = FontWeights.SemiBold });
        heading.Children.Add(new TextBlock { Text = "계정과 작업을 한곳에서", Foreground = Muted, FontSize = 11, Margin = new Thickness(0, 5, 0, 20) });
        var profileMenu = MenuButton("프로필 관리", ("외부 모델 기본 설정", EditExternalModelAsync), ("하위 에이전트 설정", ProfileProvidersAsync), ("로그인 · API 키 관리", LoginProfileAsync),
            ("현재 앱의 로그인 계정 연결", RegisterCurrentAsync), ("로그인 상태 새로 확인", RefreshLoginStatusAsync),
            ("이 프로필 다시 열기", RecoverProfileAsync), ("작업 종료 후 설정 적용 예약", RestartProfileAsync),
            ("원래 창으로 분리", DetachAsync), ("별칭 변경", RenameProfileAsync),
            ("계정을 목록에서 제거", RemoveProfileAsync), ("제거한 계정 복원", RestoreProfileAsync), ("프로필 준비", PrepareProfileAsync));
        heading.Children.Add(SidebarSection("프로필", _profileCount,
            SidebarIcon(Action("＋", AddProfileAsync), "AddProfile", "＋", "Codex 계정 또는 외부 API 프로필 추가"),
            SidebarIcon(Action("↻", RefreshAccountsAsync), "RefreshProfiles", "↻", "계정·사용량·리딤 횟수 새로고침"),
            SidebarIcon(profileMenu, "ProfileActions", "⋯", "선택한 프로필 관리")));
        sidebar.Children.Add(heading);
        _profiles.Margin = new Thickness(8, 0, 8, 8);
        _profiles.ItemTemplate = ProfileOrdering.Template();
        _profiles.ItemContainerStyle = ProfileCards.ContainerStyle();
        _profileOrdering = new ProfileOrdering(_profiles, move => Safe(() => MoveProfileAsync(move)));
        _profiles.SelectionChanged += async (_, _) => { if (!_rendering && _profiles.SelectedItem is Choice choice) await Safe(() => ShowProfileAsync(choice.Id)); };
        _profiles.PreviewMouseLeftButtonDown += (_, e) =>
        {
            if (e.Handled || _profileOrdering.IsInteracting) { _profileAlreadySelected = false; return; }
            _responsiveness?.Record("profile_click", new { queue_ms = unchecked((uint)(Environment.TickCount - e.Timestamp)) });
            var choice = ClickedChoice(e.OriginalSource);
            _profileAlreadySelected = choice?.Id is { } id && id == (_profiles.SelectedItem as Choice)?.Id;
            if (choice is not null) Log($"프로필 클릭 수신 · {choice.Data.S("alias")} · {choice.Id}");
        };
        _profiles.MouseLeftButtonUp += async (_, e) => { if (_profileAlreadySelected && ClickedChoice(e.OriginalSource) is { } choice) await Safe(() => ShowProfileAsync(choice.Id)); };
        _profiles.ContextMenu = ProfileMenu(("위로 이동", id => _profileOrdering.MoveByAsync(id, -1)), ("아래로 이동", id => _profileOrdering.MoveByAsync(id, 1)), ("외부 모델 기본 설정", EditExternalModelAsync), ("하위 에이전트 설정", id => ShowProvidersAsync(id)), ("로그인 · API 키 관리", LoginProfileAsync), ("로그인 상태 새로 확인", RefreshLoginStatusAsync), ("이 프로필 다시 열기", RecoverProfileAsync), ("작업 종료 후 설정 적용 예약", RestartProfileAsync), ("별칭 변경", RenameProfileAsync), ("계정을 목록에서 제거", RemoveProfileAsync), ("제거한 계정 복원", _ => RestoreProfileAsync()), ("프로필 준비", PrepareProfileAsync));
        _profiles.ContextMenu.Opened += (_, _) =>
        {
            // WPF closes the popup before dispatching MenuItem.Click. Keep the
            // settings target independent of the cleared transient context.
            _profiles.ContextMenu.Tag = _contextProfile ?? _selectedProfile;
            var ids = _profiles.Items.OfType<Choice>().Select(c => c.Id).ToArray();
            var index = Array.IndexOf(ids, _contextProfile ?? _selectedProfile);
            ((MenuItem)_profiles.ContextMenu.Items[0]).IsEnabled = index > 0 && !_profileOrdering.IsInteracting;
            ((MenuItem)_profiles.ContextMenu.Items[1]).IsEnabled = index >= 0 && index < ids.Length - 1 && !_profileOrdering.IsInteracting;
        };
        _profiles.PreviewMouseRightButtonDown += (_, e) =>
        {
            if (ClickedChoice(e.OriginalSource) is not { } choice) return;
            _contextProfile = choice.Id; e.Handled = true;
            _profiles.ContextMenu.PlacementTarget = _profiles; _profiles.ContextMenu.Placement = System.Windows.Controls.Primitives.PlacementMode.MousePoint; _profiles.ContextMenu.IsOpen = true;
        };
        _profiles.ContextMenu.Closed += (_, _) => _contextProfile = null;
        Grid.SetRow(_profiles, 1); sidebar.Children.Add(_profiles);
        var tasks = new StackPanel { Margin = new Thickness(18, 16, 18, 5) };
        tasks.Children.Add(new Border { Height = 1, Background = new SolidColorBrush(Color.FromRgb(48, 53, 63)), Margin = new Thickness(0, 0, 0, 12) });
        tasks.Children.Add(SidebarSection("작업 바로가기", null,
            SidebarIcon(Action("＋", AddShortcutAsync), "AddShortcut", "＋", "작업 찾아서 바로가기 추가"),
            SidebarIcon(MenuButton("바로가기 관리", ("지금 열린 작업 추가", CaptureShortcutAsync), ("삭제한 링크 복구", UndoShortcutAsync)), "ShortcutActions", "⋯", "바로가기 관리")));
        tasks.Children.Add(new TextBlock { Text = "모든 프로필에서 함께 사용", Foreground = Muted, FontSize = 11, Margin = new Thickness(0, 2, 0, 5) });
        Grid.SetRow(tasks, 2); sidebar.Children.Add(tasks);
        _shortcuts.Margin = new Thickness(8, 0, 8, 8);
        _shortcuts.ItemTemplate = ShortcutCards.Create(async (sender, e) =>
        {
            e.Handled = true;
            if (sender is not Button { DataContext: Choice choice, Tag: string action }) return;
            await Safe(() => action switch
            {
                "open" => OpenShortcutAsync(choice.Id),
                "move" => MoveShortcutByIdAsync(choice.Id),
                "rename" => RenameShortcutByIdAsync(choice.Id),
                "delete" => DeleteShortcutByIdAsync(choice.Id),
                _ => Task.CompletedTask
            });
        });
        _shortcuts.ContextMenu = Menu(("열기", () => OpenShortcutAsync(RequireContextShortcut())), ("다른 프로필로 이동", () => MoveShortcutByIdAsync(RequireContextShortcut())), ("별칭 변경", () => RenameShortcutByIdAsync(RequireContextShortcut())), ("링크 삭제", () => DeleteShortcutByIdAsync(RequireContextShortcut())));
        _shortcuts.ContextMenu.Opened += (_, _) => _shortcuts.ContextMenu.Tag = _contextShortcut ?? (_shortcuts.SelectedItem as Choice)?.Id;
        _shortcuts.PreviewMouseRightButtonDown += (_, e) =>
        {
            if (ClickedChoice(e.OriginalSource) is not { } choice) return;
            _contextShortcut = choice.Id; e.Handled = true;
            _shortcuts.ContextMenu.PlacementTarget = _shortcuts; _shortcuts.ContextMenu.Placement = System.Windows.Controls.Primitives.PlacementMode.MousePoint; _shortcuts.ContextMenu.IsOpen = true;
        };
        _shortcuts.ContextMenu.Closed += (_, _) => _contextShortcut = null;
        Grid.SetRow(_shortcuts, 3); sidebar.Children.Add(_shortcuts);
        var footer = new StackPanel { Margin = new Thickness(18, 8, 18, 12) };
        footer.Children.Add(new Border { Height = 1, Background = new SolidColorBrush(Color.FromRgb(48, 53, 63)), Margin = new Thickness(0, 0, 0, 8) });
        var allRecords = Action("전체 기록", ShowCatalogAsync, "대표 계정으로 전체 작업 기록 보기");
        allRecords.Background = Brushes.Transparent; allRecords.BorderThickness = new Thickness(0); allRecords.Padding = new Thickness(2, 7, 2, 7);
        footer.Children.Add(allRecords);
        var settings = new StackPanel();
        settings.Children.Add(Action("공통 개인 스킬", PersonalSkillsAsync));
        settings.Children.Add(Action("하위 에이전트 · API 공급자", ProvidersAsync));
        settings.Children.Add(Action("SSH 연결 준비", RemoteAsync));
        settings.Children.Add(Action("전체 프로필 업데이트", ProfileUpdatesAsync));
        settings.Children.Add(_profileUpdateStatus);
        _updateButton = Action("Codex 앱 버전 확인", UpdatesAsync);
        settings.Children.Add(_updateButton);
        settings.Children.Add(_updateStatus);
        settings.Children.Add(Action("관리 서비스 다시 연결", ReconnectAsync));
        footer.Children.Add(new Expander { Header = "설정 및 관리", Margin = new Thickness(0, 8, 0, 0),
            Content = new ScrollViewer { Content = settings, MaxHeight = 330, VerticalScrollBarVisibility = ScrollBarVisibility.Auto } });
        _status.Margin = new Thickness(0, 10, 0, 0); footer.Children.Add(_status);
        var version = Action($"{WorkspaceBuild.Label} · 버전 복사", CopyVersionAsync,
            WorkspaceBuild.CopyText + "\n\n현재 실행 중인 작업공간앱 버전입니다. 클릭하면 버전 정보가 복사됩니다.");
        version.Name = "WorkspaceVersion";
        version.FontSize = 12;
        version.Background = Brushes.Transparent;
        version.BorderThickness = new Thickness(0);
        version.Padding = new Thickness(2, 6, 2, 6);
        version.Foreground = Muted;
        version.HorizontalContentAlignment = HorizontalAlignment.Left;
        version.Margin = new Thickness(0, 8, 0, 0);
        footer.Children.Add(version);
        // Keep update/recovery actions reachable at the minimum window height.
        // The account and task area may scroll; it must not push the footer out
        // of the visible window when status messages wrap onto additional lines.
        sidebarFrame.Children.Add(new ScrollViewer { Content = sidebar,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled });
        Grid.SetRow(footer, 1); sidebarFrame.Children.Add(footer);
        layout.Children.Add(sidebarFrame);
        var right = new Grid { Background = WorkspaceAppearance.Canvas };
        right.ColumnDefinitions.Add(new ColumnDefinition());
        var dividerColumn = new ColumnDefinition { Width = new GridLength(5) };
        right.ColumnDefinitions.Add(dividerColumn);
        right.ColumnDefinitions.Add(_notesColumn);
        var divider = new GridSplitter { Name = "NotesDivider", Width = 5, HorizontalAlignment = HorizontalAlignment.Stretch,
            VerticalAlignment = VerticalAlignment.Stretch, ResizeDirection = GridResizeDirection.Columns,
            ResizeBehavior = GridResizeBehavior.PreviousAndNext, Background = WorkspaceAppearance.Canvas };
        Grid.SetColumn(divider, 1); Grid.SetRowSpan(divider, 3); right.Children.Add(divider);
        Grid.SetColumn(_notes, 2); Grid.SetRowSpan(_notes, 3); right.Children.Add(_notes);
        double notesWidth = 340;
        Button? notesToggle = null;
        void LimitNotesWidth()
        {
            if (_notes.Visibility != Visibility.Visible || right.ActualWidth <= 0) return;
            // Leave room for the task controls and native viewport even after a
            // wide notes panel is dragged open, then the manager is made smaller.
            _notesColumn.MaxWidth = Math.Max(280, Math.Min(620, right.ActualWidth - 420 - 5));
        }
        void SetNotesVisible(bool visible)
        {
            if (!visible && _notesColumn.ActualWidth > 0) notesWidth = _notesColumn.ActualWidth;
            _notesColumn.MinWidth = visible ? 280 : 0;
            _notesColumn.Width = new GridLength(visible ? notesWidth : 0);
            dividerColumn.Width = new GridLength(visible ? 5 : 0);
            _notes.Visibility = divider.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
            LimitNotesWidth();
            if (notesToggle is not null) WorkspaceAppearance.Active(notesToggle, visible);
        }
        right.SizeChanged += (_, _) => LimitNotesWidth();
        _notes.CollapseRequested += () => SetNotesVisible(false);
        SetNotesVisible(false);
        right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        right.RowDefinitions.Add(new RowDefinition());
        right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var header = new StackPanel { Margin = new Thickness(20, 16, 20, 10) };
        var headingRow = new Grid { Name = "WorkspaceHeading" };
        headingRow.ColumnDefinitions.Add(new ColumnDefinition());
        headingRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headingRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        headingRow.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        var identity = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
        identity.Children.Add(_identity); identity.Children.Add(_taskIdentity); headingRow.Children.Add(identity);
        var controls = new WrapPanel { Name = "WorkspaceTools", VerticalAlignment = VerticalAlignment.Center };
        notesToggle = WorkspaceAppearance.Tool(Action("작업 메모", () => { SetNotesVisible(_notes.Visibility != Visibility.Visible); return Task.CompletedTask; }, "이 작업의 메모와 체크리스트 열기 / 접기"), "ToggleTaskNotes");
        controls.Children.Add(notesToggle);
        controls.Children.Add(WorkspaceAppearance.Tool(Action("관리창 안에 표시", AttachSelectedAsync), "RestoreWorkspaceView"));
        controls.Children.Add(WorkspaceAppearance.Tool(MenuButton("창 및 연결", ("원래 창으로 보기", DetachAsync), ("연결 확인", VerifyConversationAsync),
            ("입력 상태 확인", CheckInputAsync), ("로그인 상태 새로 확인", RefreshLoginStatusAsync)), "WorkspaceConnections", quiet: true));
        _loginRepair = WorkspaceAppearance.Tool(Action("이 프로필에 로그인", LoginProfileAsync));
        _loginRepair.Visibility = Visibility.Collapsed;
        controls.Children.Add(_loginRepair);
        foreach (Button button in controls.Children) button.Margin = new Thickness(0, 3, 6, 3);
        headingRow.Children.Add(controls);
        void ArrangeHeading()
        {
            bool wide = headingRow.ActualWidth >= 760 && _loginRepair.Visibility != Visibility.Visible;
            Grid.SetColumn(controls, wide ? 1 : 0); Grid.SetRow(controls, wide ? 0 : 1);
            Grid.SetColumnSpan(controls, wide ? 1 : 2);
            Grid.SetColumnSpan(identity, wide ? 1 : 2);
            identity.Margin = new Thickness(0, 0, wide ? 18 : 0, 0);
            controls.Margin = new Thickness(0, wide ? 0 : 8, 0, 0);
        }
        headingRow.SizeChanged += (_, _) => ArrangeHeading();
        _loginRepair.IsVisibleChanged += (_, _) => ArrangeHeading();
        ArrangeHeading();
        header.Children.Add(headingRow); header.Children.Add(_attention); header.Children.Add(_operation);
        var details = new StackPanel();
        details.Children.Add(_mode); details.Children.Add(_accountState); details.Children.Add(_runtimeVersion);
        header.Children.Add(new Expander { Header = "연결 상세", FontSize = 11, Foreground = Muted,
            Content = new Border { Background = WorkspaceAppearance.Surface, CornerRadius = new CornerRadius(6), Padding = new Thickness(12, 6, 12, 12), Child = details }, Margin = new Thickness(0, 3, 0, 0) });
        right.Children.Add(new Border { BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(0, 0, 0, 1), Child = header });
        var client = _clientSurface;
        client.Children.Add(_empty);
        Grid.SetRow(client, 1); right.Children.Add(client);
        var logPanel = new DockPanel { Margin = new Thickness(12, 6, 12, 6) };
        var logTools = new DockPanel();
        var logActions = new StackPanel { Orientation = Orientation.Horizontal };
        _logText.Height = 140; _logText.Visibility = Visibility.Collapsed;
        _logText.Margin = new Thickness(0, 6, 0, 0); _logText.Padding = new Thickness(12, 10, 12, 10);
        _logText.Background = WorkspaceAppearance.Color("#12151B"); _logText.BorderBrush = WorkspaceAppearance.Line;
        Button? logToggle = null;
        logToggle = WorkspaceAppearance.Tool(Action("진행 기록", () => {
            bool visible = _logText.Visibility != Visibility.Visible;
            _logText.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
            WorkspaceAppearance.Active(logToggle!, visible); return Task.CompletedTask;
        }, "상세 로그 펼치기 / 접기"), "ToggleWorkspaceLog", quiet: true);
        logActions.Children.Add(logToggle);
        logActions.Children.Add(WorkspaceAppearance.Tool(Action("로그 복사", CopyLogsAsync), quiet: true));
        logActions.Children.Add(WorkspaceAppearance.Tool(MenuButton("로그 관리", ("진행 내역", () => { Dialogs.ShowEvents(this, _events); return Task.CompletedTask; }), ("로그 파일 열기", () =>
        {
            var path = _diagnostics.Export();
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(path) { UseShellExecute = true });
            _logFeedback.Text = "저장한 로그 파일을 열었습니다.";
            return Task.CompletedTask;
        }), ("화면 로그 비우기 · 파일 유지", () => { _logText.Clear(); return Task.CompletedTask; })), quiet: true));
        foreach (Button button in logActions.Children) button.Margin = new Thickness(0, 0, 4, 0);
        DockPanel.SetDock(logActions, Dock.Left); logTools.Children.Add(logActions);
        _logFeedback.SetBinding(ToolTipProperty, new System.Windows.Data.Binding(nameof(TextBlock.Text)) { Source = _logFeedback });
        logTools.Children.Add(_logFeedback);
        DockPanel.SetDock(logTools, Dock.Top); logPanel.Children.Add(logTools); logPanel.Children.Add(_logText);
        var logFrame = new Border { Background = WorkspaceAppearance.Canvas, BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(0, 1, 0, 0), Child = logPanel };
        Grid.SetRow(logFrame, 2); right.Children.Add(logFrame);
        Grid.SetColumn(right, 1); layout.Children.Add(right);
        Content = ManagerTitleBar.Wrap(this, layout, Log);
        if (!fixture) _taskContext = new TaskContextWatcher(_root, Dispatcher, task =>
        {
            _selectedTask = task;
            _taskIdentity.Text = task is null ? "작업 · 아직 열지 않음" : "작업 · " + task.Title;
            _taskIdentity.ToolTip = task is null ? null : task.Title + "\n" + task.Task.Key;
            if (_selectedProfile is { } profile && !_viewingCatalog) _workspaceNotifications?.Remember(profile, task);
            _ = _notes.SelectTaskAsync(task);
        });
        if (!fixture) Loaded += async (_, _) => await Safe(async () => {
            await InitializeAsync(); _initialized = true;
            if (_pendingNotification is { } ticket) { _pendingNotification = null; await OpenNotificationAsync(ticket); }
        });
        _timer.Tick += async (_, _) => await RefreshAsync();
        _activityTimer.Tick += (_, _) => RenderActivity();
        Closing += OnClosing;
        SourceInitialized += (_, _) =>
        {
            if (!fixture)
            {
                _notificationActivation = new NotificationActivation(Path.Combine(_root, "work", "control-center", "window-hosts"),
                    click => Dispatcher.BeginInvoke(new Action(() => ActivateNotification(click))),
                    (source, notice) => Dispatcher.InvokeAsync(() => ShowWorkspaceNotification(source, notice)).Task);
                NativeWindowLease.NotificationPipe = _notificationActivation.PipeName;
                _workspaceActivation = new WorkspaceActivation(_root, ticket => Dispatcher.BeginInvoke(new Action(() => ActivateWorkspaceNotification(ticket))));
            }

        };
        Closed += async (_, _) => { _notificationNavigation?.Cancel(); _workspaceActivation?.Dispose(); _responsiveness?.Dispose(); _taskContext?.Dispose(); _notificationActivation?.Dispose(); _timer.Stop(); _activityTimer.Stop(); if (_client is not null) await _client.DisposeAsync(); };
    }

    private bool ProfileExists(string id) => _state.Arr("profiles").Any(p => p.S("id") == id);
    private string ProfileAlias(string id) => _state.Arr("profiles").FirstOrDefault(p => p.S("id") == id).S("alias", id[..8]);
    private bool ShowWorkspaceNotification(NotificationClick source, WorkspaceNotice notice)
    {
        if (_closing || DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - source.ClickedAt > 1500) return false;
        var host = _attachedWindows.FirstOrDefault(p => p.Value.Pid == source.AppPid && p.Value.Handle == source.Hwnd && p.Value.MatchesLifetime).Key;
        var profile = _state.Arr("profiles").FirstOrDefault(p => _hostDeck.Find(p.S("id")) == host);
        return host is not null && profile.S("id") != "" &&
            _workspaceNotifications?.Show(notice, profile.S("id"), ProfileExists, ProfileAlias) == true;
    }
    internal void ActivateWorkspaceNotification(string ticket)
    {
        if (_closing) return;
        if (WindowState == WindowState.Minimized) WindowState = _lastVisibleState;
        Show(); Activate();
        if (ticket == "") return;
        if (!_initialized) { _pendingNotification = ticket; return; }
        _ = Safe(() => OpenNotificationAsync(ticket));
    }
    private async Task OpenNotificationAsync(string ticket)
    {
        var target = _workspaceNotifications?.ReadTicket(ticket, ProfileExists)
            ?? throw new InvalidOperationException("이 알림의 작업 정보를 찾을 수 없습니다. 작업 목록에서 열어 주세요.");
        if (!ProfileExists(target.ProfileId)) throw new InvalidOperationException("이 알림에 연결된 프로필이 제거되었습니다.");
        _notificationNavigation?.Cancel();
        using var cancel = new CancellationTokenSource(TimeSpan.FromSeconds(35));
        _notificationNavigation = cancel;
        try
        {
        await ShowProfileAsync(target.ProfileId);
        int navigation = _navigation;
        while (!_host.HasLiveAttachment)
        {
            cancel.Token.ThrowIfCancellationRequested();
            if (_closing || _navigation != navigation || _selectedProfile != target.ProfileId) return;
            TryAttach(Profile());
            await Task.Delay(150, cancel.Token);
        }
        if (_selectedProfile != target.ProfileId || _navigation != navigation) return;
        await _host.NavigateAsync(new(target.HostId, target.ThreadId), cancel.Token);
        if (_closing || _navigation != navigation || _selectedProfile != target.ProfileId) return;
        Log($"알림 클릭 · 프로필 {ProfileAlias(target.ProfileId)} · 작업 {target.ThreadId} · 작업공간으로 이동");
        SetStatus($"{ProfileAlias(target.ProfileId)} 프로필에서 알림의 작업으로 이동을 요청했습니다.");
        }
        finally { if (_notificationNavigation == cancel) _notificationNavigation = null; }
    }

    private void ActivateNotification(NotificationClick click)
    {
        if (_closing || DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - click.ClickedAt > 3000) return;
        var target = _attachedWindows.FirstOrDefault(entry => entry.Value.Pid == click.AppPid && entry.Value.Handle == click.Hwnd);
        if (target.Key is null || !target.Key.HasLiveAttachment || !target.Value.MatchesLifetime) return;
        var profile = _state.Arr("profiles").FirstOrDefault(p => _hostDeck.Find(p.S("id")) == target.Key);
        if (profile.S("id") == "") return;
        // A notification is an explicit navigation request. Cancel only pending
        // manager selection; native Codex has already handled the task/SSH route.
        ++_navigation; _profileRequestTicket = null; _expectedConversation = null; _expectedCanonicalThread = null;
        _viewingCatalog = false; _selectedProfile = profile.S("id");
        _hostDeck.Select(_selectedProfile); BeginAttach();
        _expectedWindowLaunch = _windowLaunches.GetValueOrDefault(_host);
        _host.Visibility = Visibility.Visible; _empty.Visibility = Visibility.Collapsed;
        Render();
        if (WindowState == WindowState.Minimized) WindowState = _lastVisibleState;
        Show();
        bool activated = Activate();
        _host.SynchronizeLayout();
        Log($"알림 클릭 · 프로필 {profile.S("alias")} · 알림의 작업 표시 · 관리창 활성화 {(activated ? "완료" : "요청")}");
        SetStatus($"{profile.S("alias")} 프로필에서 알림을 보낸 작업을 표시했습니다.");
    }

    private static Choice? ClickedChoice(object source)
    {
        var element = source as DependencyObject;
        while (element is not null)
        {
            if (element is ListBoxItem item) return item.Content as Choice;
            element = element is Visual ? VisualTreeHelper.GetParent(element) : LogicalTreeHelper.GetParent(element);
        }
        return null;
    }
    internal void UseFixture(JsonElement state) { _state = state; _selectedProfile = state.Arr("profiles").FirstOrDefault().S("id"); Render(); SetStatus("화면 배치 자체 시험 · 실제 계정과 연결하지 않음"); }
    internal TaskNotesPanel FixtureNotes(ManagerClient client) { _client = client; return _notes; }

    private static TextBlock Label(string text) => new() { Text = text, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 0) };
    private static StackPanel Row(params UIElement[] children) { var row = new StackPanel { Orientation = Orientation.Horizontal }; foreach (var child in children) row.Children.Add(child); return row; }
    private static Button SidebarIcon(Button button, string name, string glyph, string tip)
    {
        button.Name = name; button.Content = glyph; button.ToolTip = tip;
        button.Width = 30; button.Height = 30; button.Padding = new Thickness(0);
        button.Margin = new Thickness(3, 0, 0, 0); button.FontSize = 18;
        button.Background = Brushes.Transparent; button.BorderThickness = new Thickness(0);
        button.HorizontalContentAlignment = HorizontalAlignment.Center;
        System.Windows.Automation.AutomationProperties.SetName(button, tip);
        return button;
    }
    private static Grid SidebarSection(string title, TextBlock? count, params Button[] actions)
    {
        var row = new Grid();
        row.ColumnDefinitions.Add(new ColumnDefinition()); row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var label = Row(new TextBlock { Text = title, FontWeight = FontWeights.SemiBold, FontSize = 12, VerticalAlignment = VerticalAlignment.Center });
        if (count is not null) label.Children.Add(count);
        row.Children.Add(label);
        var buttons = Row(actions); Grid.SetColumn(buttons, 1); row.Children.Add(buttons);
        return row;
    }
    private Button Action(string text, Func<Task> action, string? tip = null)
    {
        var button = new Button { Content = text, ToolTip = tip, Margin = new Thickness(0, 3, 5, 3) };
        button.Click += async (_, _) => { Log("버튼 클릭 수신 · " + text); button.IsEnabled = false; try { await Safe(action); } finally { button.IsEnabled = true; } }; return button;
    }
    private Button MenuButton(string text, params (string, Func<Task>)[] actions)
    {
        var button = new Button { Content = text + "  ▾", Margin = new Thickness(0, 3, 5, 3) };
        button.ContextMenu = Menu(actions);
        button.Click += (_, _) =>
        {
            button.ContextMenu.PlacementTarget = button;
            button.ContextMenu.Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom;
            button.ContextMenu.IsOpen = true;
        };
        return button;
    }
    private async Task CopyVersionAsync()
    {
        var owner = new WindowInteropHelper(this).Handle;
        await ClipboardTextCopy.CopyAsync(WorkspaceBuild.CopyText, text => ClipboardTextCopy.Write(owner, text));
        SetStatus($"{WorkspaceBuild.Label} 버전 정보를 복사했습니다.");
    }
    private async Task CopyLogsAsync()
    {
        var text = _diagnostics.CopyText;
        if (_responsiveness is not null) text += "\n\n응답 지연·리소스 진단\n" + _responsiveness.RecentText;
        string? saved = null;
        try { saved = await Task.Run(() => _diagnostics.Export(text)); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { Log("로그 파일 저장 실패 · " + ex.Message); }
        try
        {
            var owner = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            await ClipboardTextCopy.CopyAsync(text, value => ClipboardTextCopy.Write(owner, value));
            _logFeedback.Text = $"로그 {text.Length:N0}자 복사 완료";
            _logFeedback.Foreground = Muted;
            Log(_logFeedback.Text);
        }
        catch (Exception ex)
        {
            _logFeedback.Text = saved is not null ? "복사 실패 · ‘로그 파일 열기’를 사용하세요." : "복사 실패 · 다시 눌러 주세요.";
            _logFeedback.Foreground = Brushes.Orange;
            Log(_logFeedback.Text + " · " + ex.Message);
        }
    }
    private ContextMenu Menu(params (string, Func<Task>)[] actions)
    {
        var menu = new ContextMenu(); foreach (var (label, action) in actions) { var item = new MenuItem { Header = label }; item.Click += async (_, _) => await Safe(action); menu.Items.Add(item); } return menu;
    }
    private ContextMenu ProfileMenu(params (string Label, Func<string, Task> Action)[] actions)
        => Menu(actions.Select(action => (action.Label, (Func<Task>)(() => action.Action(RequireContextProfile())))).ToArray());
    private async Task InitializeAsync()
    {
        Log($"관리 앱 시작 · {WorkspaceBuild.Label} · {WorkspaceBuild.Description} · 빌드 {WorkspaceBuild.BuildId} · IPC {ManagerProtocol.Version} · 로그: {_diagnostics.Path}");
        if (_responsiveness is not null) Log("응답 지연 상세 로그 · " + _responsiveness.Path);
        SetStatus("관리 서비스를 연결하고 있습니다…");
        _client = await ManagerClient.ConnectAsync(_root);
        await CheckStartupUpdatesAsync();
        await RefreshAsync(); _timer.Start(); SetStatus("프로필을 백그라운드에서 미리 열고 있습니다. 준비된 프로필은 선택하면 바로 표시됩니다.");
    }
    private async Task ReconnectAsync()
    {
        _timer.Stop();
        if (_client is not null) await _client.DisposeAsync();
        _client = await ManagerClient.ConnectAsync(_root);
        await Request("supervisor.reconnect");
        await CheckStartupUpdatesAsync();
        await RefreshAsync(); _timer.Start(); SetStatus("관리 서비스에 다시 연결했습니다. 이전 변경 요청은 다시 실행하지 않았습니다.");
    }
    private async Task CheckStartupUpdatesAsync()
    {
        try { await Request("manager.startup"); }
        catch (Exception ex) { Log("시작 시 업데이트 확인 실패 · " + ex.Message); }
    }
    private async Task Safe(Func<Task> action)
    {
        try { await action(); }
        catch (Exception ex) { if (_closing) return; SetStatus(ex.Message, true); }
    }
    private async Task<JsonElement> Request(string command, object? args = null)
    {
        if (_fixtureRequest is not null) return await _fixtureRequest(command, args);
        if (_client is null) throw new InvalidOperationException("관리 서비스 연결을 기다려 주세요.");
        var tracked = command is not ("state" or "conversation.navigate");
        using var timing = _responsiveness?.Time("rpc." + command, tracked ? 0 : 500);
        var actionId = Guid.NewGuid();
        if (tracked) { _pendingActions[actionId] = (CommandLabel(command), DateTime.UtcNow); RenderActivity(); _activityTimer.Start(); }
        if (tracked) Log($"요청 시작 · {CommandLabel(command)} · {command}");
        try
        {
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(command switch
            {
                "profile.show" or "profile.login" or "conversation.open" or "conversation.navigate" or "catalog.show" => 45,
                "profile.restart" or "manager.startup" or "manager.recover_legacy" => 15,
                _ => 180
            }));
            JsonElement result;
            try { result = await _client.RequestAsync(command, args, deadline.Token); }
            catch (OperationCanceledException) when (deadline.IsCancellationRequested)
            { throw new InvalidOperationException("요청 응답을 제한 시간 안에 받지 못했습니다. 이미 시작한 처리는 계속될 수 있습니다. 상태 갱신은 계속됩니다."); }
            if (tracked) Log($"요청 응답 · {CommandLabel(command)} · {result.S("state", "수신 완료")}");
            return result;
        }
        catch (Exception ex) { if (tracked) Log($"요청 실패 · {command} · {ex.Message}"); throw; }
        finally { if (tracked) { _pendingActions.Remove(actionId); RenderActivity(); } }
    }
    private static string CommandLabel(string command) => command switch
    {
        "manager.startup" => "시작 시 업데이트 자동 확인",
        "manager.recover_legacy" => "확인한 구버전 프로필 일괄 정리",
        "profile.show" => "Codex 프로필 열기", "profile.prepare" => "프로필 준비", "accounts.refresh" => "계정·사용량 확인",
        "profile.login" => "프로필 로그인 화면 열기", "profile.login_status" => "로그인 계정 상태 확인",
        "profile.restart" => "설정 적용 · 정상 종료 후 다시 열기",
        "profile.recover" => "선택한 관리용 Codex 종료",
        "catalog.list" => "대화 목록 읽기", "catalog.show" => "원본 Codex 전체 기록 열기", "conversation.open" => "지정 계정에서 대화 열기", "policy.set" => "모델 조합 적용",
        "providers.verify" => "모델 API 연결 시험", "providers.key" => "API 키 저장", "providers.save" => "모델 연결 등록",
        "updates.check" => "공식 업데이트 확인", "updates.prepare" => "공식 패키지 다운로드·확인", "updates.apply" => "업데이트 준비·실행",
        "remote.inspect" => "SSH 상태 확인", "remote.prepare" => "SSH 런타임·연결 준비", _ => "관리 요청 처리"
    };
    private void RenderActivity()
    {
        if (_pendingActions.Count == 0) { _operation.Visibility = Visibility.Collapsed; _activityTimer.Stop(); return; }
        var operation = _pendingActions.Values.First();
        _operation.Visibility = Visibility.Visible;
        _operation.Text = $"진행 중 · {operation.Label} · {(int)(DateTime.UtcNow - operation.Started).TotalSeconds}초 경과" + (_pendingActions.Count > 1 ? $" · 추가 요청 {_pendingActions.Count - 1}개 대기" : "");
    }
    private async Task RefreshAsync()
    {
        if (_client is null || _refreshing || _closing || _profileOrdering.IsInteracting) return;
        _refreshing = true;
        var navigation = _navigation;
        var revision = _stateRevision;
        try
        {
            var state = await Request("state");
            if (_closing || navigation != _navigation || revision != _stateRevision || _profileOrdering.IsInteracting) return;
            _state = state;
            Render();
            var current = _viewingCatalog ? _viewerProfile : Profile();
            if (_host.IsAttached && _expectedWindowLaunch?.Matches(current) == true &&
                _host.AttachedHandle == (nint)current.N("window_handle")) _expectedWindowLaunch = null;
        }
        catch (Exception ex) { SetStatus("상태 갱신: " + ex.Message, true); }
        finally { _refreshing = false; }
    }
    private void Render()
    {
        using var timing = _responsiveness?.Stage("shell.Render");
        _responsiveness?.Select(_attached is { } active ? new(active.Pid, active.Handle) : null,
            _attachedWindows.Values.Select(w => new ResponsivenessMonitor.Target(w.Pid, w.Handle)));
        _rendering = true;
        try
        {
            var startup = _state.Get("startup_updates");
            _profileUpdateStatus.Text = startup.Message("전체 프로필 업데이트 확인 중");
            var warmup = _state.Get("profile_warmup");
            _profileCount.Text = _state.Arr("profiles").Count().ToString();
            using (_responsiveness?.Stage("shell.profile_list", 25))
            if (!_profileOrdering.IsInteracting) Fill(_profiles, _state.Arr("profiles").Select(p =>
            {
                var update = startup.Arr("profiles").FirstOrDefault(item => item.S("profile_id") == p.S("id"));
                var suffix = update.ValueKind == JsonValueKind.Object && update.S("state") is not ("current" or "latest_on_open" or "complete")
                    ? "\n" + UpdatePresentation.ProfileState(update) : "";
                if (ProfileLoginPresentation.NeedsLogin(p)) suffix = "\n로그인 확인 필요";
                var prepared = warmup.Arr("profiles").FirstOrDefault(item => item.S("profile_id") == p.S("id"));
                if (p.S("status") != "running" && !ProfileLoginPresentation.NeedsLogin(p))
                {
                    if (prepared.S("state") is "checking" or "opening") suffix = "\n백그라운드에서 여는 중";
                    else if (prepared.S("state") == "queued") suffix = "\n미리 열기 대기";
                }
                var label = p.S("auth_mode") == "external" ? "[API] " + p.S("alias") : p.S("alias", "이름 없는 프로필");
                var usage = p.S("auth_mode") == "external" ? p.S("external_model_name", "외부 API") : Usage(p.Get("usage"));
                return new Choice(p.S("id"), $"{label}\n{usage} · {Status(p.S("status"))}" + suffix, p) { ProfileNotice = suffix };
            }), _selectedProfile);
            var shortcuts = _state.Arr("shortcuts");
            using (_responsiveness?.Stage("shell.shortcut_list", 25))
            Fill(_shortcuts, shortcuts.Select(s => new Choice(s.S("id"), s.S("alias", "이름 없는 작업") + "\n" +
                _state.Arr("profiles").FirstOrDefault(p => p.S("id") == s.S("profile_id")).S("alias", "계정 지정 필요") +
                " 계정에서 열기 · " + (s.S("host_id", "local") == "local" ? "Windows" : s.S("host_id")), s)), (_shortcuts.SelectedItem as Choice)?.Id);
            var p = Profile();
            if (_viewingCatalog)
            {
                var liveViewer = _state.Arr("view_instances").FirstOrDefault(v => v.S("id") == _viewerProfile.S("id"));
                if (liveViewer.ValueKind == JsonValueKind.Object) _viewerProfile = liveViewer;
                p = _viewerProfile;
            }
            _identity.Text = _selectedProfile is null ? "사용할 프로필을 선택하세요" : (p.S("auth_mode") == "external" ? "외부 API 프로필 " : "Codex 프로필 ") + p.S("alias", "연결된 프로필 없음");
            _identity.ToolTip = _identity.Text;
            var openedTask = p.Get("runtime_state").Get("opened_task");
            _taskIdentity.Text = openedTask.S("thread_id") == "" ? "작업 · 아직 열지 않음" : "최근 연 작업 · " + openedTask.S("title", openedTask.S("thread_id"));
            if (openedTask.S("thread_id") != "" && openedTask.S("title") == "") _taskIdentity.Text = "최근 연 작업 · " + openedTask.S("thread_id");
            _taskIdentity.ToolTip = "이 프로필에서 마지막으로 열기 완료한 작업입니다.\n" + openedTask.S("title") + "\n" + openedTask.S("thread_id");
            using (_responsiveness?.Stage("shell.task_context", 25)) _taskContext?.Select(p);
            if (_selectedTask is { } selectedTask) { _taskIdentity.Text = "작업 · " + selectedTask.Title; _taskIdentity.ToolTip = selectedTask.Title + "\n" + selectedTask.Task.Key; }
            var policy = p.Get("policy");
            var mode = p.S("auth_mode") == "external" ? p.S("external_model_name", "외부 API 모델") : "GPT 사용";
            var agentBadge = ProfileAgentPresentation.Badge(p);
            if (agentBadge.Length > 0) mode += " · " + agentBadge;
            _mode.ToolTip = ProfileAgentPresentation.Hint(p);
            var pending = agentBadge.Length == 0 && policy.N("desired_revision") != policy.N("effective_revision") ? " · 적용 대기" : "";
            _mode.Text = _selectedProfile is null ? "원본 Codex 화면 · 계정별 분리" : $"내 Windows · {mode}{pending} · {Status(p.S("status"))}";
            if (p.S("status_message") != "") _mode.Text += " · " + p.S("status_message");
            if (p.Get("restart").S("message") != "" && p.Get("restart").S("phase") is not ("complete" or "superseded"))
                _mode.Text += " · " + p.Get("restart").S("message");
            if (_viewingCatalog)
            {
                var representative = _state.Arr("profiles").FirstOrDefault(x => x.S("id") == _viewerRepresentative);
                _identity.Text = "전체 기록 · 읽기 전용";
                _mode.Text = "대표 계정 · " + representative.S("alias", Profile().S("alias", "확인 중")) + " · 원본 Codex 기록 보기";
            }
            RenderLoginState();
            var selection = p.Get("runtime_selection");
            _runtimeVersion.Text = _selectedProfile is null ? "" : selection.Message("실행 버전 확인 중");
            var automatic = p.Get("restart");
            if (automatic.S("automatic_key") != "" && automatic.S("phase") is not ("complete" or "superseded"))
                _runtimeVersion.Text = automatic.S("phase") == "attention"
                    ? "자동 업데이트 확인 필요 · " + automatic.S("message")
                    : "업데이트 자동 적용 중 · " + automatic.S("message");
            if (selection.S("current_release") != "")
                _runtimeVersion.Text += $" · 현재 {selection.S("current_release")}";
            if (selection.B("restart_required") && selection.S("current_release") != selection.S("selected_release"))
                _runtimeVersion.Text += $" · 적용할 버전 {selection.S("selected_release")}";
            if (p.B("sidebar_cache_pending"))
                _runtimeVersion.Text += " · 이전 목록 정리 대기: 새로 시작해야 적용됩니다";
            _runtimeVersion.Foreground = selection.B("restart_required") || p.B("sidebar_cache_pending") ? Brushes.Orange : Muted;
            var warnings = new List<string>();
            if (p.S("desktop_compatibility_notice") != "") warnings.Add(p.S("desktop_compatibility_notice"));
            if (ProfileLoginPresentation.NeedsLogin(p)) warnings.Add("로그인 확인 필요 · 이 프로필에 로그인해 주세요.");
            if (automatic.S("phase") == "attention") warnings.Add(automatic.S("message"));
            else if (selection.B("restart_required") || p.B("sidebar_cache_pending")) warnings.Add(_runtimeVersion.Text);
            if (p.S("status") is "failed" or "error" or "blocked") warnings.Add(p.S("status_message", "프로필 상태를 확인해 주세요."));
            _attention.Text = string.Join("\n", warnings.Where(message => message != "").Distinct());
            _attention.Visibility = _attention.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
            var update = _state.Get("updates");
            _updateButton.Content = update.B("worker_active") ? "Codex 업데이트 진행 중" :
                update.S("status") is "available" or "update_available" ? "Codex 업데이트 설치" :
                update.S("status") is "recovery_required" or "failed_restore" ? "Codex 업데이트 복구" : "Codex 앱 버전 확인";
            _updateButton.ToolTip = update.Message("Codex 업데이트 상태");
            _updateStatus.Text = UpdatePresentation.Summary(update);
            _updateStatus.ToolTip = _updateStatus.Text;
            _updateStatus.Visibility = update.B("worker_active") || update.S("status") is "complete" or "recovery_required" or "failed_restore" or "failed_install" or "blocked" or "installed_newer" or "up_to_date"
                ? Visibility.Visible : Visibility.Collapsed;
            var updateNotice = update.S("status") + ":" + _updateStatus.Text;
            if (update.S("status") != "" && _lastUpdateNotice != updateNotice)
            {
                _lastUpdateNotice = updateNotice;
                Log("전체 Codex 앱 업데이트 · " + _updateStatus.Text.Replace('\n', ' '));
            }
            using (_responsiveness?.Stage("shell.diagnostics", 25))
            foreach (var observed in _state.Arr("profiles").Concat(_state.Arr("view_instances")))
            {
                var loginProblem = observed.Get("login_health");
                var loginReason = loginProblem.S("reason");
                if (_loginNotices.GetValueOrDefault(observed.S("id"), "") != loginReason)
                {
                    _loginNotices[observed.S("id")] = loginReason;
                    if (loginReason != "") Log($"{observed.S("alias")} · 로그인 확인 필요 · {loginProblem.Message()} · {loginReason}");
                }
                var restart = observed.Get("restart");
                var stamp = restart.S("id") + ":" + restart.S("phase") + ":" + restart.S("message");
                if (restart.S("id") != "" && _restartNotices.GetValueOrDefault(observed.S("id")) != stamp)
                {
                    _restartNotices[observed.S("id")] = stamp;
                    var activity = restart.B("remote_background") ? "SSH 준비" : "다시 열기";
                    var errorCode = restart.S("code");
                    Log($"{observed.S("alias")} · {activity} · {restart.S("phase")} · {restart.S("message")}" +
                        (errorCode == "" ? "" : " · " + errorCode));
                }
                var runtime = observed.Get("runtime_state");
                var generation = observed.S("id") + ":" + runtime.S("generation");
                var seen = _observedDiagnostics.GetValueOrDefault(generation);
                foreach (var entry in runtime.Arr("recent_diagnostics").Where(e => e.N("sequence") > seen))
                {
                    _observedDiagnostics[generation] = entry.N("sequence");
                    if (!_rpcLogFilter.Accept(generation, entry.S("method"), entry.S("state"), entry.S("reason"),
                        entry.N("code"), DateTime.UtcNow, out var repeated)) continue;
                    var outcome = entry.S("state") switch { "started" => "시작", "completed" => "완료", "failed" => "실패", _ => "상태" };
                    Log($"{observed.S("alias")} · Codex {entry.S("method")} · {outcome}" +
                        (repeated > 0 ? $" · 같은 오류 {repeated}회 추가" : "") +
                        (entry.Get("code").ValueKind == JsonValueKind.Number ? $" · RPC {entry.N("code")}" : "") +
                        (entry.S("reason") switch {
                            "history_cursor_changed" => " · 대화 기록의 조회 기준이 변경되었습니다.",
                            "catalog_cursor_changed" => " · 대화 목록의 조회 기준이 변경되었습니다.",
                            "catalog_filter_unsupported" => " · 이 목록의 분류 조건이 아직 지원되지 않습니다.",
                            "catalog_record_readonly" => " · 공통 기록의 조회 화면에서 지원되지 않는 변경입니다. config.toml 오류가 아닙니다.",
                            "windows_sandbox_ready" => " · Windows 실행 환경 준비됨",
                            "windows_sandbox_notConfigured" => " · Windows 실행 환경 설정 필요",
                            "windows_sandbox_updateRequired" => " · Windows 실행 환경 업데이트 필요",
                            "windows_setup_started" => " · 설치 요청 수락 · 완료 알림 대기",
                            "windows_setup_not_started" => " · 설치가 시작되지 않았습니다.",
                            "windows_setup_succeeded" => " · Windows 설정 완료",
                            "windows_setup_failed" => " · Windows 설정 실패 · 해당 Codex 창의 안내를 확인하세요.",
                            "windows_setup_request_failed" => " · Windows 설정 요청 실패",
                            _ => "" }));
                    _observedDiagnostics[generation] = entry.N("sequence");
                }
            }
            using (_responsiveness?.Stage("shell.selected_window", 25))
            if (_embedRequested && _profileRequestTicket is null && _selectedProfile is not null)
            {
                if (p.Get("restart").S("phase") is "acquiring" or "closing" or "opening" or "releasing" or "waiting" or "recovering" or "connecting")
                    _attachDeadline = DateTime.UtcNow.AddSeconds(25);
                TryAttach(p, refreshPresentation: false);
            }
            using (_responsiveness?.Stage("shell.background_windows", 25)) ReconcileBackgroundWindows();
        }
        finally { _rendering = false; }
    }
    private static void Fill(ListBox box, IEnumerable<Choice> values, string? selected)
    {
        var items = values.ToArray();
        var old = box.Items.OfType<Choice>().ToArray();
        if (!old.Select(x => (x.Id, x.Label, x.Hint, x.AgentBadge, x.AgentHint, x.Card)).SequenceEqual(items.Select(x => (x.Id, x.Label, x.Hint, x.AgentBadge, x.AgentHint, x.Card))))
        {
            box.Items.Clear(); foreach (var value in items) box.Items.Add(value);
        }
        box.SelectedItem = box.Items.OfType<Choice>().FirstOrDefault(x => x.Id == selected);
    }
    private JsonElement Profile() => _state.Arr("profiles").FirstOrDefault(p => p.S("id") == _selectedProfile);
    private string RequireProfile() => _selectedProfile ?? throw new InvalidOperationException("먼저 왼쪽 프로필을 선택하세요.");
    private static string Status(string value) => value switch { "running" => "실행 중", "ready" => "준비됨", "stopped" or "not_started" => "닫힘", "unprepared" => "준비 전", "error" => "확인 필요", "starting" => "여는 중", "unknown" or "" => "상태 확인 중", _ => value };
    private static string Usage(JsonElement usage)
    {
        if (usage.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined) return "사용량 확인 안 됨";
        var windows = usage.Arr("windows").ToArray();
        if (windows.Length == 0) return usage.B("refreshing") ? "사용량 확인 중…" : "사용량 확인 안 됨";
        var text = windows.Take(2).Select(w =>
        {
            var remaining = w.Get("remaining_percent"); var used = w.Get("used_percent");
            if (remaining.ValueKind != JsonValueKind.Number && used.ValueKind != JsonValueKind.Number) return "확인 안 됨";
            var percent = remaining.ValueKind == JsonValueKind.Number ? remaining.GetDouble() : 100 - used.GetDouble();
            return $"{w.S("label", w.S("name", ""))} {Math.Clamp(percent, 0, 100):0}% 남음".Trim();
        });
        var age = usage.N("age_seconds");
        var stamp = usage.B("refreshing") ? " · 갱신 중" :
            age >= 86400 ? $" · {age / 86400}일 전 값" : age >= 3600 ? $" · {age / 3600}시간 전 값" :
            age >= 120 ? $" · {age / 60}분 전 값" : "";
        if (stamp == "" && (usage.S("freshness") == "stale" || usage.Get("error").ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined))) stamp = " · 이전 값";
        // Weekly reset time and any redeemable reset credits stay on the card so
        // the profile list answers "when does it come back" without a dialog.
        var reset = windows.FirstOrDefault(w => w.S("label", w.S("name")) == "주간");
        var resetText = reset.ValueKind == JsonValueKind.Object && reset.Get("resets_at").ValueKind == JsonValueKind.Number
            ? $" · {DateTimeOffset.FromUnixTimeSeconds(reset.Get("resets_at").GetInt64()).ToLocalTime():M/d HH:mm} 초기화" : "";
        var credits = usage.Get("reset_credits");
        var available = credits.Get("available");
        var creditText = available.ValueKind == JsonValueKind.Number && available.TryGetInt64(out var count) && count is >= 0 and <= 1_000_000
            ? $" · 리딤 {count:N0}회" : " · 리딤 확인 안 됨";
        return string.Join(" / ", text) + resetText + creditText + stamp;
    }
    private async Task ShowProfileAsync(string id, string command = "profile.show")
    {
        using var timing = _responsiveness?.Time("profile.switch." + id);
        using var action = _profileActions.Enter(id, "계정 열기");
        await ShowProfileCoreAsync(id, command);
    }
    private async Task ShowProfileCoreAsync(string id, string command = "profile.show")
    {
        var ticket = ++_navigation;
        _expectedConversation = null;
        if (_selectedProfile != id || _viewingCatalog) ParkCurrent();
        _viewingCatalog = false;
        _selectedProfile = id;
        _hostDeck.Select(id);
        BeginAttach(); _profileRequestTicket = ticket; Render();
        if (command == "profile.show" && ProfileLoginPresentation.ShowRecovery(Profile()))
        {
            _profileRequestTicket = null;
            ShowLoginRecovery(Profile());
            return;
        }
        if (command == "profile.show" && AutomaticProfileUpdate.IsReplacing(Profile()))
        {
            _profileRequestTicket = null;
            SetStatus("업데이트 적용 후 이 프로필의 새 창을 관리창 안에 표시합니다…");
            return;
        }
        if (command == "profile.show" && _attached is { } cached && cached.MatchesLifetime &&
            _host.IsAttached && _windowLaunches.TryGetValue(_host, out var launch) && launch.Matches(Profile()) &&
            !(Profile().Get("restart").B("remote_background") && Profile().Get("restart").S("phase") == "attention"))
        {
            _profileRequestTicket = null;
            TryAttach(Profile());
            Log($"프로필 전환 · {Profile().S("alias")} · 기존 창 연결 유지");
            return;
        }
        Log($"프로필 선택 · {Profile().S("alias")} · {id}");
        SetStatus(command == "profile.login" ? "선택한 프로필의 로그인 화면을 여는 중입니다…" : "선택한 프로필을 여는 중입니다…");
        JsonElement returnedProfile;
        try
        {
            var result = await Request(command, new { profile_id = id });
            if (ticket != _navigation || _closing) return;
            var workspace = result.Get("preparation").Get("common").Get("app").Get("workspace");
            if (workspace.ValueKind == JsonValueKind.Object)
                Log($"공통 설정 적용 · 프로젝트 {workspace.Get("projects")}개 · SSH 연결 {workspace.Get("ssh_connections")}개");
            var cache = result.Get("preparation").Get("sidebar_cache");
            if (cache.S("state") == "rebuilt")
                Log($"이전 목록 캐시 {cache.Get("removed_cache_rows")}개 정리 · 원본 기록으로 목록을 다시 불러옵니다.");
            returnedProfile = result.Get("profile").ValueKind == JsonValueKind.Object ? result.Get("profile") : result;
            if (returnedProfile.S("desktop_compatibility_notice") != "") Log(returnedProfile.S("desktop_compatibility_notice"));
            if (returnedProfile.S("id") != id || (result.S("profile_id") != "" && result.S("profile_id") != id))
                throw new InvalidOperationException("요청한 프로필과 반환된 창 정보가 일치하지 않습니다.");
            if (result.S("state") is "updating" or "opening")
            {
                // Queued background startup still describes the old generation.
                // Let subsequent state polls attach its new window.
                _expectedWindowLaunch = null;
                _attachDeadline = DateTime.UtcNow.AddSeconds(25);
                ++_stateRevision;
                await RefreshAsync();
                if (ticket == _navigation && !_closing)
                    SetStatus("백그라운드에서 이 프로필을 여는 중입니다. 준비되면 바로 표시합니다…");
                return;
            }
            Log($"{returnedProfile.S("alias")} · {(result.S("state") == "launched" ? "새 프로세스 시작" : "기존 프로세스 사용")} · PID {returnedProfile.N("process_id")} · 실행 {returnedProfile.S("generation")}");
            _expectedWindowLaunch = WindowLaunchIdentity.From(returnedProfile);
            ++_stateRevision;
            await RefreshAsync();
        }
        catch
        {
            // Startup may acquire maintenance just after the state above was
            // read. Keep the user's selection and attach the replacement on the
            // next refresh; never require another click or replay profile.show.
            if (command == "profile.show" && ticket == _navigation && !_closing)
            {
                await RefreshAsync();
                if (AutomaticProfileUpdate.IsReplacing(Profile()))
                {
                    _expectedWindowLaunch = null;
                    _attachDeadline = DateTime.UtcNow.AddSeconds(25);
                    SetStatus("업데이트 적용 후 이 프로필의 새 창을 관리창 안에 표시합니다…");
                    return;
                }
            }
            if (ticket == _navigation) _embedRequested = false;
            throw;
        }
        finally { if (_profileRequestTicket == ticket) _profileRequestTicket = null; }
        if (ticket != _navigation || _selectedProfile != id || !_embedRequested || _closing) return;
        TryAttach(returnedProfile);
    }
    private static string LoginLabel(JsonElement result) => result.S("state") switch
    {
        "credential_saved" => result.B("server_verified") ? "서버 로그인 확인됨" : "로그인 정보 저장됨 · 서버 확인 전",
        "api_key" => "외부 API 키 사용",
        "signed_out" => "로그인이 필요합니다",
        "account_changed" => "로그인 계정 변경됨 · 확인 필요",
        "signed_in" when result.B("server_verified") => "서버 로그인 확인됨",
        _ => "로그인 계정 미확인"
    };
    private void RenderLoginState()
    {
        var saved = Profile();
        if (saved.S("auth_mode") == "external")
        {
            _loginRepair.Visibility = Visibility.Collapsed;
            _accountState.Visibility = _selectedProfile is null || _viewingCatalog ? Visibility.Collapsed : Visibility.Visible;
            _accountState.Foreground = Muted;
            _accountState.Text = "외부 API 키 사용 · " + saved.S("external_model_name", "외부 모델");
            _accountState.ToolTip = "API 키는 하위 에이전트 · API 공급자 설정에서 관리합니다.";
            return;
        }
        _loginRepair.Visibility = !_viewingCatalog && ProfileLoginPresentation.NeedsLogin(saved) ? Visibility.Visible : Visibility.Collapsed;
        _loginRepair.IsEnabled = saved.S("status") != "running";
        _loginRepair.ToolTip = "이 프로필의 실행 창을 닫은 뒤, 등록된 계정으로 전용 로그인을 진행합니다.";
        _accountState.Foreground = ProfileLoginPresentation.NeedsLogin(saved) ? Brushes.Orange : Muted;
        var savedLabel = saved.S("login_state") == "signed_in" && DateTimeOffset.TryParse(saved.S("login_verified_at"), out var verified)
            ? $"마지막 서버 로그인 확인 · {verified.ToLocalTime():MM-dd HH:mm:ss}"
            : saved.S("login_state") == "credential_saved" && saved.S("runtime_channel") == "managed"
            ? "로그인 정보 저장됨 · 공통 대화 연결됨"
            : "로그인 계정 미확인 · 로그인 상태 새로 확인을 누르세요";
        _accountState.Visibility = _selectedProfile is null || _viewingCatalog ? Visibility.Collapsed : Visibility.Visible;
        _accountState.Text = _selectedProfile is not null && _loginStatuses.TryGetValue(_selectedProfile, out var login)
            ? LoginLabel(login.Result) + $" · 마지막 확인 {login.CheckedAt:HH:mm:ss}"
            : savedLabel;
        _accountState.ToolTip = _selectedProfile is not null && _loginStatuses.TryGetValue(_selectedProfile, out login)
            ? login.Result.Message("") : _accountState.Text;
        if (ProfileLoginPresentation.NeedsLogin(saved))
        {
            _accountState.Text = saved.Get("login_health").Message();
            _accountState.ToolTip = _accountState.Text;
        }
    }
    private Task LoginProfileAsync() => LoginProfileAsync(RequireProfile());
    private async Task LoginProfileAsync(string id)
    {
        if (_state.Arr("profiles").First(p => p.S("id") == id).S("auth_mode") == "external")
        {
            await ShowProvidersAsync(id);
            return;
        }
        _loginStatuses.Remove(id);
        await ShowProfileAsync(id, "profile.login");
    }
    private Task RefreshLoginStatusAsync() => RefreshLoginStatusAsync(RequireProfile());
    private async Task RefreshLoginStatusAsync(string id)
    {
        var result = await Request("profile.login_status", new { profile_id = id, verify_server = true });
        if (_closing) return;
        _loginStatuses[id] = (result, DateTime.Now);
        await RefreshAsync();
        RenderLoginState();
        var alias = _state.Arr("profiles").FirstOrDefault(p => p.S("id") == id).S("alias", result.S("alias", "선택한 프로필"));
        SetStatus(alias + " · " + LoginLabel(result) + " · " + result.Message("상태를 읽었습니다."), result.S("state") is "signed_out" or "unverified" or "account_changed");
    }
    private Task RestartProfileAsync() => RestartProfileAsync(RequireProfile());
    private async Task RestartProfileAsync(string id)
    {
        using var action = _profileActions.Enter(id, "설정 적용");
        if (_selectedProfile == id && !_viewingCatalog) BeginAttach();
        var result = await Request("profile.restart", new { profile_id = id });
        await RefreshAsync();
        SetStatus(result.Message("이 프로필의 설정 적용을 예약했습니다."), result.S("phase") == "attention");
    }
    private Task RecoverProfileAsync() => RecoverProfileAsync(RequireProfile());
    private async Task RecoverProfileAsync(string id)
    {
        using var action = _profileActions.Enter(id, "이 프로필 다시 열기");
        if (_viewingCatalog) throw new InvalidOperationException("왼쪽에서 복구할 계정 프로필을 선택하세요.");
        if (_profileRequestTicket is not null) throw new InvalidOperationException("현재 프로필 열기가 끝난 뒤 복구하세요.");
        var profile = _state.Arr("profiles").First(p => p.S("id") == id);
        if (MessageBox.Show(this, $"{profile.S("alias")} 프로필의 관리용 Codex를 종료하고 새 버전으로 다시 엽니다.\n이 창에서 실행 중인 작업은 중단되며, 보내지 않은 입력은 사라질 수 있습니다.\n\n작업을 정리했다면 확인을 누르세요. 원래 Codex와 다른 프로필은 유지됩니다.", "이 프로필 다시 열기", MessageBoxButton.OKCancel, MessageBoxImage.Warning) != MessageBoxResult.OK) return;
        var ticket = ++_navigation;
        _profileRequestTicket = ticket;
        _embedRequested = false;
        Log($"{profile.S("alias")} · 새 버전 적용 · 이전 PID {profile.N("process_id")} · 선택한 관리용 실행을 종료합니다.");
        try
        {
            // Do not call SetParent/SetFocus/WM_CLOSE on the frozen child before
            // recovery. The service operates on verified kernel process handles.
            var result = await Request("profile.recover", new { profile_id = id,
                expected_generation = profile.S("generation"), interrupt_running_work = true });
            Log($"{profile.S("alias")} · 이전 실행 종료 확인 · {result.S("state")}");
            if (_attached?.Pid == (int)profile.N("process_id"))
            {
                _host.Detach(); // The process has exited; only forget its dead HWND.
                _attached.ClearMarker(); _attached = null;
            }
            foreach (var window in _parked.Values.Where(w => w.Pid == (int)profile.N("process_id")).ToArray())
            { window.ClearMarker(); _parked.Remove(window.Handle); }
        }
        finally { if (_profileRequestTicket == ticket) _profileRequestTicket = null; }
        if (ticket != _navigation || _closing) return;
        await ShowProfileCoreAsync(id);
    }
    private Task CheckInputAsync()
    {
        SetStatus(_host.InputDiagnostic());
        return Task.CompletedTask;
    }
    private async Task ShowCatalogAsync()
    {
        var representative = _selectedProfile;
        var ticket = ++_navigation; _expectedConversation = null;
        SetStatus("대표 계정의 원본 Codex 기록 화면을 준비하고 있습니다…");
        var result = await Request("catalog.show", representative is null ? new { } : new { profile_id = representative });
        if (ticket != _navigation || _closing) return;
        var viewer = result.Get("profile");
        if (viewer.ValueKind != JsonValueKind.Object) throw new InvalidOperationException(result.Message("원본 기록 보기 인스턴스를 확인하지 못했습니다."));
        ParkCurrent();
        if (!result.B("readonly_viewer"))
        {
            _viewingCatalog = false; _selectedProfile = viewer.S("id");
            BeginAttach(); await RefreshAsync();
            if (ticket == _navigation && !_closing) { Render(); TryAttach(Profile()); SetStatus(result.Message("공통 작업 목록을 열었습니다.")); }
            return;
        }
        _viewerProfile = viewer;
        _viewerRepresentative = result.S("representative_profile_id", representative ?? "");
        if (_selectedProfile is null && _viewerRepresentative != "") _selectedProfile = _viewerRepresentative;
        _viewingCatalog = true; BeginAttach();
        await RefreshAsync();
        if (ticket != _navigation || !_viewingCatalog || !_embedRequested || _closing) return;
        Render(); TryAttach(_viewerProfile);
        Log(result.Message("대표 계정의 기록 화면 열기 응답을 받았습니다."));
    }
    private void BeginAttach()
    {
        if (_selectedProfile is not null) { _detachedProfiles.Remove(_selectedProfile); _attachAttempts.Reset(_selectedProfile); }
        _embedRequested = true; _attachDeadline = DateTime.UtcNow.AddSeconds(25);
        _expectedWindowLaunch = null; _closedWindow = 0;
    }
    private void OnAttachmentLost(NativeWindowHost host, nint hwnd, bool parentChanged)
    {
        if (_attachedWindows.TryGetValue(host, out var attached) && attached.Handle == hwnd)
        {
            // Keep ownership of the hidden window for reattachment and cleanup
            // if Electron replaces its parent during startup.
            if (parentChanged && attached.MatchesLifetime) _parked[hwnd] = attached;
            else attached.ClearMarker();
            _attachedWindows.Remove(host);
        }
        _windowLaunches.Remove(host);
        if (host != _host) return;
        if (_closing || _profileRequestTicket is not null || !_embedRequested) return;
        _host.Visibility = Visibility.Collapsed;
        _empty.Visibility = Visibility.Visible;
        if (parentChanged)
        {
            if (attached is not null && attached.MatchesLifetime)
                NativeWindowHost.SetWindowVisibility(attached.Handle, attached.Pid, attached.Executable, false, out _);
            _empty.Text = "Codex 창을 관리창 안에 다시 연결하고 있습니다…";
        }
        else
        {
            _closedWindow = hwnd;
            _expectedWindowLaunch = null;
            _attachDeadline = DateTime.UtcNow.AddSeconds(25);
            _empty.Text = "Codex 창이 종료되거나 교체되었습니다. 새 창을 기다리고 있습니다…";
            Log("창 연결 · 이전 창 종료 확인 · 새 창 연결 대기");
        }
    }
    private void AttachFailure(string error)
    {
        _embedRequested = false;
        // A failed detach must keep the native host alive and visible.
        if (!_host.IsAttached) _host.Visibility = Visibility.Collapsed;
        _empty.Visibility = _host.IsAttached ? Visibility.Collapsed : Visibility.Visible;
        _empty.Text = "Codex 창 연결을 완료하지 못했습니다.\n아래 로그를 복사해 알려주세요.\n‘관리창 안에 표시’로 다시 시도할 수 있습니다.\n\n" + error;
        SetStatus(error, true);
    }
    private void TryAttach(JsonElement profile, bool refreshPresentation = true)
    {
        if (ProfileLoginPresentation.ShowRecovery(profile)) { ShowLoginRecovery(profile); return; }
        if (!_embedRequested || _host.IsTransitioning) return;
        if (AutomaticProfileUpdate.IsClosing(profile))
        {
            _attachDeadline = DateTime.UtcNow.AddSeconds(25);
            _empty.Text = "업데이트 적용 후 새 Codex 창을 연결합니다…";
            return;
        }
        if (_expectedWindowLaunch is not null && !_expectedWindowLaunch.Matches(profile))
        {
            if (DateTime.UtcNow >= _attachDeadline)
                AttachFailure("새 실행의 창 정보가 아직 확인되지 않았습니다. ‘관리창 안에 표시’로 다시 연결할 수 있습니다.");
            return;
        }
        _hostDeck.Select(profile.S("id"));
        if (_windowLaunches.TryGetValue(_host, out var retained) && retained.Retains(_host, profile))
        {
            // State polls only verify the attached lifetime. UpdateLayout here
            // flushes the entire dirty WPF tree after every metadata refresh.
            // Native move/size events and the host's watchdog already maintain
            // geometry; explicit selection/repair keeps its immediate path.
            if (!refreshPresentation && _host.Visibility == Visibility.Visible)
            {
                _empty.Visibility = Visibility.Collapsed;
                return;
            }
            _host.Visibility = Visibility.Visible;
            _host.UpdateLayout();
            if (_host.SynchronizeLayout() && _host.IsAttached) _empty.Visibility = Visibility.Collapsed;
            return;
        }
        var hwnd = (nint)profile.N("window_handle"); var pid = (int)profile.N("process_id");
        var path = profile.S("executable_path", profile.S("executable"));
        if (hwnd == 0 || hwnd == _closedWindow || pid == 0 || string.IsNullOrWhiteSpace(path))
        {
            if (DateTime.UtcNow >= _attachDeadline) AttachFailure("25초 안에 Codex 기본 창을 찾지 못했습니다. 프로필을 다시 선택해 주세요.");
            else { _empty.Text = "Codex 기본 창을 찾고 있습니다…\n25초 안에 연결되지 않으면 오류와 확인 방법을 표시합니다."; _empty.Visibility = Visibility.Visible; }
            return;
        }
        _hostDeck.Select(profile.S("id"));
        if (_host.AttachedHandle == hwnd)
        {
            _host.Visibility = Visibility.Visible;
            _host.UpdateLayout();
            if (_host.SynchronizeLayout() && _host.IsAttached) _empty.Visibility = Visibility.Collapsed;
            return;
        }
        AttachProfileWindow(profile, _host, selected: true);
    }
    private void ShowLoginRecovery(JsonElement profile)
    {
        _embedRequested = false;
        _expectedWindowLaunch = null;
        _hostDeck.Select(profile.S("id"));
        _host.Visibility = Visibility.Hidden;
        _empty.Text = ProfileLoginPresentation.Message(profile);
        _empty.Visibility = Visibility.Visible;
        SetStatus(profile.S("alias") + " · 전용 로그인이 필요합니다. 강제 재시작할 필요는 없습니다.", true);
    }
    private void ReconcileBackgroundWindows()
    {
        if (_closing) return;
        foreach (var profile in _state.Arr("profiles").Concat(_state.Arr("view_instances")))
        {
            var id = profile.S("id");
            if (id == "" || profile.S("status") != "running" || profile.N("window_handle") == 0
                || (!_viewingCatalog && id == _selectedProfile) || (_viewingCatalog && id == _viewerProfile.S("id"))
                || AutomaticProfileUpdate.IsClosing(profile)) continue;
            if (_detachedProfiles.TryGetValue(id, out var detached) && detached.Matches(profile)) continue;
            var host = _hostDeck.Ensure(id);
            if (host.IsTransitioning) continue;
            host.Visibility = Visibility.Hidden;
            if (_windowLaunches.TryGetValue(host, out var retained) && retained.Retains(host, profile)) continue;
            AttachProfileWindow(profile, host, selected: false);
        }
    }
    private void AttachProfileWindow(JsonElement profile, NativeWindowHost host, bool selected)
    {
        using var timing = _responsiveness?.Stage("profile.attach");
        host.Visibility = selected ? Visibility.Visible : Visibility.Hidden;
        host.UpdateLayout(); _clientSurface.UpdateLayout();
        if (!host.IsLayoutReady)
        {
            // WPF finishes native child positioning asynchronously. Queue one
            // idle pass without consuming a connection attempt or spinning.
            if (!_layoutRetryQueued)
            {
                _layoutRetryQueued = true;
                Dispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, new Action(() =>
                {
                    try { if (!_closing) Render(); }
                    finally { _layoutRetryQueued = false; }
                }));
            }
            return;
        }
        var id = profile.S("id");
        var hwnd = (nint)profile.N("window_handle"); var pid = (int)profile.N("process_id");
        var path = profile.S("executable_path", profile.S("executable"));
        var lifetime = profile.S("generation") + ":" + pid + ":" + hwnd;
        if (!_attachAttempts.Begin(id, lifetime, DateTime.UtcNow, out var attempt)) return;
        if (host.IsAttached)
        {
            host.DetachForClose();
            if (host.IsAttached) { Log($"{profile.S("alias")} · 이전 창 연결 정리 대기 · {host.LastError}"); return; }
        }
        if (_attachedWindows.Remove(host, out var previous)) previous.ClearMarker();
        _windowLaunches.Remove(host);
        Log($"{profile.S("alias")} · 창 발견 · PID {pid} · HWND {hwnd} · 내부 연결 {attempt}/3");
        if (_parked.TryGetValue(hwnd, out var parked))
        {
            if (!parked.MatchesLifetime) { _parked.Remove(hwnd); return; }
            parked.ClearMarker();
            _parked.Remove(hwnd);
        }
        // Hide the detached window before parenting. Its host controls visibility
        // afterwards; a background restart must never select/raise another tab.
        NativeWindowHost.SetWindowVisibility(hwnd, pid, path, false, out _);
        host.Visibility = selected ? Visibility.Visible : Visibility.Hidden;
        host.UpdateLayout(); _clientSurface.UpdateLayout();
        if (host.Attach(hwnd, pid, path, out var error, allowHidden: true))
        {
            var identity = new WindowIdentity(hwnd, pid, path);
            identity.Marked = NativeWindowInterop.SetPropW(hwnd, identity.Marker, 1);
            _attachedWindows[host] = identity;
            _windowLaunches[host] = WindowLaunchIdentity.From(profile);
            if (selected)
            {
                _closedWindow = 0;
                _empty.Visibility = Visibility.Collapsed; SetStatus("선택한 프로필의 Codex 창을 연결했습니다.");
            }
            else Log($"{profile.S("alias")} · 관리창 내부 연결 완료 · 프로필 선택 시 표시");
        }
        else
        {
            var message = "창 연결 확인 · " + error + " · " + host.DpiSummary;
            Log($"{profile.S("alias")} · {message}");
            var identity = new WindowIdentity(hwnd, pid, path);
            identity.Marked = NativeWindowInterop.SetPropW(hwnd, identity.Marker, 1);
            _parked[hwnd] = identity;
            if (selected && attempt >= 3) AttachFailure(message);
            else if (selected)
            {
                _empty.Visibility = Visibility.Visible;
                _empty.Text = "Codex 시작 상태가 바뀌어 내부 연결을 다시 확인합니다…";
                _attachDeadline = DateTime.UtcNow.AddSeconds(25);
            }
        }
    }
    private void ParkCurrent()
    {
        if (_host.IsTransitioning) throw new InvalidOperationException("창 연결이 진행 중입니다. 연결이 끝난 뒤 프로필을 선택하세요.");
        _hostDeck.HideCurrent();
        _empty.Visibility = Visibility.Visible;
    }
    private void ReleaseCurrentWindow()
    {
        if (_host.IsTransitioning) throw new InvalidOperationException("창 연결이 진행 중입니다. 연결이 끝난 뒤 프로필을 선택하세요.");
        var previous = _attached;
        _host.Detach();
        if (_host.AttachedHandle != 0) throw new InvalidOperationException("원본 창 분리를 완료하지 못했습니다. 현재 창을 유지합니다. " + _host.LastError);
        _attached = null; _windowLaunches.Remove(_host); _host.Visibility = Visibility.Hidden; _empty.Visibility = Visibility.Visible;
        if (previous is not null)
        {
            if (previous.MatchesLifetime && NativeWindowHost.SetWindowVisibility(previous.Handle, previous.Pid, previous.Executable, false, out _)) _parked[previous.Handle] = previous;
            else previous.ClearMarker();
        }
    }
    private Task DetachAsync()
    {
        if (_host.IsTransitioning) throw new InvalidOperationException("창 연결이 진행 중입니다. 연결이 끝난 뒤 분리해 주세요.");
        ++_navigation;
        var profile = _viewingCatalog ? _viewerProfile : Profile();
        _detachedProfiles[profile.S("id")] = WindowLaunchIdentity.From(profile);
        var detached = _attached;
        _embedRequested = false; _host.Detach();
        if (_host.AttachedHandle != 0) throw new InvalidOperationException(_host.LastError ?? "창 분리에 실패했습니다.");
        _attached?.ClearMarker();
        if (detached is not null) NativeWindowHost.SetWindowVisibility(detached.Handle, detached.Pid, detached.Executable, true, out _);
        _attached = null; _host.Visibility = Visibility.Collapsed; _empty.Visibility = Visibility.Visible; _empty.Text = "선택한 Codex를 원래 창으로 표시하고 있습니다.\n관리창 안에 표시 버튼으로 다시 가져올 수 있습니다.";
        SetStatus("원본 창을 분리했습니다. 작업은 계속됩니다."); return Task.CompletedTask;
    }
    private async Task AttachSelectedAsync()
    {
        BeginAttach();
        if (_viewingCatalog) { Render(); TryAttach(_viewerProfile); }
        else await ShowProfileAsync(RequireProfile());
        if (_host.HasLiveAttachment)
        {
            bool requested = _host.RestoreViewport();
            if (!requested || _host.IsViewportSettling)
                SetStatus("Codex 표시 영역을 다시 맞추고 있습니다. 잠시만 기다려 주세요.");
            if (!requested) Log("표시 영역 복구 요청을 다시 시도하고 있습니다. " + _host.LastError);
        }
    }
    private bool _shutdownInProgress, _shutdownComplete;
    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_shutdownComplete) return;
        e.Cancel = true;
        if (_shutdownInProgress) return;
        Log("관리창 닫기 요청 수신");
        try { _notes.PreserveDrafts(); }
        catch (Exception error) { e.Cancel = true; SetStatus("메모 초안을 저장하지 못했습니다: " + error.Message, true); return; }
        // Invalidate asynchronous navigation instead of waiting for a backend
        // request. Its late response already checks this generation before it
        // attaches, selects or shows any window.
        ++_navigation; _profileRequestTicket = null; _embedRequested = false;
        _expectedConversation = null; _expectedWindowLaunch = null;
        if (_hostDeck.Hosts.Any(host => host.IsTransitioning)) { e.Cancel = true; SetStatus("창 연결이 진행 중입니다. 잠시 뒤 관리 앱을 닫아 주세요.", true); return; }
        foreach (var host in _hostDeck.Hosts)
        {
            host.DetachForClose();
            if (host.IsAttached) { e.Cancel = true; MessageBox.Show(this, "원본 창을 안전하게 분리하지 못해 관리창을 유지합니다.\n" + host.LastError, "창 분리 확인"); return; }
            if (_attachedWindows.Remove(host, out var attached)) _parked[attached.Handle] = attached;
        }
        if (_parked.Count == 0 && _client is null)
        {
            using var timing = _responsiveness?.Stage("profile.cached-select");
            _closing = true; _shutdownComplete = true; e.Cancel = false;
            return;
        }
        _shutdownInProgress = true; _closing = true;
        _timer.Stop(); _activityTimer.Stop();
        IsEnabled = false;
        SetStatus("관리 중인 Codex를 종료하고 있습니다…");
        // Return from WPF's cancellable Closing event before the final Close.
        await Task.Yield();
        try
        {
            if (_client is not null && !_client.IsConnected)
            {
                // Reconnect only for shutdown; startup hooks would enqueue new
                // background windows while we are trying to drain them.
                using var reconnect = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await _client.DisposeAsync();
                _client = await ManagerClient.ConnectAsync(_root, reconnect.Token);
            }
            if (_client?.IsConnected == true)
            {
                // Stop the queue before taking the shutdown snapshot. Otherwise
                // a warmup could create another hidden app after its peers exit.
                using var stopWarmup = new CancellationTokenSource(TimeSpan.FromSeconds(20));
                await _client.RequestAsync("manager.stop_warmup", cancellationToken: stopWarmup.Token);
                do
                {
                    _state = await _client.RequestAsync("state", cancellationToken: stopWarmup.Token);
                    if (!_state.Get("profile_warmup").B("worker_active") &&
                        _state.Get("local_launches").N("active") == 0) break;
                    await Task.Delay(100, stopWarmup.Token);
                } while (true);
                // State reads profile identities before the launch counter. A
                // launch may finish between those reads; take a post-drain
                // snapshot while the admission barrier is still armed.
                _state = await _client.RequestAsync("state", cancellationToken: stopWarmup.Token);
                // Preloaded windows have never been attached or parked. Include
                // their verified process/window identities in normal shutdown.
                var hidden = _state.Arr("profiles").Where(p => p.S("status") == "running" &&
                    p.N("window_handle") != 0 && !_parked.ContainsKey((nint)p.N("window_handle"))).ToArray();
                await Task.WhenAll(hidden.Select(async profile =>
                {
                    if (!await NativeWindowShutdown.RequestAsync(_root, (int)profile.N("process_id"),
                        (nint)profile.N("window_handle"), profile.S("executable_path"), profile.N("process_created")))
                        throw new InvalidOperationException("백그라운드 프로필의 정상 종료 확인이 필요합니다.");
                }));
            }
            await Task.WhenAll(_parked.Values.ToArray().Select(async window =>
            {
                if (!window.MatchesLifetime) { _parked.Remove(window.Handle); return; }
                Log($"관리 중인 Codex 앱 종료 요청 · PID {window.Pid}");
                if (await NativeWindowShutdown.RequestAsync(_root, window.Pid, window.Handle, window.Executable))
                {
                    Log($"관리 중인 Codex 프로세스 종료 확인 · PID {window.Pid}");
                    window.ClearMarker(); _parked.Remove(window.Handle);
                }
            }));
            if (_parked.Count > 0)
                throw new InvalidOperationException("일부 Codex가 종료 요청에 응답하지 않았습니다. 관리창을 유지합니다. 구버전 프로필은 원래 창에서 앱을 종료해 주세요.");
            if (_client?.IsConnected == true)
            {
                // A mode switch can hand the UI to a process that is no longer a
                // child of the tracked window, so the graceful close above can
                // leave ChatGPT processes behind. The service reaps every managed
                // process that carries this profile's own --user-data-dir.
                foreach (var profile in _state.Arr("profiles"))
                {
                    var id = profile.S("id");
                    if (id == "" || profile.S("process_id") == "") continue;
                    try
                    {
                        using var stopDeadline = new CancellationTokenSource(TimeSpan.FromSeconds(6));
                        await _client.RequestAsync("profile.cleanup",
                            new { profile_id = id, generation = profile.S("generation") },
                            cancellationToken: stopDeadline.Token);
                        Log($"남은 Codex 프로세스 정리 · {profile.S("alias", id)}");
                    }
                    catch (Exception error) { Log("프로세스 정리 건너뜀 · " + error.Message); }
                }
            }
            if (_client?.IsConnected == true)
            {
                try
                {
                    using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(4));
                    await _client.RequestAsync("supervisor.retire", cancellationToken: deadline.Token);
                    Log("관리 서비스 종료 요청 완료");
                }
                catch (Exception error) { Log("관리 서비스는 남은 연결·작업 종료 후 자동 정리됩니다 · " + error.Message); }
            }
            _shutdownComplete = true;
            Close();
        }
        catch (Exception error)
        {
            if (_client?.IsConnected == true)
            {
                try
                {
                    using var resume = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                    await _client.RequestAsync("manager.resume_launches", cancellationToken: resume.Token);
                }
                catch (Exception resumeError) { Log("프로필 실행 재개 확인 · " + resumeError.Message); }
            }
            _closing = false; _shutdownInProgress = false;
            IsEnabled = true;
            _timer.Start(); _activityTimer.Start();
            SetStatus(error.Message, true);
        }
    }
    private void SetStatus(string message, bool error = false)
    {
        _status.Text = message; _status.ToolTip = message; _status.Foreground = error ? new SolidColorBrush(Color.FromRgb(245, 189, 121)) : Muted;
        if (_events.Count == 0 || !_events[^1].EndsWith(message, StringComparison.Ordinal)) Log((error ? "오류 · " : "") + message);
    }
    private void Log(string message)
    {
        using var timing = _responsiveness?.Stage("shell.log-file-write");
        _events.Add(_diagnostics.Add(message));
        if (_events.Count > 300) _events.RemoveAt(0);
        if (_logFlushPending) return;
        _logFlushPending = true;
        Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() =>
        {
            _logFlushPending = false;
            if (_closing) return;
            _logText.Text = _diagnostics.Text; _logText.ScrollToEnd();
            _logText.ToolTip = _diagnostics.WriteError ?? _diagnostics.Path;
        }));
    }
    private async Task RegisterCurrentAsync()
    {
        var alias = Dialogs.Prompt(this, "현재 로그인 연결", "현재 원본 앱 계정의 별칭", "03");
        if (alias is null) return;
        var result = await Request("profile.register_current", new { alias });
        await RefreshAsync();
        await ShowProfileAsync(result.S("id"));
    }

    private async Task AddProfileAsync()
    {
        var kind = Dialogs.Select(this, "프로필 추가", "작업을 실행할 모델의 연결 방식을 선택하세요.",
            [new Choice("codex", "Codex 계정 · ChatGPT 로그인"), new Choice("external", "외부 API 모델 · Codex에서 직접 작업")]);
        if (kind is null) return;
        string? modelId = null;
        Dictionary<string, object>? modelSettings = null;
        if (kind.Id == "external")
        {
            var registry = await Request("providers.list");
            var available = registry.Arr("models").Where(m => m.B("verified") || m.Get("capabilities").B("verified"))
                .Where(m => registry.Arr("providers").Any(p => p.S("id") == m.S("provider_id") && p.B("key_saved") && p.B("adapter_available")))
                .Select(m => new Choice(m.S("id"), m.S("display_name", m.S("name")) + " · " + m.S("reasoning_effort"), m)).ToArray();
            if (available.Length == 0) { SetStatus("하위 에이전트 · API 공급자에서 모델 등록, 키 저장, 연결 시험을 먼저 완료하세요.", true); return; }
            var model = Dialogs.Select(this, "외부 API 프로필", "선택한 모델이 직접 답변하고 도구를 실행합니다. 기존 작업 기록을 이어서 사용하며, 전송한 기록은 해당 API로 전달됩니다.", available);
            if (model is null) return;
            modelId = model.Id;
            modelSettings = Dialogs.ExternalModelSettings(this, model.Data);
            if (modelSettings is null) return;
        }
        var alias = Dialogs.Prompt(this, "프로필 이름", "목록에 표시할 이름을 입력하세요."); if (alias is null) return;
        var result = await Request("profile.add", new { alias, kind = kind.Id, model_id = modelId, settings = modelSettings }); await RefreshAsync();
        await ShowProfileAsync(result.S("id"), kind.Id == "external" ? "profile.show" : "profile.login");
    }
    private string RequireContextProfile() => _profiles.ContextMenu.Tag as string ?? RequireProfile();
    private Task EditExternalModelAsync() => EditExternalModelAsync(_contextProfile ?? RequireProfile());
    private async Task EditExternalModelAsync(string id)
    {
        var profile = _state.Arr("profiles").First(p => p.S("id") == id);
        if (profile.S("auth_mode") != "external") { SetStatus("외부 API 프로필을 선택하세요. GPT 하위 에이전트 설정은 ‘하위 에이전트 · API 공급자’에서 변경합니다."); return; }
        var registry = await Request("providers.list");
        var model = registry.Arr("models").First(m => m.S("id") == profile.S("external_model_id"));
        var settings = Dialogs.ExternalModelSettings(this, model, profile.Get("external_settings"), profile.S("alias"));
        if (settings is null) return;
        var result = await Request("profile.model_settings", new { profile_id = id, settings });
        await RefreshAsync(); SetStatus(result.Message());
    }
    private Task RenameProfileAsync() => RenameProfileAsync(RequireProfile());
    private async Task RenameProfileAsync(string id)
    {
        var current = _state.Arr("profiles").FirstOrDefault(p => p.S("id") == id); var alias = Dialogs.Prompt(this, "프로필 별칭", "표시할 이름", current.S("alias")); if (alias is null) return;
        var result = await Request("profile.rename", new { profile_id = id, alias }); await RefreshAsync(); SetStatus(result.Message("별칭을 변경했습니다."));
    }
    private Task MoveProfileByAsync(int delta) => _profileOrdering.MoveByAsync(_contextProfile ?? RequireProfile(), delta);
    private async Task MoveProfileAsync(ProfileMove move)
    {
        ++_stateRevision; // Discard any state read that started before this move.
        var result = await Request("profile.move", new { profile_id = move.ProfileId, target_profile_id = move.TargetProfileId, position = move.Position });
        var ids = result.Arr("profile_ids").Select(p => p.GetString()).ToArray();
        var profiles = _state.Arr("profiles").OrderBy(p => Array.IndexOf(ids, p.S("id"))).ToArray();
        _state = JsonSerializer.SerializeToElement(_state.EnumerateObject().ToDictionary(p => p.Name,
            p => p.Name == "profiles" ? JsonSerializer.SerializeToElement(profiles) : p.Value));
        var items = _profiles.Items.OfType<Choice>().OrderBy(p => Array.IndexOf(ids, p.Id)).ToArray();
        _rendering = true;
        try { Fill(_profiles, items, _selectedProfile); }
        finally { _rendering = false; }
        SetStatus(result.Message("프로필 순서를 저장했습니다."));
    }
    private Task RemoveProfileAsync() => RemoveProfileAsync(RequireProfile());
    private async Task RemoveProfileAsync(string id)
    {
        var result = await Request("profile.remove", new { profile_id = id });
        if (_selectedProfile == id) _selectedProfile = null;
        _loginStatuses.Remove(id); await RefreshAsync(); SetStatus(result.Message());
    }
    private async Task RestoreProfileAsync()
    {
        var removed = _state.Arr("removed_profiles").Select(p => new Choice(p.S("id"), p.S("alias"), p)).ToArray();
        if (removed.Length == 0) { SetStatus("제거한 계정이 없습니다."); return; }
        var choice = Dialogs.Select(this, "계정 복원", "로그인과 대화가 보존된 계정을 선택하세요.", removed);
        if (choice is null) return;
        var result = await Request("profile.restore", new { profile_id = choice.Id });
        await RefreshAsync(); SetStatus(result.Message());
    }
    private async Task BindAccountAsync()
    {
        var id = _contextProfile ?? RequireProfile(); var data = await Request("accounts.refresh");
        var accounts = data.Arr("entries").Concat(data.Arr("accounts")).Concat(data.Arr("profiles")).ToArray();
        if (accounts.Length == 0 && data.ValueKind == JsonValueKind.Array) accounts = data.Items().ToArray();
        var choice = Dialogs.Select(this, "llm-usage 계정 연결", "이 프로필에 사용할 계정 별칭을 선택하세요.", accounts.Select(a => new Choice(a.S("id", a.S("account_id")), a.S("alias", a.S("name", "이름 없음")), a)));
        if (choice is null) return;
        var result = await Request("profile.bind", new { profile_id = id, usage_account_id = choice.Id }); await RefreshAsync(); SetStatus(result.Message("계정 별칭을 연결했습니다."));
    }
    private Task PrepareProfileAsync() => PrepareProfileAsync(RequireProfile());
    private async Task PrepareProfileAsync(string id) { var result = await Request("profile.prepare", new { profile_id = id }); await RefreshAsync(); SetStatus(result.Message()); }
    private async Task RefreshAccountsAsync() { var result = await Request("accounts.refresh"); await RefreshAsync(); SetStatus(result.Message("계정과 사용량을 새로 확인했습니다.")); }
    private async Task AddShortcutAsync()
    {
        var data = await Request("catalog.list", new { paged = true });
        var result = Dialogs.Shortcut(this, _state.Arr("profiles"), data, _selectedProfile,
            refreshCatalog: (query, cursor) => Request("catalog.list", new { query, cursor }));
        if (result is null) return;
        await Request("shortcut.add", result); await RefreshAsync(); SetStatus("작업 바로가기를 추가했습니다.");
    }
    private async Task CaptureShortcutAsync()
    {
        if (_attached is not { } window) throw new InvalidOperationException("먼저 프로필의 원본 Codex 창을 관리창 안에 표시하세요.");
        var ticket = _navigation; var profileId = _selectedProfile;
        var viewerId = _viewingCatalog ? _viewerProfile.S("id") : null;
        var captured = await NativeConversationCapture.CaptureAsync(window.Handle, window.Pid, window.Executable);
        if (ticket != _navigation || profileId != _selectedProfile || _closing) return;
        if (!captured.Success) throw new InvalidOperationException(captured.Error ?? "현재 대화 링크를 확인하지 못했습니다.");
        JsonElement resolved = default;
        if (!string.IsNullOrEmpty(viewerId))
        {
            resolved = await Request("catalog.resolve", new { viewer_profile_id = viewerId, projection_thread_id = captured.ThreadId });
            if (ticket != _navigation || profileId != _selectedProfile || _closing) return;
        }
        var data = await Request("catalog.list", new { paged = true });
        if (ticket != _navigation || profileId != _selectedProfile || _closing) return;
        var matchingRecords = new List<JsonElement>();
        string? lookupCursor = null;
        do
        {
            var lookup = await Request("catalog.list", new { thread_id = resolved.S("thread_id", captured.ThreadId!), cursor = lookupCursor });
            if (ticket != _navigation || profileId != _selectedProfile || _closing) return;
            matchingRecords.AddRange(lookup.Arr("conversations"));
            lookupCursor = lookup.S("next_cursor") is { Length: > 0 } more ? more : null;
        } while (lookupCursor is not null);
        JsonElement reference;
        if (resolved.ValueKind == JsonValueKind.Object)
        {
            if (resolved.S("thread_id") == "" || resolved.S("source_store_id") == "" || resolved.S("host_id") == "")
                throw new InvalidOperationException("읽기 전용 화면의 대화와 원본 출처를 연결하지 못했습니다.");
            var exact = matchingRecords.FirstOrDefault(t => t.S("thread_id") == resolved.S("thread_id")
                && t.S("host_id") == resolved.S("host_id") && t.S("source_store_id") == resolved.S("source_store_id"));
            reference = exact.ValueKind == JsonValueKind.Object ? exact : resolved;
        }
        else
        {
            var matches = matchingRecords.Where(t => t.S("thread_id") == captured.ThreadId).ToArray();
            if (matches.Length == 0) throw new InvalidOperationException("대화 ID는 확인했지만 원본 저장소와 호스트가 아직 색인되지 않았습니다. 전체 작업에서 해당 출처가 확인된 뒤 연결하세요.");
            if (matches.Length == 1) reference = matches[0];
            else
            {
                var source = Dialogs.Select(this, "대화 출처 확인", "같은 대화 ID가 여러 저장소에서 발견되었습니다. 실제 원본 출처를 선택하세요.", matches.Select(t => new Choice(t.S("source_store_id"), t.S("source_alias", t.S("source_store_id")) + " · " + t.S("host_id"), t)));
                if (source is null) return; reference = source.Data;
            }
        }
        var form = Dialogs.Shortcut(this, _state.Arr("profiles"), data, _selectedProfile, reference,
            (query, cursor) => Request("catalog.list", new { query, cursor }));
        if (form is null) return; await Request("shortcut.add", form); await RefreshAsync(); SetStatus("현재 대화의 확인된 출처로 바로가기를 저장했습니다.");
    }
    private async Task VerifyConversationAsync()
    {
        if (_expectedConversation is not { } expected || expected.ProfileId != _selectedProfile) throw new InvalidOperationException("먼저 작업 바로가기로 대화를 연 뒤 확인하세요.");
        if (expected.HostId != "local") throw new InvalidOperationException("SSH 대화 링크에는 호스트 정보가 없어 자동 확인할 수 없습니다. 원본 화면의 연결과 대화를 확인하세요.");
        if (_attached is not { } window) throw new InvalidOperationException("해당 프로필 창을 관리창 안에 표시한 뒤 확인하세요.");
        var ticket = _navigation;
        var result = await NativeConversationCapture.CaptureAsync(window.Handle, window.Pid, window.Executable);
        if (ticket != _navigation || _selectedProfile != expected.ProfileId || _closing) return;
        if (!result.Success) throw new InvalidOperationException(result.Error ?? "원본 화면의 현재 대화를 확인하지 못했습니다.");
        if (result.ThreadId != expected.ThreadId) throw new InvalidOperationException("현재 원본 화면은 요청한 대화와 다릅니다. 대화가 열린 뒤 다시 확인하세요. 메시지는 전송하지 않았습니다.");
        SetStatus(_viewingCatalog ? "읽기 전용 원본 화면의 대화가 바로가기의 표시 ID와 일치합니다. 실제 대화의 원본 출처는 그대로 유지됩니다." : "선택한 프로필의 대화 ID가 바로가기와 일치합니다. 로그인 계정 확인 상태는 위 상태줄을 확인하세요.");
    }
    private async Task OpenShortcutAsync(string id)
    {
        var item = _state.Arr("shortcuts").FirstOrDefault(s => s.S("id") == id);
        var profileId = item.S("profile_id"); if (profileId == "") throw new InvalidOperationException("이 바로가기에 연결된 프로필이 없습니다.");
        using var action = _profileActions.Enter(profileId, "대화 열기");
        var ticket = ++_navigation; if (_selectedProfile != profileId || _viewingCatalog) ParkCurrent(); _viewingCatalog = false; _selectedProfile = profileId;
        _hostDeck.Select(profileId); BeginAttach(); _profileRequestTicket = ticket;
        if (_host.IsAttached) _host.Visibility = Visibility.Visible;
        Render();
        JsonElement result;
        try { result = await Request("conversation.open", new { shortcut_id = id }); }
        finally { if (_profileRequestTicket == ticket) _profileRequestTicket = null; }
        if (ticket != _navigation || _closing) return;
        if (result.S("state") == "waiting_for_reader")
        {
            ApplyConversationResult(result, item, profileId);
            await RefreshAsync();
            if (ticket != _navigation || _closing) return;
            SetStatus(result.Message("Codex가 준비되면 선택한 대화로 자동 이동합니다."));
            var completed = await ConversationReadyWait.CompleteAsync(result,
                () => ticket == _navigation && _selectedProfile == profileId && !_closing,
                token => Request("conversation.navigate", new { navigation_id = token }),
                () => Task.Delay(750));
            if (completed is null) return;
            result = completed.Value;
            Log($"{Profile().S("alias")} · 준비 후 대화 이동 · {result.S("state")}");
        }
        ApplyConversationResult(result, item, profileId);
        await RefreshAsync();
        if (ticket != _navigation || _selectedProfile != profileId || _closing) return;
        SetStatus(result.Message("대화 열기 요청을 보냈습니다."), result.S("state") == "blocked");
    }
    private void ApplyConversationResult(JsonElement result, JsonElement item, string profileId)
    {
        if (result.B("readonly_viewer") && result.Get("profile").ValueKind == JsonValueKind.Object)
        {
            ParkCurrent(); _viewerProfile = result.Get("profile");
            _viewerRepresentative = result.S("representative_profile_id", profileId);
            _viewingCatalog = true; BeginAttach();
        }
        else if (_viewingCatalog)
        {
            ParkCurrent(); _viewingCatalog = false; BeginAttach();
        }
        if (result.S("state") != "request_sent")
        {
            _expectedCanonicalThread = null; _expectedConversation = null; return;
        }
        _expectedCanonicalThread = item.S("thread_id");
        _expectedConversation = (profileId, result.S("projection_thread_id", _expectedCanonicalThread), item.S("host_id", "local"));
    }
    private string RequireShortcut() => _contextShortcut ?? (_shortcuts.SelectedItem as Choice)?.Id ?? throw new InvalidOperationException("먼저 작업 바로가기를 선택하세요.");
    private string RequireContextShortcut() => _shortcuts.ContextMenu.Tag as string ?? RequireShortcut();
    private Task MoveShortcutAsync() => MoveShortcutByIdAsync(RequireShortcut());
    private async Task MoveShortcutByIdAsync(string id)
    {
        var choice = Dialogs.Select(this, "바로가기 이동", "다음에 이 작업을 열 프로필을 고르세요. 현재 작업은 계속됩니다.", _state.Arr("profiles").Select(p => new Choice(p.S("id"), p.S("alias"), p)));
        if (choice is null) return; await Request("shortcut.move", new { shortcut_id = id, profile_id = choice.Id }); await RefreshAsync(); SetStatus("바로가기를 이동했습니다. 진행 중인 작업은 유지됩니다.");
    }
    private Task DeleteShortcutAsync() => DeleteShortcutByIdAsync(RequireShortcut());
    private async Task DeleteShortcutByIdAsync(string id) { await Request("shortcut.delete", new { shortcut_id = id }); await RefreshAsync(); SetStatus("링크를 삭제했습니다. 실제 작업은 보존됩니다. ‘삭제한 링크 복구’로 되돌릴 수 있습니다."); }
    private async Task RenameShortcutByIdAsync(string id)
    {
        var item = _state.Arr("shortcuts").FirstOrDefault(s => s.S("id") == id);
        var alias = Dialogs.Prompt(this, "바로가기 별칭 변경", "이 목록에 표시할 이름입니다. 원본 작업의 제목은 그대로 유지됩니다.", item.S("alias"));
        if (alias is null) return;
        await Request("shortcut.rename", new { shortcut_id = id, alias }); await RefreshAsync(); SetStatus("바로가기 별칭을 변경했습니다.");
    }
    private async Task UndoShortcutAsync() { var result = await Request("shortcut.undo"); await RefreshAsync(); SetStatus(result.Message("바로가기 삭제를 취소했습니다.")); }
    private Task ProvidersAsync() => ShowProvidersAsync(_selectedProfile);
    private Task ProfileProvidersAsync() => ShowProvidersAsync(_contextProfile ?? RequireProfile());
    private async Task ShowProvidersAsync(string? profileId)
    {
        // Capture the menu target before the RPC yields and the context menu
        // clears itself. Editing another profile must not select or launch it.
        var registry = await Request("providers.list");
        var profile = _state.Arr("profiles").FirstOrDefault(p => p.S("id") == profileId);
        if (profileId is not null && profile.ValueKind == JsonValueKind.Undefined)
            throw new InvalidOperationException("설정할 프로필이 목록에서 제거되었습니다.");
        await Dialogs.ProvidersAsync(this, profile, registry, Request);
        await RefreshAsync();
    }
    private Task PersonalSkillsAsync()
    {
        var window = new PersonalSkillsWindow((command, args) => Request(command, args)) { Owner = this };
        window.Loaded += async (_, _) => await window.LoadAsync();
        window.ShowDialog();
        return Task.CompletedTask;
    }
    private async Task RemoteAsync()
    {
        var hosts = await Request("remote.list"); await Dialogs.RemoteAsync(this, RequireProfile(), hosts, Request); await RefreshAsync();
    }
    private async Task ProfileUpdatesAsync()
    {
        await RefreshAsync();
        var snapshot = _state;
        var action = Dialogs.ProfileUpdates(this, snapshot);
        if (action == "retry")
        {
            await Request("manager.startup", new { retry_failed = true });
            SetStatus("선택한 계정과 관계없이 모든 프로필의 최신 버전을 적용합니다.");
        }
        else if (action == "legacy")
        {
            var profiles = snapshot.Arr("profiles").Where(p => p.Get("restart").S("phase") == "attention" &&
                p.Get("restart").S("code") == "runtime_state_unavailable").Select(p => new {
                    profile_id = p.S("id"), generation = p.S("generation"), job_id = p.Get("restart").S("id") }).ToArray();
            await Request("manager.recover_legacy", new { profiles, interrupt_running_work = true });
            SetStatus("확인한 구버전 프로필을 한 번에 정리합니다. 새 창은 관리창 안에 연결됩니다.");
        }
        await RefreshAsync();
    }
    private async Task UpdatesAsync()
    {
        SetStatus("설치된 Codex와 공식 업데이트 경로를 확인하고 있습니다…");
        var result = await Request("updates.check"); await RefreshAsync();
        if (result.B("worker_active"))
        {
            SetStatus(result.Message("업데이트가 진행 중입니다."));
            return;
        }
        var action = Dialogs.Update(this, result);
        if (!action) { SetStatus(result.Message("업데이트 상태를 확인했습니다.")); return; }
        SetStatus("업데이트 준비 상태를 확인하고 있습니다…");
        result = await Request("updates.apply"); await RefreshAsync();
        var detail = Dialogs.ResultSummary(result);
        SetStatus(detail, !result.B("worker_active") && result.S("status") != "complete");
        if (!result.B("worker_active"))
            MessageBox.Show(this, detail, "Codex 업데이트", MessageBoxButton.OK, MessageBoxImage.Information);
    }
}
