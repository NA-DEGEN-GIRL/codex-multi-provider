using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

// UI-only proportions: independent of profiles, their selection and server state.
internal sealed class SidebarSectionSplit
{
    private const double DefaultRatio = 0.65;
    private readonly RowDefinition _profiles;
    private readonly RowDefinition _shortcuts;
    private readonly string _path;
    private readonly Action<string> _report;

    internal GridSplitter Divider { get; }

    internal SidebarSectionSplit(string root, RowDefinition profiles, RowDefinition shortcuts, Action<string> report)
    {
        _profiles = profiles;
        _shortcuts = shortcuts;
        _path = Path.Combine(root, "work", "control-center", "sidebar-layout.json");
        _report = report;
        SetRatio(ReadRatio());
        Divider = new GridSplitter
        {
            Name = "SidebarSectionSplitter", Height = 14,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            VerticalAlignment = VerticalAlignment.Stretch,
            ResizeDirection = GridResizeDirection.Rows,
            ResizeBehavior = GridResizeBehavior.PreviousAndNext,
            Cursor = Cursors.SizeNS, Background = Brushes.Transparent,
            Focusable = true, KeyboardIncrement = 16, DragIncrement = 1,
            ToolTip = "위아래로 드래그해 영역 조절 · 방향키로 조절 · 두 번 클릭해 기본 비율 복원"
        };
        AutomationProperties.SetName(Divider, "프로필과 작업 바로가기 영역 크기 조절");
        var line = new FrameworkElementFactory(typeof(Border));
        line.Name = "DividerLine";
        line.SetValue(FrameworkElement.HeightProperty, 1.0);
        line.SetValue(FrameworkElement.MarginProperty, new Thickness(18, 0, 18, 0));
        line.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
        line.SetValue(Border.BackgroundProperty, WorkspaceAppearance.Line);
        var surface = new FrameworkElementFactory(typeof(Border));
        surface.SetValue(Border.BackgroundProperty, Brushes.Transparent);
        surface.AppendChild(line);
        var template = new ControlTemplate(typeof(GridSplitter)) { VisualTree = surface };
        foreach (var property in new[] { UIElement.IsMouseOverProperty, UIElement.IsKeyboardFocusWithinProperty })
        {
            var highlight = new Trigger { Property = property, Value = true };
            highlight.Setters.Add(new Setter(Border.BackgroundProperty, WorkspaceAppearance.Accent, "DividerLine"));
            highlight.Setters.Add(new Setter(FrameworkElement.HeightProperty, 2.0, "DividerLine"));
            template.Triggers.Add(highlight);
        }
        Divider.Template = template;
        Divider.DragCompleted += (_, e) => { if (!e.Canceled) SaveActualRatio(); };
        Divider.KeyUp += (_, e) => { if (e.Key is Key.Up or Key.Down) SaveActualRatio(); };
        Divider.MouseDoubleClick += (_, e) =>
        {
            if (e.ChangedButton != MouseButton.Left) return;
            SetRatio(DefaultRatio);
            SaveRatio(DefaultRatio);
            e.Handled = true;
        };
    }

    private void SetRatio(double ratio)
    {
        _profiles.Height = new GridLength(ratio, GridUnitType.Star);
        _shortcuts.Height = new GridLength(1 - ratio, GridUnitType.Star);
    }

    private double ReadRatio()
    {
        try
        {
            if (!File.Exists(_path)) return DefaultRatio;
            using var json = JsonDocument.Parse(File.ReadAllText(_path));
            if (json.RootElement.ValueKind == JsonValueKind.Object &&
                json.RootElement.TryGetProperty("profiles_ratio", out var value) &&
                value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var ratio) &&
                double.IsFinite(ratio) && ratio > 0 && ratio < 1)
                return ratio;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { }
        return DefaultRatio;
    }

    private void SaveActualRatio()
    {
        // GridSplitter expresses its star sizes in current DIPs. Normalize them
        // once the gesture ends, so the next window size keeps the same ratio.
        Divider.UpdateLayout();
        var total = _profiles.ActualHeight + _shortcuts.ActualHeight;
        if (total <= 0) return;
        var ratio = _profiles.ActualHeight / total;
        if (ratio <= 0 || ratio >= 1) return;
        SetRatio(ratio);
        SaveRatio(ratio);
    }

    private void SaveRatio(double ratio)
    {
        var temporary = _path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            File.WriteAllText(temporary, JsonSerializer.Serialize(new { profiles_ratio = ratio }));
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
