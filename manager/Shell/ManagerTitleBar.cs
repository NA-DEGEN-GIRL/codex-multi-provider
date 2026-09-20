using System.Runtime.InteropServices;
using System.Windows.Interop;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shell;

namespace Codex.ControlCenter.Shell;

// Client-side buttons do not enter DefWindowProc's cross-process caption-button
// mouse-tracking loop. WindowChrome retains native drag, resize and double-click.
internal static class ManagerTitleBar
{
    internal static FrameworkElement Wrap(Window window, UIElement content, Action<string> log, string closeLabel = "닫기")
    {
        var chrome = new WindowChrome
        {
            CaptionHeight = 36,
            ResizeBorderThickness = SystemParameters.WindowResizeBorderThickness,
            GlassFrameThickness = new Thickness(0),
            CornerRadius = new CornerRadius(0),
            UseAeroCaptionButtons = false
        };
        WindowChrome.SetWindowChrome(window, chrome);
        var root = new Grid { Background = new SolidColorBrush(Color.FromRgb(23, 25, 30)) };
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(36) });
        root.RowDefinitions.Add(new RowDefinition());
        var title = new DockPanel { LastChildFill = true };
        var controls = new StackPanel { Orientation = Orientation.Horizontal };
        DockPanel.SetDock(controls, Dock.Right);
        title.Children.Add(controls);
        title.Children.Add(new TextBlock { Text = window.Title, FontSize = 13,
            Margin = new Thickness(12, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center });
        Button Make(string name, string glyph, Action action)
        {
            var button = new Button
            {
                Name = name, Content = glyph, Width = 46, Height = 36,
                Margin = new Thickness(0), Padding = new Thickness(0), BorderThickness = new Thickness(0),
                HorizontalContentAlignment = HorizontalAlignment.Center, VerticalContentAlignment = VerticalAlignment.Center,
                Style = (Style)Application.Current.FindResource("WorkspaceToolButton"), Background = Brushes.Transparent,
                FontFamily = new FontFamily("Segoe MDL2 Assets"), FontSize = 11,
                Focusable = false, IsTabStop = false
            };
            WindowChrome.SetIsHitTestVisibleInChrome(button, true);
            button.Click += (_, _) => { log("관리창 버튼 클릭 · " + button.ToolTip); action(); };
            controls.Children.Add(button);
            return button;
        }
        static void Label(Button button, string label)
        { button.ToolTip = label; AutomationProperties.SetName(button, label); }
        Label(Make("ManagerMinimize", "\uE921", () => window.WindowState = WindowState.Minimized), "최소화");
        var maximize = Make("ManagerMaximize", "\uE922", () =>
            window.WindowState = window.WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized);
        void UpdateState()
        {
            bool maximized = window.WindowState == WindowState.Maximized;
            maximize.Content = maximized ? "\uE923" : "\uE922";
            Label(maximize, maximized ? "이전 크기로" : "최대화");
        }
        window.StateChanged += (_, _) => UpdateState();
        UpdateState();
        Label(Make("ManagerClose", "\uE8BB", window.Close), closeLabel);
        root.Children.Add(title);
        Grid.SetRow(content, 1);
        root.Children.Add(content);
        var frame = new Border { Child = root, Background = root.Background, UseLayoutRounding = true };
        void UpdateInsets()
        {
            var padding = new Thickness(0);
            var hwnd = new WindowInteropHelper(window).Handle;
            if (window.WindowState == WindowState.Maximized && hwnd != 0)
            {
                var monitor = new MonitorInfo { Size = (uint)Marshal.SizeOf<MonitorInfo>() };
                var origin = new NativeWindowInterop.Point();
                if (GetMonitorInfoW(MonitorFromWindow(hwnd, 2), ref monitor) &&
                    NativeWindowInterop.ClientToScreen(hwnd, ref origin) && NativeWindowInterop.GetClientRect(hwnd, out var client))
                    padding = MaximizedInsets(origin.X, origin.Y, client.Right, client.Bottom,
                        monitor.Work.Left, monitor.Work.Top, monitor.Work.Right, monitor.Work.Bottom,
                        NativeWindowInterop.GetDpiForWindow(hwnd) / 96.0);
            }
            frame.Padding = padding;
            chrome.CaptionHeight = 36 + padding.Top;
        }
        window.SourceInitialized += (_, _) => UpdateInsets();
        window.Loaded += (_, _) => UpdateInsets();
        window.StateChanged += (_, _) => UpdateInsets();
        window.SizeChanged += (_, _) => UpdateInsets();
        return frame;
    }
    internal static Thickness MaximizedInsets(int x, int y, int width, int height,
        int left, int top, int right, int bottom, double scale) => new(
            Math.Max(0, left - x) / scale, Math.Max(0, top - y) / scale,
            Math.Max(0, x + width - right) / scale, Math.Max(0, y + height - bottom) / scale);

    [StructLayout(LayoutKind.Sequential)]
    private struct MonitorInfo { internal uint Size; internal NativeWindowInterop.Rect Monitor, Work; internal uint Flags; }
    [DllImport("user32.dll")] private static extern nint MonitorFromWindow(nint hwnd, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfoW(nint monitor, ref MonitorInfo info);
}
