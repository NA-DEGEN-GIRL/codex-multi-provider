using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal sealed partial class TaskNotesPanel : Border
{
    private readonly Func<string, object, Task<JsonElement>> request;
    private readonly NoteDrafts recovery;
    private readonly Dictionary<string, NoteDraft> drafts = [];
    private readonly List<TaskNote> notes = [];
    private SelectedTask? selected;
    private NoteDraft? editing;
    private long selection;
    private bool rendering;
    private bool loadFailed;
    private bool loading, splitting, refreshing;
    private int pendingMutations;
    private long contentVersion;
    private readonly DispatcherTimer refresh = new() { Interval = TimeSpan.FromSeconds(2) };
    private readonly TextBlock sharing = new() { Name = "NoteSharing", Foreground = WorkspaceAppearance.Accent, FontSize = 11, VerticalAlignment = VerticalAlignment.Center };
    private readonly Border sharingBadge = new() { Background = WorkspaceAppearance.Selected, CornerRadius = new CornerRadius(5), Padding = new Thickness(8, 5, 8, 5), VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock taskTitle = new() { Text = "작업을 열어 주세요", FontSize = 12, TextTrimming = TextTrimming.CharacterEllipsis, Foreground = WorkspaceAppearance.Muted, Margin = new Thickness(0, 5, 0, 12) };
    private readonly TextBlock status = new() { Text = "작업별로 자동 저장됩니다", Foreground = WorkspaceAppearance.Muted, FontSize = 11, Margin = new Thickness(0, 10, 0, 0), TextWrapping = TextWrapping.Wrap };
    private readonly StackPanel tabs = new() { Orientation = Orientation.Horizontal };
    private readonly Button add = new() { Name = "AddNote", Content = "+ 새 메모", Height = 32, Padding = new Thickness(10, 0, 10, 0), Margin = new Thickness(0), HorizontalContentAlignment = HorizontalAlignment.Center, ToolTip = "이 작업에 새 메모 추가" };
    private readonly Button options = new() { Content = "⋯", ToolTip = "메모 이름 변경 · 삭제 · 복구" };
    private readonly Button fork = new() { Name = "ForkNote", Content = "메모 분리", Height = 32, Padding = new Thickness(10, 0, 10, 0), Margin = new Thickness(6, 0, 0, 0), HorizontalContentAlignment = HorizontalAlignment.Center, Visibility = Visibility.Collapsed, ToolTip = "현재 메모와 체크 항목을 모두 복사해 이 작업만 독립적으로 편집합니다. 다른 작업의 메모는 그대로 유지됩니다." };
    private bool shared;
    private readonly Button retry = new() { Content = "다시 저장", Visibility = Visibility.Collapsed };
    private readonly TextBox editor = new() { Name = "NoteBody", AcceptsReturn = true, AcceptsTab = true, TextWrapping = TextWrapping.Wrap,
        Background = Brushes.Transparent, Foreground = Brush(233, 236, 242), BorderThickness = new Thickness(0), Padding = new Thickness(0, 6, 0, 6), Margin = new Thickness(0), FontSize = 14, MaxLength = 65000, MinHeight = 52 };
    private readonly TextBlock placeholder = new() { Text = "메모를 입력하세요…", Foreground = Brush(153, 164, 184), Margin = new Thickness(0, 6, 0, 0), IsHitTestVisible = false, FontSize = 14 };
    private readonly StackPanel checklist = new();
    private readonly ScrollViewer noteScroll;
    private readonly Grid body = new();
    private readonly StackPanel empty = new() { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(18) };
    private readonly DispatcherTimer autosave = new() { Interval = TimeSpan.FromMilliseconds(500) };
    internal event Action? CollapseRequested;

    internal TaskNotesPanel(string root, Func<string, object, Task<JsonElement>> request)
    {
        this.request = request; recovery = new(root);
        Background = WorkspaceAppearance.Surface; BorderBrush = WorkspaceAppearance.Line; BorderThickness = new Thickness(1, 0, 0, 0); Padding = new Thickness(16, 16, 16, 12);
        UseLayoutRounding = true; SnapsToDevicePixels = true;
        var layout = new DockPanel();
        var heading = new DockPanel();
        var close = WorkspaceAppearance.Icon(new Button { Content = "›", ToolTip = "메모 패널 접기" }, "CloseTaskNotes");
        close.Click += (_, _) => CollapseRequested?.Invoke();
        add.Content = "+"; WorkspaceAppearance.Icon(add, "AddNote"); WorkspaceAppearance.Icon(options, "NoteOptions");
        var headingActions = new StackPanel { Orientation = Orientation.Horizontal };
        headingActions.Children.Add(add); headingActions.Children.Add(options); headingActions.Children.Add(close);
        DockPanel.SetDock(headingActions, Dock.Right); heading.Children.Add(headingActions);
        heading.Children.Add(new TextBlock { Text = "작업 메모", FontSize = 16, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        var top = new StackPanel(); top.Children.Add(heading); top.Children.Add(taskTitle);
        var sharingRow = new DockPanel { Margin = new Thickness(0, 0, 0, 12) };
        WorkspaceAppearance.Tool(fork, quiet: true); DockPanel.SetDock(fork, Dock.Right); sharingRow.Children.Add(fork);
        sharingBadge.Child = sharing; sharingBadge.HorizontalAlignment = HorizontalAlignment.Left; sharingRow.Children.Add(sharingBadge); top.Children.Add(sharingRow);
        WorkspaceAppearance.Tool(retry);
        // A single scrollable tab strip keeps many notes from pushing the editor
        // out of view. Tab names remain available through keyboard focus/tooltips.
        top.Children.Add(new ScrollViewer { Name = "NoteTabs", Content = tabs, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            VerticalScrollBarVisibility = ScrollBarVisibility.Disabled, Margin = new Thickness(0, 0, 0, 10), MaxHeight = 46 });
        retry.Click += async (_, _) => { if (loadFailed) await SelectTaskAsync(selected, reload: true); else if (editing is { } draft) await SaveAsync(draft); };
        options.Click += (_, _) =>
        {
            var actions = new ContextMenu { PlacementTarget = options };
            if (editing is { } draft)
            {
                var rename = new MenuItem { Header = "이름 변경" }; rename.Click += (_, _) => Rename(draft.Note);
                var delete = new MenuItem { Header = "메모 삭제" }; delete.Click += async (_, _) => await DeleteAsync(draft.Note);
                actions.Items.Add(rename); actions.Items.Add(delete); actions.Items.Add(new Separator());
            }
            var restore = new MenuItem { Header = "삭제한 메모 복구", IsEnabled = notes.Any(n => n.Deleted) };
            restore.Click += async (_, _) => await RestoreAsync(); actions.Items.Add(restore); actions.IsOpen = true;
        };
        DockPanel.SetDock(top, Dock.Top); layout.Children.Add(top);
        var footer = new StackPanel(); footer.Children.Add(status); footer.Children.Add(retry);
        retry.Margin = new Thickness(0, 8, 0, 0); retry.HorizontalAlignment = HorizontalAlignment.Left;
        DockPanel.SetDock(footer, Dock.Bottom); layout.Children.Add(footer);
        var textArea = new Grid(); textArea.Children.Add(editor); textArea.Children.Add(placeholder);
        var document = new StackPanel { Margin = new Thickness(14, 10, 14, 14) }; document.Children.Add(textArea); document.Children.Add(checklist);
        InitializeImages(root, document);
        noteScroll = new ScrollViewer { Content = document, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Background = Brushes.Transparent, Visibility = Visibility.Collapsed };
        empty.Children.Add(new TextBlock { Text = "생각과 할 일을 한곳에", TextAlignment = TextAlignment.Center, FontSize = 14, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 8) });
        empty.Children.Add(new TextBlock { Text = "+ 버튼으로 새 메모를 만들고\n글과 체크 항목을 함께 남겨 보세요.", TextAlignment = TextAlignment.Center, TextWrapping = TextWrapping.Wrap, FontSize = 12, LineHeight = 20, Foreground = WorkspaceAppearance.Muted });
        body.Children.Add(noteScroll); body.Children.Add(empty);
        layout.Children.Add(new Border { Background = Brush(23, 25, 30), BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Child = body }); Child = layout;
        add.Click += (_, _) => Add();
        fork.Click += async (_, _) => await ForkAsync();
        editor.TextChanged += (_, _) =>
        {
            placeholder.Visibility = editor.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
            if (!rendering && editing is { } draft) { draft.Note.Body = editor.Text; Dirty(draft); }
        };
        autosave.Tick += async (_, _) => { autosave.Stop(); foreach (var draft in drafts.Values.Where(d => d.Changed != d.Saved).ToArray()) await SaveAsync(draft); };
        refresh.Tick += async (_, _) => await RefreshSharedAsync();
        Loaded += (_, _) => refresh.Start();
        Unloaded += (_, _) => refresh.Stop();
        UpdateSharing(); RenderTabs(); RenderEditor();
    }
    private static SolidColorBrush Brush(byte r, byte g, byte b) => new(Color.FromRgb(r,g,b));
    private string DraftKey(NoteTask task, string note) => task.Key + "/" + note;
    internal async Task SelectTaskAsync(SelectedTask? task, bool reload = false)
    {
        if (!reload && selected?.Task == task?.Task) { if (task is not null) { taskTitle.Text = task.Title; taskTitle.ToolTip = task.Title; } return; }
        var ticket = ++selection; selected = task; editing = null; notes.Clear(); loadFailed = false; loading = true; shared = false;
        UpdateSharing();
        taskTitle.Text = task?.Title ?? "작업을 열어 주세요"; taskTitle.ToolTip = task?.Title;
        RenderTabs(); RenderEditor();
        status.Text = task is null ? "열린 작업에 메모가 연결됩니다" : "메모 불러오는 중…";
        try
        {
            // A newly opened fork joins the parent's shared notes. Drain any
            // draft save already in flight before that first read, including
            // when selection changes again while the previous save completes.
            await FlushAsync();
            if (selection != ticket || task is null) return;
            var value = await request("notes.list", new { task = task.Task.Wire });
            if (selection != ticket) return;
            SetImageCapabilities(value);
            notes.AddRange(value.GetProperty("notes").Deserialize<List<TaskNote>>(NoteDrafts.Json) ?? []);
            shared = value.TryGetProperty("shared", out var sharedFlag) && sharedFlag.ValueKind == JsonValueKind.True;
            foreach (var recovered in recovery.Recover(task.Task))
            {
                var existing = notes.FindIndex(n => n.Id == recovered.Id);
                if (existing >= 0) notes[existing] = recovered; else notes.Add(recovered);
                string key = DraftKey(task.Task, recovered.Id);
                if (!drafts.ContainsKey(key)) drafts[key] = new(task.Task, recovered) { Changed = 1 };
                else if (drafts[key].Changed == drafts[key].Saved) { drafts[key].Note = recovered; drafts[key].Changed++; }
            }
            foreach (var draft in drafts.Values.Where(d => d.Task == task.Task && d.Changed != d.Saved))
            { var index = notes.FindIndex(n => n.Id == draft.Note.Id); if (index >= 0) notes[index] = draft.Note; else notes.Add(draft.Note); }
            var first = notes.FirstOrDefault(n => !n.Deleted);
            if (first is not null) Edit(first); else { RenderTabs(); RenderEditor(); status.Text = "아직 메모가 없습니다"; }
            foreach (var draft in drafts.Values.Where(d => d.Task == task.Task && d.Changed != d.Saved).ToArray()) _ = SaveAsync(draft);
            UpdateSharing();
        }
        catch (Exception error) { if (selection == ticket) { loadFailed = true; status.Text = "불러오기 실패 · " + error.Message; add.IsEnabled = false; retry.Content = "다시 불러오기"; retry.Visibility = Visibility.Visible; } }
        finally { if (selection == ticket) { loading = false; UpdateSharing(); } }
    }
    private void UpdateSharing()
    {
        sharing.Text = selected is null || loading || loadFailed ? "" : shared ? "공유 메모" : "독립 메모";
        sharingBadge.Visibility = sharing.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        sharingBadge.ToolTip = shared ? "연결된 작업과 같은 메모를 사용합니다. 수정 내용도 함께 반영됩니다." : "이 작업에만 저장되는 메모입니다.";
        fork.Visibility = selected is not null && shared && !loading && !loadFailed ? Visibility.Visible : Visibility.Collapsed;
        fork.IsEnabled = !splitting && pendingMutations == 0;
    }
    internal async Task RefreshSharedAsync()
    {
        // Poll read-only state while idle; never replace an active edit or reset
        // the caret/scroll position just because another window is open.
        bool Busy() => loading || splitting || pendingMutations > 0 || IsKeyboardFocusWithin ||
            drafts.Values.Any(d => d.Saving || d.Changed != d.Saved);
        if (refreshing || !IsVisible || selected is not { } task || loadFailed || Busy()) return;
        refreshing = true; var ticket = selection; var version = contentVersion;
        try
        {
            var value = await request("notes.list", new { task = task.Task.Wire });
            if (ticket != selection || version != contentVersion || Busy()) return;
            SetImageCapabilities(value);
            var incoming = value.GetProperty("notes").Deserialize<List<TaskNote>>(NoteDrafts.Json) ?? [];
            shared = value.B("shared"); UpdateSharing();
            if (JsonSerializer.Serialize(incoming, NoteDrafts.Json) == JsonSerializer.Serialize(notes, NoteDrafts.Json)) return;
            var noteId = editing?.Note.Id; var offset = noteScroll.VerticalOffset;
            notes.Clear(); notes.AddRange(incoming); editing = null;
            var next = notes.FirstOrDefault(n => !n.Deleted && n.Id == noteId) ?? notes.FirstOrDefault(n => !n.Deleted);
            if (next is not null) Edit(next);
            else { RenderTabs(); RenderEditor(); status.Text = "아직 메모가 없습니다"; }
            noteScroll.ScrollToVerticalOffset(offset);
        }
        catch { /* A later idle read retries; saved notes and local drafts stay visible. */ }
        finally { refreshing = false; }
    }
    private async Task ForkAsync()
    {
        if (selected is null || loadFailed || !shared || splitting || pendingMutations > 0) return;
        var task = selected; var ticket = selection;
        splitting = true; IsEnabled = false; contentVersion++;
        try
        {
            await FlushAsync();
            if (selection != ticket) return;
            await request("notes.fork", new { task = task.Task.Wire });
            if (selection != ticket) return;
            await SelectTaskAsync(task, reload: true);
            if (selected?.Task == task.Task && !loadFailed) status.Text = "메모를 분리했습니다. 이 작업에서 독립적으로 편집합니다.";
        }
        catch (Exception error) { if (selection == ticket) status.Text = "메모 분리 실패 · " + error.Message; }
        finally { splitting = false; IsEnabled = true; UpdateSharing(); }
    }
    internal void Add()
    {
        if (selected is null || loadFailed || splitting) return;
        var note = new TaskNote { Title = "메모 " + (notes.Count(n => !n.Deleted) + 1) };
        notes.Add(note); Edit(note); Dirty(editing!); editor.Focus();
    }
    private void Edit(TaskNote note)
    {
        if (selected is null) return;
        if (editing is { } old) _ = SaveAsync(old);
        var key = DraftKey(selected.Task, note.Id);
        if (!drafts.TryGetValue(key, out var draft)) drafts[key] = draft = new(selected.Task, note);
        else if (draft.Changed == draft.Saved) draft.Note = note;
        editing = draft; RenderTabs(); RenderEditor();
    }
    private void RenderTabs()
    {
        tabs.Children.Clear(); add.IsEnabled = selected is not null && !loadFailed; options.IsEnabled = selected is not null && !loadFailed;
        foreach (var note in notes.Where(n => !n.Deleted))
        {
            var button = WorkspaceAppearance.Tool(new Button { Content = new TextBlock { Text = note.Title, TextTrimming = TextTrimming.CharacterEllipsis, MaxWidth = 124 },
                ToolTip = note.Title + " · 오른쪽 클릭으로 이름 변경 / 삭제" }, quiet: true);
            bool active = editing?.Note.Id == note.Id;
            button.Margin = new Thickness(0, 0, 6, 2); button.Height = 30;
            button.Background = active ? WorkspaceAppearance.Selected : Brushes.Transparent;
            button.Foreground = active ? WorkspaceAppearance.Accent : WorkspaceAppearance.Muted;
            ((TextBlock)button.Content).Foreground = button.Foreground;
            button.BorderBrush = active ? WorkspaceAppearance.Color("#687CA6") : Brushes.Transparent;
            System.Windows.Automation.AutomationProperties.SetItemStatus(button, active ? "선택됨" : "");
            button.GotKeyboardFocus += (_, _) => button.BringIntoView();
            if (active) button.Loaded += (_, _) => button.BringIntoView();
            button.Click += (_, _) => Edit(note);
            var menu = new ContextMenu();
            var rename = new MenuItem { Header = "이름 변경" }; rename.Click += (_, _) => Rename(note);
            var delete = new MenuItem { Header = "메모 삭제" }; delete.Click += async (_, _) => await DeleteAsync(note);
            menu.Items.Add(rename); menu.Items.Add(delete); button.ContextMenu = menu; tabs.Children.Add(button);
        }
    }
    private void RenderEditor()
    {
        rendering = true;
        try
        {
            // Legacy text/checklist notes use the same document. Keep their stored
            // kind, IDs and both content fields intact, including recovered drafts.
            noteScroll.Visibility = editing is not null ? Visibility.Visible : Visibility.Collapsed;
            empty.Visibility = editing is null ? Visibility.Visible : Visibility.Collapsed;
            editor.Text = editing?.Note.Body ?? "";
            RenderChecklist();
            RenderImages();
            noteScroll.ScrollToTop();
            UpdateStatus();
        }
        finally { rendering = false; }
    }
    private void RenderChecklist()
    {
        checklist.Children.Clear(); if (editing is not { } draft) return;
        foreach (var item in draft.Note.Items)
        {
            var row = new Grid { Margin = new Thickness(0, 2, 0, 2), MinHeight = 34 };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(26) });
            row.ColumnDefinitions.Add(new ColumnDefinition());
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(28) });
            var check = new CheckBox { IsChecked = item.Done, VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(0, 8, 0, 0), Width = 18, Height = 18,
                HorizontalAlignment = HorizontalAlignment.Left, ToolTip = "완료 표시", Style = (Style)FindResource("NoteCheckBox") };
            row.Children.Add(check);
            var remove = WorkspaceAppearance.Icon(new Button { Content = "×", ToolTip = "항목 삭제", Foreground = WorkspaceAppearance.Muted, VerticalAlignment = VerticalAlignment.Top }, "");
            remove.Width = remove.Height = 28; remove.Margin = new Thickness(0, 3, 0, 0);
            remove.Click += (_, _) =>
            {
                var index = draft.Note.Items.IndexOf(item);
                draft.Note.Items.Remove(item); Dirty(draft); RenderChecklist();
                var next = checklist.Children.OfType<Grid>().ElementAtOrDefault(Math.Min(index, draft.Note.Items.Count - 1));
                (next?.Children.OfType<TextBox>().FirstOrDefault() ?? editor).Focus();
            };
            Grid.SetColumn(remove, 2); row.Children.Add(remove);
            var text = new TextBox { Tag = item.Id, Text = item.Text, TextWrapping = TextWrapping.Wrap, AcceptsReturn = true, Padding = new Thickness(0, 6, 6, 6), Margin = new Thickness(0), MaxLength = 2000, MinHeight = 34,
                Background = Brushes.Transparent, BorderThickness = new Thickness(0), Foreground = item.Done ? Brush(139,150,168) : Brush(233,236,242), FontSize = 14 };
            check.Click += (_, _) => { item.Done = check.IsChecked == true; text.Foreground = item.Done ? Brush(139,150,168) : Brush(233,236,242); Dirty(draft); };
            text.TextChanged += (_, _) => { item.Text = text.Text; Dirty(draft); }; Grid.SetColumn(text, 1); row.Children.Add(text); checklist.Children.Add(row);
        }
        var append = WorkspaceAppearance.Tool(new Button { Name = "AddNoteCheck", Content = "+ 체크 항목 추가", ToolTip = "이 메모에 체크 항목 추가",
            Foreground = WorkspaceAppearance.Accent, HorizontalAlignment = HorizontalAlignment.Left, IsEnabled = draft.Note.Items.Count < 500 }, quiet: true);
        append.Margin = new Thickness(0, 8, 0, 0); append.Padding = new Thickness(6, 0, 6, 0);
        append.Click += (_, _) => { draft.Note.Items.Add(new()); Dirty(draft); RenderChecklist();
            if (checklist.Children.OfType<Grid>().LastOrDefault()?.Children.OfType<TextBox>().FirstOrDefault() is { } input) { input.Focus(); input.BringIntoView(); } };
        checklist.Children.Add(append);
    }
    private void Dirty(NoteDraft draft)
    {
        contentVersion++; draft.Changed++; draft.Error = null; autosave.Stop(); autosave.Start(); UpdateStatus();
    }
    private void UpdateStatus()
    {
        if (editing is not { } draft) { retry.Visibility = Visibility.Collapsed; return; }
        status.Foreground = draft.Error is null ? Brush(153,164,184) : Brush(240,178,116);
        retry.Content = "다시 저장";
        retry.Visibility = draft.Error is null ? Visibility.Collapsed : Visibility.Visible;
        status.Text = draft.Error ?? (draft.Saving ? "저장 중…" : draft.Changed != draft.Saved ? "저장 대기…" : "저장됨");
        if (draft.Note.Items.Count > 0) status.Text += $" · {draft.Note.Items.Count(i => i.Done)}/{draft.Note.Items.Count} 완료";
    }
    private async Task SaveAsync(NoteDraft draft)
    {
        if (draft.Saving || draft.Changed == draft.Saved || draft.Note.Deleted) return;
        draft.Saving = true; UpdateStatus();
        contentVersion++;
        try
        {
            while (draft.Changed != draft.Saved)
            {
                long version = draft.Changed;
                var snapshot = JsonSerializer.Deserialize<TaskNote>(JsonSerializer.Serialize(draft.Note, NoteDrafts.Json),NoteDrafts.Json)!;
                await Task.Run(() => recovery.Write(draft.Task, snapshot, version));
                if (snapshot.Images.Count > 0 && !imageAttachmentsSupported)
                    throw new IOException("이미지가 포함된 초안은 새 관리 서비스 적용 후 저장할 수 있습니다.");
                var result = await request("notes.save", new { task = draft.Task.Wire, note_id = snapshot.Id, revision = snapshot.Revision,
                    title = snapshot.Title, kind = snapshot.Kind, body = snapshot.Body, images = snapshot.Images,
                    items = snapshot.Items.Select(i => new { id = i.Id, text = i.Text, done = i.Done }) });
                if (result.S("state") == "conflict")
                {
                    var remote = result.Get("current").Deserialize<TaskNote>(NoteDrafts.Json);
                    if (remote is not null && !remote.Deleted && remote.Title == snapshot.Title && remote.Kind == snapshot.Kind && remote.Body == snapshot.Body &&
                        JsonSerializer.Serialize(remote.Items, NoteDrafts.Json) == JsonSerializer.Serialize(snapshot.Items, NoteDrafts.Json) &&
                        JsonSerializer.Serialize(remote.Images, NoteDrafts.Json) == JsonSerializer.Serialize(snapshot.Images, NoteDrafts.Json))
                    {
                        // The previous response may have been lost after an atomic save.
                        draft.Note.Revision = remote.Revision; draft.Saved = version;
                        if (draft.Changed == version) recovery.Remove(draft.Task, draft.Note.Id, version);
                        continue;
                    }
                    // Preserve both versions. A conflict is not permission to
                    // overwrite changes from a second window.
                    var previousId = draft.Note.Id;
                    draft.Note.Id = Guid.NewGuid().ToString(); draft.Note.Revision = 0;
                    draft.Note.Title = draft.Note.Title[..Math.Min(70,draft.Note.Title.Length)] + " · 복구본";
                    drafts.Remove(DraftKey(draft.Task, previousId)); drafts[DraftKey(draft.Task,draft.Note.Id)] = draft;
                    recovery.Write(draft.Task, draft.Note, draft.Changed);
                    recovery.Remove(draft.Task, previousId);
                    if (selected?.Task == draft.Task && remote is not null && notes.All(n => n.Id != remote.Id)) notes.Insert(0, remote);
                    if (editing == draft) RenderTabs();
                    continue;
                }
                draft.Note.Revision = result.Get("note").GetProperty("revision").GetInt64();
                draft.Saved = version;
                if (draft.Changed == version) recovery.Remove(draft.Task, draft.Note.Id, version);
            }
            draft.Error = null;
        }
        catch (Exception error) { draft.Error = "저장 실패 · 초안 보존 · " + error.Message; }
        finally { contentVersion++; draft.Saving = false; UpdateStatus(); }
    }
    private void Rename(TaskNote note)
    {
        var title = Dialogs.Prompt(Window.GetWindow(this), "메모 이름 변경", "메모 탭 이름", note.Title);
        if (string.IsNullOrWhiteSpace(title) || title.Length > 80) return;
        Edit(note); note.Title = title.Trim(); Dirty(editing!); RenderTabs();
    }
    private async Task DeleteAsync(TaskNote note)
    {
        if (selected is null || splitting) return;
        var task = selected.Task; var key = DraftKey(task,note.Id);
        if (drafts.TryGetValue(key,out var draft)) { await SaveAsync(draft); if (draft.Changed != draft.Saved) return; }
        if (MessageBox.Show(Window.GetWindow(this), $"‘{note.Title}’ 메모를 삭제할까요? ⋯ 메뉴에서 복구할 수 있습니다.", "메모 삭제", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
        try
        {
            pendingMutations++; contentVersion++; UpdateSharing();
            var result = await request("notes.delete",new { task = task.Wire, note_id = note.Id, revision = note.Revision });
            if (result.S("state") == "conflict") throw new InvalidOperationException("다른 창에서 변경되었습니다. 작업을 다시 열어 확인하세요.");
            note.Deleted = true; note.Revision = result.Get("note").GetProperty("revision").GetInt64();
            drafts.Remove(key); recovery.Remove(task,note.Id);
            if (selected?.Task != task) return;
            if (editing?.Note.Id == note.Id) { editing = null; if (notes.FirstOrDefault(n => !n.Deleted) is { } next) Edit(next); }
            RenderTabs(); RenderEditor();
        }
        catch (Exception error) { status.Text = error.Message; }
        finally { pendingMutations--; contentVersion++; UpdateSharing(); }
    }
    private async Task RestoreAsync()
    {
        if (selected is null || splitting) return;
        var task = selected.Task; var note = notes.LastOrDefault(n => n.Deleted);
        if (note is null) { status.Text = "복구할 메모가 없습니다"; return; }
        try
        {
            pendingMutations++; contentVersion++; UpdateSharing();
            var result = await request("notes.restore",new { task = task.Wire, note_id = note.Id, revision = note.Revision });
            if (result.S("state") == "conflict") throw new InvalidOperationException("다른 창에서 변경되었습니다. 작업을 다시 열어 주세요.");
            note.Deleted = false; note.Revision = result.Get("note").GetProperty("revision").GetInt64();
            if (selected?.Task == task) Edit(note);
        }
        catch (Exception error) { status.Text = error.Message; }
        finally { pendingMutations--; contentVersion++; UpdateSharing(); }
    }
    internal void PreserveDrafts()
    {
        autosave.Stop();
        foreach (var draft in drafts.Values.Where(d => d.Changed != d.Saved)) recovery.Write(draft.Task,draft.Note,draft.Changed);
    }
    internal async Task FlushAsync()
    {
        autosave.Stop();
        while (importingImage) await Task.Delay(10);
        foreach (var draft in drafts.Values.Where(d => d.Changed != d.Saved).ToArray())
        {
            while (draft.Saving) await Task.Delay(10);
            await SaveAsync(draft);
            if (draft.Changed != draft.Saved) throw new IOException(draft.Error);
        }
    }
}
