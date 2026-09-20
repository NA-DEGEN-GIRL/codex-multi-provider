using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal sealed class ManagerUpdatePanel : Border, IDisposable
{
    private readonly Func<CancellationToken, Task<JsonElement?>> _service;
    private readonly string _root;
    private readonly bool _fixture;
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(30) };
    private readonly CancellationTokenSource _lifetime = new();
    private readonly TextBlock _title = new() { Name = "WorkspaceUpdateStatus", FontSize = 11, FontWeight = FontWeights.SemiBold,
        TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _detail = new() { Name = "WorkspaceUpdateDetail", FontSize = 11, LineHeight = 16,
        Foreground = WorkspaceAppearance.Muted, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 3, 0, 0) };
    private readonly Button _check;
    private bool _checking, _disposed;

    internal ManagerUpdatePanel(string root, Func<CancellationToken, Task<JsonElement?>> service, bool fixture)
    {
        _root = root; _service = service; _fixture = fixture;
        Name = "WorkspaceUpdatePanel";
        Margin = new Thickness(0, 5, 0, 5); Padding = new Thickness(8, 6, 6, 8);
        CornerRadius = new CornerRadius(6); Background = WorkspaceAppearance.Raised;
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition());
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        _check = WorkspaceAppearance.Icon(new Button { Content = "↻", Focusable = false,
            ToolTip = "업데이트 호환성 다시 확인 · 확인만 하며 작업이나 관리창을 종료하지 않습니다." }, "CheckWorkspaceUpdate");
        _check.Width = 24; _check.Height = 24; _check.FontSize = 15;
        _check.Click += async (_, _) => await RefreshAsync();
        Grid.SetColumn(_check, 1); grid.Children.Add(_title); grid.Children.Add(_check);
        Grid.SetRow(_detail, 1); Grid.SetColumnSpan(_detail, 2); grid.Children.Add(_detail);
        Child = grid;
        AutomationProperties.SetLiveSetting(_title, AutomationLiveSetting.Polite);
        Present(ManagerUpdateNotice.Checking);
        _timer.Tick += async (_, _) => await RefreshAsync();
        if (!fixture) Loaded += async (_, _) => { _timer.Start(); await RefreshAsync(); };
        Unloaded += (_, _) => _timer.Stop();
    }

    internal async Task RefreshAsync()
    {
        if (_fixture || _checking || _disposed) return;
        _checking = true; _check.IsEnabled = false;
        try
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
            deadline.CancelAfter(TimeSpan.FromSeconds(4));
            var service = await _service(deadline.Token).WaitAsync(deadline.Token);
            var result = await Task.Run(() => ManagerUpdateCompatibility.Inspect(_root,
                Environment.ProcessPath ?? "", WorkspaceBuild.Revision, service), deadline.Token).WaitAsync(deadline.Token);
            if (!_disposed) Present(result);
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            if (!_disposed) Present(ManagerUpdateNotice.Unknown("업데이트 확인이 지연되거나 연결이 끊겼습니다."));
        }
        finally { _checking = false; if (!_disposed) _check.IsEnabled = true; }
    }

    internal void Present(ManagerUpdateNotice notice)
    {
        _title.Text = notice.Title; _detail.Text = notice.Detail;
        _title.Foreground = notice.State switch
        {
            ManagerUpdateState.Ready => WorkspaceAppearance.Color("#79C69D"),
            ManagerUpdateState.NeedsStop or ManagerUpdateState.Unknown or ManagerUpdateState.Deferred => WorkspaceAppearance.Color("#E6B875"),
            _ => WorkspaceAppearance.Muted
        };
        ToolTip = notice.Title + "\n" + notice.Detail + "\n30초마다 자동 확인합니다. ↻ 버튼으로 지금 확인할 수 있습니다.";
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true; _timer.Stop(); _lifetime.Cancel(); _lifetime.Dispose();
    }
}
