using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// UI-only preferences, independent of profiles, their selection and server
// state: whether the profile rail is expanded into cards, and the width of the
// task shortcut column, which the divider beside it resizes. Unknown keys in
// sidebar-layout.json (an older build's profiles_ratio) are kept.
internal sealed class SidebarLayout
{
    internal const double DefaultShortcutWidth = 280, MinShortcutWidth = 240, MaxShortcutWidth = 440;
    private readonly ColumnDefinition _shortcuts;
    private readonly string _path;
    private readonly Action<string> _report;
    private double _width;

    internal GridSplitter Divider { get; }
    internal bool ProfilesExpanded { get; private set; }
    // The user's chosen width; the column may be narrower while the window is small.
    internal double ShortcutWidth => _width;
    internal event Action? ShortcutWidthChanged;

    internal SidebarLayout(string root, ColumnDefinition shortcuts, Action<string> report)
    {
        _shortcuts = shortcuts;
        _path = Path.Combine(root, "work", "control-center", "sidebar-layout.json");
        _report = report;
        var saved = Read();
        _width = Width(saved?["shortcuts_width"]) ?? DefaultShortcutWidth;
        ProfilesExpanded = saved?["profiles_expanded"] is JsonValue expanded && expanded.TryGetValue<bool>(out var value) && value;
        Divider = new GridSplitter
        {
            Name = "ShortcutColumnSplitter", Width = 6,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            VerticalAlignment = VerticalAlignment.Stretch,
            ResizeDirection = GridResizeDirection.Columns,
            ResizeBehavior = GridResizeBehavior.PreviousAndNext,
            Cursor = Cursors.SizeWE, Background = Brushes.Transparent,
            Focusable = true, KeyboardIncrement = 16, DragIncrement = 1,
            ToolTip = "좌우로 드래그해 작업 바로가기 너비 조절 · 방향키로 조절 · 두 번 클릭해 기본 너비 복원"
        };
        AutomationProperties.SetName(Divider, "작업 바로가기 영역 너비 조절");
        var line = new FrameworkElementFactory(typeof(Border));
        line.Name = "DividerLine";
        line.SetValue(FrameworkElement.WidthProperty, 1.0);
        line.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
        line.SetValue(Border.BackgroundProperty, WorkspaceAppearance.Divider);
        var surface = new FrameworkElementFactory(typeof(Border));
        surface.SetValue(Border.BackgroundProperty, Brushes.Transparent);
        surface.AppendChild(line);
        var template = new ControlTemplate(typeof(GridSplitter)) { VisualTree = surface };
        foreach (var property in new[] { UIElement.IsMouseOverProperty, UIElement.IsKeyboardFocusWithinProperty })
        {
            var highlight = new Trigger { Property = property, Value = true };
            highlight.Setters.Add(new Setter(Border.BackgroundProperty, WorkspaceAppearance.Accent, "DividerLine"));
            highlight.Setters.Add(new Setter(FrameworkElement.WidthProperty, 2.0, "DividerLine"));
            template.Triggers.Add(highlight);
        }
        Divider.Template = template;
        Divider.DragCompleted += (_, e) => { if (!e.Canceled) SaveActualWidth(); };
        Divider.KeyUp += (_, e) => { if (e.Key is Key.Left or Key.Right) SaveActualWidth(); };
        Divider.MouseDoubleClick += (_, e) =>
        {
            if (e.ChangedButton != MouseButton.Left) return;
            _width = DefaultShortcutWidth;
            Save();
            ShortcutWidthChanged?.Invoke();
            e.Handled = true;
        };
    }

    internal void SetProfilesExpanded(bool expanded)
    {
        if (ProfilesExpanded == expanded) return;
        ProfilesExpanded = expanded;
        Save();
    }

    private void SaveActualWidth()
    {
        // GridSplitter writes pixel widths. Keep the gesture's result as the
        // preferred width; a later, narrower window only clamps the column.
        Divider.UpdateLayout();
        var width = _shortcuts.ActualWidth;
        if (!double.IsFinite(width) || width < MinShortcutWidth - 0.5) return;
        _width = Math.Clamp(width, MinShortcutWidth, MaxShortcutWidth);
        Save();
        ShortcutWidthChanged?.Invoke();
    }

    private static double? Width(JsonNode? node) =>
        node is JsonValue value && value.TryGetValue<double>(out var width) && double.IsFinite(width)
            ? Math.Clamp(width, MinShortcutWidth, MaxShortcutWidth) : null;

    private JsonObject? Read()
    {
        try
        {
            if (!File.Exists(_path)) return null;
            return JsonNode.Parse(File.ReadAllText(_path)) as JsonObject;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { return null; }
    }

    private void Save()
    {
        var temporary = _path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            var data = Read() ?? new JsonObject();
            data["profiles_expanded"] = ProfilesExpanded;
            data["shortcuts_width"] = Math.Round(_width, 1);
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            File.WriteAllText(temporary, data.ToJsonString());
            File.Move(temporary, _path, overwrite: true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            _report("사이드바 크기 저장 실패 · " + error.Message);
        }
        finally
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
        }
    }
}
