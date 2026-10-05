using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

// Task shortcut reordering in a real (offscreen, fixture-only) workspace
// window: press-vs-drag, insertion before/after, cancel paths, auto-scroll,
// the filtered list, the ⋯ menu moves and the exact "shortcut.reorder"
// payload. The recorder mirrors store.shortcut_reorder; nothing reaches a
// service, account or Codex window. Run from --profile-order-self-test.
internal static class ShortcutOrderingSelfTest
{
    private const BindingFlags Private = BindingFlags.Instance | BindingFlags.NonPublic;

    internal static async Task<(int Checks, string[] Notes)> RunAsync(string png)
    {
        int checks = 0;
        void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException("Shortcut order: " + message); checks++; }
        var root = Path.Combine(Path.GetTempPath(), "codex-shortcut-order-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        var order = new List<string>();
        JsonElement State(int count) => JsonSerializer.SerializeToElement(new
        {
            profiles = new[] { new { id = "p1", alias = "01 · 개발", status = "running", current_task = new { thread_id = "t2" },
                usage = new { windows = new object[] { new { label = "주간", remaining_percent = 64 }, new { label = "5시간", remaining_percent = 38 } } } } },
            shortcuts = order.Take(count).Select(id => new { id, alias = Title(id), profile_id = "p1", thread_id = "t" + id[1..],
                host_id = id == "s4" ? "ssh:build-linux" : "local" }).ToArray()
        });
        static string Title(string id) => id switch
        {
            "s1" => "작업 공간 UI 개선", "s2" => "서버 API 정리", "s3" => "모델 조합 검토", "s4" => "빌드 서버 로그 확인",
            "s5" => "배포 체크리스트", _ => "검토 작업 " + id[1..]
        };
        var requests = new List<(string Command, JsonElement Args)>();
        Task<JsonElement> Recorder(string command, object? args)
        {
            var json = JsonSerializer.SerializeToElement(args);
            requests.Add((command, json));
            if (command == "shortcut.reorder")
            {
                // Same rule as store.shortcut_reorder.
                var id = json.S("shortcut_id");
                order.Remove(id);
                order.Insert(order.IndexOf(json.S("target_shortcut_id")) + (json.S("position") == "after" ? 1 : 0), id);
                return Task.FromResult(JsonSerializer.SerializeToElement(new { shortcut_ids = order.ToArray(), message = "작업 바로가기 순서를 저장했습니다." }));
            }
            if (command == "conversation.open")
                return Task.FromResult(JsonSerializer.SerializeToElement(new { state = "blocked", message = "자체 시험 · 작업을 열지 않습니다." }));
            throw new InvalidOperationException("Unexpected request: " + command);
        }
        order.AddRange(Enumerable.Range(1, 5).Select(index => "s" + index));
        var window = new MainWindow(root, fixture: true, fixtureRequest: Recorder) { WindowState = WindowState.Normal, Width = 1440, Height = 960,
            Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        try
        {
            window.UseFixture(State(5));
            window.Show();
            await Settle(window);
            T Field<T>(string name) => (T)typeof(MainWindow).GetField(name, Private)!.GetValue(window)!;
            var list = Field<ListBox>("_shortcuts");
            var ordering = Field<ListOrdering>("_shortcutOrdering");
            var filter = Field<TextBox>("_shortcutFilter");
            string[] Ids() => list.Items.OfType<Choice>().Select(choice => choice.Id).ToArray();
            string? Selected() => (list.SelectedItem as Choice)?.Id;
            ListBoxItem Container(string id) => (ListBoxItem)list.ItemContainerGenerator.ContainerFromItem(list.Items.OfType<Choice>().Single(choice => choice.Id == id));
            Point At(string id, double fraction)
            {
                var item = Container(id);
                return item.TranslatePoint(new Point(60, item.ActualHeight * fraction), list);
            }
            (string Command, JsonElement Args)[] Since(int start) => requests.Skip(start).ToArray();
            void RequireReorder((string Command, JsonElement Args)[] sent, string id, string target, string position, string what)
            {
                Require(sent.Length == 1 && sent[0].Command == "shortcut.reorder"
                    && sent[0].Args.EnumerateObject().Select(property => property.Name).SequenceEqual(["shortcut_id", "target_shortcut_id", "position"])
                    && sent[0].Args.S("shortcut_id") == id && sent[0].Args.S("target_shortcut_id") == target && sent[0].Args.S("position") == position,
                    $"{what} must send exactly {{ shortcut_id: {id}, target_shortcut_id: {target}, position: {position} }}, sent "
                    + string.Join("; ", sent.Select(item => item.Command + " " + item.Args.GetRawText())));
            }
            Require(Ids().SequenceEqual(order) && Selected() == "s2", "the fixture renders the saved order and highlights the open task (s2)");
            var highlightChanges = 0;
            list.SelectionChanged += (_, _) => { if (Selected() is { } id && id != "s2") highlightChanges++; };

            // A press on the card body is taken over (click or drag); its ⋯ button keeps its own click.
            bool Pressed(UIElement element)
            {
                // PreviewMouseDown tunnels; each element re-raises it as its own
                // PreviewMouseLeftButtonDown, which is what ListOrdering listens to.
                var press = new MouseButtonEventArgs(Mouse.PrimaryDevice, Environment.TickCount, MouseButton.Left) { RoutedEvent = UIElement.PreviewMouseDownEvent };
                element.RaiseEvent(press);
                ordering.Cancel();
                return press.Handled;
            }
            var openButton = Descendants(Container("s1")).OfType<Button>().Single(button => Equals(button.Tag, "open"));
            var menuButton = Descendants(Container("s1")).OfType<Button>().Single(button => Equals(button.Tag, "menu"));
            var (openPressed, menuPressed) = (Pressed(openButton), Pressed(menuButton));
            Require(openPressed && !menuPressed, $"a card press starts click-or-drag while the ⋯ button keeps its own click (card {openPressed}, menu {menuPressed})");

            // Mid-list drag, rendered: the line sits in the gap above 서버 API 정리 and the moved card fades.
            ordering.Begin("s5", At("s5", .5), item: true);
            ordering.Update(At("s2", .25));
            await Settle(window);
            Require((AdornerLayer.GetAdornerLayer(Container("s2"))?.GetAdorners(Container("s2")) ?? []).Length == 1 && Container("s5").Opacity < 1
                && ordering.Target(At("s2", .25)) == new ListMove("s5", "s2", "before"), "a mid-list drag shows the line between the two cards and fades the moved card");
            LayoutSelfTest.SaveImage(Field<Border>("_shortcutPanel"), png);
            ordering.Cancel();
            await Settle(window);
            Require(Container("s5").Opacity == 1 && (AdornerLayer.GetAdornerLayer(Container("s2"))?.GetAdorners(Container("s2")) ?? []).Length == 0,
                "cancelling restores the card and removes the line");

            // Drag before the first card: insertion line, cursor, exact payload, saved order, same highlight.
            var start = requests.Count;
            ordering.Begin("s4", At("s4", .5), item: true);
            ordering.Update(At("s1", .2));
            var firstLines = AdornerLayer.GetAdornerLayer(Container("s1"))?.GetAdorners(Container("s1")) ?? [];
            Require(ordering.IsDragging && Equals(list.Cursor, Cursors.SizeNS) && firstLines.Length == 1
                && ordering.Target(At("s1", .2)) == new ListMove("s4", "s1", "before"), "dragging over the top half of a card shows one insertion line before it");
            await ordering.CompleteAsync(At("s1", .2));
            await Settle(window);
            RequireReorder(Since(start), "s4", "s1", "before", "a drop before the first card");
            Require(Ids().SequenceEqual(["s4", "s1", "s2", "s3", "s5"]) && Ids().SequenceEqual(order) && Selected() == "s2" && highlightChanges == 0
                && !ordering.IsInteracting && list.ReadLocalValue(FrameworkElement.CursorProperty) == DependencyProperty.UnsetValue
                && (AdornerLayer.GetAdornerLayer(Container("s1"))?.GetAdorners(Container("s1")) ?? []).Length == 0,
                "after the save the list follows the returned order, the open task stays highlighted and the drag state is cleared");

            // Drag below the last card's middle: after it.
            start = requests.Count;
            ordering.Begin("s1", At("s1", .5), item: true);
            ordering.Update(At("s5", .8));
            Require(ordering.Target(At("s5", .8)) == new ListMove("s1", "s5", "after"), "dragging over the bottom half of a card targets after it");
            await ordering.CompleteAsync(At("s5", .8));
            await Settle(window);
            RequireReorder(Since(start), "s1", "s5", "after", "a drop after the last card");
            Require(Ids().SequenceEqual(["s4", "s2", "s3", "s5", "s1"]) && Selected() == "s2" && highlightChanges == 0, "an after-drop keeps the highlight");

            // Cancelled drags save nothing: outside the list, on itself, and Esc.
            start = requests.Count;
            ordering.Begin("s3", At("s3", .5), item: true); ordering.Update(At("s4", .2)); await ordering.CompleteAsync(new Point(-20, 30));
            ordering.Begin("s3", At("s3", .5), item: true); ordering.Update(At("s3", .9)); await ordering.CompleteAsync(At("s3", .9));
            // Esc reaches the window wherever focus is (here: the search box).
            ordering.Begin("s3", At("s3", .5), item: true); ordering.Update(At("s4", .2));
            var escape = new KeyEventArgs(Keyboard.PrimaryDevice, PresentationSource.FromVisual(filter)!, Environment.TickCount, Key.Escape)
                { RoutedEvent = Keyboard.PreviewKeyDownEvent };
            filter.RaiseEvent(escape);
            Require(escape.Handled && !ordering.IsInteracting && Since(start).Length == 0 && Ids().SequenceEqual(["s4", "s2", "s3", "s5", "s1"]),
                "a drop outside the list, on the dragged card itself, or Esc saves nothing and opens nothing");

            // The next state poll returns the saved order: same order, same highlight.
            window.UseFixture(State(5)); await Settle(window);
            Require(Ids().SequenceEqual(order) && Selected() == "s2" && highlightChanges == 0, "a later poll keeps the saved order and highlight");

            // The ⋯ menu: 위로/아래로 이동, disabled at the ends.
            var menu = list.ContextMenu;
            MenuItem Item(string header) => menu.Items.OfType<MenuItem>().Single(item => Equals(item.Header, header));
            void Open(string id)
            {
                typeof(MainWindow).GetField("_contextShortcut", Private)!.SetValue(window, id);
                menu.RaiseEvent(new RoutedEventArgs(ContextMenu.OpenedEvent));
                menu.RaiseEvent(new RoutedEventArgs(ContextMenu.ClosedEvent));
            }
            Open(order[0]);
            Require(!Item("위로 이동").IsEnabled && Item("아래로 이동").IsEnabled, "the first card cannot move up");
            Open(order[^1]);
            Require(Item("위로 이동").IsEnabled && !Item("아래로 이동").IsEnabled, "the last card cannot move down");
            start = requests.Count;
            Open("s2");
            Require(Item("위로 이동").IsEnabled && Item("아래로 이동").IsEnabled, "a middle card can move both ways");
            Item("아래로 이동").RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
            await Settle(window);
            RequireReorder(Since(start), "s2", "s3", "after", "아래로 이동");
            start = requests.Count;
            Open("s2");
            Item("위로 이동").RaiseEvent(new RoutedEventArgs(MenuItem.ClickEvent));
            await Settle(window);
            RequireReorder(Since(start), "s2", "s3", "before", "위로 이동");
            Require(Ids().SequenceEqual(["s4", "s2", "s3", "s5", "s1"]) && Selected() == "s2" && highlightChanges == 0, "menu moves keep the highlight");

            // A filtered list has no reliable neighbours: no drag, no menu moves, plain card buttons.
            filter.Text = "검토"; await Settle(window);
            start = requests.Count;
            Require(Ids().SequenceEqual(["s3"]) && !ordering.CanReorder, "the search filter turns reordering off");
            var filteredOpen = Descendants(Container("s3")).OfType<Button>().Single(button => Equals(button.Tag, "open"));
            Require(!Pressed(filteredOpen), "a filtered card press is left to the card button");
            ordering.Begin("s3", At("s3", .5), item: true); ordering.Update(new Point(At("s3", .5).X, At("s3", .5).Y + 200));
            Require(!ordering.IsDragging && list.ReadLocalValue(FrameworkElement.CursorProperty) == DependencyProperty.UnsetValue,
                "a filtered list never starts a drag or shows the move cursor");
            ordering.Cancel();
            await ordering.MoveByAsync("s3", 1);
            Open("s3");
            Require(!Item("위로 이동").IsEnabled && !Item("아래로 이동").IsEnabled && Item("아래로 이동").ToolTip is string tip && tip.Contains("검색")
                && Since(start).Length == 0, "while filtered the menu moves are disabled and explain why; nothing is sent");
            filter.Text = ""; await Settle(window);
            Require(ordering.CanReorder && Ids().SequenceEqual(order), "clearing the search restores reordering and the full saved order");

            // Long lists scroll while dragging near the bottom edge.
            order.AddRange(Enumerable.Range(6, 25).Select(index => "s" + index));
            window.UseFixture(State(order.Count)); await Settle(window);
            var viewer = Descendants(list).OfType<ScrollViewer>().First();
            ordering.Begin(order[0], At(order[0], .5), item: true);
            ordering.Update(new Point(60, list.ActualHeight - 6));
            for (var tick = 0; tick < 8 && viewer.VerticalOffset <= 0; tick++) { await Task.Delay(100); await Settle(window); }
            Require(viewer.VerticalOffset > 0, "dragging near the list edge scrolls the list");
            ordering.Cancel();
            viewer.ScrollToTop(); await Settle(window);

            // Released in place, a card press opens the task (and is not a move).
            start = requests.Count;
            ordering.Begin("s3", At("s3", .5), item: true);
            await ordering.CompleteAsync(At("s3", .5));
            await Settle(window);
            var sent = Since(start);
            Require(sent.Length == 1 && sent[0].Command == "conversation.open" && sent[0].Args.S("shortcut_id") == "s3",
                "a press released in place opens that task instead of reordering");
            return (checks, ["Shortcut cards reorder by drag with a gap-centred insertion line; a press released in place opens the task.",
                "Drops send exactly { shortcut_id, target_shortcut_id, position }; the list follows the returned order and the open task's card stays highlighted.",
                "Drops outside, onto the dragged card and Esc save nothing; dragging near the edge scrolls a long list.",
                "The ⋯ menu moves up/down and is disabled at the ends; search turns drag and menu moves off and says why."]);
        }
        finally { window.Close(); }
    }

    private static async Task Settle(Window window)
    {
        await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
    }

    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (int index = 0; index < VisualTreeHelper.GetChildrenCount(parent); index++)
        {
            var child = VisualTreeHelper.GetChild(parent, index);
            yield return child;
            foreach (var descendant in Descendants(child)) yield return descendant;
        }
    }
}
