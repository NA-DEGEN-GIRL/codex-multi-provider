using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal sealed class RemoteUpdatesWindow : Window
{
    private readonly string profileId;
    private readonly Func<string, object?, Task<JsonElement>> request;
    private readonly DispatcherTimer poll = new() { Interval = TimeSpan.FromSeconds(2) };
    private readonly ComboBox hosts = new() { Name = "HostSelector", HorizontalContentAlignment = HorizontalAlignment.Stretch };
    private readonly CheckBox autoCheck = Toggle("AutoCheck", "새 버전 자동 확인", true);
    private readonly CheckBox autoApply = Toggle("AutoApply", "관리 런타임을 작업 종료 후 자동 적용", false);
    private readonly CheckBox stockConfirmation = Toggle("StockConfirmation", "기본 Codex 작업을 모두 마쳤으며, 서비스 재시작에 동의합니다", false);
    private readonly Button check = Action("CheckNow", "지금 확인");
    private readonly Button schedule = Action("ScheduleManaged", "작업 종료 후 업데이트");
    private readonly Button cancel = Action("CancelManaged", "예약 취소");
    private readonly Button stockUpdate = Action("StockUpdate", "기본 Codex 수동 업데이트");
    private readonly Button prepare = Action("PrepareRemote", "이 프로필의 원격 실행 준비");
    private readonly TextBlock managedActive = Version("ManagedActiveVersion");
    private readonly TextBlock managedPrepared = Version("ManagedPreparedVersion");
    private readonly TextBlock managedAvailable = Version("ManagedAvailableVersion");
    private readonly TextBlock stockCli = Version("StockCliVersion");
    private readonly TextBlock stockDaemon = Version("StockDaemonVersion");
    private readonly TextBlock managedState = Note("", "ManagedState");
    private readonly TextBlock stockState = Note("", "StockState");
    private readonly TextBlock jobState = Note("", "JobState");
    private readonly TextBlock managedMessage = Note("");
    private readonly TextBlock stockMessage = Note("");
    private readonly TextBlock checkedAt = Note("");
    private readonly TextBlock feedback = Note("연결을 선택하고 버전을 확인하세요.");
    private JsonElement data;
    private int context, requestSerial;
    private bool busy, polling, closed;
    private bool statusError;
    private string stockIdentity = "";
    private static readonly SolidColorBrush Muted = new(Color.FromRgb(159, 173, 194));

    internal RemoteUpdatesWindow(string profileId, JsonElement hostsData, Func<string, object?, Task<JsonElement>> request)
    {
        this.profileId = profileId;
        this.request = request;
        Title = "SSH 업데이트"; Width = 760; Height = 850; MinWidth = 560; MinHeight = 520;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        Background = new SolidColorBrush(Color.FromRgb(23, 25, 30));
        var layout = new DockPanel { Margin = new Thickness(24) };
        var header = new StackPanel { Margin = new Thickness(0, 0, 0, 16) };
        header.Children.Add(new TextBlock { Text = "SSH 업데이트", FontSize = 23, FontWeight = FontWeights.SemiBold });
        header.Children.Add(Note("관리 런타임과 기본 Codex의 버전을 함께 확인합니다."));
        DockPanel.SetDock(header, Dock.Top); layout.Children.Add(header);
        var footer = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
        footer.Children.Add(new ScrollViewer { Content = feedback, MaxHeight = 96, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled });
        var close = Action("CloseRemoteUpdates", "닫기"); close.HorizontalAlignment = HorizontalAlignment.Right;
        close.Click += (_, _) => Close(); footer.Children.Add(close);
        DockPanel.SetDock(footer, Dock.Bottom); layout.Children.Add(footer);
        var body = new StackPanel();
        body.Children.Add(Note("SSH 설정에 등록된 연결"));
        var records = hostsData.ValueKind == JsonValueKind.Array ? hostsData.Items() : hostsData.Arr("hosts");
        foreach (var host in records)
        {
            var alias = host.S("alias", host.S("id"));
            if (alias.Length > 0) hosts.Items.Add(new Host(alias, host.S("alias", host.S("name", alias))));
        }
        hosts.SelectedIndex = hosts.Items.Count > 0 ? 0 : -1;
        body.Children.Add(hosts);
        var checkRow = new DockPanel { LastChildFill = true };
        check.Margin = new Thickness(12, 0, 0, 0); DockPanel.SetDock(check, Dock.Right); checkRow.Children.Add(check);
        autoCheck.VerticalAlignment = VerticalAlignment.Center; checkRow.Children.Add(autoCheck);
        body.Children.Add(checkRow); body.Children.Add(checkedAt);

        var managed = new StackPanel();
        managed.Children.Add(Heading("관리 SSH 런타임")); managed.Children.Add(managedState);
        managed.Children.Add(Versions(("현재 실행", managedActive), ("준비된 버전", managedPrepared), ("사용 가능한 버전", managedAvailable)));
        managed.Children.Add(managedMessage);
        managed.Children.Add(autoApply);
        managed.Children.Add(Note("이 프로필에 연결된 SSH 호스트 전체를 작업 종료 후 업데이트합니다. 로컬 작업은 유지하며 원격 상태가 불명확하면 기다립니다."));
        var managedActions = new WrapPanel { Margin = new Thickness(0, 7, 0, 0) };
        managedActions.Children.Add(schedule); managedActions.Children.Add(cancel); managed.Children.Add(managedActions);
        managed.Children.Add(jobState); body.Children.Add(Card(managed));

        var stock = new StackPanel();
        stock.Children.Add(Heading("기본 Codex · 수동 업데이트")); stock.Children.Add(stockState);
        stock.Children.Add(Versions(("설치된 CLI", stockCli), ("실행 중인 서비스", stockDaemon)));
        stock.Children.Add(stockMessage);
        stock.Children.Add(Note("관리 런타임의 활동 상태만으로 기본 Codex의 작업 종료를 확인할 수 없습니다. 업데이트하면 서비스가 재시작되며 현재 작업이 중단될 수 있습니다."));
        stock.Children.Add(stockConfirmation); stock.Children.Add(stockUpdate); body.Children.Add(Card(stock));

        var preparation = new StackPanel();
        preparation.Children.Add(Note("현재 프로필의 로그인으로 SSH 실행을 준비합니다. 기본 Codex CLI와 공용 SSH 설정은 보존합니다."));
        preparation.Children.Add(prepare);
        body.Children.Add(new Expander { Header = "이 프로필의 SSH 연결 준비", Content = preparation, Margin = new Thickness(0, 3, 0, 0) });
        layout.Children.Add(new ScrollViewer { Name = "RemoteUpdatesScroll", Content = body, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled });
        Content = layout;

        check.Click += async (_, _) => await ExecuteAsync("remote.updates.check");
        schedule.Click += async (_, _) => { if (schedule.IsEnabled) await ExecuteAsync("remote.updates.schedule"); };
        cancel.Click += async (_, _) => { if (cancel.IsEnabled) await ExecuteAsync("remote.updates.cancel"); };
        prepare.Click += async (_, _) => await ExecuteAsync("remote.prepare");
        autoCheck.Click += async (_, _) => await SaveSettingsAsync();
        autoApply.Click += async (_, _) => await SaveSettingsAsync();
        stockConfirmation.Click += (_, _) => UpdateControls();
        stockUpdate.Click += async (_, _) =>
        {
            // Gate the handler as well as the button: programmatic activation
            // must never bypass an explicit, current-host confirmation.
            if (!stockUpdate.IsEnabled || stockConfirmation.IsChecked != true || !data.Get("stock").B("update_supported") || !HasStockObservation(data.Get("stock"))) return;
            stockConfirmation.IsChecked = false;
            await ExecuteAsync("remote.updates.stock_update", confirmed: true);
        };
        hosts.SelectionChanged += async (_, _) =>
        {
            context++; requestSerial++; busy = polling = false; data = default; stockIdentity = "";
            stockConfirmation.IsChecked = false; statusError = false; feedback.Text = ""; Render(); await LoadAsync();
        };
        poll.Tick += async (_, _) => await LoadAsync();
        Loaded += (_, _) => poll.Start();
        Closed += (_, _) => { closed = true; context++; requestSerial++; poll.Stop(); };
        PreviewKeyDown += (_, e) => { if (e.Key == Key.Escape) { Close(); e.Handled = true; } };
        Render();
    }

    private string Alias => (hosts.SelectedItem as Host)?.Alias ?? "";
    private bool Current(int capturedContext, string alias) => !closed && context == capturedContext && Alias == alias;

    internal async Task LoadAsync()
    {
        if (closed || busy || polling || Alias.Length == 0) return;
        var alias = Alias; var capturedProfile = profileId; int capturedContext = context, serial = ++requestSerial;
        polling = true;
        try
        {
            var result = await request("remote.updates.status", new { profile_id = capturedProfile, alias });
            if (Current(capturedContext, alias) && serial == requestSerial && Matches(result, alias, capturedProfile)) { data = result; Render(); }
        }
        catch (Exception error) { if (Current(capturedContext, alias) && serial == requestSerial) { statusError = true; ShowError(error.Message); } }
        finally { if (Current(capturedContext, alias)) { polling = false; UpdateControls(); } }
    }

    private Task SaveSettingsAsync() => ExecuteAsync("remote.updates.settings", settings: true);

    private async Task ExecuteAsync(string command, bool settings = false, bool confirmed = false)
    {
        if (closed || busy || Alias.Length == 0) return;
        var alias = Alias; var capturedProfile = profileId; int capturedContext = context, serial = ++requestSerial;
        var args = new Dictionary<string, object?> { ["alias"] = alias, ["profile_id"] = capturedProfile };
        if (settings) { args["auto_check"] = autoCheck.IsChecked == true; args["auto_apply"] = autoApply.IsChecked == true; }
        if (confirmed)
        {
            args["confirmed"] = true;
            args["observation_id"] = data.Get("stock").S("observation_id");
        }
        busy = true; statusError = false; feedback.Foreground = Muted; feedback.Text = command == "remote.prepare" ? "이 프로필의 원격 실행을 준비하고 있습니다…" : "요청을 처리하고 있습니다…"; UpdateControls();
        bool refresh = false;
        try
        {
            var result = await request(command, args);
            if (!Current(capturedContext, alias) || serial != requestSerial) return;
            if (!Matches(result, alias, capturedProfile)) { ShowError("응답의 SSH 연결이 일치하지 않습니다. 상태를 다시 확인하세요."); return; }
            if (command == "remote.prepare") { feedback.Text = PreparationSummary(result); refresh = true; }
            else { data = result; feedback.Text = ""; Render(); }
        }
        catch (Exception error) { if (Current(capturedContext, alias) && serial == requestSerial) { Render(); ShowError(error.Message); } }
        finally { if (Current(capturedContext, alias)) { busy = false; UpdateControls(); } }
        if (refresh && Current(capturedContext, alias)) await LoadAsync();
    }

    private static bool Matches(JsonElement result, string alias, string profile) =>
        (result.S("alias").Length == 0 || result.S("alias") == alias) && (result.S("profile_id").Length == 0 || result.S("profile_id") == profile);

    private void Render()
    {
        autoCheck.IsChecked = Boolean(data, "auto_check", true);
        autoApply.IsChecked = Boolean(data, "auto_apply", false);
        var managed = data.Get("managed"); var stock = data.Get("stock"); var job = data.Get("job");
        managedActive.Text = DisplayVersion(managed.S("active_version"));
        managedPrepared.Text = DisplayVersion(managed.S("prepared_version"));
        managedAvailable.Text = DisplayVersion(managed.S("available_version"));
        stockCli.Text = DisplayVersion(stock.S("cli_version"));
        stockDaemon.Text = DisplayVersion(stock.S("daemon_version"));
        SetState(managedState, managed.S("state"));
        SetState(stockState, stock.S("state"), " · 서비스 " + StateLabel(stock.S("daemon_state")));
        if (stock.S("state") == "current") stockState.Text = "CLI·서비스 버전 일치 · 서비스 " + StateLabel(stock.S("daemon_state"));
        managedMessage.Text = managed.S("message"); stockMessage.Text = stock.S("message");
        var stockJob = stock.Get("update_job");
        if (stockJob.ValueKind == JsonValueKind.Object)
        {
            stockMessage.Text += (stockMessage.Text.Length > 0 ? "\n" : "") + "수동 업데이트 · " + StateLabel(stockJob.S("state"));
            if (stockJob.S("message").Length > 0) stockMessage.Text += " · " + stockJob.S("message");
        }
        var identity = string.Join("|", stock.S("cli_version"), stock.S("daemon_version"), stock.S("daemon_state"),
            stock.S("observation_id"), stock.S("host_identity"));
        if (identity != stockIdentity) { stockConfirmation.IsChecked = false; stockIdentity = identity; }
        jobState.Text = job.ValueKind == JsonValueKind.Object ? StateLabel(job.S("state")) + (job.S("message").Length > 0 ? " · " + job.S("message") : "") : "예약된 업데이트 없음";
        checkedAt.Text = data.B("checking") ? "새 버전을 확인하고 있습니다…" : data.S("checked_at").Length == 0 ? "아직 확인하지 않았습니다. 버전을 자동으로 확인하거나 ‘지금 확인’을 누르세요." : "마지막 확인 · " + DisplayTime(data.S("checked_at"));
        if (Alias.Length == 0) feedback.Text = "등록된 SSH 연결이 없습니다. Codex 설정에서 연결을 추가하세요.";
        else if (data.S("error").Length > 0) { statusError = true; ShowError(data.S("error")); }
        else if (statusError) { statusError = false; feedback.Text = ""; feedback.Foreground = Muted; }
        UpdateControls();
    }

    private void UpdateControls()
    {
        bool available = !closed && Alias.Length > 0 && !busy;
        bool loaded = data.ValueKind == JsonValueKind.Object;
        var managed = data.Get("managed"); var stock = data.Get("stock"); var job = data.Get("job");
        var jobStatus = job.S("state");
        bool queued = jobStatus is "queued" or "waiting" or "waiting_for_idle" or "waiting_for_work" or "pending" or "blocked" or "busy";
        bool running = queued || jobStatus is "running" or "updating" or "applying" or "preparing" or "checking" or "recovering";
        bool stockRunning = stock.Get("update_job").S("state") is "starting" or "applying" or "dispatching" or "unknown";
        check.IsEnabled = available && !data.B("checking");
        prepare.IsEnabled = available && !running;
        autoCheck.IsEnabled = autoApply.IsEnabled = available && loaded;
        schedule.IsEnabled = available && loaded && managed.B("can_schedule") && !running && !data.B("checking") &&
            managed.S("state") is "update_available" or "available" or "stopped" or "unknown" or "busy" or "ready";
        cancel.IsEnabled = available && queued;
        stockConfirmation.IsEnabled = available && loaded && stock.B("update_supported") && HasStockObservation(stock) && !running && !stockRunning;
        stockUpdate.IsEnabled = stockConfirmation.IsEnabled && stockConfirmation.IsChecked == true && stock.S("state") is not ("updating" or "running" or "applying") && !data.B("checking");
    }

    private void ShowError(string message) { feedback.Foreground = Brushes.Orange; feedback.Text = "확인 필요 · " + message; }
    private static string PreparationSummary(JsonElement result)
    {
        var lines = new List<string> { Dialogs.ResultSummary(result) };
        if (result.B("authentication_required")) lines.Add("원격 프로필에서 선택한 GPT 계정의 인증이 필요합니다.");
        foreach (var notice in result.Arr("notices"))
            if (notice.ValueKind == JsonValueKind.String && !lines.Contains(notice.GetString()!)) lines.Add(notice.GetString()!);
        return string.Join("\n", lines);
    }
    private static bool Boolean(JsonElement value, string key, bool fallback) => value.Get(key).ValueKind is JsonValueKind.True or JsonValueKind.False ? value.Get(key).GetBoolean() : fallback;
    private static bool HasStockObservation(JsonElement stock) => stock.S("observation_id") is { Length: 64 } token &&
        token.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f');
    private static string DisplayVersion(string value) => string.IsNullOrWhiteSpace(value) ? "확인되지 않음" : value;
    private static string DisplayTime(string value) => DateTimeOffset.TryParse(value, out var time) ? time.ToLocalTime().ToString("yyyy-MM-dd HH:mm") : value;
    private static void SetState(TextBlock target, string state, string suffix = "")
    {
        target.Text = StateLabel(state) + suffix;
        target.Foreground = state is "error" or "failed" or "attention" ? Brushes.Orange : state is "current" or "latest" or "up_to_date" or "ready" ? new SolidColorBrush(Color.FromRgb(147, 210, 172)) : Muted;
    }
    private static string StateLabel(string state) => state switch
    {
        "current" or "latest" or "up_to_date" => "최신 버전",
        "update_available" or "available" => "새 버전 있음",
        "update_needed" => "CLI·서비스 버전 다름",
        "busy" or "active" => "작업 중",
        "idle" => "대기 중",
        "ready" or "prepared" => "준비됨",
        "not_prepared" => "실행 준비 필요",
        "running" => "실행 중",
        "stopped" or "not_running" => "중지됨",
        "queued" or "pending" => "업데이트 예약됨",
        "waiting" or "waiting_for_idle" or "waiting_for_work" or "blocked" => "작업 종료·상태 확인 대기",
        "checking" => "확인 중",
        "updating" or "applying" or "preparing" or "starting" or "dispatching" => "업데이트 중",
        "recovering" => "이전 업데이트 결과 확인 중",
        "complete" or "completed" or "success" => "업데이트 완료",
        "cancelled" or "canceled" => "예약 취소됨",
        "error" or "failed" => "오류 · 확인 필요",
        "attention" => "확인 필요",
        "unavailable" or "unsupported" => "사용할 수 없음",
        "missing" or "not_installed" => "설치되지 않음",
        _ => "상태 확인 필요"
    };

    private static TextBlock Note(string text, string name = "") => new() { Name = name, Text = text, Foreground = Muted, FontSize = 12, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 5, 0, 5) };
    private static TextBlock Version(string name) => new() { Name = name, TextWrapping = TextWrapping.Wrap, FontSize = 14, FontWeight = FontWeights.SemiBold };
    private static TextBlock Heading(string text) => new() { Text = text, FontSize = 16, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap };
    private static Button Action(string name, string text) => new() { Name = name, Content = text, Margin = new Thickness(0, 3, 8, 3), HorizontalAlignment = HorizontalAlignment.Left };
    private static CheckBox Toggle(string name, string text, bool value) => new() { Name = name, Content = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap }, IsChecked = value, HorizontalContentAlignment = HorizontalAlignment.Stretch, VerticalContentAlignment = VerticalAlignment.Center };
    private static Border Card(UIElement child) => new() { Child = child, Padding = new Thickness(16), CornerRadius = new CornerRadius(8), BorderThickness = new Thickness(1), BorderBrush = new SolidColorBrush(Color.FromRgb(49, 56, 68)), Background = new SolidColorBrush(Color.FromRgb(28, 32, 39)), Margin = new Thickness(0, 10, 8, 0) };
    private static Grid Versions(params (string Label, TextBlock Value)[] values)
    {
        var grid = new Grid { Margin = new Thickness(0, 5, 0, 9) };
        for (int i = 0; i < values.Length; i++)
        {
            grid.ColumnDefinitions.Add(new ColumnDefinition());
            var cell = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
            cell.Children.Add(Note(values[i].Label)); cell.Children.Add(values[i].Value);
            Grid.SetColumn(cell, i); grid.Children.Add(cell);
        }
        return grid;
    }
    private sealed record Host(string Alias, string Label) { public override string ToString() => Label; }
}
