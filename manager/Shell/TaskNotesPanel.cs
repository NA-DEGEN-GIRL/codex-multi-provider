using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal sealed class TaskNotesPanel : Border
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
    private readonly TextBlock taskTitle = new() { Text = "작업을 열어 주세요", TextTrimming = TextTrimming.CharacterEllipsis, Foreground = Brush(169, 177, 194), Margin = new Thickness(0, 6, 0, 16) };
    private readonly TextBlock status = new() { Text = "작업별로 자동 저장됩니다", Foreground = Brush(153, 164, 184), FontSize = 12, Margin = new Thickness(0, 10, 0, 0), TextWrapping = TextWrapping.Wrap };
    private readonly WrapPanel tabs = new();
    private readonly Button add = new() { Name = "AddNote", Content = "+ 새 메모", Height = 32, Padding = new Thickness(10, 0, 10, 0), Margin = new Thickness(0), HorizontalContentAlignment = HorizontalAlignment.Center, ToolTip = "이 작업에 새 메모 추가" };
    private readonly Button options = new() { Content = "⋯", Width = 32, Height = 32, Margin = new Thickness(6, 0, 0, 0), Padding = new Thickness(0), HorizontalContentAlignment = HorizontalAlignment.Center, ToolTip = "메모 이름 변경 · 삭제 · 복구" };
    private readonly Button fork = new() { Name = "ForkNote", Content = "?? fork", Height = 32, Padding = new Thickness(10, 0, 10, 0), Margin = new Thickness(6, 0, 0, 0), HorizontalContentAlignment = HorizontalAlignment.Center, Visibility = Visibility.Collapsed, ToolTip = "????? ??? ??? ? ???? ??? ?????. ???? ??? ????? ????." };
    private bool shared;
    private readonly Button retry = new() { Content = "다시 저장", Visibility = Visibility.Collapsed };
    private readonly TextBox editor = new() { Name = "NoteBody", AcceptsReturn = true, AcceptsTab = true, TextWrapping = TextWrapping.Wrap,
        Background = Brushes.Transparent, Foreground = Brush(233, 236, 242), BorderThickness = new Thickness(0), Padding = new Thickness(0, 6, 0, 6), Margin = new Thickness(0), FontSize = 14, MaxLength = 65000, MinHeight = 52 };
    private readonly TextBlock placeholder = new() { Text = "메모를 입력하세요…", Foreground = Brush(153, 164, 184), Margin = new Thickness(0, 6, 0, 0), IsHitTestVisible = false, FontSize = 14 };
    private readonly StackPanel checklist = new();
    private readonly ScrollViewer noteScroll;
    private readonly Grid body = new();
    private readonly TextBlock empty = new() { Text = "이 작업의 메모를 남겨 보세요.\n+ 버튼으로 메모를 만들고, 메모 안에 체크 항목을 추가할 수 있습니다.", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(12, 28, 12, 12), Foreground = Brush(153,164,184) };
    private readonly DispatcherTimer autosave = new() { Interval = TimeSpan.FromMilliseconds(500) };
    internal event Action? CollapseRequested;

    internal TaskNotesPanel(string root, Func<string, object, Task<JsonElement>> request)
    {
        this.request = request; recovery = new(root);
        Background = Brush(24, 27, 33); BorderBrush = Brush(54, 60, 70); BorderThickness = new Thickness(1, 0, 0, 0); Padding = new Thickness(16);
        UseLayoutRounding = true; SnapsToDevicePixels = true;
        var layout = new DockPanel();
        var heading = new DockPanel();
        var close = new Button { Content = "›", ToolTip = "메모 패널 접기", Width = 32, Height = 32, Margin = new Thickness(0), Padding = new Thickness(0), Background = Brushes.Transparent, BorderThickness = new Thickness(0), HorizontalContentAlignment = HorizontalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Right };
        close.Click += (_, _) => CollapseRequested?.Invoke(); DockPanel.SetDock(close, Dock.Right); heading.Children.Add(close);
        heading.Children.Add(new TextBlock { Text = "작업 메모", FontSize = 18, FontWeight = FontWeights.SemiBold, VerticalAlignment = VerticalAlignment.Center });
        var top = new StackPanel(); top.Children.Add(heading); top.Children.Add(taskTitle);
        var toolbar = new DockPanel { Margin = new Thickness(0, 0, 0, 10) };
        DockPanel.SetDock(options, Dock.Right); toolbar.Children.Add(options);
        DockPanel.SetDock(fork, Dock.Right); toolbar.Children.Add(fork);
        add.HorizontalAlignment = HorizontalAlignment.Left; toolbar.Children.Add(add); top.Children.Add(toolbar);
        tabs.Margin = new Thickness(0, 0, 0, 10); top.Children.Add(tabs);
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
        DockPanel.SetDock(footer, Dock.Bottom); layout.Children.Add(footer);
        var textArea = new Grid(); textArea.Children.Add(editor); textArea.Children.Add(placeholder);
        var document = new StackPanel { Margin = new Thickness(12, 8, 12, 12) }; document.Children.Add(textArea); document.Children.Add(checklist);
        noteScroll = new ScrollViewer { Content = document, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Background = Brushes.Transparent, Visibility = Visibility.Collapsed };
        body.Children.Add(noteScroll); body.Children.Add(empty);
        layout.Children.Add(new Border { Background = Brush(18, 21, 27), BorderBrush = Brush(48, 54, 65), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Child = body }); Child = layout;
        add.Click += (_, _) => Add();
        fork.Click += async (_, _) => await ForkAsync();
        editor.TextChanged += (_, _) =>
        {
            placeholder.Visibility = editor.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
            if (!rendering && editing is { } draft) { draft.Note.Body = editor.Text; Dirty(draft); }
        };
        autosave.Tick += async (_, _) => { autosave.Stop(); foreach (var draft in drafts.Values.Where(d => d.Changed != d.Saved).ToArray()) await SaveAsync(draft); };
        RenderTabs(); RenderEditor();
    }
    private static SolidColorBrush Brush(byte r, byte g, byte b) => new(Color.FromRgb(r,g,b));
    private string DraftKey(NoteTask task, string note) => task.Key + "/" + note;
    internal async Task SelectTaskAsync(SelectedTask? task, bool reload = false)
    {
        if (!reload && selected?.Task == task?.Task) { if (task is not null) taskTitle.Text = task.Title; return; }
        if (editing is { } old) _ = SaveAsync(old);
        var ticket = ++selection; selected = task; editing = null; notes.Clear(); loadFailed = false;
        taskTitle.Text = task?.Title ?? "작업을 열어 주세요"; taskTitle.ToolTip = task?.Title;
        RenderTabs(); RenderEditor();
        if (task is null) { status.Text = "열린 작업에 메모가 연결됩니다"; return; }
        status.Text = "메모 불러오는 중…";
        try
        {
            var value = await request("notes.list", new { task = task.Task.Wire });
            if (selection != ticket) return;
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
            fork.Visibility = shared && notes.Any(n => !n.Deleted) ? Visibility.Visible : Visibility.Collapsed;
        }
        catch (Exception error) { if (selection == ticket) { loadFailed = true; status.Text = "불러오기 실패 · " + error.Message; add.IsEnabled = false; retry.Content = "다시 불러오기"; retry.Visibility = Visibility.Visible; } }
    }
    private async Task ForkAsync()
    {
        if (selected is null || loadFailed || !shared) return;
        if (editing is { } draft) await SaveAsync(draft);
        try
        {
            await request("notes.fork", new { task = selected.Task.Wire });
            status.Text = "??? ??????. ?? ? ??? ?? ?????.";
            await SelectTaskAsync(selected, reload: true);
        }
        catch (Exception error) { status.Text = "?? fork ?? ? " + error.Message; }
    }
    internal void Add()
    {
        if (selected is null || loadFailed) return;
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
            var button = new Button { Content = note.Title, MaxWidth = 150, Height = 32, Padding = new Thickness(10,0,10,0), Margin = new Thickness(0,0,6,6), Background = editing?.Note.Id == note.Id ? Brush(43, 53, 74) : Brushes.Transparent,
                BorderThickness = new Thickness(1), BorderBrush = editing?.Note.Id == note.Id ? Brush(91, 121, 172) : Brushes.Transparent,
                Foreground = editing?.Note.Id == note.Id ? Brush(162,193,255) : Brush(187,193,205), ToolTip = note.Title + " · 오른쪽 클릭으로 이름 변경 / 삭제" };
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
            var remove = new Button { Content = "×", Width = 28, Height = 28, Margin = new Thickness(0, 3, 0, 0), Padding = new Thickness(0), HorizontalContentAlignment = HorizontalAlignment.Center,
                Background = Brushes.Transparent, BorderThickness = new Thickness(0), Foreground = Brush(153,164,184), ToolTip = "항목 삭제", VerticalAlignment = VerticalAlignment.Top };
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
        var append = new Button { Name = "AddNoteCheck", Content = "+ 체크 항목 추가", ToolTip = "이 메모에 체크 항목 추가", Height = 32, Margin = new Thickness(0,8,0,0), Padding = new Thickness(8,0,8,0),
            Background = Brushes.Transparent, BorderThickness = new Thickness(0), Foreground = Brush(163,193,255), HorizontalAlignment = HorizontalAlignment.Left, IsEnabled = draft.Note.Items.Count < 500 };
        append.Click += (_, _) => { draft.Note.Items.Add(new()); Dirty(draft); RenderChecklist();
            if (checklist.Children.OfType<Grid>().LastOrDefault()?.Children.OfType<TextBox>().FirstOrDefault() is { } input) { input.Focus(); input.BringIntoView(); } };
        checklist.Children.Add(append);
    }
    private void Dirty(NoteDraft draft)
    {
        draft.Changed++; draft.Error = null; autosave.Stop(); autosave.Start(); UpdateStatus();
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
        try
        {
            while (draft.Changed != draft.Saved)
            {
                long version = draft.Changed;
                var snapshot = JsonSerializer.Deserialize<TaskNote>(JsonSerializer.Serialize(draft.Note, NoteDrafts.Json),NoteDrafts.Json)!;
                await Task.Run(() => recovery.Write(draft.Task, snapshot, version));
                var result = await request("notes.save", new { task = draft.Task.Wire, note_id = snapshot.Id, revision = snapshot.Revision,
                    title = snapshot.Title, kind = snapshot.Kind, body = snapshot.Body, items = snapshot.Items.Select(i => new { id = i.Id, text = i.Text, done = i.Done }) });
                if (result.S("state") == "conflict")
                {
                    var remote = result.Get("current").Deserialize<TaskNote>(NoteDrafts.Json);
                    if (remote is not null && !remote.Deleted && remote.Title == snapshot.Title && remote.Kind == snapshot.Kind && remote.Body == snapshot.Body &&
                        JsonSerializer.Serialize(remote.Items, NoteDrafts.Json) == JsonSerializer.Serialize(snapshot.Items, NoteDrafts.Json))
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
        finally { draft.Saving = false; UpdateStatus(); }
    }
    private void Rename(TaskNote note)
    {
        var title = Dialogs.Prompt(Window.GetWindow(this), "메모 이름 변경", "메모 탭 이름", note.Title);
        if (string.IsNullOrWhiteSpace(title) || title.Length > 80) return;
        Edit(note); note.Title = title.Trim(); Dirty(editing!); RenderTabs();
    }
    private async Task DeleteAsync(TaskNote note)
    {
        if (selected is null) return;
        var task = selected.Task; var key = DraftKey(task,note.Id);
        if (drafts.TryGetValue(key,out var draft)) { await SaveAsync(draft); if (draft.Changed != draft.Saved) return; }
        if (MessageBox.Show(Window.GetWindow(this), $"‘{note.Title}’ 메모를 삭제할까요? ⋯ 메뉴에서 복구할 수 있습니다.", "메모 삭제", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
        try
        {
            var result = await request("notes.delete",new { task = task.Wire, note_id = note.Id, revision = note.Revision });
            if (result.S("state") == "conflict") throw new InvalidOperationException("다른 창에서 변경되었습니다. 작업을 다시 열어 확인하세요.");
            note.Deleted = true; note.Revision = result.Get("note").GetProperty("revision").GetInt64();
            drafts.Remove(key); recovery.Remove(task,note.Id);
            if (selected?.Task != task) return;
            if (editing?.Note.Id == note.Id) { editing = null; if (notes.FirstOrDefault(n => !n.Deleted) is { } next) Edit(next); }
            RenderTabs(); RenderEditor();
        }
        catch (Exception error) { status.Text = error.Message; }
    }
    private async Task RestoreAsync()
    {
        if (selected is null) return;
        var task = selected.Task; var note = notes.LastOrDefault(n => n.Deleted);
        if (note is null) { status.Text = "복구할 메모가 없습니다"; return; }
        try
        {
            var result = await request("notes.restore",new { task = task.Wire, note_id = note.Id, revision = note.Revision });
            if (result.S("state") == "conflict") throw new InvalidOperationException("다른 창에서 변경되었습니다. 작업을 다시 열어 주세요.");
            note.Deleted = false; note.Revision = result.Get("note").GetProperty("revision").GetInt64();
            if (selected?.Task == task) Edit(note);
        }
        catch (Exception error) { status.Text = error.Message; }
    }
    internal void PreserveDrafts()
    {
        autosave.Stop();
        foreach (var draft in drafts.Values.Where(d => d.Changed != d.Saved)) recovery.Write(draft.Task,draft.Note,draft.Changed);
    }
    internal async Task FlushAsync()
    {
        autosave.Stop();
        foreach (var draft in drafts.Values.Where(d => d.Changed != d.Saved).ToArray())
        {
            while (draft.Saving) await Task.Delay(10);
            await SaveAsync(draft);
            if (draft.Changed != draft.Saved) throw new IOException(draft.Error);
        }
    }
}
