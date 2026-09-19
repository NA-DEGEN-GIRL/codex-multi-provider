using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class LayoutSelfTest
{
    internal static async Task RunAsync(string root, string report)
    {
        // Synthetic labels only. This path never connects to the backend or opens Codex.
        using var fixture = JsonDocument.Parse("""
        {
          "profiles":[
            {"id":"fixture-01","alias":"01 · 개인 개발 · 아주 긴 계정 이름도 한 줄에 표시","status":"ready","runtime_state":{"opened_task":{"thread_id":"layout-task","title":"작업 공간 UI 개선"}},"policy":{"enabled":true,"model_ids":["a","b"],"desired_revision":1,"effective_revision":1},"usage":{"windows":[{"label":"5시간","remaining_percent":72},{"label":"주간","remaining_percent":46,"resets_at":1800000000}],"reset_credits":{"available":0,"expires_at":null}}},
            {"id":"fixture-02","alias":"02 · 작업용","status":"running","policy":{"enabled":false,"model_ids":[],"desired_revision":1,"effective_revision":1},"usage":{"windows":[{"label":"5시간","remaining_percent":28},{"label":"주간","remaining_percent":81,"resets_at":1800000000}],"reset_credits":{"available":2,"expires_at":null}}},
            {"id":"fixture-03","alias":"03 · 외부 모델","auth_mode":"external","external_model_name":"Example Model","status":"stopped","policy":{"enabled":true,"selection_mode":"external_only","model_ids":["a"],"desired_revision":2,"effective_revision":1},"usage":{"windows":[{"label":"주간","remaining_percent":50}],"reset_credits":{"available":9}}}
          ],
          "shortcuts":[
            {"id":"task-01","alias":"Codex 작업 공간 개발","profile_id":"fixture-01","host_id":"local"},
            {"id":"task-02","alias":"서버 API 정리","profile_id":"fixture-01","host_id":"remote-dev"},
            {"id":"task-03","alias":"모델 조합 실험","profile_id":"fixture-01","host_id":"local"}
          ],
          "startup_updates":{"state":"attention","worker_active":false,
            "message":"전체 프로필 3개 · 최신/다음 실행 준비 2개 · 적용 대기 0개 · 확인 필요 1개",
            "profiles":[
              {"profile_id":"fixture-01","alias":"01 · 개인 개발","state":"current"},
              {"profile_id":"fixture-02","alias":"02 · 작업용","state":"attention","message":"구버전 실행 상태 확인 필요"},
              {"profile_id":"fixture-03","alias":"03 · 예비","state":"latest_on_open"}]},
          "updates":{"status":"installed_newer","message":"설치된 Codex가 공식 다운로드보다 새 버전입니다. 이전 버전으로 바꾸지 않습니다."}
        }
        """);
        var fixtureNotes = new[] {
            new TaskNote { Title = "업데이트 준비", Body = "프로필 카드와 같은 느낌으로\n작업 도구와 메모를 정리합니다.\n\n다음 확인 사항을 함께 남겨 두세요.", Items = [
                new() { Text = "제목과 버튼 간격 맞추기", Done = true },
                new() { Text = "작은 창에서 메모와 채팅 영역 확인" },
                new() { Text = "공유 메모와 체크 항목 유지" }] },
            new TaskNote { Title = "아이디어" }
        };
        var window = new MainWindow(root, fixture: true, fixtureRequest: (command, _) => command == "notes.list"
            ? Task.FromResult(JsonSerializer.SerializeToElement(new { notes = fixtureNotes, shared = true }, NoteDrafts.Json))
            : throw new InvalidOperationException("Unexpected fixture request: " + command)) { WindowState = WindowState.Normal, Width = 1440, Height = 960,
            Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        window.UseFixture(fixture.RootElement.Clone());
        window.Show();
        await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
        var layout = (FrameworkElement)window.Content;
        var sidebar = Descendants(layout).OfType<Grid>().Single(grid => grid.ColumnDefinitions.Count == 2
            && grid.ColumnDefinitions[0].Width == new GridLength(304)).Children.OfType<Grid>().First();
        if (Math.Abs(sidebar.ActualWidth - 304) > 0.5) throw new InvalidOperationException("Sidebar width does not match its 304-DIP layout.");
        var controls = Descendants(layout).ToArray();
        var badges = controls.OfType<TextBlock>().Where(block => block.Text.StartsWith("하위 에이전트 · ")).ToArray();
        if (badges.Length != 2 || !badges.Any(b => b.Text == "하위 에이전트 · 혼합") || !badges.Any(b => b.Text == "하위 에이전트 · 외부 전용 · 대기"))
            throw new InvalidOperationException("Profile policy badges were not rendered by the real list template.");
        foreach (var badge in badges) AssertVisible(badge, layout);
        AssertActionsReachable(layout);
        AssertProfileCards(layout);
        // Exercise real bindings after a count-only refresh; all these values are
        // unknown, while the baseline fixture proves that integer zero is known.
        foreach (var count in new double?[] { null, 1.5, -1 })
        {
            var variant = JsonNode.Parse(fixture.RootElement.GetRawText())!;
            variant["profiles"]![0]!["usage"]!["reset_credits"] = count is null ? null : new JsonObject { ["available"] = count.Value };
            window.UseFixture(JsonSerializer.SerializeToElement(variant));
            window.UpdateLayout();
            var unknown = Card(layout, "fixture-01");
            var text = Descendants(unknown).OfType<TextBlock>().Single(block => block.Text == "리딤 확인 안 됨");
            AssertVisible(text, layout);
            if (Descendants(unknown).OfType<TextBlock>().Any(block => block.IsVisible && block.Text == "리딤 0회"))
                throw new InvalidOperationException("An unknown reset-credit count was displayed as zero.");
        }
        window.UseFixture(fixture.RootElement.Clone());
        window.UpdateLayout();
        AssertProfileCards(layout);
        var settings = controls.OfType<Expander>().Single(expander => Equals(expander.Header, "설정 및 관리"));
        var details = controls.OfType<Expander>().Single(expander => Equals(expander.Header, "연결 상세"));
        if (settings.IsExpanded || details.IsExpanded || controls.OfType<TaskNotesPanel>().Any(panel => panel.IsVisible)
            || controls.OfType<TextBox>().Any(box => box.IsReadOnly && box.AcceptsReturn && box.IsVisible))
            throw new InvalidOperationException("Secondary panels must start collapsed.");
        AssertVisible(settings, layout);
        settings.IsExpanded = true;
        window.UpdateLayout();
        var update = Descendants(layout).OfType<Button>().Single(button => Equals(button.Content, "전체 프로필 업데이트"));
        AssertVisible(update, layout);
        settings.IsExpanded = false;
        window.UpdateLayout();
        var png = Path.ChangeExtension(report, ".png");
        var sidebarPng = Path.Combine(Path.GetDirectoryName(report)!, Path.GetFileNameWithoutExtension(report) + "-sidebar.png");
        SaveImage(layout, png);
        SaveImage(sidebar, sidebarPng);
        var notesPanel = Descendants(layout).OfType<TaskNotesPanel>().Single();
        var notesToggle = Descendants(layout).OfType<Button>().Single(button => button.Name == "ToggleTaskNotes");
        notesToggle.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        await notesPanel.SelectTaskAsync(new(new("local", "layout-notes-fixture"), "작업 공간 UI 개선"));
        await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
        AssertNotesLayout(layout, notesPanel);
        var notesPng = Path.Combine(Path.GetDirectoryName(report)!, Path.GetFileNameWithoutExtension(report) + "-notes.png");
        SaveImage(layout, notesPng);
        var notesDetailPng = Path.Combine(Path.GetDirectoryName(report)!, Path.GetFileNameWithoutExtension(report) + "-notes-detail.png");
        SaveImage(notesPanel, notesDetailPng);
        // Simulate a wide user-resized notes panel, then shrink the manager.
        var notesGrid = (Grid)notesPanel.Parent;
        notesGrid.ColumnDefinitions[2].Width = new GridLength(620);
        window.Width = 1024; window.Height = 840;
        await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
        AssertActionsReachable(layout);
        AssertProfileCards(layout);
        AssertNotesLayout(layout, notesPanel);
        var logToggle = Descendants(layout).OfType<Button>().Single(button => button.Name == "ToggleWorkspaceLog");
        logToggle.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        window.UpdateLayout();
        AssertVisible(Descendants(layout).OfType<TextBox>().Single(box => box.Name == "WorkspaceLogOutput"), layout);
        AssertNotesLayout(layout, notesPanel);
        var narrowPng = Path.Combine(Path.GetDirectoryName(report)!, Path.GetFileNameWithoutExtension(report) + "-compact.png");
        SaveImage(layout, narrowPng);
        // A crowded tab strip must stay horizontally scrollable without taking
        // away the document. Selecting a late tab must reveal its label.
        fixtureNotes = Enumerable.Range(1, 14).Select(index => new TaskNote { Title = $"검토 메모 {index} · 긴 메모 이름도 표시", Body = "테스트 메모" }).ToArray();
        await notesPanel.SelectTaskAsync(new(new("local", "layout-many-notes"), "긴 작업 제목도 한 줄로 표시되는지 확인하는 작업"));
        window.UpdateLayout();
        var tabScroll = Descendants(notesPanel).OfType<ScrollViewer>().Single(scroll => scroll.Name == "NoteTabs");
        var lastTab = Descendants(tabScroll).OfType<Button>().Last();
        lastTab.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
        if (tabScroll.ScrollableWidth <= 0 || tabScroll.HorizontalOffset <= 0 || tabScroll.ActualHeight > 46)
            throw new InvalidOperationException("Many notes do not remain accessible in the compact tab strip.");
        AssertNotesLayout(layout, notesPanel);
        // Closing from the panel must restore the native viewport's allocation.
        Descendants(notesPanel).OfType<Button>().Single(button => button.Name == "CloseTaskNotes").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        window.UpdateLayout();
        if (notesPanel.IsVisible || notesGrid.ColumnDefinitions[2].ActualWidth > 0)
            throw new InvalidOperationException("Closing notes did not return the space to the workspace.");
        settings.IsExpanded = true;
        window.UpdateLayout();
        AssertVisible(update, layout);
        foreach (var badge in Descendants(layout).OfType<TextBlock>().Where(block => block.Text.StartsWith("하위 에이전트 · ")))
            AssertVisible(badge, layout);
        File.WriteAllText(report, JsonSerializer.Serialize(new { ok = true, width = layout.ActualWidth, height = layout.ActualHeight, sidebar_dip = sidebar.ActualWidth,
            grouped_actions_reachable = true, secondary_panels_collapsed = true, minimum_window_checked = true,
            zero_positive_unknown_credits_checked = true, malformed_credits_unknown = true, external_api_quota_hidden = true,
            long_alias_ellipsis = true, quota_and_reset_values_single_line = true, selection_retained = true,
            notes_open_close_checked = true, compact_notes_and_log_checked = true, wide_notes_resize_checked = true, many_note_tabs_accessible = true,
            source = "synthetic WPF controls only; no live profile or Codex process", png, sidebar_png = sidebarPng, notes_png = notesPng, notes_detail_png = notesDetailPng, compact_png = narrowPng }, new JsonSerializerOptions { WriteIndented = true }));
        window.Close();
    }

    private static void AssertActionsReachable(FrameworkElement layout)
    {
        var controls = Descendants(layout).ToArray();
        foreach (var name in new[] { "ProfileActions", "ShortcutActions", "AddProfile", "RefreshProfiles", "AddShortcut" })
        {
            var button = controls.OfType<Button>().Single(control => control.Name == name);
            if (string.IsNullOrWhiteSpace(button.ToolTip?.ToString()))
                throw new InvalidOperationException("A sidebar icon has no accessible action description: " + name);
            AssertVisible(button, layout);
        }
        foreach (var label in new[] { "진행 기록", "로그 복사" })
            AssertVisible(controls.OfType<Button>().Single(button => Equals(button.Content, label)), layout);
        foreach (var name in new[] { "ToggleTaskNotes", "RestoreWorkspaceView", "WorkspaceConnections" })
            AssertVisible(controls.OfType<Button>().Single(button => button.Name == name), layout);
    }

    private static void AssertNotesLayout(FrameworkElement layout, TaskNotesPanel panel)
    {
        AssertActionsReachable(layout);
        var controls = Descendants(panel).ToArray();
        foreach (var name in new[] { "CloseTaskNotes", "AddNote", "NoteOptions", "ForkNote", "AddNoteCheck" })
            AssertVisible(controls.OfType<Button>().Single(button => button.Name == name), layout);
        AssertVisible(controls.OfType<TextBox>().Single(box => box.Name == "NoteBody"), layout);
        var grid = (Grid)panel.Parent;
        if (grid.ColumnDefinitions[0].ActualWidth < 419 || panel.ActualWidth < 279)
            throw new InvalidOperationException("Notes resizing leaves too little room for the task or notes tools.");
        var tools = Descendants(layout).OfType<WrapPanel>().Single(row => row.Name == "WorkspaceTools");
        foreach (Button button in tools.Children)
        {
            if (!button.IsVisible) continue;
            var buttonBounds = button.TransformToAncestor(layout).TransformBounds(new Rect(button.RenderSize));
            var panelBounds = panel.TransformToAncestor(layout).TransformBounds(new Rect(panel.RenderSize));
            if (buttonBounds.IntersectsWith(panelBounds)) throw new InvalidOperationException("A workspace tool overlaps the notes panel.");
        }
    }

    private static ListBoxItem Card(FrameworkElement layout, string id)
    {
        var list = Descendants(layout).OfType<ListBox>().Single(box => box.Items.OfType<Choice>().Any(choice => choice.Id == id));
        var choice = list.Items.OfType<Choice>().Single(item => item.Id == id);
        return list.ItemContainerGenerator.ContainerFromItem(choice) as ListBoxItem
            ?? throw new InvalidOperationException("Profile card was not realized: " + id);
    }

    private static void AssertProfileCards(FrameworkElement layout)
    {
        foreach (var (id, percent, redeem) in new[] { ("fixture-01", "46%", "리딤 0회"), ("fixture-02", "81%", "리딤 2회") })
        {
            var card = Card(layout, id);
            var blocks = Descendants(card).OfType<TextBlock>().ToArray();
            foreach (var expected in new[] { "주간 남음", percent, redeem })
                AssertVisible(blocks.Single(block => block.Text == expected), layout);
            var meter = Descendants(card).OfType<ProgressBar>().Single();
            if (!meter.IsVisible || meter.ActualWidth <= 0 || meter.Value != (id == "fixture-01" ? 46 : 81))
                throw new InvalidOperationException("The quota meter does not represent the displayed weekly percentage.");
            foreach (var block in blocks.Where(block => BindingOperations.GetBinding(block, TextBlock.TextProperty)?.Path.Path
                is "Card.QuotaLabel" or "Card.QuotaText" or "Card.ResetText" or "Card.RedeemText"))
            {
                if (block.TextWrapping != TextWrapping.NoWrap)
                    throw new InvalidOperationException("A quota or reset value can wrap within its text: " + block.Text);
                AssertVisible(block, layout);
            }
        }
        var selected = Card(layout, "fixture-01");
        if (!selected.IsSelected) throw new InvalidOperationException("The selected profile was lost during card refresh.");
        var name = Descendants(selected).OfType<TextBlock>().Single(block => block.Text.StartsWith("01 · 개인 개발 · "));
        if (name.TextTrimming != TextTrimming.CharacterEllipsis || name.TextWrapping != TextWrapping.NoWrap || name.ActualWidth <= 0
            || name.ToolTip?.ToString() != name.Text)
            throw new InvalidOperationException("A long profile alias does not retain a single-line ellipsis and full tooltip.");
        AssertVisible(name, layout);
        var external = Card(layout, "fixture-03");
        var externalBlocks = Descendants(external).OfType<TextBlock>().Where(block => block.IsVisible).ToArray();
        if (!externalBlocks.Any(block => block.Text == "API") || !externalBlocks.Any(block => block.Text == "Example Model")
            || externalBlocks.Any(block => block.Text.Contains("리딤") || block.Text.EndsWith("%") || block.Text.EndsWith(" 초기화")))
            throw new InvalidOperationException("An external API card exposes native quota or reset-credit information.");
        if (Descendants(external).OfType<ProgressBar>().Any(meter => meter.IsVisible))
            throw new InvalidOperationException("An external API card exposes a native quota meter.");
    }

    private static void SaveImage(FrameworkElement element, string path)
    {
        var bitmap = new RenderTargetBitmap((int)Math.Ceiling(element.ActualWidth), (int)Math.Ceiling(element.ActualHeight), 96, 96, PixelFormats.Pbgra32);
        // Render at the origin even when the element is in a right-hand column.
        var visual = new DrawingVisual();
        var offset = VisualTreeHelper.GetOffset(element);
        using (var drawing = visual.RenderOpen())
            drawing.DrawRectangle(new VisualBrush(element) { ViewboxUnits = BrushMappingMode.Absolute,
                Viewbox = new Rect(offset.X, offset.Y, element.ActualWidth, element.ActualHeight) }, null,
                new Rect(0, 0, element.ActualWidth, element.ActualHeight));
        bitmap.Render(visual);
        using var file = File.Create(path);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));
        encoder.Save(file);
    }

    private static void AssertVisible(FrameworkElement control, FrameworkElement layout)
    {
        var bounds = control.TransformToAncestor(layout).TransformBounds(new Rect(control.RenderSize));
        if (!control.IsVisible || bounds.Top < 0 || bounds.Bottom > layout.ActualHeight || bounds.Left < 0 || bounds.Right > layout.ActualWidth)
            throw new InvalidOperationException("A workspace action is clipped outside the window: " + (control is ContentControl content ? content.Content : control.GetType().Name));
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
