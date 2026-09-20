using System.Reflection;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shell;

namespace Codex.ControlCenter.Shell;

internal static class WindowControlsSelfTest
{
    internal static void Run(string root, string report)
    {
        // Real WPF controls and production closing handler; never show a window,
        // connect the service, touch Codex or change desktop focus.
        var window = new MainWindow(root, fixture: true);
        var checks = new List<string>();
        var buttons = Children((DependencyObject)window.Content).OfType<Button>()
            .Where(b => b.Name.StartsWith("Manager", StringComparison.Ordinal)).ToDictionary(b => b.Name);
        void Click(string name) => buttons[name].RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
        Require(WindowChrome.GetWindowChrome(window) is { UseAeroCaptionButtons: false, CaptionHeight: 36 }, "Native caption tracking still owns buttons.");
        Require(buttons.Values.All(b => !b.Focusable && WindowChrome.GetIsHitTestVisibleInChrome(b)), "Caption buttons can steal editor focus or become drag area.");
        Require(buttons["ManagerClose"].ToolTip?.ToString()?.Contains("작업은 계속") == true,
            "Close does not disclose that live work continues.");
        // This test never shows the window: ScrollViewer templates in the
        // footer have not materialized their visual children yet.
        Require(LogicalChildren((DependencyObject)window.Content).OfType<Button>().Any(b => b.Name == "ExitWorkspace"),
            "Explicit full shutdown action is missing.");
        Require(window.Title == "Codex 작업 공간" && WorkspaceBuild.CopyText.Contains(WorkspaceBuild.Label),
            "The caption must contain only the app name while version copying retains the release label.");
        Require(LogicalChildren((DependencyObject)window.Content).OfType<ManagerUpdatePanel>().Any(),
            "Pre-restart update compatibility indicator is missing.");
        checks.Add("Manager owns accessible caption buttons without taking editor keyboard focus.");
        Require(ManagerTitleBar.MaximizedInsets(-12, -12, 3864, 2124, 0, 0, 3840, 2100, 1.5) == new Thickness(8), "Maximized border clipping was not compensated at 144 DPI.");
        Require(ManagerTitleBar.MaximizedInsets(-1920, 0, 1920, 1040, -1920, 0, 0, 1040, 1) == new Thickness(0), "Normal client bounds gained a spurious title offset.");
        checks.Add("Maximized title and controls stay inside the monitor work area at 144 DPI and negative monitor coordinates.");
        Click("ManagerMaximize"); Require(window.WindowState == WindowState.Normal, "Restore button failed.");
        Click("ManagerMaximize"); Require(window.WindowState == WindowState.Maximized, "Maximize button failed.");
        Click("ManagerMinimize"); Require(window.WindowState == WindowState.Minimized, "Minimize button failed.");
        checks.Add("Restore, maximize and minimize buttons update the real WPF window state.");
        var fields = BindingFlags.Instance | BindingFlags.NonPublic;
        var selectedProfile = typeof(MainWindow).GetField("_selectedProfile", fields)!;
        var contextProfile = typeof(MainWindow).GetField("_contextProfile", fields)!;
        var profiles = (ListBox)typeof(MainWindow).GetField("_profiles", fields)!.GetValue(window)!;
        selectedProfile.SetValue(window, "selected-fixture");
        contextProfile.SetValue(window, "right-clicked-fixture");
        profiles.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.OpenedEvent));
        profiles.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.ClosedEvent));
        Require(contextProfile.GetValue(window) is null, "Menu close did not clear transient context.");
        var menuTarget = typeof(MainWindow).GetMethod("RequireContextProfile", fields)!.Invoke(window, null);
        Require((string?)menuTarget == "right-clicked-fixture" && (string?)selectedProfile.GetValue(window) == "selected-fixture",
            "Settings target followed the active profile after menu close.");
        Require(profiles.ContextMenu.Items.OfType<MenuItem>().Any(item => (string?)item.Header == "하위 에이전트 설정"),
            "Profile menu omitted subagent settings.");
        profiles.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.OpenedEvent));
        Require((string?)profiles.ContextMenu.Tag == "selected-fixture", "Keyboard reopening retained the previous right-click target.");
        profiles.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.ClosedEvent));
        selectedProfile.SetValue(window, null);
        checks.Add("Profile settings retain the right-clicked account after popup close without selecting it; keyboard reopening resets the target.");
        CheckMenuCommands(root, checks);
        ProfileAgentPresentationSelfTest.Run(checks);
        var field = typeof(MainWindow).GetField("_profileRequestTicket", BindingFlags.Instance | BindingFlags.NonPublic)!;
        field.SetValue(window, (int?)987);
        var navigation = typeof(MainWindow).GetField("_navigation", BindingFlags.Instance | BindingFlags.NonPublic)!;
        int before = (int)navigation.GetValue(window)!;
        bool closed = false;
        window.Closed += (_, _) => closed = true;
        Click("ManagerClose");
        Require(closed, "Pending profile request blocked closing.");
        Require(!(bool)typeof(MainWindow).GetField("_exitAllRequested", fields)!.GetValue(window)!,
            "Caption close requested a full workspace shutdown.");
        Require(field.GetValue(window) is null && (int)navigation.GetValue(window)! > before, "Late profile response can reopen/attach a window.");
        checks.Add("Close finishes with a pending profile request and invalidates its late response.");
        File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, checks }, new JsonSerializerOptions { WriteIndented = true }));
    }

    private static void CheckMenuCommands(string root, List<string> checks)
    {
        var requests = new List<(string Command, JsonElement Args)>();
        var window = new MainWindow(root, fixture: true, fixtureRequest: (command, args) =>
        {
            requests.Add((command, JsonSerializer.SerializeToElement(args)));
            return Task.FromResult(JsonSerializer.SerializeToElement(new { state = "complete", profile_ids = new[] { "target", "selected" } }));
        });
        using var fixture = JsonDocument.Parse("""
        {"profiles":[{"id":"selected","alias":"A"},{"id":"target","alias":"B"}],
         "shortcuts":[{"id":"selected-link","alias":"A","profile_id":"selected"},{"id":"target-link","alias":"B","profile_id":"target"}]}
        """);
        window.UseFixture(fixture.RootElement);
        var flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var profiles = (ListBox)typeof(MainWindow).GetField("_profiles", flags)!.GetValue(window)!;
        var context = typeof(MainWindow).GetField("_contextProfile", flags)!;
        foreach (var (label, command) in new[] { ("위로 이동", "profile.move"), ("로그인 상태 새로 확인", "profile.login_status"),
            ("작업 종료 후 설정 적용 예약", "profile.restart"), ("계정을 목록에서 제거", "profile.remove"), ("프로필 준비", "profile.prepare") })
        {
            context.SetValue(window, "target");
            profiles.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.OpenedEvent));
            profiles.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.ClosedEvent));
            var item = profiles.ContextMenu.Items.OfType<MenuItem>().Single(item => Equals(item.Header, label));
            item.RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
            if (!requests.Any(r => r.Command == command && r.Args.S("profile_id") == "target"))
                throw new InvalidOperationException("Post-close profile menu command targeted the wrong account: " + command);
        }
        if ((string?)typeof(MainWindow).GetField("_selectedProfile", flags)!.GetValue(window) != "selected")
            throw new InvalidOperationException("Editing the context target changed the selected profile.");
        var shortcuts = (ListBox)typeof(MainWindow).GetField("_shortcuts", flags)!.GetValue(window)!;
        shortcuts.SelectedIndex = 0;
        typeof(MainWindow).GetField("_contextShortcut", flags)!.SetValue(window, "target-link");
        shortcuts.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.OpenedEvent));
        shortcuts.ContextMenu.RaiseEvent(new RoutedEventArgs(ContextMenu.ClosedEvent));
        shortcuts.ContextMenu.Items.OfType<MenuItem>().Single(item => Equals(item.Header, "링크 삭제"))
            .RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
        if (!requests.Any(r => r.Command == "shortcut.delete" && r.Args.S("shortcut_id") == "target-link"))
            throw new InvalidOperationException("Post-close shortcut menu deleted the selected shortcut instead of the context target.");
        if (requests.Any(r => r.Args.S("profile_id") == "selected" || r.Args.S("shortcut_id") == "selected-link"))
            throw new InvalidOperationException("A context action reached the selected item.");
        var beforeClose = requests.Count;
        window.Close();
        if (requests.Skip(beforeClose).Any(r => r.Command is "profile.cleanup" or "supervisor.retire" or "manager.stop_warmup"))
            throw new InvalidOperationException("Normal UI close dispatched a work-stopping request.");
        checks.Add("Real post-close WPF menu Click routes move, login status, restart, remove, prepare and shortcut deletion to the right-clicked item using an isolated RPC recorder.");
    }

    private static IEnumerable<DependencyObject> LogicalChildren(DependencyObject root)
    {
        foreach (var child in LogicalTreeHelper.GetChildren(root).OfType<DependencyObject>())
        {
            yield return child;
            foreach (var descendant in LogicalChildren(child)) yield return descendant;
        }
    }

    private static IEnumerable<DependencyObject> Children(DependencyObject root)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++)
        {
            var child = VisualTreeHelper.GetChild(root, i);
            yield return child;
            foreach (var descendant in Children(child)) yield return descendant;
        }
    }
}
