using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// Shared chrome for the workspace tools and notes. Native Codex keeps its own UI.
// Spacing follows a 4/8/12/16 scale; cards use an 8-DIP radius. The XAML card
// templates (ProfileCards, ProfileRail, ShortcutCards) repeat these hex values.
internal static class WorkspaceAppearance
{
    internal static readonly Brush Surface = Color("#202228");
    internal static readonly Brush Canvas = Color("#17191E");
    internal static readonly Brush Rail = Color("#1A1C21");
    internal static readonly Brush Raised = Color("#252A33");
    internal static readonly Brush Line = Color("#353C48");
    internal static readonly Brush Divider = Color("#2A2F38");
    internal static readonly Brush Text = Color("#E9ECF2");
    internal static readonly Brush Muted = Color("#9FA7B7");
    internal static readonly Brush Faint = Color("#8E9BB2");
    internal static readonly Brush Accent = Color("#A3C1FF");
    internal static readonly Brush Selected = Color("#2E3443");
    internal static readonly Brush Ready = Color("#77C6A0");
    internal static readonly Brush Warning = Color("#E5B773");
    internal static readonly Brush Critical = Color("#E57B73");
    internal static readonly FontFamily Icons = new("Segoe Fluent Icons, Segoe MDL2 Assets");

    // Segoe MDL2 / Fluent glyphs; both fonts share these code points.
    internal const string GlyphAdd = "", GlyphRefresh = "", GlyphMore = "",
        GlyphHistory = "", GlyphSettings = "", GlyphPower = "",
        GlyphOpenPane = "", GlyphClosePane = "", GlyphList = "",
        GlyphSearch = "", GlyphClose = "";

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

    // A quiet square icon button drawn with the icon font. The tooltip is the
    // accessible name, so every icon-only action still reads as text.
    internal static Button Glyph(Button button, string name, string glyph, string tip, double size = 32)
    {
        button.ToolTip = tip;
        Tool(button, name, quiet: true);
        button.Style = GlyphStyle();
        button.ClearValue(Control.BackgroundProperty); button.ClearValue(Control.BorderBrushProperty);
        button.Content = glyph; button.FontFamily = Icons; button.FontSize = size >= 38 ? 16 : 14;
        button.Width = size; button.Height = size; button.Padding = new Thickness(0);
        AutomationProperties.SetName(button, tip);
        return button;
    }

    // Quiet and muted at rest; the icon brightens under the pointer.
    private static Style? glyphStyle;
    private static Style GlyphStyle()
    {
        if (glyphStyle is not null) return glyphStyle;
        var style = new Style(typeof(Button), (Style)Application.Current.FindResource("WorkspaceToolButton"));
        style.Setters.Add(new Setter(Control.ForegroundProperty, Muted));
        style.Setters.Add(new Setter(Control.BackgroundProperty, Brushes.Transparent));
        style.Setters.Add(new Setter(Control.BorderBrushProperty, Brushes.Transparent));
        var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
        hover.Setters.Add(new Setter(Control.ForegroundProperty, Text));
        style.Triggers.Add(hover);
        style.Seal();
        return glyphStyle = style;
    }

    // Returns a glyph button that Active() highlighted to its quiet resting look.
    internal static void Rest(Button button)
    {
        button.ClearValue(Control.BackgroundProperty); button.ClearValue(Control.BorderBrushProperty);
        button.ClearValue(Control.ForegroundProperty);
        AutomationProperties.SetItemStatus(button, "접힘");
    }

    internal static void Active(Button button, bool active)
    {
        button.Background = active ? Selected : Raised;
        button.Foreground = active ? Accent : Text;
        button.BorderBrush = active ? Color("#687CA6") : Line;
        AutomationProperties.SetItemStatus(button, active ? "열림" : "접힘");
    }

    // Provider tints: GPT, Claude, cloud API and local model profiles.
    internal static (Brush Fill, Brush Text) Tint(string provider) => provider switch
    {
        "claude" => (ClaudeFill, ClaudeText),
        "api" => (ApiFill, ApiText),
        "local" => (LocalFill, LocalText),
        _ => (GptFill, GptText)
    };
    private static readonly Brush GptFill = Color("#1F3A33"), GptText = Color("#8FDCBE"),
        ClaudeFill = Color("#3E2C24"), ClaudeText = Color("#F0B08E"),
        ApiFill = Color("#2C3150"), ApiText = Color("#B8C3FF"),
        LocalFill = Color("#3A2F1E"), LocalText = Color("#EAC27F");
}
