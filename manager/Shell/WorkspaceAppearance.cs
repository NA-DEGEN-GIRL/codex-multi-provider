using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// Shared chrome for the workspace tools and notes. Native Codex keeps its own UI.
internal static class WorkspaceAppearance
{
    internal static readonly Brush Surface = Color("#202228");
    internal static readonly Brush Canvas = Color("#17191E");
    internal static readonly Brush Raised = Color("#252A33");
    internal static readonly Brush Line = Color("#353C48");
    internal static readonly Brush Text = Color("#E9ECF2");
    internal static readonly Brush Muted = Color("#9FA7B7");
    internal static readonly Brush Accent = Color("#A3C1FF");
    internal static readonly Brush Selected = Color("#2E3443");

    internal static Brush Color(string value)
    {
        var brush = (SolidColorBrush)new BrushConverter().ConvertFromString(value)!;
        brush.Freeze(); return brush;
    }

    internal static Button Tool(Button button, string name = "", bool quiet = false)
    {
        button.Style = (Style)Application.Current.FindResource("WorkspaceToolButton");
        button.FontSize = 12; button.Height = 32;
        button.Padding = new Thickness(10, 0, 10, 0);
        button.Margin = new Thickness(0);
        button.HorizontalContentAlignment = HorizontalAlignment.Center;
        button.VerticalContentAlignment = VerticalAlignment.Center;
        if (quiet) { button.Background = Brushes.Transparent; button.BorderBrush = Brushes.Transparent; }
        if (name.Length > 0) button.Name = name;
        AutomationProperties.SetName(button, button.ToolTip?.ToString() ?? button.Content?.ToString() ?? name);
        return button;
    }

    internal static Button Icon(Button button, string name)
    {
        Tool(button, name, quiet: true);
        button.Width = 30; button.Height = 30; button.FontSize = 18;
        button.Padding = new Thickness(0);
        return button;
    }

    internal static void Active(Button button, bool active)
    {
        button.Background = active ? Selected : Raised;
        button.Foreground = active ? Accent : Text;
        button.BorderBrush = active ? Color("#687CA6") : Line;
        AutomationProperties.SetItemStatus(button, active ? "열림" : "접힘");
    }
}
