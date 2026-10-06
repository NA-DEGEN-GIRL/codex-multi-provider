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

public sealed partial class MainWindow : Window
{
    private readonly string _root;
    private readonly bool _fixture;
    private ManagerClient? _client;
    private readonly Func<string, object?, Task<JsonElement>>? _fixtureRequest;
    private readonly TaskNotesPanel _notes;
    // Notes open/closed is remembered per profile; _notesKey is the profile
    // (or catalog) whose state the panel currently shows.
    private readonly NotesPanelVisibility _notesVisibility;
    private Action<bool>? _setNotesVisible;
    private string? _notesKey;
    private readonly ManagerUpdatePanel _managerUpdates;
    private TaskContextWatcher? _taskContext;
    private SelectedTask? _selectedTask;
    // Cold-profile cache notices already shown (task/profile/warm profile -> time).
    private readonly Dictionary<string, DateTime> _cacheNotices = new();
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
    private readonly TextBlock _profileCount = new() { Foreground = Muted, FontSize = 12, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
    private readonly ListOrdering _profileOrdering;
    private readonly ListOrdering _shortcutOrdering;
    private readonly ListBox _shortcuts = new();
    private readonly TextBlock _status = new() { Name = "WorkspaceStatus", TextWrapping = TextWrapping.NoWrap, TextTrimming = TextTrimming.CharacterEllipsis, FontSize = 12, Foreground = Muted, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _executionMode = new() { TextWrapping = TextWrapping.Wrap, FontSize = 12, LineHeight = 18, Foreground = Muted, Margin = new Thickness(10, 4, 6, 4) };
    private string? _profileOpenNoticeProfile;
    private readonly TextBlock _identity = new() { Name = "WorkspaceIdentity", FontSize = 17, FontWeight = FontWeights.SemiBold, TextTrimming = TextTrimming.CharacterEllipsis, Margin = new Thickness(0, 6, 0, 6) };
    private readonly TextBlock _taskIdentity = new() { Name = "WorkspaceTask", FontSize = 15, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 12, 0, 0), TextTrimming = TextTrimming.CharacterEllipsis };
    private readonly TextBlock _attention = new() { Foreground = Brushes.Orange, FontSize = 12, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0), Visibility = Visibility.Collapsed };
    private readonly TextBlock _mode = new() { Foreground = Muted, Margin = new Thickness(0, 5, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _accountState = new() { Foreground = Muted, Margin = new Thickness(0, 5, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _runtimeVersion = new() { Foreground = Muted, Margin = new Thickness(0, 5, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap };
    // SSH hosts of the selected profile: kept here and in the tooltip, not on cards.
    private readonly TextBlock _sshHosts = new() { Name = "WorkspaceSshHosts", Foreground = Muted, Margin = new Thickness(0, 5, 0, 0), FontSize = 12, TextWrapping = TextWrapping.Wrap };
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
    private readonly TextBlock _profileUpdateStatus = new() { TextWrapping = TextWrapping.Wrap, Foreground = Muted, FontSize = 12, LineHeight = 18, Margin = new Thickness(0, 4, 0, 8) };
    private string? _lastUpdateNotice;
    private readonly TextBlock _updateStatus = new() { TextWrapping = TextWrapping.Wrap, FontSize = 12, Foreground = Muted,
        Margin = new Thickness(0, 4, 6, 6) };
    private readonly Expander _updateDetails = new() { Name = "CodexUpdateDetails", Header = "Codex 업데이트 상태", Visibility = Visibility.Collapsed };
    private readonly TextBlock _desktopCompatibility = new() { Name = "DesktopCompatibilityNotice", TextWrapping = TextWrapping.Wrap,
        FontSize = 12, LineHeight = 18, Foreground = Muted, Margin = new Thickness(0, 0, 0, 8), Visibility = Visibility.Collapsed };
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
    // Each profile's profile.show/login payload until a state requested after it
    // lands. Older state must not hide (login recovery), fail or detach it.
    private readonly Dictionary<string, JsonElement> _shownProfiles = [];
    private (int Ticket, string Id, long Started)? _attachTiming;
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
    private bool _administratorLaunchPending, _restartAsAdministrator;
    private int _navigation;
    private int _stateRevision;
    private string? _contextProfile, _contextShortcut;
    // The shortcut the user opened, its profile and that profile's task at the
    // time. It stays highlighted until another profile is shown or Codex opens
    // a different task there; otherwise the highlight follows the open task.
    private (string Id, string ProfileId, string BaselineThread)? _shortcutSelection;
    private (string ProfileId, string ThreadId, string HostId)? _expectedConversation;
    private string? _expectedCanonicalThread;
    private bool _profileAlreadySelected;
    private bool _viewingCatalog;
    private JsonElement _viewerProfile;
    private string? _viewerRepresentative;
    private static readonly SolidColorBrush Muted = new(Color.FromRgb(159, 167, 183));
    private sealed class ProfileOpenException(string profileId, Exception cause) : Exception(cause.Message, cause)
    {
        public string ProfileId { get; } = profileId;
    }
    private sealed record WindowIdentity(nint Handle, int Pid, string Executable)
    {
        public string Marker { get; } = "Codex.ControlCenter.Parked." + Guid.NewGuid().ToString("N");
        public bool Marked { get; set; }
        public bool MatchesLifetime => Marked && NativeWindowInterop.IsWindow(Handle) && NativeWindowInterop.GetPropW(Handle, Marker) == 1;
        public void ClearMarker() { if (MatchesLifetime) NativeWindowInterop.RemovePropW(Handle, Marker); Marked = false; }
    }

    public MainWindow(string root, bool fixture = false, Func<string, object?, Task<JsonElement>>? fixtureRequest = null, bool administratorPending = false)
    {
        ConfirmProfileRestart = (message, title) => MessageBox.Show(this, message, title,
            MessageBoxButton.OKCancel, MessageBoxImage.Warning) == MessageBoxResult.OK;
        if (!fixture && fixtureRequest is not null) throw new ArgumentException("Request fixtures require an isolated fixture window.");
        _fixtureRequest = fixtureRequest;
        _fixture = fixture;
        _administratorLaunchPending = administratorPending;
        _root = Path.GetFullPath(root);
        _notes = new TaskNotesPanel(_root, async (command, args) =>
        {
            if (_fixtureRequest is not null) return await _fixtureRequest(command, args);
            if (_client is null) throw new InvalidOperationException("관리 서비스 연결을 기다려 주세요.");
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            return await _client.RequestAsync(command, args, timeout.Token);
        });
        _diagnostics = new DiagnosticLog(_root);
        _notesVisibility = new NotesPanelVisibility(_root, Log);
        // Toast delivery reports from its STA worker; the log belongs to this thread.
        if (!fixture) _workspaceNotifications = new WorkspaceNotifications(_root, message =>
        { if (Dispatcher.CheckAccess()) Log(message); else Dispatcher.BeginInvoke(new Action(() => Log(message))); });
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
        BuildSidebar(ProfileEmailButton());
        ScrollViewer.SetHorizontalScrollBarVisibility(_profiles, ScrollBarVisibility.Disabled);
        VirtualizingPanel.SetScrollUnit(_profiles, ScrollUnit.Pixel);
        var profileMenu = MenuButton("프로필 관리", ("모델 기본 설정 · 로컬 / API / Claude", EditExternalModelAsync), ("실행 프리셋", () => ManagePresetsAsync(_contextProfile ?? RequireProfile())), ("하위 에이전트 설정", ProfileProvidersAsync), ("로그인 · API 키 관리", LoginProfileAsync),
            ("현재 앱의 로그인 계정 연결", RegisterCurrentAsync), ("로그인 상태 새로 확인", RefreshLoginStatusAsync),
            ("이 프로필 다시 열기", RecoverProfileAsync), ("작업 종료 후 설정 적용 예약", RestartProfileAsync), ("SSH 작업 종료 후 설정 적용", RestartRemoteProfileAsync),
            ("원래 창으로 분리", DetachAsync), ("별칭 변경", RenameProfileAsync),
            ("계정을 목록에서 제거", RemoveProfileAsync), ("제거한 계정 복원", RestoreProfileAsync), ("프로필 준비", PrepareProfileAsync));
        profileMenu.ContextMenu.Opened += (_, _) => SetClaudeProfileMenu(profileMenu.ContextMenu, Profile());
        // The email toggle also lives in the expanded panel; the menu keeps it
        // reachable while the profiles are a rail.
        var emailItem = new MenuItem();
        emailItem.Click += async (_, _) => await Safe(ToggleProfileEmailsAsync);
        profileMenu.ContextMenu.Items.Add(new Separator());
        profileMenu.ContextMenu.Items.Add(emailItem);
        profileMenu.ContextMenu.Opened += (_, _) => emailItem.Header = _profileEmailsVisible ? "계정 이메일 숨기기" : "계정 이메일 표시";
        // A press on an avatar or card selects it when released in place and
        // reorders the profiles once dragged (ListOrdering).
        _profileOrdering = new ListOrdering(_profiles, move => Safe(() => MoveProfileAsync(move)), ProfileClickedAsync);
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
        _profiles.ContextMenu = ProfileMenu(("위로 이동", id => _profileOrdering.MoveByAsync(id, -1)), ("아래로 이동", id => _profileOrdering.MoveByAsync(id, 1)), ("모델 기본 설정 · 로컬 / API / Claude", EditExternalModelAsync), ("실행 프리셋", id => ManagePresetsAsync(id)), ("하위 에이전트 설정", id => ShowProvidersAsync(id)), ("로그인 · API 키 관리", LoginProfileAsync), ("로그인 상태 새로 확인", RefreshLoginStatusAsync), ("이 프로필 다시 열기", RecoverProfileAsync), ("작업 종료 후 설정 적용 예약", RestartProfileAsync), ("SSH 작업 종료 후 설정 적용", RestartRemoteProfileAsync), ("별칭 변경", RenameProfileAsync), ("계정을 목록에서 제거", RemoveProfileAsync), ("제거한 계정 복원", _ => RestoreProfileAsync()), ("프로필 준비", PrepareProfileAsync));
        _profiles.ContextMenu.Opened += (_, _) =>
        {
            // WPF closes the popup before dispatching MenuItem.Click. Keep the
            // settings target independent of the cleared transient context.
            _profiles.ContextMenu.Tag = _contextProfile ?? _selectedProfile;
            var ids = _profiles.Items.OfType<Choice>().Select(c => c.Id).ToArray();
            var index = Array.IndexOf(ids, _contextProfile ?? _selectedProfile);
            ((MenuItem)_profiles.ContextMenu.Items[0]).IsEnabled = index > 0 && !_profileOrdering.IsInteracting;
            ((MenuItem)_profiles.ContextMenu.Items[1]).IsEnabled = index >= 0 && index < ids.Length - 1 && !_profileOrdering.IsInteracting;
            SetClaudeProfileMenu(_profiles.ContextMenu, _state.Arr("profiles").FirstOrDefault(p => p.S("id") == (_contextProfile ?? _selectedProfile)));
        };
        _profiles.PreviewMouseRightButtonDown += (_, e) =>
        {
            if (ClickedChoice(e.OriginalSource) is not { } choice) return;
            _contextProfile = choice.Id; e.Handled = true;
            _profiles.ContextMenu.PlacementTarget = _profiles; _profiles.ContextMenu.Placement = System.Windows.Controls.Primitives.PlacementMode.MousePoint; _profiles.ContextMenu.IsOpen = true;
        };
        _profiles.ContextMenu.Closed += (_, _) => _contextProfile = null;
        _shortcuts.Margin = new Thickness(8, 0, 8, 8);
        ScrollViewer.SetVerticalScrollBarVisibility(_shortcuts, ScrollBarVisibility.Auto);
        ScrollViewer.SetHorizontalScrollBarVisibility(_shortcuts, ScrollBarVisibility.Disabled);
        VirtualizingPanel.SetScrollUnit(_shortcuts, ScrollUnit.Pixel);
        _shortcuts.ItemTemplate = ShortcutCards.Create();
        _shortcuts.ItemContainerStyle = ProfileCards.ContainerStyle();
        // Press-and-drag on a card reorders the links; released in place it
        // opens the task. A filtered list has no reliable neighbours, so search
        // turns reordering off and the cards are plain buttons again.
        _shortcutOrdering = new ListOrdering(_shortcuts, move => Safe(() => MoveShortcutAsync(move)), ShortcutClickedAsync,
            () => _shortcutFilter.Text.Trim().Length == 0, [ListOrdering.ItemTag, "open"]);
        ShortcutCards.Attach(_shortcuts, async (choice, action, button) =>
        {
            if (action == "menu")
            {
                _contextShortcut = choice.Id;
                _shortcuts.ContextMenu.PlacementTarget = button;
                _shortcuts.ContextMenu.Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom;
                _shortcuts.ContextMenu.IsOpen = true;
                return;
            }
            if (action == "open") { CloseShortcutOverlay(); await Safe(() => OpenShortcutAsync(choice.Id)); }
        });
        _shortcuts.ContextMenu = Menu(("위로 이동", () => _shortcutOrdering.MoveByAsync(RequireContextShortcut(), -1)), ("아래로 이동", () => _shortcutOrdering.MoveByAsync(RequireContextShortcut(), 1)),
            ("열기", () => { CloseShortcutOverlay(); return OpenShortcutAsync(RequireContextShortcut()); }), ("다른 프로필로 이동", () => MoveShortcutByIdAsync(RequireContextShortcut())), ("별칭 변경", () => RenameShortcutByIdAsync(RequireContextShortcut())), ("링크 삭제", () => DeleteShortcutByIdAsync(RequireContextShortcut())));
        _shortcuts.ContextMenu.Opened += (_, _) =>
        {
            // WPF closes the popup before dispatching MenuItem.Click; keep the target.
            var target = _contextShortcut ?? (_shortcuts.SelectedItem as Choice)?.Id;
            _shortcuts.ContextMenu.Tag = target;
            var ids = _shortcuts.Items.OfType<Choice>().Select(c => c.Id).ToArray();
            var index = Array.IndexOf(ids, target);
            var free = _shortcutOrdering.CanReorder && !_shortcutOrdering.IsInteracting;
            foreach (var (item, enabled) in new[] { ((MenuItem)_shortcuts.ContextMenu.Items[0], free && index > 0),
                ((MenuItem)_shortcuts.ContextMenu.Items[1], free && index >= 0 && index < ids.Length - 1) })
            {
                item.IsEnabled = enabled;
                item.ToolTip = _shortcutOrdering.CanReorder ? null : "검색 중에는 순서를 바꿀 수 없습니다. 검색어를 지운 뒤 이동하세요.";
                ToolTipService.SetShowOnDisabled(item, true);
            }
        };
        // The card buttons are not focusable; Enter opens the selected card.
        _shortcuts.KeyDown += async (_, e) =>
        {
            if (e.Key != System.Windows.Input.Key.Enter || _shortcuts.SelectedItem is not Choice selected) return;
            e.Handled = true;
            CloseShortcutOverlay();
            await Safe(() => OpenShortcutAsync(selected.Id));
        };
        _shortcuts.PreviewMouseRightButtonDown += (_, e) =>
        {
            if (ClickedChoice(e.OriginalSource) is not { } choice) return;
            _contextShortcut = choice.Id; e.Handled = true;
            _shortcuts.ContextMenu.PlacementTarget = _shortcuts; _shortcuts.ContextMenu.Placement = System.Windows.Controls.Primitives.PlacementMode.MousePoint; _shortcuts.ContextMenu.IsOpen = true;
        };
        _shortcuts.ContextMenu.Closed += (_, _) => _contextShortcut = null;
        // 설정 및 관리: the former sidebar expander, now a flyout from the rail.
        var settings = new StackPanel { Name = "WorkspaceSettingsActions" };
        settings.Children.Add(SettingsSection("작업 공간"));
        settings.Children.Add(SettingsAction("공통 개인 스킬", PersonalSkillsAsync));
        settings.Children.Add(SettingsAction("실행 프리셋", () => ManagePresetsAsync()));
        settings.Children.Add(SettingsAction("하위 에이전트 · 모델 연결", ProvidersAsync));
        settings.Children.Add(SettingsAction("SSH 업데이트 · 연결 준비", RemoteAsync));
        settings.Children.Add(SettingsSection("업데이트"));
        settings.Children.Add(SettingsAction("전체 프로필 업데이트", ProfileUpdatesAsync));
        _profileUpdateStatus.Margin = new Thickness(10, 0, 6, 6);
        settings.Children.Add(_profileUpdateStatus);
        _updateButton = SettingsAction("Codex 앱 버전 확인", UpdatesAsync);
        settings.Children.Add(_updateButton);
        _updateDetails.Margin = new Thickness(8, 0, 0, 0);
        _updateDetails.Content = new ScrollViewer { Content = _updateStatus, MaxHeight = 200,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        settings.Children.Add(_updateDetails);
        _desktopCompatibility.Margin = new Thickness(10, 2, 6, 8);
        settings.Children.Add(_desktopCompatibility);
        settings.Children.Add(SettingsSection("관리 서비스"));
        settings.Children.Add(SettingsAction("관리 서비스 다시 연결", ReconnectAsync));
        settings.Children.Add(SettingsAction("Windows 실행 권한…", ExecutionModeAsync));
        settings.Children.Add(SettingsAction("완전 종료 후 관리자 실행…", RestartAdministratorAsync));
        RenderExecutionMode();
        var version = Action($"{WorkspaceBuild.Label} · 버전 복사", CopyVersionAsync,
            WorkspaceBuild.CopyText + "\n\n현재 실행 중인 작업공간앱 버전입니다. 클릭하면 버전 정보가 복사됩니다.");
        version.Name = "WorkspaceVersion";
        version.FontSize = 12;
        version.Background = Brushes.Transparent;
        version.BorderThickness = new Thickness(0);
        version.Padding = new Thickness(10, 6, 10, 6);
        version.Foreground = Muted;
        version.HorizontalContentAlignment = HorizontalAlignment.Left;
        version.HorizontalAlignment = HorizontalAlignment.Stretch;
        version.Margin = new Thickness(0, 2, 0, 0);
        _managerUpdates = new ManagerUpdatePanel(_root, async cancellation =>
        {
            var client = _client;
            return client?.IsConnected == true
                ? await client.RequestAsync("supervisor.status", cancellationToken: cancellation)
                : (JsonElement?)null;
        }, fixture);
        BuildSettingsFlyout(settings, _executionMode, version);
        BuildUpdateBanner();
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
        _notes.CollapseRequested += () => ChooseNotesVisible(false);
        SetNotesVisible(false);
        _setNotesVisible = SetNotesVisible;
        right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        right.RowDefinitions.Add(new RowDefinition());
        right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        // Header: the selected profile's summary (or the catalog identity), the
        // workspace tools, the recent task, and any attention or update notice.
        var header = new StackPanel { Margin = new Thickness(20, 14, 20, 12) };
        // One row: identity | profile icons | tools (while wide). The usage line
        // and the narrow tools row sit below it, outside the grid: width-dependent
        // content spanning its star and auto columns would never settle.
        var headingRow = new Grid { Name = "WorkspaceHeading" };
        headingRow.ColumnDefinitions.Add(new ColumnDefinition());
        headingRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        headingRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var identity = new StackPanel { VerticalAlignment = VerticalAlignment.Top };
        _profileSummary.ContentTemplate = ProfileCards.Summary();
        identity.Children.Add(_identity); identity.Children.Add(_profileSummary);
        headingRow.Children.Add(identity);
        var profileTools = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(8, 4, 0, 0) };
        profileTools.Children.Add(WorkspaceAppearance.Glyph(Action("↻", RefreshAccountsAsync), "RefreshProfiles", WorkspaceAppearance.GlyphRefresh, "계정·사용량·리딤 횟수 새로고침"));
        profileTools.Children.Add(WorkspaceAppearance.Glyph(profileMenu, "ProfileActions", WorkspaceAppearance.GlyphMore, "선택한 프로필 관리"));
        Grid.SetColumn(profileTools, 1); headingRow.Children.Add(profileTools);
        var controls = new WrapPanel { Name = "WorkspaceTools", VerticalAlignment = VerticalAlignment.Top };
        notesToggle = WorkspaceAppearance.Tool(Action("작업 메모", () => { ChooseNotesVisible(_notes.Visibility != Visibility.Visible); return Task.CompletedTask; }, "이 작업의 메모와 체크리스트 열기 / 접기"), "ToggleTaskNotes");
        controls.Children.Add(notesToggle);
        _presetButton = WorkspaceAppearance.Tool(Action("실행 프리셋", ChooseTaskPresetAsync), "ExecutionPreset");
        controls.Children.Add(_presetButton);
        controls.Children.Add(WorkspaceAppearance.Tool(Action("관리창 안에 표시", AttachSelectedAsync), "RestoreWorkspaceView"));
        controls.Children.Add(WorkspaceAppearance.Tool(MenuButton("창 및 연결", ("원래 창으로 보기", DetachAsync), ("연결 확인", VerifyConversationAsync),
            ("입력 상태 확인", CheckInputAsync), ("로그인 상태 새로 확인", RefreshLoginStatusAsync)), "WorkspaceConnections", quiet: true));
        var details = new StackPanel();
        details.Children.Add(_mode); details.Children.Add(_accountState); details.Children.Add(_runtimeVersion); details.Children.Add(_sshHosts);
        var detailsPanel = new Border { Name = "WorkspaceDetailsPanel", Background = WorkspaceAppearance.Surface, CornerRadius = new CornerRadius(8),
            Padding = new Thickness(12, 6, 12, 12), Margin = new Thickness(0, 10, 0, 0), Child = details, Visibility = Visibility.Collapsed };
        Button? detailsToggle = null;
        detailsToggle = WorkspaceAppearance.Tool(Action("연결 상세", () =>
        {
            var open = detailsPanel.Visibility != Visibility.Visible;
            detailsPanel.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
            WorkspaceAppearance.Active(detailsToggle!, open);
            return Task.CompletedTask;
        }, "실행 방식 · 로그인 상태 · 실행 버전 펼치기 / 접기"), "WorkspaceDetails");
        WorkspaceAppearance.Active(detailsToggle, false);
        controls.Children.Add(detailsToggle);
        _loginRepair = WorkspaceAppearance.Tool(Action("이 프로필에 로그인", LoginProfileAsync));
        _loginRepair.Visibility = Visibility.Collapsed;
        controls.Children.Add(_loginRepair);
        foreach (Button button in controls.Children) button.Margin = new Thickness(0, 3, 6, 3);
        Grid.SetColumn(controls, 2); headingRow.Children.Add(controls);
        var narrowTools = new Border { Name = "WorkspaceToolsRow" };
        var arrangingHeading = false;
        void ArrangeHeading()
        {
            // Moving the tools changes the login button's IsVisible, which
            // calls back here; the outer call finishes the move.
            if (arrangingHeading) return;
            arrangingHeading = true;
            try
            {
                // Tools stay beside the profile while its name and chips keep 360
                // DIP; otherwise they move below instead of truncating the name.
                controls.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
                profileTools.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
                bool wide = headingRow.ActualWidth - controls.DesiredSize.Width - profileTools.DesiredSize.Width >= 360;
                if (wide && controls.Parent != headingRow) { narrowTools.Child = null; headingRow.Children.Add(controls); }
                else if (!wide && controls.Parent != narrowTools) { headingRow.Children.Remove(controls); narrowTools.Child = controls; }
                controls.Margin = new Thickness(wide ? 12 : 0, wide ? 0 : 10, 0, 0);
            }
            finally { arrangingHeading = false; }
        }
        headingRow.SizeChanged += (_, _) => ArrangeHeading();
        _loginRepair.IsVisibleChanged += (_, _) => ArrangeHeading();
        ArrangeHeading();
        header.Children.Add(headingRow); header.Children.Add(narrowTools);
        header.Children.Add(_taskIdentity);
        header.Children.Add(_attention); header.Children.Add(_operation); header.Children.Add(_updateBanner);
        header.Children.Add(detailsPanel);
        right.Children.Add(new Border { BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(0, 0, 0, 1), Child = header });
        var client = _clientSurface;
        client.Children.Add(_empty);
        Grid.SetRow(client, 1); right.Children.Add(client);
        // Status bar: log tools, the latest status (one line, full text in its
        // tooltip) and copy feedback. A fixed height never resizes the viewport.
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
        _logFeedback.MaxWidth = 280;
        DockPanel.SetDock(_logFeedback, Dock.Right); logTools.Children.Add(_logFeedback);
        _status.Margin = new Thickness(12, 0, 12, 0);
        logTools.Children.Add(_status);
        DockPanel.SetDock(logTools, Dock.Top); logPanel.Children.Add(logTools); logPanel.Children.Add(_logText);
        var logFrame = new Border { Background = WorkspaceAppearance.Canvas, BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(0, 1, 0, 0), Child = logPanel };
        Grid.SetRow(logFrame, 2); right.Children.Add(logFrame);
        Grid.SetColumn(right, 3); _layout.Children.Add(right);
        Content = ManagerTitleBar.Wrap(this, _layout, Log, "창 닫기 · 작업은 계속 실행");
        if (!fixture) _taskContext = new TaskContextWatcher(_root, Dispatcher, task =>
        {
            _selectedTask = task;
            _taskIdentity.Text = task is null ? "작업 · 아직 열지 않음" : "작업 · " + task.Title;
            _taskIdentity.ToolTip = task is null ? null : task.Title + "\n" + task.Task.Key;
            if (_selectedProfile is { } profile && !_viewingCatalog) _workspaceNotifications?.Remember(profile, task);
            _ = _notes.SelectTaskAsync(task);
            _ = RefreshTaskPresetAsync();
            RefreshCacheLines();
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
                    (source, notice) => Dispatcher.InvokeAsync(() => ShowWorkspaceNotification(source, notice)).Task.Unwrap());
                NativeWindowLease.NotificationPipe = _notificationActivation.PipeName;
                _workspaceActivation = new WorkspaceActivation(_root, ticket => Dispatcher.BeginInvoke(new Action(() => ActivateWorkspaceNotification(ticket))));
            }

        };
        Closed += async (_, _) => { _managerUpdates.Dispose(); _notificationNavigation?.Cancel(); _workspaceActivation?.Dispose(); _responsiveness?.Dispose(); _taskContext?.Dispose(); _notificationActivation?.Dispose(); _workspaceNotifications?.Dispose(); _timer.Stop(); _activityTimer.Stop(); if (_client is not null) await _client.DisposeAsync(); };
    }

    private bool ProfileExists(string id) => _state.Arr("profiles").Any(p => p.S("id") == id);
    private string ProfileAlias(string id) => _state.Arr("profiles").FirstOrDefault(p => p.S("id") == id).S("alias", id[..8]);
    private Task<bool> ShowWorkspaceNotification(NotificationClick source, WorkspaceNotice notice)
    {
        using var timing = _responsiveness?.Stage("notification.show");
        if (_closing || _workspaceNotifications is not { } notifications ||
            DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - source.ClickedAt > 1500) return Task.FromResult(false);
        var host = _attachedWindows.FirstOrDefault(p => p.Value.Pid == source.AppPid && p.Value.Handle == source.Hwnd && p.Value.MatchesLifetime).Key;
        var profile = _state.Arr("profiles").FirstOrDefault(p => _hostDeck.Find(p.S("id")) == host);
        if (host is null || profile.S("id") == "") return Task.FromResult(false);
        // The toast worker must not read _state, which this thread replaces.
        var aliases = new Dictionary<string, string>();
        foreach (var p in _state.Arr("profiles"))
            if (p.S("id") is { Length: > 0 } id) aliases.TryAdd(id, p.S("alias", id.Length >= 8 ? id[..8] : id));
        // Same 1.5 s acceptance window as above, inside Electron's 1.8 s fallback.
        return notifications.ShowAsync(notice, profile.S("id"), aliases.ContainsKey,
            id => aliases.TryGetValue(id, out var alias) ? alias : id[..8], DateTimeOffset.FromUnixTimeMilliseconds(source.ClickedAt + 1500));
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
    // A toast waits on TryAttach's own budget (_attachDeadline, extended while
    // the launch is in progress); this much later it stops a wait TryAttach
    // never reported, such as a host that stays in transition.
    private static readonly TimeSpan NotificationAttachSlack = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan NotificationWindowGrace = TimeSpan.FromSeconds(30);
    internal static bool NotificationAttachOverdue(DateTime now, DateTime attachDeadline, bool launching)
        => AttachOverdue(now, attachDeadline + NotificationAttachSlack, launching);
    private async Task OpenNotificationAsync(string ticket)
    {
        var target = _workspaceNotifications?.ReadTicket(ticket, ProfileExists)
            ?? throw new InvalidOperationException("이 알림의 작업 정보를 찾을 수 없습니다. 작업 목록에서 열어 주세요.");
        if (!ProfileExists(target.ProfileId)) throw new InvalidOperationException("이 알림에 연결된 프로필이 제거되었습니다.");
        _notificationNavigation?.Cancel();
        // Cancelled by a newer toast or closing; the wait's budget is checked below.
        using var cancel = new CancellationTokenSource();
        _notificationNavigation = cancel;
        try
        {
        await ShowProfileAsync(target.ProfileId);
        int navigation = _navigation;
        // TryAttach owns the wait, as in the normal open: the show (which a
        // launch can block) does not count, the budget extends while the
        // service reports this launch in progress, and it reports a missing
        // window itself. That report (_embedRequested false) ends this wait and
        // keeps its status. Once the window exists, attach passes can still
        // retry (layout, parent change, transition), so a slow launch gets
        // NotificationWindowGrace from its window instead of the deadline.
        DateTime? windowSeen = null;
        while (!_host.HasLiveAttachment)
        {
            cancel.Token.ThrowIfCancellationRequested();
            if (_closing || _navigation != navigation || _selectedProfile != target.ProfileId) return;
            var profile = Profile();
            TryAttach(profile);
            if (_host.HasLiveAttachment) break;
            if (!_embedRequested) return;
            var latest = Latest(profile);
            var now = DateTime.UtcNow;
            if (latest.N("window_handle") != 0) windowSeen ??= now;
            if (windowSeen is { } seen
                    ? now > seen + NotificationWindowGrace
                    : NotificationAttachOverdue(now, _attachDeadline, LaunchInProgress(_state, latest)))
                throw new TimeoutException("알림의 Codex 창을 연결하지 못했습니다. 프로필을 다시 선택한 뒤 작업 목록에서 열어 주세요.");
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
    internal void UseFixture(JsonElement state) { _state = state; _shownProfiles.Clear(); _selectedProfile = state.Arr("profiles").FirstOrDefault().S("id"); Render(); SetStatus("화면 배치 자체 시험 · 실제 계정과 연결하지 않음"); }
    // Self-tests only: state refreshes also go through the request fixture.
    internal bool FixtureRefreshesState { get; init; }
    internal TaskNotesPanel FixtureNotes(ManagerClient client) { _client = client; return _notes; }

    private static TextBlock Label(string text) => new() { Text = text, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 0) };
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
        int navigation = _navigation;
        var session = WorkspaceSession.Read(_root);
        Log($"관리 앱 시작 · {WorkspaceBuild.Label} · {WorkspaceBuild.Description} · 빌드 {WorkspaceBuild.BuildId} · IPC {ManagerProtocol.Version} · 로그: {_diagnostics.Path}");
        if (_responsiveness is not null) Log("응답 지연 상세 로그 · " + _responsiveness.Path);
        SetStatus("관리 서비스를 연결하고 있습니다…");
        _client = await ManagerClient.ConnectAsync(_root);
        if (_closing) { await _client.DisposeAsync(); return; }
        RenderExecutionMode();
        Log(_executionMode.Text);
        // The profile shown first starts first; the service holds the other
        // background launches briefly behind it (profile_warmup leader gate).
        // The toast that started the app opens its own profile instead of the
        // restored one. Read before any state, so the ticket's saved profile
        // leads: a recent/activity owner (ReadTicket) may be a removed profile
        // that OpenNotificationAsync skips; the service ignores unknown ones.
        await CheckStartupUpdatesAsync(StartupLeader(session, _pendingNotification, _workspaceNotifications));
        await RefreshAsync();
        if (_closing) return;
        _timer.Start();
        // The first toast otherwise froze this thread for 3-10 s (toolkit
        // registration); do that once on the toast worker after first render.
        _workspaceNotifications?.Warm();
        if (_client.ServiceUpdateDeferred) Log("새 관리창을 기존 서비스에 연결했습니다. 작업을 유지하며 서비스 업데이트는 완전 종료 후 적용합니다.");
        _ = _managerUpdates.RefreshAsync();
        SetStatus(_administratorLaunchPending
            ? "일반 권한으로 진행 중인 작업에 다시 연결했습니다. 작업을 마친 뒤 설정 및 관리의 ‘완전 종료 후 관리자 실행…’을 누르세요."
            : "창을 닫아도 작업은 계속됩니다. 모두 끝내려면 완전 종료를 누르세요.");
        if (_navigation == navigation && _pendingNotification is null && session is not null)
        {
            if (session.ViewingCatalog) await ShowCatalogAsync();
            else if (session.ProfileId is { } id && ProfileExists(id)) await ShowProfileAsync(id);
        }
    }
    private async Task ReconnectAsync()
    {
        _timer.Stop();
        if (_client is not null) await _client.DisposeAsync();
        _client = null;
        RenderExecutionMode();
        _client = await ManagerClient.ConnectAsync(_root);
        RenderExecutionMode();
        await Request("supervisor.reconnect");
        await CheckStartupUpdatesAsync();
        await RefreshAsync(); _timer.Start(); SetStatus("관리 서비스에 다시 연결했습니다. 이전 변경 요청은 다시 실행하지 않았습니다.");
        _ = _managerUpdates.RefreshAsync();
    }
    private void RenderExecutionMode()
    {
        var current = WindowsExecutionIdentity.Current();
        var shell = !current.Known ? "확인 불가" : current.Elevated == true ? "관리자" : "일반";
        var service = _client?.IsConnected == true && _client.ServiceElevated is bool elevated
            ? elevated ? "관리자" : "일반" : "연결 안 됨";
        _executionMode.Text = $"Windows 권한 · 관리창 {shell} · 서비스 {service}";
        if (_administratorLaunchPending) _executionMode.Text += "\n관리자 전환 대기 · 작업 종료 후 적용";
        if (!_fixture)
        {
            try
            {
                bool requested = WorkspaceExecutionMode.ReadAdministrator(_root);
                if (!_administratorLaunchPending && current.Known && requested != current.Elevated)
                    _executionMode.Text += requested ? "\n다음 실행 · 관리자 요청" : "\n다음 실행 · 일반 (일반 권한에서 실행)";
            }
            catch (InvalidOperationException) { _executionMode.Text += "\n다음 실행 설정 확인 필요"; }
        }
    }
    private Task ExecutionModeAsync()
    {
        RenderExecutionMode();
        var requested = Dialogs.ExecutionMode(this, WorkspaceExecutionMode.ReadAdministrator(_root), _executionMode.Text);
        if (requested is not bool administrator) return Task.CompletedTask;
        WorkspaceExecutionMode.SaveAdministrator(_root, administrator);
        _administratorLaunchPending = administrator && !WindowsExecutionIdentity.IsElevated;
        RenderExecutionMode();
        SetStatus("실행 권한 설정을 저장했습니다. 작업을 마친 뒤 완전 종료하고 다시 실행하면 적용됩니다.");
        return Task.CompletedTask;
    }
    // The startup warmup's leader: a pending toast's saved profile (none when
    // its ticket cannot be read), otherwise the restored profile unless the
    // session ended on the task catalog.
    internal static string? StartupLeader(WorkspaceSession? session, string? pendingTicket,
        WorkspaceNotifications? notifications)
        => pendingTicket is not null ? notifications?.ReadStoredTicket(pendingTicket)?.ProfileId
            : session is { ViewingCatalog: false } ? session.ProfileId : null;
    private async Task CheckStartupUpdatesAsync(string? selectedProfile = null)
    {
        try { await Request("manager.startup", selectedProfile is null ? null : new { selected_profile_id = selectedProfile }); }
        catch (Exception ex) { Log("시작 시 업데이트 확인 실패 · " + ex.Message); }
    }
    private async Task Safe(Func<Task> action)
    {
        try { await action(); }
        catch (Exception ex)
        {
            if (_closing) return;
            SetStatus(ex.Message, true);
            _profileOpenNoticeProfile = (ex as ProfileOpenException)?.ProfileId;
        }
    }
    private async Task<JsonElement> Request(string command, object? args = null)
    {
        if (_serviceShutdown.DrainStarted)
            throw new InvalidOperationException("관리 서비스가 종료 중입니다. 완전 종료를 다시 눌러 마무리해 주세요.");
        if (_client is null && _fixtureRequest is null) throw new InvalidOperationException("관리 서비스 연결을 기다려 주세요.");
        var tracked = command is not ("state" or "conversation.navigate" or "remote.updates.status");
        // Shortcut progress stays in the status line. Expanding this header
        // would resize the native viewport twice for every task navigation.
        var showActivity = tracked && command is not ("conversation.open" or "profile.email");
        using var timing = _responsiveness?.Time("rpc." + command, tracked ? 0 : 500);
        var actionId = Guid.NewGuid();
        if (showActivity) { _pendingActions[actionId] = (CommandLabel(command), DateTime.UtcNow); RenderActivity(); _activityTimer.Start(); }
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
            try { result = _fixtureRequest is not null ? await _fixtureRequest(command, args) : await _client!.RequestAsync(command, args, deadline.Token); }
            catch (OperationCanceledException) when (deadline.IsCancellationRequested)
            { throw new InvalidOperationException("요청 응답을 제한 시간 안에 받지 못했습니다. 이미 시작한 처리는 계속될 수 있습니다. 상태 갱신은 계속됩니다."); }
            if (tracked) Log($"요청 응답 · {CommandLabel(command)} · {result.S("state", "수신 완료")}");
            return result;
        }
        catch (Exception ex) { if (tracked) Log($"요청 실패 · {command} · {ex.Message}"); throw; }
        finally { if (showActivity) { _pendingActions.Remove(actionId); RenderActivity(); } }
    }
    private static string CommandLabel(string command) => command switch
    {
        "manager.startup" => "시작 시 업데이트 자동 확인",
        "manager.recover_legacy" => "확인한 구버전 프로필 일괄 정리",
        "profile.show" => "Codex 프로필 열기", "profile.prepare" => "프로필 준비", "accounts.refresh" => "계정·사용량 확인",
        "profile.login" => "프로필 로그인 화면 열기", "profile.login_status" => "로그인 계정 상태 확인",
        "profile.email" => "계정 이메일 확인",
        "profile.restart" => "설정 적용 · 정상 종료 후 다시 열기",
        "profile.remote_restart" => "프로필 SSH 설정 적용", "profile.remote_stop" => "프로필 SSH 종료",
        "profile.recover" => "선택한 관리용 Codex 종료",
        "catalog.list" => "대화 목록 읽기", "catalog.show" => "원본 Codex 전체 기록 열기", "conversation.open" => "지정 계정에서 대화 열기", "policy.set" => "모델 조합 적용",
        "providers.verify" => "모델 API 연결 시험", "providers.key" => "API 키 저장", "providers.save" => "모델 연결 등록",
        "updates.check" => "공식 업데이트 확인", "updates.prepare" => "공식 패키지 다운로드·확인", "updates.apply" => "업데이트 준비·실행",
        "remote.inspect" => "SSH 상태 확인", "remote.prepare" => "SSH 런타임·연결 준비",
        "remote.updates.status" => "SSH 업데이트 상태", "remote.updates.check" => "SSH 버전 확인",
        "remote.updates.settings" => "SSH 업데이트 설정", "remote.updates.schedule" => "SSH 업데이트 예약",
        "remote.updates.cancel" => "SSH 업데이트 예약 취소", "remote.updates.stock_update" => "기본 Codex 수동 업데이트", _ => "관리 요청 처리"
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
        if ((_client is null && !FixtureRefreshesState) || _refreshing || _closing || _profileOrdering.IsInteracting || _shortcutOrdering.IsInteracting) return;
        _refreshing = true;
        var navigation = _navigation;
        var revision = _stateRevision;
        try
        {
            var state = await Request("state");
            if (_closing || navigation != _navigation || revision != _stateRevision || _profileOrdering.IsInteracting || _shortcutOrdering.IsInteracting) return;
            // Every show bumps _stateRevision, so this state was requested after
            // each launch still kept in _shownProfiles.
            _state = state; _shownProfiles.Clear();
            Render();
            var current = _viewingCatalog ? _viewerProfile : Profile();
            if (_host.IsAttached && _expectedWindowLaunch?.Matches(current) == true &&
                _host.AttachedHandle == (nint)current.N("window_handle")) _expectedWindowLaunch = null;
        }
        catch (Exception ex) { SetStatus("상태 갱신: " + ex.Message, true); }
        finally { _refreshing = false; }
    }
    // A notes toggle belongs to the profile selected now (the sidebar already
    // shows it), even before that selection's Render.
    private void ChooseNotesVisible(bool visible)
    {
        _notesKey = NotesPanelVisibility.Key(_selectedProfile, _viewingCatalog);
        _notesVisibility.Set(_notesKey, visible);
        _setNotesVisible?.Invoke(visible);
    }
    // Restores the selected profile's own notes state once per selection change;
    // periodic state renders of the same profile never undo the user's choice.
    private void ApplyProfileNotes()
    {
        var key = NotesPanelVisibility.Key(_selectedProfile, _viewingCatalog);
        if (key == _notesKey) return;
        _notesKey = key;
        // Re-showing an open panel would reset a dragged width to the last
        // width captured on close; only a real open/closed change applies.
        var open = _notesVisibility.IsOpen(key);
        if (open != (_notes.Visibility == Visibility.Visible)) _setNotesVisible?.Invoke(open);
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
            // Profiles share the managed desktop. Keep its version fallback in
            // settings once; account and runtime failures still use attention.
            var desktopNotices = _state.Arr("profiles").Concat(_state.Arr("view_instances"))
                .Select(profile => profile.S("desktop_compatibility_notice").Trim())
                .Where(notice => notice.Length > 0).Distinct().ToArray();
            _desktopCompatibility.Text = desktopNotices.Length == 0 ? ""
                : "관리용 Codex 버전 안내\n" + string.Join("\n", desktopNotices);
            _desktopCompatibility.ToolTip = _desktopCompatibility.Text;
            _desktopCompatibility.Visibility = desktopNotices.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
            var warmup = _state.Get("profile_warmup");
            _profileCount.Text = _state.Arr("profiles").Count().ToString();
            using (_responsiveness?.Stage("shell.profile_list", 25))
            if (!_profileOrdering.IsInteracting) Fill(_profiles, _state.Arr("profiles").Select(p =>
            {
                var update = startup.Arr("profiles").FirstOrDefault(item => item.S("profile_id") == p.S("id"));
                // The card shows one short word: update attention on the warning
                // chip, progress (updating, opening in the background) on the
                // status pill. The full sentence stays in the tooltip and Label.
                var updateState = update.ValueKind == JsonValueKind.Object ? update.S("state") : "";
                var suffix = updateState is not ("" or "current" or "latest_on_open" or "complete")
                    ? "\n" + UpdatePresentation.ProfileState(update) : "";
                var (noticeTone, noticeShort) = suffix.Length == 0 ? ("", "")
                    : updateState is "attention" or "superseded" ? ("warning", "업데이트 필요")
                    : ("transient", updateState switch { "waiting" => "적용 대기", "login_pending" => "로그인 대기", "unknown" => "버전 확인 중", _ => "업데이트 중" });
                // The status pill already reads 로그인 필요.
                if (ProfileLoginPresentation.NeedsLogin(p)) { suffix = "\n로그인 확인 필요"; (noticeTone, noticeShort) = ("", ""); }
                var prepared = warmup.Arr("profiles").FirstOrDefault(item => item.S("profile_id") == p.S("id"));
                if (p.S("status") != "running" && !ProfileLoginPresentation.NeedsLogin(p))
                {
                    if (prepared.S("state") is "checking" or "opening") { suffix = "\n백그라운드에서 여는 중"; (noticeTone, noticeShort) = ("transient", "여는 중"); }
                    else if (prepared.S("state") == "queued") { suffix = "\n미리 열기 대기"; (noticeTone, noticeShort) = ("transient", "열기 대기"); }
                }
                var label = ClaudeProfilePresentation.IsClaude(p) ? "[Claude] " + p.S("alias")
                    : LocalModelPresentation.IsLocalProfile(p) ? "[로컬] " + p.S("alias")
                    : p.S("auth_mode") == "external" ? "[API] " + p.S("alias") : p.S("alias", "이름 없는 프로필");
                var usage = ClaudeProfilePresentation.IsClaude(p) ? ClaudeProfilePresentation.Detail(p)
                    : p.S("auth_mode") == "external" ? p.S("external_model_name", "외부 API") : Usage(p.Get("usage"));
                return ProfileCacheLine.Apply(new Choice(p.S("id"), $"{label}\n{usage} · {Status(p.S("status"))}" + suffix, p)
                    { ProfileNotice = suffix, ProfileNoticeTone = noticeTone, ProfileNoticeShort = noticeShort, ProfileEmail = ProfileEmailText(p) }, _state, _selectedTask);
            }), _selectedProfile);
            using (_responsiveness?.Stage("shell.shortcut_list", 25))
            if (!_shortcutOrdering.IsInteracting) RenderShortcuts();
            var p = Profile();
            if (_viewingCatalog)
            {
                var liveViewer = _state.Arr("view_instances").FirstOrDefault(v => v.S("id") == _viewerProfile.S("id"));
                if (liveViewer.ValueKind == JsonValueKind.Object) _viewerProfile = liveViewer;
                p = _viewerProfile;
            }
            ApplyProfileNotes();
            _identity.Text = _selectedProfile is null ? "사용할 프로필을 선택하세요" : (ClaudeProfilePresentation.IsClaude(p) ? "Claude 프로필 " : LocalModelPresentation.IsLocalProfile(p) ? "로컬 모델 프로필 " : p.S("auth_mode") == "external" ? "외부 API 프로필 " : "Codex 프로필 ") + p.S("alias", "연결된 프로필 없음");
            _identity.ToolTip = _identity.Text;
            UpdateProfileSummary();
            var openedTask = p.Get("runtime_state").Get("opened_task");
            _taskIdentity.Text = openedTask.S("thread_id") == "" ? "작업 · 아직 열지 않음" : "최근 연 작업 · " + openedTask.S("title", openedTask.S("thread_id"));
            if (openedTask.S("thread_id") != "" && openedTask.S("title") == "") _taskIdentity.Text = "최근 연 작업 · " + openedTask.S("thread_id");
            _taskIdentity.ToolTip = "이 프로필에서 마지막으로 열기 완료한 작업입니다.\n" + openedTask.S("title") + "\n" + openedTask.S("thread_id");
            using (_responsiveness?.Stage("shell.task_context", 25)) _taskContext?.Select(p);
            if (_selectedTask is { } selectedTask) { _taskIdentity.Text = "작업 · " + selectedTask.Title; _taskIdentity.ToolTip = selectedTask.Title + "\n" + selectedTask.Task.Key; }
            ShowCacheNotice();
            var policy = p.Get("policy");
            var mode = ClaudeProfilePresentation.IsClaude(p) ? "Claude " + ClaudeProfilePresentation.Model(p)
                : p.S("auth_mode") == "external" ? p.S("external_model_name", "외부 API 모델") : "GPT 사용";
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
            var sshHosts = p.Arr("remote_bindings").Where(binding => binding.B("prepared")).Select(binding => binding.S("alias"))
                .Where(alias => alias.Length > 0).Distinct().ToArray();
            _sshHosts.Text = sshHosts.Length == 0 || _selectedProfile is null ? "" : "SSH 연결 · " + string.Join(", ", sshHosts);
            _sshHosts.Visibility = _sshHosts.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
            var warnings = new List<string>();
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
            _updateDetails.Header = update.S("status") == "blocked" ? "업데이트 보류 · 상세 이유" : "Codex 업데이트 상태";
            _updateDetails.Visibility = update.B("worker_active") || update.S("status") is "complete" or "recovery_required" or "failed_restore" or "failed_install" or "blocked" or "installed_newer" or "up_to_date"
                ? Visibility.Visible : Visibility.Collapsed;
            UpdateAttention();
            var updateNotice = update.S("status") + ":" + _updateStatus.Text;
            if (update.S("status") != "" && _lastUpdateNotice != updateNotice)
            {
                _lastUpdateNotice = updateNotice;
                Log("전체 Codex 앱 업데이트 · " + _updateStatus.Text.Replace('\n', ' '));
            }
            using (_responsiveness?.Stage("shell.diagnostics", 25))
            foreach (var observed in _state.Arr("profiles").Concat(_state.Arr("view_instances")))
            {
                var loginProblem = ClaudeProfilePresentation.IsClaude(observed) ? default : observed.Get("login_health");
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
                // Layout-retry renders can run before the post-show refresh lands.
                var selected = _viewingCatalog ? p : Latest(_selectedProfile, p);
                if (selected.Get("restart").S("phase") is "acquiring" or "closing" or "opening" or "releasing" or "waiting" or "recovering" or "connecting")
                    _attachDeadline = DateTime.UtcNow.AddSeconds(25);
                TryAttach(selected, refreshPresentation: false);
            }
            using (_responsiveness?.Stage("shell.background_windows", 25)) ReconcileBackgroundWindows();
        }
        finally { _rendering = false; }
    }
    private string? SelectedShortcutId(JsonElement[] shortcuts)
    {
        if (_viewingCatalog || _selectedProfile is null) return null;
        var current = Profile().Get("current_task").S("thread_id");
        if (_shortcutSelection is { } chosen)
        {
            var link = shortcuts.FirstOrDefault(s => s.S("id") == chosen.Id);
            if (link.ValueKind == JsonValueKind.Object && chosen.ProfileId == _selectedProfile
                && (current == chosen.BaselineThread || current == link.S("thread_id")))
                return chosen.Id;
            _shortcutSelection = null;
        }
        if (current == "") return null;
        var matches = shortcuts.Where(s => s.S("thread_id") == current).ToArray();
        var own = matches.FirstOrDefault(s => s.S("profile_id") == _selectedProfile);
        var match = own.ValueKind == JsonValueKind.Object ? own : matches.FirstOrDefault();
        return match.S("id") is { Length: > 0 } id ? id : null;
    }
    private static void Fill(ListBox box, IEnumerable<Choice> values, string? selected)
    {
        var items = values.ToArray();
        var old = box.Items.OfType<Choice>().ToArray();
        static object Appearance(Choice x) => (x.Id, x.Label, x.Hint, x.AgentBadge, x.AgentHint, x.Card, x.Shortcut);
        var ids = items.Select(x => x.Id).ToArray();
        if (old.Length == items.Length && ids.Distinct().Count() == ids.Length && new HashSet<string>(ids).SetEquals(old.Select(x => x.Id)))
        {
            // Status/usage refreshes must not reset the virtualized panel's
            // measured card heights and shift the user's scroll position. A
            // reorder moves only the items whose place changed, so the other
            // cards keep their containers and the viewport stays put.
            for (var index = 0; index < items.Length; index++)
            {
                var current = (Choice)box.Items[index]!;
                if (current.Id != items[index].Id)
                {
                    var from = index + 1;
                    while (((Choice)box.Items[from]!).Id != items[index].Id) from++;
                    box.Items.RemoveAt(from);
                    box.Items.Insert(index, items[index]);
                }
                else if (!Equals(Appearance(current), Appearance(items[index]))) box.Items[index] = items[index];
            }
        }
        else
        {
            box.Items.Clear(); foreach (var value in items) box.Items.Add(value);
        }
        box.SelectedItem = box.Items.OfType<Choice>().FirstOrDefault(x => x.Id == selected);
    }
    // The selected task changed between state polls: update only the cards'
    // cache lines from the current state, without a new request or render.
    private void RefreshCacheLines()
    {
        if (_rendering || _profileOrdering.IsInteracting || _profiles.Items.Count == 0) return;
        var items = _profiles.Items.OfType<Choice>().Select(choice => ProfileCacheLine.Apply(choice, _state, _selectedTask)).ToArray();
        _rendering = true;
        try { Fill(_profiles, items, _selectedProfile); }
        finally { _rendering = false; }
        UpdateProfileSummary();
        ShowCacheNotice();
    }
    // The selected task is open on a profile whose cache is cold while another
    // account on the same provider still holds it: one status-line notice.
    private void ShowCacheNotice()
    {
        // A pending profile-open error keeps the status line; the card line stays.
        if (_viewingCatalog || _profileOpenNoticeProfile is not null) return;
        if (ProfileCacheLine.Notice(_state, _selectedTask, _selectedProfile, _cacheNotices, DateTime.UtcNow) is { } notice) SetStatus(notice);
    }
    private JsonElement Profile() => _state.Arr("profiles").FirstOrDefault(p => p.S("id") == _selectedProfile);
    // A returned launch is newer than _state until a state requested after it
    // lands, so launch identity (attach, re-clicks, background hosts) prefers it.
    private JsonElement Latest(JsonElement profile) => Latest(profile.S("id"), profile);
    private JsonElement Latest(string? id, JsonElement fallback) =>
        id is not null && _shownProfiles.TryGetValue(id, out var shown) ? shown : fallback;
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
            age >= 600 ? $" · {age / 60}분 전 값" : ""; // Two 300 s usage refresh periods.
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
        await ShowProfileCoreAsync(id, command, action);
    }
    // Ends the caller's action gate once the returned window is attached; the
    // caller's using still releases it on every earlier exit.
    private async Task ShowProfileCoreAsync(string id, string command, IDisposable action)
    {
        var started = Environment.TickCount64;
        var ticket = ++_navigation;
        _expectedConversation = null;
        if (_selectedProfile != id || _viewingCatalog) ParkCurrent();
        _viewingCatalog = false;
        _selectedProfile = id;
        _hostDeck.Select(id);
        BeginAttach(); _profileRequestTicket = ticket; Render();
        // A re-click before the post-show state lands judges the launch that show
        // returned, not the older state.
        var current = Latest(id, Profile());
        if (command == "profile.show" && ProfileLoginPresentation.ShowRecovery(current))
        {
            _profileRequestTicket = null;
            ShowLoginRecovery(current);
            return;
        }
        if (command == "profile.show" && AutomaticProfileUpdate.IsReplacing(current))
        {
            _profileRequestTicket = null;
            SetStatus("업데이트 적용 후 이 프로필의 새 창을 관리창 안에 표시합니다…");
            return;
        }
        if (command == "profile.show" && _attached is { } cached && cached.MatchesLifetime &&
            _host.IsAttached && _windowLaunches.TryGetValue(_host, out var launch) && launch.Matches(current) &&
            !(current.Get("restart").B("remote_background") && current.Get("restart").S("phase") == "attention" &&
              !current.Get("restart").B("connections_restored")))
        {
            _profileRequestTicket = null;
            TryAttach(current);
            Log($"프로필 전환 · {current.S("alias")} · 기존 창 연결 유지");
            return;
        }
        Log($"프로필 선택 · {current.S("alias")} · {id}");
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
            // The show can outlive BeginAttach's budget (admission waits); the
            // returned launch gets its own.
            _attachDeadline = DateTime.UtcNow.AddSeconds(25);
            // Kept until a later state lands. The bump discards a poll that
            // started before this show; the refresh runs after attaching below.
            _shownProfiles[id] = returnedProfile;
            ++_stateRevision;
            _attachTiming = (ticket, id, started);
        }
        catch (Exception error)
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
            if (command == "profile.show" && ticket == _navigation &&
                error is ManagerException { Code: "update_in_progress" or "profile_prepare_busy" })
                throw new ProfileOpenException(id, error);
            throw;
        }
        finally { if (_profileRequestTicket == ticket) _profileRequestTicket = null; }
        // TryAttach reads only the returned profile, pinned by _expectedWindowLaunch,
        // so attach first. The full state (0.8-2 s, longer when the service is
        // busy) then refreshes the sidebar without holding the window back.
        if (ticket == _navigation && _selectedProfile == id && _embedRequested && !_closing)
        {
            TryAttach(returnedProfile);
            if (_host.HasLiveAttachment && _windowLaunches.TryGetValue(_host, out var attached) && attached.Matches(returnedProfile))
                RecordAttached(id);
        }
        // Re-clicks and same-profile actions read Latest(id), so the gate ends
        // here instead of covering an in-flight poll plus a fresh state (~40 s).
        action.Dispose();
        using (_responsiveness?.Time("profile.switch.refresh." + id))
        {
            // A poll already in flight was discarded above and would turn this
            // refresh into a no-op. Wait only briefly: a slow one is left to the
            // next timer poll (_shownProfiles bridges until then), so callers such
            // as notification navigation are not held for two full state calls.
            var waitUntil = Environment.TickCount64 + 2000;
            while (_refreshing && !_closing && Environment.TickCount64 < waitUntil) await Task.Delay(25);
            if (!_refreshing && _shownProfiles.ContainsKey(id)) await RefreshAsync();
        }
    }
    // profile.switch.<id> also covers the post-show refresh; this is the time
    // from the click to the attached window.
    private void RecordAttached(string id)
    {
        if (_attachTiming is not { } timing || timing.Ticket != _navigation || timing.Id != id) return;
        _attachTiming = null;
        _responsiveness?.Record("operation", new { name = "profile.attached." + id, elapsed_ms = Environment.TickCount64 - timing.Started });
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
        _loginRepair.Content = "이 프로필에 로그인";
        System.Windows.Automation.AutomationProperties.SetName(_loginRepair, "이 프로필에 로그인");
        if (ClaudeProfilePresentation.IsClaude(saved))
        {
            var claude = ClaudeProfilePresentation.Status(saved);
            _loginRepair.Content = "Claude 로그인 · 설정";
            System.Windows.Automation.AutomationProperties.SetName(_loginRepair, "Claude 로그인 · 설정");
            _loginRepair.Visibility = !_viewingCatalog && _selectedProfile is not null ? Visibility.Visible : Visibility.Collapsed;
            _loginRepair.IsEnabled = true;
            _loginRepair.ToolTip = "Claude Code의 공식 로그인 콘솔을 열거나 이 프로필의 기본 모델과 컨텍스트를 설정합니다.";
            _accountState.Visibility = _selectedProfile is null || _viewingCatalog ? Visibility.Collapsed : Visibility.Visible;
            _accountState.Foreground = ClaudeProfilePresentation.Tone(claude) == "warning" ? Brushes.Orange : Muted;
            _accountState.Text = ClaudeProfilePresentation.Detail(saved);
            _accountState.ToolTip = claude.Message(_accountState.Text);
            return;
        }
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
        _loginRepair.IsEnabled = true;
        _loginRepair.ToolTip = "등록된 계정으로 다시 로그인합니다. 기존 실행이 남아 있으면 종료 확인 후 전용 로그인 화면을 엽니다.";
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
        var saved = _state.Arr("profiles").First(p => p.S("id") == id);
        if (ClaudeProfilePresentation.IsClaude(saved)) { await EditClaudeProfileAsync(id); return; }
        if (saved.S("auth_mode") == "external")
        {
            await ShowProvidersAsync(id);
            return;
        }
        _loginStatuses.Remove(id);
        // X may hide Electron while its background process keeps the bound
        // login guard alive. Ask before using the existing profile-scoped exit.
        var status = await Request("profile.login_status", new { profile_id = id });
        if (_closing) return;
        var profile = Latest(id, _state.Arr("profiles").First(p => p.S("id") == id));
        if (ProfileLoginPresentation.RequiresRestartForLogin(profile, status))
        {
            await RecoverProfileAsync(id, forLogin: true);
            return;
        }
        await ShowProfileAsync(id, "profile.login");
    }
    private Task RefreshLoginStatusAsync() => RefreshLoginStatusAsync(RequireProfile());
    private async Task RefreshLoginStatusAsync(string id)
    {
        if (ClaudeProfilePresentation.IsClaude(_state.Arr("profiles").First(p => p.S("id") == id)))
        {
            var claude = await Request("claude.status", new { profile_id = id });
            if (_closing) return;
            await Request("claude.usage", new { profile_id = id });
            if (_closing) return;
            await RefreshAsync();
            SetStatus("Claude · " + ClaudeProfilePresentation.Label(claude) + " · " + claude.Message("상태를 확인했습니다."),
                ClaudeProfilePresentation.Tone(claude) == "warning");
            return;
        }
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
    private Task RestartRemoteProfileAsync() => RestartRemoteProfileAsync(RequireProfile());
    private async Task RestartRemoteProfileAsync(string id)
    {
        using var action = _profileActions.Enter(id, "SSH 설정 적용");
        if (!_state.Get("capabilities").B("remote_profile_lifecycle"))
            throw new InvalidOperationException("실행 중인 관리 서비스가 이전 버전입니다. 작업을 마친 뒤 완전 종료하고 새 버전으로 다시 열어 주세요.");
        var profile = Latest(id, _state.Arr("profiles").First(p => p.S("id") == id));
        if (MessageBox.Show(this, $"{profile.S("alias")} 프로필의 SSH 작업이 끝나면 원격 Codex를 다시 열어 모델·하위 에이전트 설정을 적용합니다.\n\n현재 답변은 끝날 때까지 기다립니다. 연결이 잠시 끊겼다가 다시 연결됩니다. 다른 프로필과 터미널용 Codex는 유지됩니다.",
            "이 프로필의 SSH 설정 적용", MessageBoxButton.OKCancel, MessageBoxImage.Information) != MessageBoxResult.OK) return;
        var result = await Request("profile.remote_restart", new { profile_id = id, generation = profile.S("generation") });
        await RefreshAsync();
        SetStatus(result.Message());
    }

    private async Task StopRemoteProfilesAsync()
    {
        if (_client?.IsConnected != true) throw new InvalidOperationException("원격 종료를 확인할 관리 서비스가 연결되지 않았습니다.");
        if (!_state.Get("capabilities").B("remote_profile_lifecycle"))
        {
            Log("이전 서비스의 로컬 종료만 수행 · SSH 원격 실행 유지");
            return; // The confirmation dialog explicitly describes this legacy scope.
        }
        var jobs = new Dictionary<string, string>();
        var refused = new List<string>();
        var maxHosts = 0;
        foreach (var profile in _state.Arr("profiles"))
        {
            if (profile.S("generation") == "" || !profile.Arr("remote_bindings").Any(b => b.B("prepared"))) continue;
            maxHosts = Math.Max(maxHosts, profile.Arr("remote_bindings").Count(b => b.B("prepared")));
            using var requestDeadline = new CancellationTokenSource(TimeSpan.FromSeconds(20));
            JsonElement job;
            try
            {
                job = await _client.RequestAsync("profile.remote_stop",
                    new { profile_id = profile.S("id"), generation = profile.S("generation") }, requestDeadline.Token);
            }
            catch (Exception error)
            {
                // Older services reject stop while ordinary SSH preparation
                // is pending. Still attempt the other profiles and offer the
                // existing explicit choice to leave unconfirmed remotes alone.
                var detail = error is OperationCanceledException
                    ? "SSH 종료 요청의 결과를 확인하지 못했습니다."
                    : error.Message;
                refused.Add($"{profile.S("alias", profile.S("id"))} · {detail}");
                Log($"프로필 원격 종료 요청 확인 필요 · {profile.S("alias", profile.S("id"))} · {detail}");
                continue;
            }
            jobs[profile.S("id")] = job.S("id");
            // The service turned a pending "SSH 작업 종료 후 설정 적용" into this stop.
            if (job.B("stop_converted"))
                Log($"{profile.S("alias", profile.S("id"))} · " + (job.B("stop_only")
                    ? "진행 중이던 SSH 설정 적용 대신 현재 답변이 끝나면 원격 실행을 종료합니다."
                    : "진행 중인 SSH 설정 적용이 끝나는 대로 원격 실행을 종료합니다."));
        }
        // Each host is checked in turn and an unreachable one costs a full
        // connect timeout (10 s), so the wait grows with the host count.
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(30 + 11 * maxHosts));
        // A refused remote stop (an unaudited runtime, no shutdown proof) does
        // not resolve by waiting. Collect them and let the user leave those
        // listeners on the server, exactly like closing only the window.
        var aliases = _state.Arr("profiles").ToDictionary(p => p.S("id"), p => p.S("alias", p.S("id")));
        var waitStarted = DateTime.UtcNow;
        try
        {
            while (jobs.Count > 0)
            {
                var state = await _client.RequestAsync("state", cancellationToken: deadline.Token);
                foreach (var (id, jobId) in jobs.ToArray())
                {
                    var job = state.Get("profile_restarts").Get(id);
                    if (job.S("id") != jobId) throw new InvalidOperationException("SSH 종료 요청이 변경되어 종료 확인을 중지했습니다.");
                    if (job.S("phase") == "complete") { jobs.Remove(id); Log("프로필 원격 종료 확인 · " + id); }
                    else if (job.S("phase") is "attention" or "superseded")
                    {
                        jobs.Remove(id);
                        var detail = job.Message("SSH 종료 결과를 확인하지 못했습니다.").Replace("로컬 창은 사용할 수 있습니다. ", "");
                        // An unreachable host (powered off, offline) has nothing we
                        // could stop now; its reachable hosts were stopped. No prompt.
                        if (job.S("code") == "ssh_hosts_unreachable")
                        {
                            Log($"프로필 원격 종료 · {aliases.GetValueOrDefault(id, id)} · {detail}");
                            continue;
                        }
                        refused.Add($"{aliases.GetValueOrDefault(id, id)} · {detail}");
                        Log($"프로필 원격 종료 안 됨 · {aliases.GetValueOrDefault(id, id)} · {detail}");
                    }
                }
                if (jobs.Count == 0) break;
                SetStatus($"원격 Codex 종료를 확인하고 있습니다 · {string.Join(", ", jobs.Keys.Select(id => aliases.GetValueOrDefault(id, id)))} · {(int)(DateTime.UtcNow - waitStarted).TotalSeconds}초");
                await Task.Delay(1000, deadline.Token);
            }
        }
        catch (OperationCanceledException) when (deadline.IsCancellationRequested)
        {
            // A remote that neither exits nor reports a running answer does not
            // resolve by waiting longer. Offer the same choice as a refused stop
            // instead of making the user press 완전 종료 again; the stop request
            // stays on the service either way.
            foreach (var id in jobs.Keys)
            {
                refused.Add($"{aliases.GetValueOrDefault(id, id)} · {(int)(DateTime.UtcNow - waitStarted).TotalSeconds}초 동안 종료 확인이 오지 않았습니다.");
                Log($"프로필 원격 종료 확인 시간 초과 · {aliases.GetValueOrDefault(id, id)}");
            }
        }
        if (refused.Count > 0 && MessageBox.Show(this,
                "다음 SSH 원격 실행은 자동으로 종료하지 못했습니다.\n\n" + string.Join("\n", refused) +
                "\n\n원격 실행을 서버에 남겨 두고 완전 종료하려면 확인을 누르세요. 로컬 Codex는 모두 종료됐고, " +
                "다음 실행 때 남은 원격 실행에 다시 연결합니다. 관리창을 유지하려면 취소를 누르세요.",
                "SSH 원격 실행 남겨 두기", MessageBoxButton.OKCancel, MessageBoxImage.Warning, MessageBoxResult.OK) != MessageBoxResult.OK)
            throw new InvalidOperationException("SSH 원격 실행을 종료하지 못해 관리창을 유지합니다.");
        if (refused.Count > 0) Log("SSH 원격 실행을 서버에 남겨 두고 완전 종료");
    }

    private Task RecoverProfileAsync() => RecoverProfileAsync(RequireProfile());
    private Task RecoverProfileAsync(string id) => RecoverProfileAsync(id, forLogin: false);
    private async Task RecoverProfileAsync(string id, bool forLogin)
    {
        using var action = _profileActions.Enter(id, "이 프로필 다시 열기");
        if (_viewingCatalog) throw new InvalidOperationException("왼쪽에서 복구할 계정 프로필을 선택하세요.");
        if (_profileRequestTicket is not null) throw new InvalidOperationException("현재 프로필 열기가 끝난 뒤 복구하세요.");
        // The launch a show just attached, not an older state's generation/PID.
        var profile = Latest(id, _state.Arr("profiles").First(p => p.S("id") == id));
        var prompt = forLogin
            ? $"{profile.S("alias")}의 기존 실행이 백그라운드에 남아 있습니다. 이 프로필을 종료한 뒤 전용 로그인 화면을 엽니다.\n\n이 프로필의 진행 중인 작업과 SSH 연결이 끊길 수 있고, 보내지 않은 입력은 사라질 수 있습니다. 저장된 대화와 다른 프로필은 유지됩니다.\n\n이 프로필의 작업을 마쳤다면 확인을 누르세요."
            : $"{profile.S("alias")} 프로필의 관리용 Codex를 종료하고 새 버전으로 다시 엽니다.\n이 창에서 실행 중인 작업은 중단되며, 보내지 않은 입력은 사라질 수 있습니다.\n\n작업을 정리했다면 확인을 누르세요. 원래 Codex와 다른 프로필은 유지됩니다.";
        if (!ConfirmProfileRestart(prompt, forLogin ? "이 프로필 다시 로그인" : "이 프로필 다시 열기")) return;
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
        await ShowProfileCoreAsync(id, forLogin ? "profile.login" : "profile.show", action);
    }
    internal Func<string, string, bool> ConfirmProfileRestart { get; set; } = null!;
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
    // Eight desktops starting together created their windows 40-48 s after
    // spawn (revision 94); 25 s + 65 s still bounds a launch that never shows.
    private static readonly TimeSpan LaunchProgressGrace = TimeSpan.FromSeconds(65);
    // The service still reports this profile's launch in progress: queued or
    // preparing in the startup warmup (also behind the leader gate), or
    // spawned without its first window yet.
    private static bool LaunchInProgress(JsonElement state, JsonElement profile)
    {
        if (profile.S("status") == "running" && profile.N("process_id") != 0 && profile.N("window_handle") == 0) return true;
        var warmup = state.Get("profile_warmup");
        return warmup.B("worker_active") && warmup.Arr("profiles").Any(entry =>
            entry.S("profile_id") == profile.S("id") && entry.S("state") is "queued" or "checking" or "opening");
    }
    // Shared by TryAttach and a toast's open: past its deadline a missing
    // window is an error, unless the launch is in progress (LaunchProgressGrace more).
    internal static bool AttachOverdue(DateTime now, DateTime deadline, bool launching)
        => now >= deadline + (launching ? LaunchProgressGrace : TimeSpan.Zero);
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
        profile = Latest(profile);
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
                ResolveProfileOpenNotice(profile);
                return;
            }
            _host.Visibility = Visibility.Visible;
            _host.UpdateLayout();
            if (_host.SynchronizeLayout() && _host.IsAttached)
            {
                _empty.Visibility = Visibility.Collapsed;
                ResolveProfileOpenNotice(profile);
            }
            return;
        }
        var hwnd = (nint)profile.N("window_handle"); var pid = (int)profile.N("process_id");
        var path = profile.S("executable_path", profile.S("executable"));
        if (hwnd == 0 || hwnd == _closedWindow || pid == 0 || string.IsNullOrWhiteSpace(path))
        {
            // While the service still reports this launch in progress, the 25 s
            // budget extends by LaunchProgressGrace (90 s in all).
            var starting = LaunchInProgress(_state, profile);
            if (AttachOverdue(DateTime.UtcNow, _attachDeadline, starting))
                AttachFailure(starting ? "90초 안에 Codex 기본 창을 찾지 못했습니다. 프로필을 다시 선택해 주세요."
                    : "25초 안에 Codex 기본 창을 찾지 못했습니다. 프로필을 다시 선택해 주세요.");
            else
            {
                _empty.Text = starting
                    ? "Codex 기본 창을 찾고 있습니다…\n프로필을 여는 중입니다. 여러 프로필을 함께 시작하면 1분 넘게 걸릴 수 있으며, 창이 준비되면 바로 연결합니다."
                    : "Codex 기본 창을 찾고 있습니다…\n25초 안에 연결되지 않으면 오류와 확인 방법을 표시합니다.";
                _empty.Visibility = Visibility.Visible;
            }
            return;
        }
        _hostDeck.Select(profile.S("id"));
        if (_host.AttachedHandle == hwnd)
        {
            _host.Visibility = Visibility.Visible;
            _host.UpdateLayout();
            if (_host.SynchronizeLayout() && _host.IsAttached)
            {
                _empty.Visibility = Visibility.Collapsed;
                if (_windowLaunches.TryGetValue(_host, out var attached) && attached.Matches(profile))
                    ResolveProfileOpenNotice(profile);
            }
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
        foreach (var listed in _state.Arr("profiles").Concat(_state.Arr("view_instances")))
        {
            // A profile switched away from before its post-show refresh landed:
            // the older launch must not replace the window attached for the new one.
            var profile = Latest(listed);
            var id = profile.S("id");
            if (id == "" || profile.S("status") != "running" || profile.N("window_handle") == 0
                || (!_viewingCatalog && id == _selectedProfile) || (_viewingCatalog && id == _viewerProfile.S("id"))
                || AutomaticProfileUpdate.IsClosing(profile)) continue;
            if (_detachedProfiles.TryGetValue(id, out var detached) && detached.Matches(profile)) continue;
            var host = _hostDeck.Ensure(id);
            if (host.IsTransitioning) continue;
            host.Visibility = Visibility.Hidden;
            if (_windowLaunches.TryGetValue(host, out var retained) && retained.Retains(host, profile))
            {
                ResolveProfileOpenNotice(profile);
                continue;
            }
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
                RecordAttached(id);
            }
            else
            {
                Log($"{profile.S("alias")} · 관리창 내부 연결 완료 · 프로필 선택 시 표시");
                ResolveProfileOpenNotice(profile);
            }
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
        var profile = _viewingCatalog ? _viewerProfile : Latest(_selectedProfile, Profile());
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
    private bool _shutdownInProgress, _shutdownComplete, _exitAllRequested;
    private readonly WorkspaceShutdown _serviceShutdown = new();
    private Task ExitWorkspaceAsync() => RequestWorkspaceExitAsync(false);
    private Task RestartAdministratorAsync()
    {
        if (WindowsExecutionIdentity.IsElevated)
        { SetStatus("현재 관리창은 이미 관리자 권한으로 실행 중입니다."); return Task.CompletedTask; }
        if (_client?.IsConnected != true)
        { SetStatus("기존 관리 서비스에 연결한 뒤 다시 시도해 주세요. 진행 중인 작업은 유지했습니다.", true); return Task.CompletedTask; }
        return RequestWorkspaceExitAsync(true);
    }
    private Task RequestWorkspaceExitAsync(bool restartAdministrator)
    {
        if (_shutdownInProgress || (_closing && !_serviceShutdown.DrainStarted && !_serviceShutdown.BackendResetting)) return Task.CompletedTask;
        var message = restartAdministrator
            ? "관리 중인 모든 Codex 프로필과 작업을 종료한 뒤 관리자 권한으로 다시 실행합니다.\n진행 중인 작업은 중단됩니다. 작업을 모두 마쳤을 때만 계속하세요.\n\n종료가 확인되면 Windows 권한 허용 창이 나타납니다."
            : "관리 중인 모든 Codex 프로필과 작업을 종료합니다.\n진행 중인 작업은 중단됩니다.\n\n창만 닫고 작업을 계속하려면 취소한 뒤 제목줄의 X를 누르세요.";
        message += _state.Get("capabilities").B("remote_profile_lifecycle")
            ? "\n\nSSH에서는 이 앱에 등록된 프로필의 현재 답변이 끝나기를 기다린 뒤 종료합니다. 서버와 별도 터미널 Codex는 유지합니다."
            : "\n\n현재 실행 중인 이전 관리 서비스는 로컬 종료만 지원합니다. SSH 원격 실행은 유지됩니다. 새 서비스로 다시 연 뒤 SSH 종료 기능을 사용할 수 있습니다.";
        if (MessageBox.Show(this, message,
            restartAdministrator ? "완전 종료 후 관리자 실행" : "작업 공간 완전 종료", MessageBoxButton.OKCancel, MessageBoxImage.Warning, MessageBoxResult.Cancel) != MessageBoxResult.OK)
            return Task.CompletedTask;
        _restartAsAdministrator = restartAdministrator;
        _exitAllRequested = true;
        Close();
        return Task.CompletedTask;
    }
    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_shutdownComplete) return;
        e.Cancel = true;
        if (_shutdownInProgress) return;
        Log("관리창 닫기 요청 수신");
        if (!_exitAllRequested && _client is { PreservesBackgroundProfiles: false })
        {
            SetStatus("현재 연결된 구버전 서비스는 창 종료 후 작업 유지를 지원하지 않습니다. 최초 한 번은 작업을 마친 뒤 완전 종료하여 새 서비스를 적용해 주세요.", true);
            return;
        }
        try { _notes.PreserveDrafts(); }
        catch (Exception error) { e.Cancel = true; SetStatus("메모 초안을 저장하지 못했습니다: " + error.Message, true); return; }
        if (!_fixture)
        {
            try { new WorkspaceSession(1, _selectedProfile, _viewingCatalog).Save(_root); }
            catch (Exception error) { SetStatus("다시 열 작업 정보를 저장하지 못했습니다: " + error.Message, true); return; }
        }
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
        if (!_exitAllRequested)
        {
            // Destroy this UI process, not the work. The independent Rust
            // service and original native Codex instances retain their state.
            // Releasing our HWND properties does not show or close any window.
            foreach (var window in _parked.Values) window.ClearMarker();
            _parked.Clear();
            _closing = true; _shutdownComplete = true;
            _timer.Stop(); _activityTimer.Stop();
            Log("관리창만 종료 · Codex 작업과 SSH 연결 유지 · 다시 열면 실행 중인 작업에 연결합니다.");
            e.Cancel = false;
            return;
        }
        if (_parked.Count == 0 && _client is null)
        {
            if (_restartAsAdministrator)
            { SetStatus("관리 서비스 종료를 확인하지 못해 관리자 실행을 보류했습니다.", true); return; }
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
            if (_serviceShutdown.BackendResetting)
            {
                using var settle = new CancellationTokenSource(TimeSpan.FromSeconds(60));
                await _serviceShutdown.SettleLegacyRestartsAsync(
                    (command, token) => _client!.RequestAsync(command, cancellationToken: token),
                    message => SetStatus(message), settle.Token);
            }
            if (!_serviceShutdown.DrainStarted)
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
                        // No show lands after _closing, so this snapshot is newer than any kept launch.
                        _state = await _client.RequestAsync("state", cancellationToken: stopWarmup.Token); _shownProfiles.Clear();
                        if (!_state.Get("profile_warmup").B("worker_active") &&
                            _state.Get("local_launches").N("active") == 0 &&
                            (!_state.Get("capabilities").B("shutdown_restart_barrier") ||
                             (_state.Get("local_restarts").B("paused") &&
                              _state.Get("local_restarts").N("pending") == 0 &&
                              _state.Get("local_restarts").N("active") == 0))) break;
                        await Task.Delay(100, stopWarmup.Token);
                    } while (true);
                    // State reads profile identities before the launch counter. A
                    // launch may finish between those reads; take a post-drain
                    // snapshot while the admission barrier is still armed.
                    _state = await _client.RequestAsync("state", cancellationToken: stopWarmup.Token);
                    // Preloaded windows have never been attached or parked. Include
                    // their verified process/window identities in normal shutdown.
                    var hidden = _state.Arr("profiles").Concat(_state.Arr("view_instances")).Where(p => p.S("status") == "running" &&
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
                    throw new InvalidOperationException("일부 Codex가 1분 안에 종료를 마치지 못했습니다. 관리창을 유지합니다. 잠시 뒤 완전 종료를 다시 눌러 주세요.");
                if (_client?.IsConnected == true)
                {
                    // A mode switch can hand the UI to a process that is no longer a
                    // child of the tracked window, so the graceful close above can
                    // leave ChatGPT processes behind. The service reaps every managed
                    // process that carries this profile's own --user-data-dir.
                    // Every profile that ever launched is swept, not only observed
                    // ones: a launch that failed before its identity was saved leaves
                    // a desktop that no recorded process id points to.
                    var cleanupErrors = new List<string>();
                    var cleanupWarnings = new List<string>();
                    foreach (var profile in _state.Arr("profiles").Concat(_state.Arr("view_instances")))
                    {
                        var id = profile.S("id");
                        if (id == "" || profile.S("generation") == "") continue;
                        var observed = profile.S("process_id") != "";
                        try
                        {
                            using var stopDeadline = new CancellationTokenSource(TimeSpan.FromSeconds(6));
                            var cleanup = await _client.RequestAsync("profile.cleanup",
                                new { profile_id = id, generation = profile.S("generation") },
                                cancellationToken: stopDeadline.Token);
                            if (observed || cleanup.S("state") != "already_stopped")
                                Log($"남은 Codex 프로세스 정리 · {profile.S("alias", id)}");
                        }
                        catch (Exception error)
                        {
                            var reason = error is OperationCanceledException ? "정리 확인 시간 초과" : error.Message;
                            Log($"프로세스 종료 확인 실패 · {profile.S("alias", id)} · {reason}");
                            // An observed process blocks the exit. Without one, only an
                            // older service's refusal of a profile with no recorded
                            // process is expected; any other failure (a sweep timeout,
                            // an unverifiable process) may leave an orphan running.
                            if (observed) cleanupErrors.Add(profile.S("alias", id));
                            else if (!reason.Contains("실행 프로필이 없습니다", StringComparison.Ordinal))
                                cleanupWarnings.Add($"{profile.S("alias", id)} · {reason}");
                        }
                    }
                    if (cleanupErrors.Count > 0)
                        throw new InvalidOperationException("일부 프로필의 종료를 확인하지 못해 관리창을 유지합니다: " + string.Join(", ", cleanupErrors));
                    if (cleanupWarnings.Count > 0 && MessageBox.Show(this,
                            "다음 프로필에 남은 Codex 프로세스가 없는지 확인하지 못했습니다. 그대로 종료하면 백그라운드 Codex가 남을 수 있습니다.\n\n" +
                            string.Join("\n", cleanupWarnings) +
                            "\n\n그래도 완전 종료하려면 확인을, 관리창을 유지하고 다시 시도하려면 취소를 누르세요.",
                            "남은 Codex 정리 확인", MessageBoxButton.OKCancel, MessageBoxImage.Warning, MessageBoxResult.Cancel) != MessageBoxResult.OK)
                        throw new InvalidOperationException("남은 Codex 프로세스 정리를 확인하지 못해 관리창을 유지합니다: " +
                            string.Join(", ", cleanupWarnings));
                    using var settle = new CancellationTokenSource(TimeSpan.FromSeconds(60));
                    await _serviceShutdown.SettleLegacyRestartsAsync(
                        (command, token) => _client.RequestAsync(command, cancellationToken: token),
                        message => SetStatus(message), settle.Token);
                    await StopRemoteProfilesAsync();
                }
            }
            if (_client?.IsConnected == true)
            {
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(60));
                await _serviceShutdown.FinishAsync(
                    (command, token) => _client.RequestAsync(command, cancellationToken: token),
                    message => SetStatus(message), deadline.Token);
            }
            if (_serviceShutdown.DrainStarted)
            {
                using var exitDeadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                try
                {
                    using var service = System.Diagnostics.Process.GetProcessById(_serviceShutdown.ServicePid);
                    await service.WaitForExitAsync(exitDeadline.Token);
                }
                catch (ArgumentException) { /* The verified service already exited. */ }
                catch (OperationCanceledException)
                { throw new InvalidOperationException("관리 서비스의 종료를 기다리고 있습니다. 잠시 뒤 완전 종료를 다시 눌러 주세요."); }
                Log("관리 서비스와 남은 관리 작업 종료 확인");
            }
            if (_restartAsAdministrator)
            {
                if (!_serviceShutdown.DrainStarted)
                    throw new InvalidOperationException("관리 서비스 종료를 확인하지 못해 관리자 실행을 보류했습니다.");
                ((App)Application.Current).RestartAdministratorAfterExit(_root);
            }
            if (_serviceShutdown.DrainStarted && !_restartAsAdministrator)
                PackageUpdateAfterExit.Queue(_root, Log);
            _shutdownComplete = true;
            Close();
        }
        catch (Exception error)
        {
            if (!_serviceShutdown.DrainStarted && !_serviceShutdown.BackendResetting && _client?.IsConnected == true)
            {
                try
                {
                    using var resume = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                    await _client.RequestAsync("manager.resume_launches", cancellationToken: resume.Token);
                }
                catch (Exception resumeError) { Log("프로필 실행 재개 확인 · " + resumeError.Message); }
            }
            _closing = _serviceShutdown.DrainStarted || _serviceShutdown.BackendResetting;
            _shutdownInProgress = false; _exitAllRequested = _closing;
            IsEnabled = true;
            if (!_closing) { _timer.Start(); _activityTimer.Start(); }
            SetStatus(error.Message, true);
        }
    }
    private void SetStatus(string message, bool error = false)
    {
        _profileOpenNoticeProfile = null;
        _status.Text = message; _status.ToolTip = message; _status.Foreground = error ? new SolidColorBrush(Color.FromRgb(245, 189, 121)) : Muted;
        if (_events.Count == 0 || !_events[^1].EndsWith(message, StringComparison.Ordinal)) Log((error ? "오류 · " : "") + message);
    }
    private void ResolveProfileOpenNotice(JsonElement profile)
    {
        // Called only after verifying the current native window's attachment.
        // A different profile or a later unrelated error must keep its notice.
        var id = profile.S("id");
        if (_profileOpenNoticeProfile != id || profile.S("status") != "running" ||
            profile.Get("restart").S("phase") is not ("" or "complete" or "superseded")) return;
        foreach (var gate in new[] { _state.Get("update_maintenance"),
            _state.Get("profile_maintenance").Get(id), _state.Get("ssh_maintenance").Get(id) })
            if (gate.S("state") is not ("" or "released")) return;
        SetStatus($"{profile.S("alias")} 프로필의 Codex 창이 준비되었습니다.");
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
            [new Choice("codex", "Codex 계정 · ChatGPT 로그인"), new Choice("claude_code", "Claude · 이 PC의 Claude Code CLI로 실행"),
                new Choice("local", "로컬 모델 · 직접 준비한 서버에서 실행"), new Choice("external", "외부 API 모델 · 클라우드 API에서 실행")]);
        if (kind is null) return;
        string? modelId = null;
        Dictionary<string, object>? modelSettings = null;
        if (kind.Id is "external" or "local")
        {
            var registry = await Request("providers.list");
            var local = kind.Id == "local";
            var available = LocalModelPresentation.AvailableModels(registry, local)
                .Select(m => new Choice(m.S("id"), m.S("display_name", m.S("name")) + " · " + m.S("reasoning_effort"), m)).ToArray();
            if (available.Length == 0) { SetStatus(local ? "하위 에이전트 · 모델 연결에서 로컬 모델을 등록하고 연결 시험을 완료하세요." : "하위 에이전트 · 모델 연결에서 클라우드 모델 등록, 키 저장, 연결 시험을 먼저 완료하세요.", true); return; }
            var model = Dialogs.Select(this, local ? "로컬 모델 프로필" : "외부 API 프로필", local
                ? "선택한 로컬 서버의 모델이 직접 답변하고 도구를 실행합니다. 작업 기록은 지정한 서버에 전달됩니다."
                : "선택한 모델이 직접 답변하고 도구를 실행합니다. 기존 작업 기록을 이어서 사용하며, 전송한 기록은 해당 클라우드 API로 전달됩니다.", available);
            if (model is null) return;
            modelId = model.Id;
            modelSettings = Dialogs.ExternalModelSettings(this, LocalModelPresentation.ForSettings(registry, model.Data));
            if (modelSettings is null) return;
        }
        if (kind.Id == "claude_code")
        {
            modelSettings = Dialogs.ClaudeProfileSettings(this);
            if (modelSettings is null) return;
        }
        var alias = Dialogs.Prompt(this, "프로필 이름", "목록에 표시할 이름을 입력하세요."); if (alias is null) return;
        var result = await Request("profile.add", new { alias, kind = kind.Id == "local" ? "external" : kind.Id, model_id = modelId, settings = modelSettings }); await RefreshAsync();
        if (kind.Id == "claude_code")
        {
            var login = await Request("claude.login", new { profile_id = result.S("id") });
            await RefreshAsync();
            await ShowProfileAsync(result.S("id"));
            SetStatus(login.Message("Claude Code가 연 콘솔과 브라우저에서 로그인을 완료한 뒤 상태를 새로 확인하세요."));
        }
        else await ShowProfileAsync(result.S("id"), kind.Id is "external" or "local" ? "profile.show" : "profile.login");
    }
    private string RequireContextProfile() => _profiles.ContextMenu.Tag as string ?? RequireProfile();
    private static void SetClaudeProfileMenu(ContextMenu menu, JsonElement profile)
    {
        foreach (var item in menu.Items.OfType<MenuItem>().Where(item => Equals(item.Header, "하위 에이전트 설정")))
            item.Visibility = ClaudeProfilePresentation.IsClaude(profile) ? Visibility.Collapsed : Visibility.Visible;
    }
    private Task EditExternalModelAsync() => EditExternalModelAsync(_contextProfile ?? RequireProfile());
    private async Task EditExternalModelAsync(string id)
    {
        var profile = _state.Arr("profiles").First(p => p.S("id") == id);
        if (ClaudeProfilePresentation.IsClaude(profile)) { await EditClaudeProfileAsync(id); return; }
        if (profile.S("auth_mode") != "external") { SetStatus("로컬 모델 또는 외부 API 프로필을 선택하세요. GPT 하위 에이전트 설정은 ‘하위 에이전트 · 모델 연결’에서 변경합니다."); return; }
        var registry = await Request("providers.list");
        var model = registry.Arr("models").First(m => m.S("id") == profile.S("external_model_id"));
        var settings = Dialogs.ExternalModelSettings(this, LocalModelPresentation.ForSettings(registry, model), profile.Get("external_settings"), profile.S("alias"));
        if (settings is null) return;
        var result = await Request("profile.model_settings", new { profile_id = id, settings });
        await RefreshAsync(); SetStatus(result.Message());
    }
    private async Task EditClaudeProfileAsync(string id)
    {
        var profile = _state.Arr("profiles").First(p => p.S("id") == id);
        var settings = Dialogs.ClaudeProfileSettings(this, profile, async command =>
        {
            var result = await Request(command, new { profile_id = id });
            await RefreshAsync();
            return result;
        });
        if (settings is null) return;
        settings["profile_id"] = id;
        var saved = await Request("claude.settings", settings);
        await RefreshAsync(); SetStatus(saved.Message("Claude 기본 설정을 저장했습니다."));
    }
    private Task RenameProfileAsync() => RenameProfileAsync(RequireProfile());
    private async Task RenameProfileAsync(string id)
    {
        var current = _state.Arr("profiles").FirstOrDefault(p => p.S("id") == id); var alias = Dialogs.Prompt(this, "프로필 별칭", "표시할 이름", current.S("alias")); if (alias is null) return;
        var result = await Request("profile.rename", new { profile_id = id, alias }); await RefreshAsync(); SetStatus(result.Message("별칭을 변경했습니다."));
    }
    private Task MoveProfileByAsync(int delta) => _profileOrdering.MoveByAsync(_contextProfile ?? RequireProfile(), delta);
    private async Task MoveProfileAsync(ListMove move)
    {
        ++_stateRevision; // Discard any state read that started before this move.
        var result = await Request("profile.move", new { profile_id = move.Id, target_profile_id = move.TargetId, position = move.Position });
        var ids = result.Arr("profile_ids").Select(p => p.GetString()).ToArray();
        var profiles = _state.Arr("profiles").OrderBy(p => Array.IndexOf(ids, p.S("id"))).ToArray();
        _state = JsonSerializer.SerializeToElement(_state.EnumerateObject().ToDictionary(p => p.Name,
            p => p.Name == "profiles" ? JsonSerializer.SerializeToElement(profiles) : p.Value));
        var items = _profiles.Items.OfType<Choice>().OrderBy(p => Array.IndexOf(ids, p.Id)).ToArray();
        _rendering = true;
        try { Fill(_profiles, items, _selectedProfile); }
        finally { _rendering = false; }
        UpdateProfileSummary();
        SetStatus(result.Message("프로필 순서를 저장했습니다."));
    }
    // The service returns the saved order; the list follows it at once and
    // keeps the highlighted card (the open task's), without a state request.
    private async Task MoveShortcutAsync(ListMove move)
    {
        ++_stateRevision; // Discard any state read that started before this move.
        var result = await Request("shortcut.reorder", new { shortcut_id = move.Id, target_shortcut_id = move.TargetId, position = move.Position });
        var ids = result.Arr("shortcut_ids").Select(id => id.ValueKind == JsonValueKind.String ? id.GetString() ?? "" : "").ToArray();
        if (ids.Length > 0)
        {
            int Rank(JsonElement shortcut) { var index = Array.IndexOf(ids, shortcut.S("id")); return index < 0 ? int.MaxValue : index; }
            var shortcuts = _state.Arr("shortcuts").OrderBy(Rank).ToArray();
            _state = JsonSerializer.SerializeToElement(_state.EnumerateObject().ToDictionary(p => p.Name,
                p => p.Name == "shortcuts" ? JsonSerializer.SerializeToElement(shortcuts) : p.Value));
        }
        _rendering = true;
        try { RenderShortcuts(); }
        finally { _rendering = false; }
        SetStatus(result.Message("작업 바로가기 순서를 저장했습니다."));
    }
    // A card press released in place (ListOrdering) opens the task, like the card button.
    private async Task ShortcutClickedAsync(Choice choice, int timestamp)
    {
        CloseShortcutOverlay();
        await Safe(() => OpenShortcutAsync(choice.Id));
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
        // The desktop's last task open (thread start/resume, local or over SSH)
        // is observed by the manager. Prefer it: no keyboard shortcut, clipboard
        // or focus change. The keyboard capture remains only when none is known.
        var owner = _viewingCatalog ? _viewerProfile : Profile();
        var current = owner.Get("current_task");
        string? currentHost = null;
        NativeConversationCaptureResult captured;
        if (current.S("thread_id") != "")
        {
            captured = new(current.S("thread_id"), null, true);
            currentHost = current.S("host_id") is { Length: > 0 } host ? host : null;
            Log($"바로가기 추가 · 최근 연 작업 사용 · {current.S("title", current.S("thread_id"))}");
        }
        else
        {
            captured = await NativeConversationCapture.CaptureAsync(window.Handle, window.Pid, window.Executable,
                new WindowInteropHelper(this).Handle, () => ticket == _navigation && profileId == _selectedProfile &&
                    !_closing && _host.HasLiveAttachment && _host.AttachedHandle == window.Handle && window.MatchesLifetime);
        }
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
            // The observed open also names its host; it settles a same-ID match.
            if (currentHost is not null && matches.Count(t => t.S("host_id") == currentHost) == 1)
                matches = matches.Where(t => t.S("host_id") == currentHost).ToArray();
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
        var result = await NativeConversationCapture.CaptureAsync(window.Handle, window.Pid, window.Executable,
            new WindowInteropHelper(this).Handle, () => ticket == _navigation && expected.ProfileId == _selectedProfile &&
                !_closing && _host.HasLiveAttachment && _host.AttachedHandle == window.Handle && window.MatchesLifetime);
        if (ticket != _navigation || _selectedProfile != expected.ProfileId || _closing) return;
        if (!result.Success) throw new InvalidOperationException(result.Error ?? "원본 화면의 현재 대화를 확인하지 못했습니다.");
        if (result.ThreadId != expected.ThreadId) throw new InvalidOperationException("현재 원본 화면은 요청한 대화와 다릅니다. 대화가 열린 뒤 다시 확인하세요. 메시지는 전송하지 않았습니다.");
        SetStatus(_viewingCatalog ? "읽기 전용 원본 화면의 대화가 바로가기의 표시 ID와 일치합니다. 실제 대화의 원본 출처는 그대로 유지됩니다." : "선택한 프로필의 대화 ID가 바로가기와 일치합니다. 로그인 계정 확인 상태는 위 상태줄을 확인하세요.");
    }
    private async Task OpenShortcutAsync(string id)
    {
        using var timing = _responsiveness?.Time("shortcut.open." + id);
        var item = _state.Arr("shortcuts").FirstOrDefault(s => s.S("id") == id);
        var profileId = item.S("profile_id"); if (profileId == "") throw new InvalidOperationException("이 바로가기에 연결된 프로필이 없습니다.");
        using var action = _profileActions.Enter(profileId, "대화 열기");
        _shortcutSelection = (id, profileId, _state.Arr("profiles").FirstOrDefault(p => p.S("id") == profileId)
            .Get("current_task").S("thread_id"));
        _shortcuts.SelectedItem = _shortcuts.Items.OfType<Choice>().FirstOrDefault(c => c.Id == id);
        var ticket = ++_navigation; if (_selectedProfile != profileId || _viewingCatalog) ParkCurrent(); _viewingCatalog = false; _selectedProfile = profileId;
        _expectedConversation = null; _expectedCanonicalThread = null;
        _hostDeck.Select(profileId); BeginAttach(); _profileRequestTicket = ticket;
        Render();
        // A retained target is usable while its exact task navigation is sent.
        // Synchronize it now, as profile selection does, without waiting for state.
        var retained = Latest(profileId, Profile());
        if (_host.HasLiveAttachment) TryAttach(retained);
        SetStatus("선택한 계정에서 작업으로 이동하고 있습니다…");
        JsonElement result;
        try { result = await Request("conversation.open", new { shortcut_id = id, expected_profile_id = profileId }); }
        finally { if (_profileRequestTicket == ticket) _profileRequestTicket = null; }
        if (ticket != _navigation || _closing) return;
        if (result.S("state") == "waiting_for_reader")
        {
            PresentShortcutResult(result, item, profileId);
            SetStatus(result.Message("Codex가 준비되면 선택한 대화로 자동 이동합니다."));
            var completed = await ConversationReadyWait.CompleteAsync(result,
                () => ticket == _navigation && _selectedProfile == profileId && !_closing,
                token => Request("conversation.navigate", new { navigation_id = token }),
                () => Task.Delay(750));
            if (completed is null) return;
            result = completed.Value;
            Log($"{Profile().S("alias")} · 준비 후 대화 이동 · {result.S("state")}");
        }
        PresentShortcutResult(result, item, profileId);
        // A background state poll updates the sidebar. Its latency must not
        // delay the attached task or keep this account's action gate occupied.
        action.Dispose();
        SetStatus(result.Message("대화 열기 요청을 보냈습니다."), result.S("state") == "blocked");
        _ = VerifyShortcutNavigationAsync(ticket, result, item);
    }
    private void PresentShortcutResult(JsonElement result, JsonElement item, string profileId)
    {
        var returned = result.Get("profile");
        if (returned.ValueKind == JsonValueKind.Object && (returned.S("id") == ""
            || (!result.B("readonly_viewer") && returned.S("id") != profileId)
            || (result.S("profile_id") != "" && result.S("profile_id") != returned.S("id"))))
            throw new InvalidOperationException("요청한 프로필과 반환된 창 정보가 일치하지 않습니다.");
        ApplyConversationResult(result, item, profileId);
        if (returned.ValueKind != JsonValueKind.Object) return;
        _shownProfiles[returned.S("id")] = returned;
        ++_stateRevision;
        _expectedWindowLaunch = WindowLaunchIdentity.From(returned);
        _attachDeadline = DateTime.UtcNow.AddSeconds(25);
        TryAttach(returned);
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
    private Task ProvidersAsync() => ShowProvidersAsync(ClaudeProfilePresentation.IsClaude(Profile()) ? null : _selectedProfile);
    private Task ProfileProvidersAsync() => ShowProvidersAsync(_contextProfile ?? RequireProfile());
    private async Task ShowProvidersAsync(string? profileId)
    {
        // Capture the menu target before the RPC yields and the context menu
        // clears itself. Editing another profile must not select or launch it.
        var profile = _state.Arr("profiles").FirstOrDefault(p => p.S("id") == profileId);
        if (ClaudeProfilePresentation.IsClaude(profile))
        {
            SetStatus("Claude 프로필의 하위 에이전트는 Claude Code CLI가 관리합니다.");
            return;
        }
        var registry = await Request("providers.list");
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
        var profileId = RequireProfile();
        var hosts = await Request("remote.list");
        await Dialogs.RemoteAsync(this, profileId, hosts, Request);
        await RefreshAsync();
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
