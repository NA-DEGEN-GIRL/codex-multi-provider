using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Media;
using System.Windows.Media.Imaging;

using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class LayoutSelfTest
{
    private const BindingFlags Private = BindingFlags.Instance | BindingFlags.NonPublic;

    internal static async Task RunAsync(string root, string report)
    {
        var compatibilityChecks = ManagerUpdateCompatibilitySelfTest.Run();
        // Synthetic labels only. This path never connects to the backend or opens Codex.
        var fixtureRoot = Path.Combine(Path.GetTempPath(), "codex-layout-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(fixtureRoot);
        using var fixture = JsonDocument.Parse("""
        {
          "profiles":[
            {"id":"fixture-01","alias":"01 · 개인 개발 · 아주 긴 계정 이름도 한 줄에 표시","status":"ready","runtime_state":{"opened_task":{"thread_id":"layout-task","title":"작업 공간 UI 개선"}},"remote_bindings":[{"alias":"remote-host","prepared":true},{"alias":"lab-gpu","prepared":true}],"policy":{"enabled":true,"model_ids":["a","b"],"desired_revision":1,"effective_revision":1},"usage":{"windows":[{"label":"5시간","remaining_percent":72},{"label":"주간","remaining_percent":46,"resets_at":1800000000}],"reset_credits":{"available":0,"expires_at":null}}},
            {"id":"fixture-02","alias":"02 · 작업용","status":"running","policy":{"enabled":false,"model_ids":[],"desired_revision":1,"effective_revision":1},"usage":{"windows":[{"label":"5시간","remaining_percent":28},{"label":"주간","remaining_percent":81,"resets_at":1800000000}],"reset_credits":{"available":2,"expires_at":null}}},
            {"id":"fixture-03","alias":"03 · 외부 모델","auth_mode":"external","external_model_name":"Example Model","status":"stopped","policy":{"enabled":true,"selection_mode":"external_only","model_ids":["a"],"desired_revision":2,"effective_revision":1},"usage":{"windows":[{"label":"주간","remaining_percent":50}],"reset_credits":{"available":9}}}
          ],
          "shortcuts":[
            {"id":"task-01","alias":"Codex 작업 공간 개발","profile_id":"fixture-01","host_id":"local"},
            {"id":"task-02","alias":"서버 API 정리","profile_id":"fixture-01","host_id":"remote-dev"},
            {"id":"task-03","alias":"모델 조합 실험 · 긴 작업 제목은 두 줄까지 줄바꿈해 표시합니다","profile_id":"fixture-02","host_id":"local"}
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
        var unexpectedRequests = new List<string>();
        Task<JsonElement> FixtureRequest(string command, object? args)
        {
            if (command == "notes.list") return Task.FromResult(JsonSerializer.SerializeToElement(new { notes = fixtureNotes, shared = true }, NoteDrafts.Json));
            unexpectedRequests.Add(command);
            throw new InvalidOperationException("Unexpected fixture request: " + command);
        }
        var window = new MainWindow(fixtureRoot, fixture: true, fixtureRequest: FixtureRequest) { WindowState = WindowState.Normal, Width = 1440, Height = 960,
            Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        window.UseFixture(fixture.RootElement.Clone());
        window.Show();
        await Settle(window);
        var layout = (FrameworkElement)window.Content;
        var grid = Named<Grid>(layout, "WorkspaceLayout");
        var directory = Path.GetDirectoryName(report)!;
        string Png(string suffix) => Path.Combine(directory, Path.GetFileNameWithoutExtension(report) + suffix + ".png");

        // Default: a 64-DIP rail and the docked 280-DIP shortcut column.
        Require(Math.Abs(grid.ColumnDefinitions[0].ActualWidth - 64) < 0.5, "The profile list does not start as the 64-DIP rail.");
        Require(Math.Abs(grid.ColumnDefinitions[1].ActualWidth - SidebarLayout.DefaultShortcutWidth) < 0.5, "The shortcut column does not start at 280 DIP.");
        Require(grid.ColumnDefinitions[3].ActualWidth >= MainWindow.MinWorkspaceWidth, "The workspace lost its minimum width beside the docked column.");
        AssertActionsReachable(window, layout);
        AssertRail(window, layout);
        var tooltipPng = Png("-rail-tooltip");
        AssertRailTooltip(window, tooltipPng);
        AssertSummary(window, layout, "46%", "리딤 0회");
        AssertShortcutCards(window, layout);
        AssertShortcutFilter(window);
        AssertUpdateDetails(window, fixture.RootElement);
        // Exercise real bindings after a count-only refresh; all these values are
        // unknown, while the baseline fixture proves that integer zero is known.
        foreach (var count in new double?[] { null, 1.5, -1 })
        {
            var variant = JsonNode.Parse(fixture.RootElement.GetRawText())!;
            variant["profiles"]![0]!["usage"]!["reset_credits"] = count is null ? null : new JsonObject { ["available"] = count.Value };
            window.UseFixture(JsonSerializer.SerializeToElement(variant));
            window.UpdateLayout();
            AssertSummary(window, layout, "46%", "리딤 확인 안 됨");
            if (VisibleTexts(Named<ContentControl>(layout, "ProfileUsageHost")).Contains("리딤 0회"))
                throw new InvalidOperationException("An unknown reset-credit count was displayed as zero.");
        }
        window.UseFixture(fixture.RootElement.Clone());
        window.UpdateLayout();
        AssertRail(window, layout);
        var details = Descendants(layout).OfType<Button>().Single(button => button.Name == "WorkspaceDetails");
        var detailsPanel = Named<Border>(layout, "WorkspaceDetailsPanel");
        if (Field<Popup>(window, "_settingsFlyout").IsOpen || detailsPanel.IsVisible || Descendants(layout).OfType<TaskNotesPanel>().Any(panel => panel.IsVisible)
            || Descendants(layout).OfType<TextBox>().Any(box => box.IsReadOnly && box.AcceptsReturn && box.IsVisible))
            throw new InvalidOperationException("Secondary panels must start collapsed.");
        details.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        window.UpdateLayout();
        Require(detailsPanel.IsVisible && Descendants(detailsPanel).OfType<TextBlock>().Any(block => block.IsVisible && block.Text.Length > 0),
            "연결 상세 did not open the connection details.");
        details.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        window.UpdateLayout();
        Require(!detailsPanel.IsVisible, "연결 상세 did not close again.");

        // Settings flyout: every former sidebar action plus the update notice.
        var compatibility = Field<ManagerUpdatePanel>(window, "_managerUpdates");
        compatibility.Present(new(ManagerUpdateState.Ready, "작업 유지하며 업데이트 가능",
            "새 관리창이 준비되었습니다. X로 창만 닫고 다시 실행하세요."));
        window.UpdateLayout();
        AssertUpdateNotice(window, layout, visible: true);
        var flyout = (FrameworkElement)Field<Popup>(window, "_settingsFlyout").Child;
        AssertSettingsFlyout(flyout, Png("-settings"));
        compatibility.Present(new(ManagerUpdateState.Current, "관리창 최신 · 작업 유지 가능", "X는 창만 닫습니다. 작업까지 끝내려면 완전 종료를 누르세요."));
        window.UpdateLayout();
        AssertUpdateNotice(window, layout, visible: false);
        compatibility.Present(new(ManagerUpdateState.Ready, "작업 유지하며 업데이트 가능",
            "새 관리창이 준비되었습니다. X로 창만 닫고 다시 실행하세요."));
        window.UpdateLayout();
        var png = Path.ChangeExtension(report, ".png");
        var sidebarPng = Png("-sidebar");
        SaveImage(layout, png);
        SaveImage(layout, sidebarPng, new Rect(0, 0, grid.ColumnDefinitions[0].ActualWidth + grid.ColumnDefinitions[1].ActualWidth
            + grid.ColumnDefinitions[2].ActualWidth, layout.ActualHeight));

        // Expanded profile cards, persisted with the sidebar layout.
        var expandedPng = Png("-expanded");
        await AssertExpandedAsync(window, layout, fixtureRoot, fixture.RootElement, FixtureRequest, expandedPng);

        var notesPanel = Descendants(layout).OfType<TaskNotesPanel>().Single();
        var notesToggle = Descendants(layout).OfType<Button>().Single(button => button.Name == "ToggleTaskNotes");
        notesToggle.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        await notesPanel.SelectTaskAsync(new(new("local", "layout-notes-fixture"), "작업 공간 UI 개선"));
        await Settle(window);
        AssertNotesLayout(window, layout, notesPanel);
        var notesPng = Png("-notes");
        SaveImage(layout, notesPng);
        var notesDetailPng = Png("-notes-detail");
        SaveImage(notesPanel, notesDetailPng);
        // Simulate a wide user-resized notes panel, then shrink the manager: the
        // shortcut column becomes an overlay and the workspace keeps its width.
        var notesGrid = (Grid)notesPanel.Parent;
        notesGrid.ColumnDefinitions[2].Width = new GridLength(620);
        window.Width = 1024; window.Height = 840;
        await Settle(window);
        AssertCompact(window, layout, grid);
        AssertActionsReachable(window, layout);
        AssertNotesLayout(window, layout, notesPanel);
        var logToggle = Descendants(layout).OfType<Button>().Single(button => button.Name == "ToggleWorkspaceLog");
        foreach (var notice in new[] {
            new ManagerUpdateNotice(ManagerUpdateState.NeedsStop, "작업 종료 후 적용 필요", "새 버전과 실행 중인 서비스가 호환되지 않습니다. 작업을 마친 뒤 완전 종료하세요."),
            ManagerUpdateNotice.Unknown("업데이트 정보를 읽지 못했습니다."),
            new ManagerUpdateNotice(ManagerUpdateState.Deferred, "관리창 최신 · 서비스 적용 대기", "작업은 계속됩니다. 서비스 업데이트는 완전 종료 후 적용됩니다.") })
        {
            compatibility.Present(notice); window.UpdateLayout();
            AssertUpdateNotice(window, layout, visible: ManagerUpdatePanel.NeedsAttention(notice));
            var refresh = Descendants(compatibility).OfType<Button>().Single();
            refresh.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        }
        logToggle.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        window.UpdateLayout();
        AssertVisible(Descendants(layout).OfType<TextBox>().Single(box => box.Name == "WorkspaceLogOutput"), layout);
        AssertNotesLayout(window, layout, notesPanel);
        var narrowPng = Png("-compact");
        SaveImage(layout, narrowPng);
        var overlayPng = Png("-overlay");
        AssertOverlay(window, layout, overlayPng);
        // A crowded tab strip must stay horizontally scrollable without taking
        // away the document. Selecting a late tab must reveal its label.
        fixtureNotes = Enumerable.Range(1, 14).Select(index => new TaskNote { Title = $"검토 메모 {index} · 긴 메모 이름도 표시", Body = "테스트 메모" }).ToArray();
        await notesPanel.SelectTaskAsync(new(new("local", "layout-many-notes"), "긴 작업 제목도 한 줄로 표시되는지 확인하는 작업"));
        window.UpdateLayout();
        var tabScroll = Descendants(notesPanel).OfType<ScrollViewer>().Single(scroll => scroll.Name == "NoteTabs");
        var lastTab = Descendants(tabScroll).OfType<Button>().Last();
        lastTab.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        await Settle(window);
        if (tabScroll.ScrollableWidth <= 0 || tabScroll.HorizontalOffset <= 0 || tabScroll.ActualHeight > 46)
            throw new InvalidOperationException("Many notes do not remain accessible in the compact tab strip.");
        AssertNotesLayout(window, layout, notesPanel);
        // Closing from the panel must restore the native viewport's allocation.
        Descendants(notesPanel).OfType<Button>().Single(button => button.Name == "CloseTaskNotes").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        window.UpdateLayout();
        if (notesPanel.IsVisible || notesGrid.ColumnDefinitions[2].ActualWidth > 0)
            throw new InvalidOperationException("Closing notes did not return the space to the workspace.");
        await AssertNotesPerProfileAsync(window, fixtureRoot, fixture.RootElement, FixtureRequest);
        var crowdedPng = Png("-crowded");
        await AssertSidebarSplitAsync(window, fixtureRoot, fixture.RootElement, FixtureRequest, crowdedPng);
        if (unexpectedRequests.Count != 0) throw new InvalidOperationException("Layout interactions issued backend requests: " + string.Join(", ", unexpectedRequests));
        File.WriteAllText(report, JsonSerializer.Serialize(new { ok = true, width = layout.ActualWidth, height = layout.ActualHeight,
            rail_dip = 64, expanded_profiles_dip = 304, shortcut_column_dip = SidebarLayout.DefaultShortcutWidth, min_workspace_dip = MainWindow.MinWorkspaceWidth,
            grouped_actions_reachable = true, secondary_panels_collapsed = true, minimum_window_checked = true,
            rail_avatars_rings_status_and_selection = true, rail_tooltip_details = true, header_summary_meters_and_chips = true,
            zero_positive_unknown_credits_checked = true, malformed_credits_unknown = true, external_api_quota_hidden = true,
            expanded_cards_wrap_names = true, expanded_state_persisted = true, quota_and_reset_values_single_line = true, selection_retained = true,
            shortcut_cards_two_lines = true, shortcut_filter_checked = true, compact_overlay_checked = true,
            settings_flyout_actions_and_badge = true, update_banner_states_checked = true,
            notes_open_close_checked = true, notes_per_profile_checked = true, compact_notes_and_log_checked = true, wide_notes_resize_checked = true, many_note_tabs_accessible = true,
            compatibility_states_and_refresh_reachable = true, compatibility_inspection_checks = compatibilityChecks,
            update_blockers_expand_scroll_and_identify_profiles = true,
            shortcut_column_drag_checked = true, independent_list_scrolling = true, sidebar_selection_preserved = true,
            sidebar_extremes_and_compact_settings_checked = true, sidebar_width_reloaded = true, sidebar_scroll_preserved_on_refresh = true,
            source = "synthetic WPF controls only; no live profile or Codex process", png, sidebar_png = sidebarPng, expanded_png = expandedPng,
            settings_png = Png("-settings"), rail_tooltip_png = tooltipPng, crowded_png = crowdedPng, notes_png = notesPng, notes_detail_png = notesDetailPng, compact_png = narrowPng, overlay_png = overlayPng },
            new JsonSerializerOptions { WriteIndented = true }));
        window.Close();
    }

    // Rail: one avatar per profile with its short label, the weekly ring when
    // known, a status dot, the selection indicator and a rich tooltip.
    private static void AssertRail(MainWindow window, FrameworkElement layout)
    {
        var list = Field<ListBox>(window, "_profiles");
        Require(list.ItemTemplate == ProfileCards.Rail(), "The collapsed profile list does not use the rail template.");
        foreach (var (id, label, ring) in new[] { ("fixture-01", "01", 46d), ("fixture-02", "02", 81d), ("fixture-03", "03", double.NaN) })
        {
            var item = Card(layout, id);
            AssertVisible(item, layout);
            Require(item.ActualWidth <= 64 && item.ActualHeight is >= 52 and <= 64, "A rail avatar does not fit the rail: " + id);
            var texts = Descendants(item).OfType<TextBlock>().Where(block => block.IsVisible).Select(block => block.Text).ToArray();
            Require(texts.SequenceEqual([label]), $"Rail avatar {id} shows {string.Join("|", texts)} instead of its short label.");
            var arc = Descendants(item).OfType<System.Windows.Shapes.Path>().Single();
            Require(double.IsNaN(ring) ? arc.Data.IsEmpty() || arc.Data == Geometry.Empty : !arc.Data.IsEmpty() && arc.IsVisible,
                "The rail ring does not follow the weekly remaining usage: " + id);
            var name = AutomationProperties.GetName(item);
            Require(name.StartsWith(((Choice)item.DataContext).Card.Name, StringComparison.Ordinal)
                && (double.IsNaN(ring) || name.Contains($"주간 남음 {ring:0}%")), "A rail avatar has no descriptive accessible name: " + name);
            var avatar = Descendants(item).OfType<Grid>().First(g => Equals(g.Tag, "ProfileDragItem"));
            Require(avatar.ToolTip is ToolTip { ContentTemplate: not null }, "A rail avatar has no rich tooltip.");
        }
        var selected = Card(layout, "fixture-01");
        Require(selected.IsSelected && Descendants(selected).OfType<Border>().Any(border => border.Width == 3 && border.IsVisible),
            "The selected profile has no rail indicator.");
        Require(!Descendants(Card(layout, "fixture-02")).OfType<Border>().Any(border => border.Width == 3 && border.IsVisible),
            "An unselected profile shows the selection indicator.");
    }

    // The hover card of a rail avatar, laid out offscreen exactly as its
    // tooltip instantiates it: everything the collapsed rail leaves out.
    private static void AssertRailTooltip(MainWindow window, string png)
    {
        var choice = Field<ListBox>(window, "_profiles").Items.OfType<Choice>().Single(item => item.Id == "fixture-01");
        var tip = new Border { Background = WorkspaceAppearance.Color("#30343D"), BorderBrush = WorkspaceAppearance.Color("#596273"),
            BorderThickness = new Thickness(1), Padding = new Thickness(10), CornerRadius = new CornerRadius(5), MaxWidth = 360,
            Child = new ContentControl { Content = choice, ContentTemplate = (DataTemplate)ProfileCards.Rail().Resources["RailTip"] } };
        Layout(tip, 360, double.PositiveInfinity);
        var texts = Descendants(tip).OfType<TextBlock>().Where(block => block.ActualWidth > 0).Select(block => block.Text).ToArray();
        foreach (var expected in new[] { choice.Card.Name, "준비됨", "GPT", "주간 남음", "46%", "5시간 남음", "72%", "리딤 0회",
            "하위 에이전트 · 혼합", "SSH 연결 · remote-host, lab-gpu" })
            Require(texts.Contains(expected), $"The rail tooltip does not show '{expected}': {string.Join("|", texts)}");
        Require(texts.Any(text => text.EndsWith(" 초기화", StringComparison.Ordinal)), "The rail tooltip has no reset time.");
        SaveImage(tip, png);
    }

    // The selected profile in the header: name, status, provider, agent chip,
    // SSH hosts, both usage windows with numbers, reset and redeem.
    private static void AssertSummary(MainWindow window, FrameworkElement layout, string weekly, string redeem)
    {
        var summary = Named<ContentControl>(layout, "ProfileSummaryHost");
        var usage = Named<ContentControl>(layout, "ProfileUsageHost");
        AssertVisible(summary, layout); AssertVisible(usage, layout);
        var blocks = Descendants(summary).Concat(Descendants(usage)).OfType<TextBlock>().Where(block => block.IsVisible).ToArray();
        var texts = blocks.Select(block => block.Text).ToArray();
        foreach (var expected in new[] { "01", "준비됨", "GPT", "하위 에이전트 · 혼합", "SSH · remote-host +1", "주간 남음", weekly, "5시간 남음", "72%", redeem })
            Require(texts.Contains(expected), $"The header summary does not show '{expected}': {string.Join("|", texts)}");
        Require(texts.Any(text => text.EndsWith(" 초기화", StringComparison.Ordinal)), "The header summary has no reset time.");
        var name = blocks.Single(block => block.Text.StartsWith("01 · 개인 개발 · "));
        Require(name.TextTrimming == TextTrimming.CharacterEllipsis && name.TextWrapping == TextWrapping.NoWrap && Equals(name.ToolTip, name.Text)
            && name.FontSize >= 16, "The header profile name is not a prominent single line with its full name in a tooltip.");
        var meters = Descendants(usage).OfType<ProgressBar>().Where(meter => meter.IsVisible).Select(meter => meter.Value).ToArray();
        // The usage line is a single clipped row: every value must fit inside it.
        foreach (var block in Descendants(usage).OfType<TextBlock>().Where(block => block.IsVisible && block.Text.Length > 0))
        {
            var bounds = block.TransformToAncestor(usage).TransformBounds(new Rect(block.RenderSize));
            Require(bounds.Right <= usage.ActualWidth + 0.5, "A header usage value is clipped: " + block.Text);
        }
        Require(meters.SequenceEqual([46d, 72d]), "The header meters do not show the weekly and 5-hour windows: " + string.Join(",", meters));
        foreach (var block in blocks.Where(block => block.Text.EndsWith('%') || block.Text.EndsWith(" 초기화") || block.Text.StartsWith("리딤")))
            Require(block.TextWrapping == TextWrapping.NoWrap && block.FontSize >= 12, "A header usage value can wrap or is too small: " + block.Text);
        Require(Named<TextBlock>(layout, "WorkspaceTask").Text == "최근 연 작업 · 작업 공간 UI 개선", "The recent task line was lost.");
        Require(!Named<TextBlock>(layout, "WorkspaceIdentity").IsVisible, "The plain identity line competes with the profile summary.");
    }

    private static void AssertShortcutCards(MainWindow window, FrameworkElement layout)
    {
        var list = Field<ListBox>(window, "_shortcuts");
        foreach (var (id, host) in new[] { ("task-01", ""), ("task-02", "remote-dev"), ("task-03", "") })
        {
            var item = (ListBoxItem)list.ItemContainerGenerator.ContainerFromItem(list.Items.OfType<Choice>().Single(choice => choice.Id == id));
            var card = ((Choice)item.DataContext).Shortcut!;
            var blocks = Descendants(item).OfType<TextBlock>().Where(block => block.IsVisible).ToArray();
            var title = blocks.Single(block => block.Text == card.Title);
            Require(title.TextWrapping == TextWrapping.Wrap && title.MaxHeight <= 40 && title.FontSize >= 14, "A shortcut title does not wrap to two lines: " + id);
            Require(blocks.Any(block => block.Text == card.StateText) && blocks.Any(block => block.Text == card.ProfileName)
                && blocks.Any(block => block.Text == card.AccountShort), "A shortcut card lost its status, profile badge or ring label: " + id);
            Require(host.Length == 0 ? !blocks.Any(block => block.Text.StartsWith("SSH")) : blocks.Any(block => block.Text == "SSH · " + host),
                $"The SSH host chip is wrong: {id} · {string.Join("|", blocks.Select(block => block.Text))}");
            Require(blocks.Where(block => block != title).All(block => block.FontSize >= 11), "Shortcut meta text is too small: " + id);
            Require(Descendants(item).OfType<Button>().Any(button => Equals(button.Tag, "menu") && button.IsVisible), "A shortcut card has no ⋯ menu: " + id);
        }
        var longTitle = Descendants(list).OfType<TextBlock>().Single(block => block.Text.StartsWith("모델 조합 실험"));
        Require(longTitle.ActualHeight > 30, "A long shortcut title did not use its second line.");
        Require(Named<TextBlock>(layout, "ShortcutCount").Text == "3", "The shortcut header does not count its links.");
    }

    // Title, account name and SSH host filter the list without changing selection.
    private static void AssertShortcutFilter(MainWindow window)
    {
        var filter = Field<TextBox>(window, "_shortcutFilter");
        var list = Field<ListBox>(window, "_shortcuts");
        var count = Field<TextBlock>(window, "_shortcutCount");
        var empty = Field<TextBlock>(window, "_shortcutEmpty");
        string[] Ids() => list.Items.OfType<Choice>().Select(choice => choice.Id).ToArray();
        foreach (var (query, expected) in new[] { ("api", new[] { "task-02" }), ("개인 개발", new[] { "task-01", "task-02" }),
            ("REMOTE", new[] { "task-02" }), ("02 작업용", new[] { "task-03" }), ("없는 작업", Array.Empty<string>()) })
        {
            filter.Text = query; window.UpdateLayout();
            Require(Ids().SequenceEqual(expected), $"Filtering '{query}' showed {string.Join(",", Ids())}.");
            Require(count.Text == $"{expected.Length} / 3", "The filtered count is wrong: " + count.Text);
            Require((expected.Length == 0) == (empty.Visibility == Visibility.Visible) && (expected.Length > 0 || empty.Text == "검색 결과가 없습니다."),
                "The empty search result is not explained.");
        }
        filter.Text = ""; window.UpdateLayout();
        Require(Ids().Length == 3 && count.Text == "3" && empty.Visibility == Visibility.Collapsed, "Clearing the filter did not restore every link.");
    }

    private static async Task AssertExpandedAsync(MainWindow window, FrameworkElement layout, string fixtureRoot, JsonElement state,
        Func<string, object?, Task<JsonElement>> fixtureRequest, string png)
    {
        var grid = Named<Grid>(layout, "WorkspaceLayout");
        Click(layout, "ToggleProfilePanel");
        await Settle(window);
        Require(Math.Abs(grid.ColumnDefinitions[0].ActualWidth - 304) < 0.5, "The profile toggle did not expand the rail into cards.");
        AssertActionsReachable(window, layout);
        AssertProfileCards(window, layout);
        Require(Named<Button>(layout, "ProfileEmailToggle").IsVisible, "The email toggle is not available in the expanded panel.");
        SaveImage(layout, png);
        using (var saved = JsonDocument.Parse(File.ReadAllText(Path.Combine(fixtureRoot, "work", "control-center", "sidebar-layout.json"))))
            Require(saved.RootElement.GetProperty("profiles_expanded").GetBoolean(), "Expanding the profiles was not saved.");
        var reopened = new MainWindow(fixtureRoot, fixture: true, fixtureRequest: fixtureRequest)
        { WindowState = WindowState.Normal, Width = 1440, Height = 960, Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        try
        {
            reopened.UseFixture(state);
            reopened.Show();
            await Settle(reopened);
            Require(Math.Abs(Named<Grid>((FrameworkElement)reopened.Content, "WorkspaceLayout").ColumnDefinitions[0].ActualWidth - 304) < 0.5,
                "A new window did not restore the expanded profile panel.");
        }
        finally { reopened.Close(); }
        Click(layout, "ToggleProfilePanel");
        await Settle(window);
        Require(Math.Abs(grid.ColumnDefinitions[0].ActualWidth - 64) < 0.5, "The profile toggle did not collapse the cards into the rail.");
        using (var saved = JsonDocument.Parse(File.ReadAllText(Path.Combine(fixtureRoot, "work", "control-center", "sidebar-layout.json"))))
            Require(!saved.RootElement.GetProperty("profiles_expanded").GetBoolean(), "Collapsing the profiles was not saved.");
        AssertRail(window, layout);
    }

    private static void AssertProfileCards(MainWindow window, FrameworkElement layout)
    {
        foreach (var (id, weekly, shortWindow, redeem) in new[] { ("fixture-01", 46d, 72d, "리딤 0회"), ("fixture-02", 81d, 28d, "리딤 2회") })
        {
            var card = Card(layout, id);
            var blocks = Descendants(card).OfType<TextBlock>().ToArray();
            foreach (var expected in new[] { "주간 남음", $"{weekly:0}%", "5시간 남음", $"{shortWindow:0}%", redeem })
                AssertScrollContentReachable(blocks.Single(block => block.Text == expected && block.IsVisible), layout);
            var meters = Descendants(card).OfType<ProgressBar>().Where(meter => meter.IsVisible).ToArray();
            if (meters.Length != 2 || meters[0].Value != weekly || meters[1].Value != shortWindow || meters.Any(meter => meter.ActualWidth <= 0))
                throw new InvalidOperationException("The quota meters do not represent the weekly and 5-hour windows.");
            foreach (var block in blocks.Where(block => BindingOperations.GetBinding(block, TextBlock.TextProperty)?.Path.Path
                is "Label" or "Text" or "Card.ResetText" or "Card.RedeemText"))
            {
                if (block.TextWrapping != TextWrapping.NoWrap)
                    throw new InvalidOperationException("A quota or reset value can wrap within its text: " + block.Text);
                if (block.IsVisible) AssertScrollContentReachable(block, layout);
            }
            Require(blocks.Any(block => block.IsVisible && block.Text == (id == "fixture-01" ? "준비됨" : "실행 중")), "A card lost its status pill: " + id);
        }
        var selected = Card(layout, "fixture-01");
        if (!selected.IsSelected) throw new InvalidOperationException("The selected profile was lost during card refresh.");
        var name = Descendants(selected).OfType<TextBlock>().Single(block => block.Text.StartsWith("01 · 개인 개발 · "));
        if (name.TextWrapping != TextWrapping.Wrap || name.MaxHeight > 40 || name.ActualHeight < 30 || name.ToolTip?.ToString() != name.Text)
            throw new InvalidOperationException("A long profile name does not wrap to two lines with its full name in a tooltip.");
        var shortName = Descendants(Card(layout, "fixture-02")).OfType<TextBlock>().Single(block => block.Text == "02 · 작업용");
        Require(shortName.ActualHeight < 24, "A short profile name took more than one line.");
        AssertScrollContentReachable(name, layout);
        AssertScrollContentReachable(Descendants(selected).OfType<TextBlock>().Single(block => block.Text == "하위 에이전트 · 혼합"), layout);
        AssertScrollContentReachable(Descendants(selected).OfType<TextBlock>().Single(block => block.Text == "SSH · remote-host +1"), layout);
        var external = Card(layout, "fixture-03");
        var externalBlocks = Descendants(external).OfType<TextBlock>().Where(block => block.IsVisible).ToArray();
        if (!externalBlocks.Any(block => block.Text == "API") || !externalBlocks.Any(block => block.Text == "Example Model")
            || externalBlocks.Any(block => block.Text.Contains("리딤") || block.Text.EndsWith("%") || block.Text.EndsWith(" 초기화")))
            throw new InvalidOperationException("An external API card exposes native quota or reset-credit information.");
        if (Descendants(external).OfType<ProgressBar>().Any(meter => meter.IsVisible))
            throw new InvalidOperationException("An external API card exposes a native quota meter.");
        AssertScrollContentReachable(Descendants(external).OfType<TextBlock>().Single(block => block.Text == "하위 에이전트 · 외부 전용 · 대기"), layout);
        Require(Descendants(Card(layout, "fixture-02")).OfType<TextBlock>().Any(block => block.IsVisible && block.Text == "업데이트 확인 필요"),
            "A profile update notice is missing from its card.");
        if (!Card(layout, "fixture-01").IsSelected) throw new InvalidOperationException("Scrolling profile cards changed the selected account.");
    }

    private static void AssertUpdateDetails(MainWindow window, JsonElement baseline)
    {
        var state = JsonNode.Parse(baseline.GetRawText())!;
        state["updates"] = JsonSerializer.SerializeToNode(new
        {
            status = "blocked", message = "업데이트 조건을 다시 확인해야 합니다.",
            blockers = Enumerable.Range(1, 12).Select(i => new
            {
                code = "jobs_not_quiescent", profile_alias = $"fixture-{i:00}",
                message = "SSH 작업이 끝났는지 확인하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요."
            }).ToArray()
        });
        window.UseFixture(JsonSerializer.SerializeToElement(state));
        var flyout = (FrameworkElement)Field<Popup>(window, "_settingsFlyout").Child;
        var details = Field<Expander>(window, "_updateDetails");
        try
        {
            details.IsExpanded = true;
            Layout(flyout, 358, double.PositiveInfinity);
            var scroll = (ScrollViewer)details.Content;
            var text = (TextBlock)scroll.Content;
            if (details.Visibility != Visibility.Visible || scroll.ScrollableHeight <= 0 ||
                !double.IsPositiveInfinity(text.MaxHeight) || !text.Text.Contains("[fixture-01]") ||
                !text.Text.Contains("[fixture-12]"))
                throw new InvalidOperationException("Update reasons must identify each profile and remain scrollable without clipped text.");
        }
        finally
        {
            details.IsExpanded = false;
            window.UseFixture(baseline); window.UpdateLayout();
        }
    }

    // Laid out offscreen: a closed popup is never shown on the desktop by a self-test.
    private static void AssertSettingsFlyout(FrameworkElement flyout, string png)
    {
        Layout(flyout, 358, double.PositiveInfinity);
        var buttons = Descendants(flyout).OfType<Button>().ToArray();
        foreach (var label in new[] { "공통 개인 스킬", "실행 프리셋", "하위 에이전트 · 모델 연결", "SSH 업데이트 · 연결 준비", "전체 프로필 업데이트",
            "Codex 앱 버전 확인", "관리 서비스 다시 연결", "Windows 실행 권한…", "완전 종료 후 관리자 실행…" })
        {
            var button = buttons.Single(item => Equals(item.Content, label));
            Require(button.ActualWidth > 200 && button.ActualHeight >= 28, "A settings action is not a full-width row: " + label);
        }
        Require(buttons.Any(button => button.Name == "WorkspaceVersion"), "The version copy action is missing from settings.");
        Require(Descendants(flyout).OfType<ManagerUpdatePanel>().Single().ActualHeight > 0, "The update compatibility panel is missing from settings.");
        Require(Descendants(flyout).OfType<TextBlock>().Any(block => block.Text.StartsWith("Windows 권한 ·")), "The execution mode is missing from settings.");
        SaveImage(flyout, png);
    }

    private static void AssertUpdateNotice(MainWindow window, FrameworkElement layout, bool visible)
    {
        var banner = Named<Border>(layout, "WorkspaceUpdateBanner");
        var badge = Field<System.Windows.Shapes.Ellipse>(window, "_settingsBadge");
        var notice = Field<ManagerUpdatePanel>(window, "_managerUpdates").Notice;
        Require(banner.IsVisible == visible, $"The header update banner visibility is wrong for {notice.State}.");
        if (visible)
        {
            AssertVisible(banner, layout);
            Require(Descendants(banner).OfType<TextBlock>().Any(block => block.Text == notice.Title), "The header banner does not name the update state.");
        }
        // The fixture's profile update attention keeps the badge on in every state.
        Require(badge.IsVisible && Named<Button>(layout, "WorkspaceSettings").ToolTip?.ToString()?.Contains("전체 프로필 업데이트 확인 필요") == true
            && (!visible || Named<Button>(layout, "WorkspaceSettings").ToolTip!.ToString()!.Contains(notice.Title)),
            "The settings badge does not summarize what needs attention.");
    }

    private static void AssertCompact(MainWindow window, FrameworkElement layout, Grid grid)
    {
        Require(grid.ColumnDefinitions[1].ActualWidth == 0 && grid.ColumnDefinitions[2].ActualWidth == 0,
            "The narrow window still docks the shortcut column beside the workspace.");
        Require(grid.ColumnDefinitions[3].ActualWidth >= layout.ActualWidth - 64 - 0.5, "The narrow window does not give the workspace the freed width.");
        var toggle = Named<Button>(layout, "ShortcutOverlayToggle");
        AssertVisible(toggle, layout);
        Require(Field<Border>(window, "_shortcutPanel").Parent == Field<Border>(window, "_overlayFrame"), "The shortcut column did not move into the overlay.");
        Require(Field<ListBox>(window, "_shortcuts").Items.Count == Field<JsonElement>(window, "_state").Arr("shortcuts").Count(),
            "The overlay lost its shortcuts.");
    }

    // The overlay content laid out at its open size, without opening the popup.
    private static void AssertOverlay(MainWindow window, FrameworkElement layout, string png)
    {
        var frame = Field<Border>(window, "_overlayFrame");
        Layout(frame, SidebarLayout.DefaultShortcutWidth + frame.Margin.Right, layout.ActualHeight);
        foreach (var name in new[] { "AddShortcut", "ShortcutActions", "CloseShortcutOverlay", "ClearShortcutFilter" })
            Require(Descendants(frame).OfType<Button>().Any(button => button.Name == name), "The overlay lost an action: " + name);
        Require(Descendants(frame).OfType<Button>().Single(button => button.Name == "CloseShortcutOverlay").Visibility == Visibility.Visible,
            "The overlay has no close button.");
        var cards = Descendants(frame).OfType<ListBoxItem>().ToArray();
        Require(cards.Length == 3 && cards.All(card => card.ActualHeight > 40), "The overlay does not lay out its shortcut cards.");
        SaveImage(frame, png);
    }

    private static async Task AssertNotesPerProfileAsync(MainWindow window, string fixtureRoot, JsonElement state,
        Func<string, object?, Task<JsonElement>> fixtureRequest)
    {
        var selected = typeof(MainWindow).GetField("_selectedProfile", Private)!;
        var render = typeof(MainWindow).GetMethod("Render", Private, Type.EmptyTypes)!;
        // The production switch path: ShowProfileCoreAsync sets the field, then renders.
        void Select(MainWindow target, string id) { selected.SetValue(target, id); render.Invoke(target, null); target.UpdateLayout(); }
        static TaskNotesPanel Panel(Window target) => Descendants((FrameworkElement)target.Content).OfType<TaskNotesPanel>().Single();
        var layout = (FrameworkElement)window.Content;
        var panel = Panel(window);
        Select(window, "fixture-01");
        Require(!panel.IsVisible, "Closing notes was not kept for the selected profile.");
        Click(layout, "ToggleTaskNotes");
        Require(panel.IsVisible, "The notes toggle did not open the selected profile's notes.");
        Select(window, "fixture-02");
        Require(!panel.IsVisible, "Opening notes in one profile opened them in another profile.");
        Select(window, "fixture-01");
        Require(panel.IsVisible, "Returning to a profile did not restore its open notes.");
        // The shared width a user dragged survives refreshes and switches between open profiles.
        var column = ((Grid)panel.Parent).ColumnDefinitions[2];
        column.Width = new GridLength(480);
        window.UseFixture(state); window.UpdateLayout();
        Require(panel.IsVisible, "A state refresh of the same profile changed its notes panel.");
        Require(Math.Abs(column.Width.Value - 480) < 0.5, "A state refresh reset the dragged notes width.");
        Select(window, "fixture-02");
        Click(layout, "ToggleTaskNotes");
        column.Width = new GridLength(480);
        Select(window, "fixture-01");
        Require(panel.IsVisible && Math.Abs(column.Width.Value - 480) < 0.5, "Switching between open profiles reset the dragged notes width.");
        Click(panel, "CloseTaskNotes");
        Require(!panel.IsVisible, "The panel close button did not close the selected profile's notes.");
        Select(window, "fixture-02");
        Require(panel.IsVisible, "Closing notes in one profile closed them in another profile.");
        var file = Path.Combine(fixtureRoot, "work", "control-center", "notes-panel.json");
        using (var saved = JsonDocument.Parse(File.ReadAllText(file)))
        {
            var profiles = saved.RootElement.GetProperty("profiles");
            Require(saved.RootElement.GetProperty("version").GetInt32() == 1 && !profiles.GetProperty("fixture-01").GetBoolean()
                && profiles.GetProperty("fixture-02").GetBoolean() && !profiles.TryGetProperty("fixture-03", out _),
                "Notes visibility was not saved as a per-profile map.");
        }
        var reopened = new MainWindow(fixtureRoot, fixture: true, fixtureRequest: fixtureRequest)
        { WindowState = WindowState.Normal, Width = 1440, Height = 960, Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        try
        {
            reopened.UseFixture(state);
            reopened.Show();
            await Settle(reopened);
            var restored = Panel(reopened);
            Require(!restored.IsVisible, "A new window opened notes for a profile that had closed them.");
            Select(reopened, "fixture-02");
            Require(restored.IsVisible, "A new window did not restore a profile's open notes.");
            Select(reopened, "fixture-03");
            Require(!restored.IsVisible, "A profile without a saved choice did not start with notes closed.");
        }
        finally { reopened.Close(); }
        // Unreadable, mistyped or future preferences start every profile closed.
        var broken = Path.Combine(fixtureRoot, "notes-panel-broken");
        Directory.CreateDirectory(Path.Combine(broken, "work", "control-center"));
        foreach (var text in new[] { "{", """{"version":1,"profiles":{"fixture-02":"yes"}}""", """{"version":2,"profiles":{"fixture-02":true}}""", """{"profiles":{"fixture-02":true}}""" })
        {
            File.WriteAllText(Path.Combine(broken, "work", "control-center", "notes-panel.json"), text);
            Require(!new NotesPanelVisibility(broken, _ => { }).IsOpen("fixture-02"), "Malformed notes preferences opened a panel: " + text);
        }
        // Leave every profile closed and the baseline profile selected.
        Click(layout, "ToggleTaskNotes");
        Require(!panel.IsVisible, "The notes toggle did not close the selected profile's notes.");
        Select(window, "fixture-01");
        Require(!panel.IsVisible, "Notes reopened after switching back to a closed profile.");
    }

    private static async Task AssertSidebarSplitAsync(MainWindow window, string fixtureRoot, JsonElement baseline,
        Func<string, object?, Task<JsonElement>> fixtureRequest, string png)
    {
        var layout = (FrameworkElement)window.Content;
        window.Width = 1440; window.Height = 960;
        var crowded = JsonNode.Parse(baseline.GetRawText())!;
        var profileRows = (JsonArray)crowded["profiles"]!;
        for (int index = 4; index <= 24; index++)
        {
            var profile = profileRows[0]!.DeepClone();
            profile["id"] = $"fixture-{index:00}";
            profile["alias"] = $"{index:00} · 스크롤 검증 계정";
            profileRows.Add(profile);
        }
        var shortcutRows = (JsonArray)crowded["shortcuts"]!;
        for (int index = 4; index <= 36; index++)
            shortcutRows.Add(new JsonObject { ["id"] = $"task-{index:00}", ["alias"] = $"검토 작업 {index}", ["profile_id"] = "fixture-01", ["host_id"] = "local" });
        // Rendering derives shortcut selection from the profile's current task.
        // A ListBox-only selection is intentionally cleared on the next refresh.
        const string selectedThread = "layout-sidebar-selected-task";
        profileRows[0]!["current_task"] = new JsonObject { ["thread_id"] = selectedThread };
        shortcutRows[1]!["thread_id"] = selectedThread;
        var state = JsonSerializer.SerializeToElement(crowded);
        window.UseFixture(state);
        await Settle(window);
        var grid = Named<Grid>(layout, "WorkspaceLayout");
        var profiles = Field<ListBox>(window, "_profiles");
        var shortcuts = Field<ListBox>(window, "_shortcuts");
        if (profiles.SelectedItem is not Choice profileChoice || profileChoice.Id != "fixture-01" ||
            shortcuts.SelectedItem is not Choice shortcutChoice || shortcutChoice.Id != "task-02")
            throw new InvalidOperationException("The sidebar fixture did not select the current profile and its task shortcut.");
        var selectedProfile = profileChoice.Id;
        var selectedShortcut = shortcutChoice.Id;
        int profileSelectionChanges = 0, shortcutSelectionChanges = 0;
        profiles.SelectionChanged += (_, _) => profileSelectionChanges++;
        shortcuts.SelectionChanged += (_, _) => shortcutSelectionChanges++;
        var splitter = Splitter(layout);
        if (splitter.ResizeDirection != GridResizeDirection.Columns || splitter.ResizeBehavior != GridResizeBehavior.PreviousAndNext)
            throw new InvalidOperationException("The shortcut divider does not resize the shortcut column against the workspace.");
        AssertVisible(splitter, layout);
        var column = grid.ColumnDefinitions[1];
        var before = column.ActualWidth;
        await Drag(100);
        if (column.ActualWidth < before + 90)
            throw new InvalidOperationException("Dragging the real divider did not widen the shortcut column.");
        var savedWidth = column.ActualWidth;
        using (var saved = JsonDocument.Parse(File.ReadAllText(Path.Combine(fixtureRoot, "work", "control-center", "sidebar-layout.json"))))
            Require(Math.Abs(saved.RootElement.GetProperty("shortcuts_width").GetDouble() - savedWidth) < 0.6, "The dragged shortcut width was not saved.");
        var profileScroll = Descendants(profiles).OfType<ScrollViewer>().Single();
        var shortcutScroll = Descendants(shortcuts).OfType<ScrollViewer>().Single();
        if (profileScroll.ScrollableHeight <= 0 || shortcutScroll.ScrollableHeight <= 0)
            throw new InvalidOperationException("Crowded profile and shortcut lists do not have independent scroll ranges.");
        var shortcutOffset = shortcutScroll.VerticalOffset;
        profileScroll.ScrollToBottom();
        await Settle(window);
        if (profileScroll.VerticalOffset <= 0 || Math.Abs(shortcutScroll.VerticalOffset - shortcutOffset) > .01)
            throw new InvalidOperationException("Scrolling profiles changed the shortcut viewport or did not reach later accounts.");
        var profileOffset = profileScroll.VerticalOffset;
        shortcutScroll.ScrollToBottom();
        await Settle(window);
        if (shortcutScroll.VerticalOffset <= 0 || Math.Abs(profileScroll.VerticalOffset - profileOffset) > .01)
            throw new InvalidOperationException("Scrolling shortcuts changed the profile viewport or did not reach later tasks.");
        if (profileSelectionChanges != 0 || shortcutSelectionChanges != 0 ||
            ((Choice)profiles.SelectedItem).Id != selectedProfile || ((Choice)shortcuts.SelectedItem).Id != selectedShortcut)
            throw new InvalidOperationException("Divider dragging or list scrolling changed the selected profile or task.");

        // Test an unpinned viewport: bottom anchoring could conceal a jump.
        profileScroll.ScrollToVerticalOffset(profileScroll.ScrollableHeight * .6);
        shortcutScroll.ScrollToVerticalOffset(shortcutScroll.ScrollableHeight * .6);
        await Settle(window);
        var profileOffsetBeforeRefresh = profileScroll.VerticalOffset;
        var shortcutOffsetBeforeRefresh = shortcutScroll.VerticalOffset;
        var profileAnchorBeforeRefresh = VisibleAnchor(profiles);
        var shortcutAnchorBeforeRefresh = VisibleAnchor(shortcuts);
        var refreshed = JsonNode.Parse(state.GetRawText())!;
        refreshed["profiles"]![0]!["usage"]!["windows"]![1]!["remaining_percent"] = 45;
        const string refreshedAlias = "이름이 갱신된 작업 바로가기";
        refreshed["shortcuts"]![0]!["alias"] = refreshedAlias;
        window.UseFixture(JsonSerializer.SerializeToElement(refreshed));
        await Settle(window);
        if (!profiles.Items.OfType<Choice>().Single(choice => choice.Id == "fixture-01").Label.Contains("45%") ||
            !shortcuts.Items.OfType<Choice>().Single(choice => choice.Id == "task-01").Label.StartsWith(refreshedAlias, StringComparison.Ordinal))
            throw new InvalidOperationException("The scroll-retention fixture did not update actual profile and shortcut card text.");
        var profileAnchorAfterRefresh = VisibleAnchor(profiles);
        var shortcutAnchorAfterRefresh = VisibleAnchor(shortcuts);
        if (profileAnchorBeforeRefresh.Id != profileAnchorAfterRefresh.Id || Math.Abs(profileAnchorBeforeRefresh.Y - profileAnchorAfterRefresh.Y) > 1 ||
            shortcutAnchorBeforeRefresh.Id != shortcutAnchorAfterRefresh.Id || Math.Abs(shortcutAnchorBeforeRefresh.Y - shortcutAnchorAfterRefresh.Y) > 1)
            throw new InvalidOperationException($"Periodic card refresh moved visible content: profiles {profileAnchorBeforeRefresh} -> {profileAnchorAfterRefresh}, shortcuts {shortcutAnchorBeforeRefresh} -> {shortcutAnchorAfterRefresh}; offsets {profileOffsetBeforeRefresh:F1} -> {profileScroll.VerticalOffset:F1}, {shortcutOffsetBeforeRefresh:F1} -> {shortcutScroll.VerticalOffset:F1}.");
        if (((Choice)profiles.SelectedItem).Id != selectedProfile || ((Choice)shortcuts.SelectedItem).Id != selectedShortcut)
            throw new InvalidOperationException("Periodic card refresh changed the selected profile or task.");
        // Render may replace item collections internally; subsequent gesture
        // checks count only changes made after that refresh has settled.
        profileSelectionChanges = shortcutSelectionChanges = 0;

        // A separate constructor must read the drag result from disk, with no
        // production workspace preferences or service involved.
        var reopened = new MainWindow(fixtureRoot, fixture: true, fixtureRequest: fixtureRequest)
        { WindowState = WindowState.Normal, Width = 1440, Height = 960, Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        try
        {
            reopened.UseFixture(state);
            reopened.Show();
            await Settle(reopened);
            var restored = Named<Grid>((FrameworkElement)reopened.Content, "WorkspaceLayout").ColumnDefinitions[1].ActualWidth;
            if (Math.Abs(restored - savedWidth) > .6)
                throw new InvalidOperationException($"The next window did not restore the saved shortcut width ({savedWidth:F1} -> {restored:F1}).");
        }
        finally { reopened.Close(); }

        profileScroll.ScrollToTop(); shortcutScroll.ScrollToTop();
        await Settle(window);
        var selectedCard = (ListBoxItem)shortcuts.ItemContainerGenerator.ContainerFromItem(shortcuts.SelectedItem);
        Require(selectedCard.IsSelected && selectedCard.BorderBrush is SolidColorBrush { Color: var accent } && accent == Color.FromRgb(0xA3, 0xC1, 0xFF),
            "The selected shortcut card has no accent border.");
        SaveImage(layout, png);

        // Oversized drags stay within the column limits and keep the workspace
        // usable; a narrow window then turns the column into an overlay.
        foreach (var (direction, expected) in new[] { (-10000d, SidebarLayout.MinShortcutWidth), (10000d, SidebarLayout.MaxShortcutWidth) })
        {
            await Drag(direction);
            if (Math.Abs(column.ActualWidth - expected) > .6 || grid.ColumnDefinitions[3].ActualWidth < MainWindow.MinWorkspaceWidth
                || shortcuts.ActualHeight < 100 || profiles.ActualHeight < 100)
                throw new InvalidOperationException($"An extreme divider drag left a {column.ActualWidth:F1}-DIP column or collapsed a list viewport.");
            AssertActionsReachable(window, layout);
            AssertVisible(splitter, layout);
        }
        window.Width = 1024; window.Height = 840;
        await Settle(window);
        AssertCompact(window, layout, grid);
        AssertActionsReachable(window, layout);
        Click(layout, "ToggleProfilePanel");
        await Settle(window);
        Require(grid.ColumnDefinitions[3].ActualWidth >= 1024 - 304 - 1 && grid.ColumnDefinitions[1].ActualWidth == 0,
            "The expanded panel squeezed the workspace in a narrow window.");
        AssertActionsReachable(window, layout);
        Click(layout, "ToggleProfilePanel");
        await Settle(window);
        window.Width = 1440;
        await Settle(window);
        Require(grid.ColumnDefinitions[1].ActualWidth > 0 && Field<Border>(window, "_shortcutPanel").Parent == grid,
            "Widening the window did not dock the shortcut column again.");
        if (profileSelectionChanges != 0 || shortcutSelectionChanges != 0)
            throw new InvalidOperationException("Compact resize or extreme dragging changed list selection.");

        async Task Drag(double horizontal)
        {
            splitter.RaiseEvent(new DragStartedEventArgs(0, 0) { RoutedEvent = Thumb.DragStartedEvent });
            splitter.RaiseEvent(new DragDeltaEventArgs(horizontal, 0) { RoutedEvent = Thumb.DragDeltaEvent });
            window.UpdateLayout();
            splitter.RaiseEvent(new DragCompletedEventArgs(horizontal, 0, false) { RoutedEvent = Thumb.DragCompletedEvent });
            await Settle(window);
        }
        static GridSplitter Splitter(FrameworkElement parent) => Descendants(parent).OfType<GridSplitter>()
            .Single(control => control.Name == "ShortcutColumnSplitter");
        static (string Id, double Y) VisibleAnchor(ListBox list)
        {
            var viewport = Descendants(list).OfType<ScrollContentPresenter>().Single();
            var visible = Descendants(list).OfType<ListBoxItem>()
                .Where(item => item.IsVisible && item.DataContext is Choice)
                .Select(item => (Id: ((Choice)item.DataContext).Id,
                    Bounds: item.TransformToAncestor(viewport).TransformBounds(new Rect(item.RenderSize))))
                .Where(item => item.Bounds.Bottom > .5 && item.Bounds.Top < viewport.ActualHeight - .5)
                .OrderBy(item => item.Bounds.Top).ToArray();
            if (visible.Length == 0) throw new InvalidOperationException("A scrolled list has no visible card anchor.");
            return (visible[0].Id, visible[0].Bounds.Top);
        }
    }

    private static void AssertActionsReachable(MainWindow window, FrameworkElement layout)
    {
        var controls = Descendants(layout).ToArray();
        var docked = Field<Border>(window, "_shortcutPanel").Parent is Grid;
        var names = new List<string> { "AddProfile", "AllRecords", "WorkspaceSettings", "ToggleProfilePanel", "ExitWorkspace", "ProfileActions", "RefreshProfiles" };
        names.AddRange(docked ? ["AddShortcut", "ShortcutActions"] : ["ShortcutOverlayToggle"]);
        foreach (var name in names)
        {
            var button = controls.OfType<Button>().Single(control => control.Name == name);
            if (string.IsNullOrWhiteSpace(button.ToolTip?.ToString()) || string.IsNullOrWhiteSpace(AutomationProperties.GetName(button)))
                throw new InvalidOperationException("An icon action has no accessible description: " + name);
            AssertVisible(button, layout);
        }
        if (docked) AssertVisible(controls.OfType<TextBox>().Single(box => box.Name == "ShortcutFilter"), layout);
        else Require(!controls.OfType<Button>().Any(button => button.Name == "AddShortcut"), "The shortcut column is docked and overlaid at once.");
        foreach (var label in new[] { "진행 기록", "로그 복사" })
            AssertVisible(controls.OfType<Button>().Single(button => Equals(button.Content, label)), layout);
        foreach (var name in new[] { "ToggleTaskNotes", "RestoreWorkspaceView", "WorkspaceConnections", "WorkspaceDetails" })
            AssertVisible(controls.OfType<Button>().Single(button => button.Name == name), layout);
        AssertVisible(controls.OfType<TextBlock>().Single(block => block.Name == "WorkspaceStatus"), layout);
    }

    private static void AssertNotesLayout(MainWindow window, FrameworkElement layout, TaskNotesPanel panel)
    {
        AssertActionsReachable(window, layout);
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
        list.ScrollIntoView(choice);
        list.UpdateLayout();
        return list.ItemContainerGenerator.ContainerFromItem(choice) as ListBoxItem
            ?? throw new InvalidOperationException("Profile card was not realized: " + id);
    }

    private static void AssertScrollContentReachable(FrameworkElement control, FrameworkElement layout)
    {
        control.BringIntoView();
        layout.UpdateLayout();
        AssertVisible(control, layout);
        for (DependencyObject? parent = VisualTreeHelper.GetParent(control); parent is not null; parent = VisualTreeHelper.GetParent(parent))
        {
            if (parent is not ScrollContentPresenter viewport) continue;
            var bounds = control.TransformToAncestor(viewport).TransformBounds(new Rect(control.RenderSize));
            if (bounds.Top < -1 || bounds.Bottom > viewport.ActualHeight + 1)
                throw new InvalidOperationException("Sidebar content cannot be scrolled into its viewport: " + (control is ContentControl content ? content.Content : control.GetType().Name));
        }
    }

    private static void Layout(FrameworkElement element, double width, double height)
    {
        element.Measure(new Size(width, height));
        element.Arrange(new Rect(0, 0, width, double.IsInfinity(height) ? element.DesiredSize.Height : height));
        element.UpdateLayout();
    }

    private static void SaveImage(FrameworkElement element, string path, Rect? region = null)
    {
        var area = region ?? new Rect(0, 0, element.ActualWidth, element.ActualHeight);
        var bitmap = new RenderTargetBitmap((int)Math.Ceiling(area.Width), (int)Math.Ceiling(area.Height), 96, 96, PixelFormats.Pbgra32);
        // Render at the origin even when the element is in a right-hand column.
        var visual = new DrawingVisual();
        var offset = VisualTreeHelper.GetOffset(element);
        using (var drawing = visual.RenderOpen())
        {
            drawing.DrawRectangle(WorkspaceAppearance.Canvas, null, new Rect(0, 0, area.Width, area.Height));
            drawing.DrawRectangle(new VisualBrush(element) { ViewboxUnits = BrushMappingMode.Absolute,
                Viewbox = new Rect(offset.X + area.X, offset.Y + area.Y, area.Width, area.Height) }, null,
                new Rect(0, 0, area.Width, area.Height));
        }
        bitmap.Render(visual);
        using var file = File.Create(path);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));
        encoder.Save(file);
    }

    private static void AssertVisible(FrameworkElement control, FrameworkElement layout)
    {
        var bounds = control.TransformToAncestor(layout).TransformBounds(new Rect(control.RenderSize));
        if (!control.IsVisible || bounds.Top < 0 || bounds.Bottom > layout.ActualHeight + 0.5 || bounds.Left < 0 || bounds.Right > layout.ActualWidth + 0.5)
            throw new InvalidOperationException("A workspace action is clipped outside the window: " + (control.Name is { Length: > 0 } name ? name
                : control is ContentControl content ? content.Content : control.GetType().Name));
    }

    private static void Click(FrameworkElement parent, string name)
    {
        Descendants(parent).OfType<Button>().Single(button => button.Name == name).RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        parent.UpdateLayout();
    }

    private static async Task Settle(Window target)
    {
        await target.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        target.UpdateLayout();
    }

    private static T Field<T>(MainWindow window, string name) => (T)typeof(MainWindow).GetField(name, Private)!.GetValue(window)!;
    private static T Named<T>(FrameworkElement parent, string name) where T : FrameworkElement =>
        Descendants(parent).OfType<T>().Single(element => element.Name == name);
    private static string[] VisibleTexts(FrameworkElement parent) =>
        Descendants(parent).OfType<TextBlock>().Where(block => block.IsVisible).Select(block => block.Text).ToArray();
    private static void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }

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
