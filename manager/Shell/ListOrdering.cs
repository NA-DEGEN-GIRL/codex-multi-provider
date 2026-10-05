using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

// One saved reorder: move Id before or after TargetId ("profile.move",
// "shortcut.reorder").
internal sealed record ListMove(string Id, string TargetId, string Position);

// Drag reordering for a ListBox of Choice items (profiles, task shortcuts).
// A grip (Tag "DragGrip") only drags. A whole item (Tag "DragItem", or another
// tag the caller names, such as a shortcut card's "open" button) drags once the
// pointer moves past the system threshold; released in place, it is a click.
// Any other button inside an item (a card's ⋯ menu) keeps its own click. While
// canReorder is false (a filtered list) presses, drags and moves are left
// alone, so items behave as plain buttons. Esc cancels a drag; Alt+Up/Down
// moves the selected item. Selection is never changed here.
internal sealed class ListOrdering
{
    internal const string ItemTag = "DragItem", GripTag = "DragGrip";
    private readonly ListBox list;
    private readonly Func<ListMove, Task> save;
    private readonly Func<Choice, int, Task>? click;
    private readonly Func<bool> canReorder;
    private readonly string[] itemTags;
    private readonly DispatcherTimer scroll = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private string? source;
    private Point origin, last;
    private bool dragging, saving, itemPress;
    private InsertionLine? line;
    private UIElement? faded;
    private Window? escapeScope;
    internal bool IsInteracting => source is not null || saving;
    internal bool IsDragging => dragging;
    internal bool CanReorder => canReorder();

    internal ListOrdering(ListBox list, Func<ListMove, Task> save, Func<Choice, int, Task>? click = null,
        Func<bool>? canReorder = null, IEnumerable<string>? itemTags = null)
    {
        this.list = list; this.save = save; this.click = click;
        this.canReorder = canReorder ?? (() => true);
        this.itemTags = (itemTags ?? [ItemTag]).ToArray();
        list.PreviewMouseLeftButtonDown += (_, e) =>
        {
            if (!CanReorder || Handle(e.OriginalSource) is not { } kind) return;
            e.Handled = true;
            if (IsInteracting || ItemAt(e.GetPosition(list))?.DataContext is not Choice choice) return;
            Begin(choice.Id, e.GetPosition(list), item: kind == ItemTag && click is not null);
            if (!list.CaptureMouse()) Cancel();
        };
        list.PreviewMouseMove += (_, e) => { if (source is not null) { Update(e.GetPosition(list)); e.Handled = true; } };
        list.PreviewMouseLeftButtonUp += async (_, e) =>
        {
            if (source is null) return;
            e.Handled = true; await CompleteAsync(e.GetPosition(list), e.Timestamp);
        };
        list.LostMouseCapture += (_, _) => Cancel();
        list.PreviewKeyDown += async (_, e) =>
        {
            if (e.Key == Key.Escape && source is not null) { Cancel(); e.Handled = true; }
            else if (Keyboard.Modifiers == ModifierKeys.Alt && e.SystemKey is Key.Up or Key.Down && list.SelectedItem is Choice choice)
            { e.Handled = true; await MoveByAsync(choice.Id, e.SystemKey == Key.Up ? -1 : 1); }
        };
        list.Unloaded += (_, _) => Cancel();
        // Holding the pointer near an edge keeps scrolling; the captured list
        // reports every move, so the last drag point is where the pointer is.
        scroll.Tick += (_, _) =>
        {
            if (!dragging) return;
            var viewer = Descendant<ScrollViewer>(list);
            if (last.Y < 28) viewer?.LineUp();
            else if (last.Y > list.ActualHeight - 28) viewer?.LineDown();
            Update(last);
        };
    }

    // "DragGrip" or the item tag for a press inside an item; null for anything
    // else, including other buttons inside the item and the scroll bar.
    private string? Handle(object pressed)
    {
        for (var node = pressed as DependencyObject; node is not null && node != list;
             node = node is Visual ? VisualTreeHelper.GetParent(node) : LogicalTreeHelper.GetParent(node))
        {
            if (node is FrameworkElement { Tag: string tag })
            {
                if (tag == GripTag) return GripTag;
                if (itemTags.Contains(tag)) return ItemTag;
            }
            if (node is ButtonBase) return null;
        }
        return null;
    }

    internal void Begin(string id, Point point, bool item = false) { source = id; origin = last = point; itemPress = item; }
    internal void Update(Point point)
    {
        if (source is null) return;
        last = point;
        if (!dragging && CanReorder && (Math.Abs(point.X - origin.X) >= SystemParameters.MinimumHorizontalDragDistance ||
            Math.Abs(point.Y - origin.Y) >= SystemParameters.MinimumVerticalDragDistance))
        {
            dragging = true; scroll.Start(); list.Cursor = Cursors.SizeNS;
            // The card being moved fades until it is dropped.
            if (list.ItemContainerGenerator.ContainerFromItem(list.Items.OfType<Choice>().FirstOrDefault(c => c.Id == source)) is UIElement moving)
            { faded = moving; moving.Opacity = 0.5; }
            // Esc cancels wherever keyboard focus is (the search box, notes,
            // a popup); focusing the list instead could scroll it mid-drag.
            escapeScope = Window.GetWindow(list);
            if (escapeScope is not null) escapeScope.PreviewKeyDown += CancelOnEscape;
        }
        ClearLine();
        if (!dragging || Target(point) is not { } target) return;
        if (list.ItemContainerGenerator.ContainerFromItem(list.Items.OfType<Choice>().First(c => c.Id == target.TargetId)) is FrameworkElement item &&
            AdornerLayer.GetAdornerLayer(item) is { } layer)
        { line = new InsertionLine(item, target.Position == "after"); layer.Add(line); }
    }
    internal ListMove? Target(Point point)
    {
        if (source is null || point.X < 0 || point.X > list.ActualWidth || point.Y < 0 || point.Y > list.ActualHeight) return null;
        var container = ItemAt(point);
        if (container?.DataContext is not Choice target || target.Id == source) return null;
        var top = container.TranslatePoint(new Point(), list).Y;
        return new(source, target.Id, point.Y >= top + container.ActualHeight / 2 ? "after" : "before");
    }
    private ListBoxItem? ItemAt(Point point)
    {
        ListBoxItem? lastRow = null;
        foreach (var item in list.Items)
        {
            if (list.ItemContainerGenerator.ContainerFromItem(item) is not ListBoxItem row) continue;
            var y = row.TranslatePoint(new Point(), list).Y;
            if (point.Y <= y + row.ActualHeight) return row;
            lastRow = row;
        }
        return lastRow;
    }
    internal async Task CompleteAsync(Point point, int timestamp = 0)
    {
        var move = dragging ? Target(point) : null;
        var clicked = !dragging && itemPress ? list.Items.OfType<Choice>().FirstOrDefault(c => c.Id == source) : null;
        Cancel();
        if (move is not null) await SaveAsync(move);
        else if (clicked is not null && click is not null) await click(clicked, timestamp);
    }
    internal async Task MoveByAsync(string id, int delta)
    {
        if (IsInteracting || !CanReorder) return;
        var items = list.Items.OfType<Choice>().ToArray();
        var index = Array.FindIndex(items, c => c.Id == id);
        if (index < 0 || index + delta < 0 || index + delta >= items.Length) return;
        await SaveAsync(new(id, items[index + delta].Id, delta < 0 ? "before" : "after"));
    }
    private async Task SaveAsync(ListMove move)
    {
        saving = true;
        try { await save(move); }
        finally { saving = false; }
    }
    internal void Cancel()
    {
        source = null; dragging = false; itemPress = false; scroll.Stop(); ClearLine(); list.ClearValue(FrameworkElement.CursorProperty);
        faded?.ClearValue(UIElement.OpacityProperty); faded = null;
        if (escapeScope is not null) { escapeScope.PreviewKeyDown -= CancelOnEscape; escapeScope = null; }
        if (list.IsMouseCaptured) list.ReleaseMouseCapture();
    }
    private void CancelOnEscape(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Escape || source is null) return;
        Cancel(); e.Handled = true;
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

    // An accent line with a dot at its start, centred in the gap between two
    // items. Kept inside the scroll viewport, so the first item's "before"
    // line is visible too.
    private sealed class InsertionLine(FrameworkElement item, bool after) : Adorner(item)
    {
        private static readonly Brush Accent = WorkspaceAppearance.Accent;
        protected override void OnRender(DrawingContext drawing)
        {
            IsHitTestVisible = false;
            var gap = item.Margin.Top + item.Margin.Bottom;
            var y = after ? item.ActualHeight + gap / 2 : -gap / 2;
            if (VisualParent is UIElement layer)
            {
                var top = item.TranslatePoint(new Point(), layer).Y;
                y = Math.Clamp(y, 2 - top, layer.RenderSize.Height - top - 2);
            }
            var pen = new Pen(Accent, 2) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round };
            drawing.DrawLine(pen, new(7, y), new(Math.Max(8, item.ActualWidth - 3), y));
            drawing.DrawEllipse(Accent, null, new(4, y), 3.5, 3.5);
        }
    }
}
