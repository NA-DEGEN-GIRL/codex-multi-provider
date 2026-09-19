using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal sealed record ProfileMove(string ProfileId, string TargetProfileId, string Position);

// Drag only the grip: selecting/opening an account remains a separate action.
internal sealed class ProfileOrdering
{
    private readonly ListBox list;
    private readonly Func<ProfileMove, Task> save;
    private readonly DispatcherTimer scroll = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private string? source;
    private Point origin;
    private bool dragging, saving;
    private InsertionLine? line;
    internal bool IsInteracting => source is not null || saving;

    internal ProfileOrdering(ListBox list, Func<ProfileMove, Task> save)
    {
        this.list = list; this.save = save;
        list.PreviewMouseLeftButtonDown += (_, e) =>
        {
            if (!IsGrip(e.OriginalSource)) return;
            e.Handled = true;
            if (IsInteracting || ItemAt(e.GetPosition(list))?.DataContext is not Choice choice) return;
            Begin(choice.Id, e.GetPosition(list));
            if (!list.CaptureMouse()) Cancel();
        };
        list.PreviewMouseMove += (_, e) => { if (source is not null) { Update(e.GetPosition(list)); e.Handled = true; } };
        list.PreviewMouseLeftButtonUp += async (_, e) =>
        {
            if (source is null) return;
            e.Handled = true; await CompleteAsync(e.GetPosition(list));
        };
        list.LostMouseCapture += (_, _) => Cancel();
        list.PreviewKeyDown += async (_, e) =>
        {
            if (e.Key == Key.Escape && source is not null) { Cancel(); e.Handled = true; }
            else if (Keyboard.Modifiers == ModifierKeys.Alt && e.SystemKey is Key.Up or Key.Down && list.SelectedItem is Choice choice)
            { e.Handled = true; await MoveByAsync(choice.Id, e.SystemKey == Key.Up ? -1 : 1); }
        };
        list.Unloaded += (_, _) => Cancel();
        scroll.Tick += (_, _) =>
        {
            if (!dragging) return;
            var point = Mouse.GetPosition(list);
            var viewer = Descendant<ScrollViewer>(list);
            if (point.Y < 28) viewer?.LineUp();
            else if (point.Y > list.ActualHeight - 28) viewer?.LineDown();
            Update(point);
        };
    }

    internal static DataTemplate Template() => ProfileCards.Create();

    private static bool IsGrip(object source)
    {
        for (var node = source as DependencyObject; node is not null; node = VisualTreeHelper.GetParent(node))
            if (node is FrameworkElement { Tag: "ProfileDragGrip" }) return true;
        return false;
    }

    internal void Begin(string id, Point point) { source = id; origin = point; }
    internal void Update(Point point)
    {
        if (source is null) return;
        if (!dragging && (Math.Abs(point.X - origin.X) >= SystemParameters.MinimumHorizontalDragDistance ||
            Math.Abs(point.Y - origin.Y) >= SystemParameters.MinimumVerticalDragDistance))
        { dragging = true; scroll.Start(); }
        ClearLine();
        if (!dragging || Target(point) is not { } target) return;
        if (list.ItemContainerGenerator.ContainerFromItem(list.Items.OfType<Choice>().First(c => c.Id == target.TargetProfileId)) is UIElement item &&
            AdornerLayer.GetAdornerLayer(item) is { } layer)
        { line = new InsertionLine(item, target.Position == "after"); layer.Add(line); }
    }
    internal ProfileMove? Target(Point point)
    {
        if (source is null || point.X < 0 || point.X > list.ActualWidth || point.Y < 0 || point.Y > list.ActualHeight) return null;
        var container = ItemAt(point);
        if (container?.DataContext is not Choice target || target.Id == source) return null;
        var top = container.TranslatePoint(new Point(), list).Y;
        return new(source, target.Id, point.Y >= top + container.ActualHeight / 2 ? "after" : "before");
    }
    private ListBoxItem? ItemAt(Point point)
    {
        ListBoxItem? last = null;
        foreach (var item in list.Items)
        {
            if (list.ItemContainerGenerator.ContainerFromItem(item) is not ListBoxItem row) continue;
            var y = row.TranslatePoint(new Point(), list).Y;
            if (point.Y <= y + row.ActualHeight) return row;
            last = row;
        }
        return last;
    }
    internal async Task CompleteAsync(Point point)
    {
        var move = dragging ? Target(point) : null;
        Cancel();
        if (move is not null) await SaveAsync(move);
    }
    internal async Task MoveByAsync(string id, int delta)
    {
        if (IsInteracting) return;
        var items = list.Items.OfType<Choice>().ToArray();
        var index = Array.FindIndex(items, c => c.Id == id);
        if (index < 0 || index + delta < 0 || index + delta >= items.Length) return;
        await SaveAsync(new(id, items[index + delta].Id, delta < 0 ? "before" : "after"));
    }
    private async Task SaveAsync(ProfileMove move)
    {
        saving = true;
        try { await save(move); }
        finally { saving = false; }
    }
    internal void Cancel()
    {
        source = null; dragging = false; scroll.Stop(); ClearLine();
        if (list.IsMouseCaptured) list.ReleaseMouseCapture();
    }
    private void ClearLine()
    {
        if (line is null) return;
        AdornerLayer.GetAdornerLayer(line.AdornedElement)?.Remove(line); line = null;
    }
    private static T? Descendant<T>(DependencyObject parent) where T : DependencyObject
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            if (child is T found) return found;
            if (Descendant<T>(child) is { } nested) return nested;
        }
        return null;
    }
    private sealed class InsertionLine(UIElement item, bool after) : Adorner(item)
    {
        protected override void OnRender(DrawingContext drawing)
        {
            IsHitTestVisible = false;
            var y = after ? AdornedElement.RenderSize.Height : 0;
            drawing.DrawLine(new Pen(new SolidColorBrush(Color.FromRgb(163, 193, 255)), 2), new(0, y), new(AdornedElement.RenderSize.Width, y));
        }
    }
}
