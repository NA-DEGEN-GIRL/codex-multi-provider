using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal static class NotesSelfTest
{
    private static int checks;
    internal static async Task RunAsync(string report)
    {
        checks = 0;
        var root = Path.Combine(Path.GetTempPath(), "codex-notes-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "scripts"));
        File.WriteAllText(Path.Combine(root, "scripts", "control_center.py"), "raise RuntimeError('fixture must never start account adapter')");
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(40));
        var client = await ManagerClient.ConnectAsync(root, deadline.Token);
        var task = new SelectedTask(new("local", Guid.NewGuid().ToString()), "Codex 작업 공간 개발");
        var other = new SelectedTask(new("local", Guid.NewGuid().ToString()), "다른 작업");
        // This UI fixture tests existing task documents without starting an
        // account adapter. First-access native ancestry is covered by the
        // SQLite/WAL and service sharing tests using indexed fork metadata.
        SeedEmptyNotes(root, task.Task);
        SeedEmptyNotes(root, other.Task);
        var window = new MainWindow(root, fixture: true) { WindowState = WindowState.Normal, Width = 1600, Height = 1020, Left = -28000, Top = -28000, ShowActivated = false, ShowInTaskbar = false };
        try
        {
            window.UseFixture(JsonSerializer.SerializeToElement(new { profiles = new[] { new { id = Guid.NewGuid().ToString(), alias = "04", status = "ready" } }, shortcuts = new object[0] }));
            var panel = window.FixtureNotes(client); window.Show();
            window.UpdateLayout();
            Require(!panel.IsVisible, "notes start collapsed");
            Descendants(window).OfType<Button>().Single(button => Equals(button.Content, "작업 메모"))
                .RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            window.UpdateLayout();
            Require(panel.IsVisible, "task notes action opens the panel");
            // Existing checklist notes become editable as ordinary mixed notes,
            // without changing their identity or completion state.
            var legacyId = Guid.NewGuid().ToString(); var legacyItem = Guid.NewGuid().ToString();
            await client.RequestAsync("notes.save", new { task = task.Task.Wire, note_id = legacyId, revision = 0,
                title = "기존 체크리스트", kind = "checklist", body = "", items = new[] { new { id = legacyItem, text = "이미 완료한 항목", done = true } } });
            await panel.SelectTaskAsync(task); window.UpdateLayout();
            Require(NoteBody(panel).IsVisible && ItemInputs(panel).Count() == 1, "legacy checklist exposes text and checks together");
            Require(Descendants(panel).OfType<CheckBox>().Single().IsChecked == true, "legacy completion retained");
            NoteBody(panel).Text = "기존 체크리스트에도 설명을 적을 수 있다.";
            await panel.FlushAsync();
            var value = await client.RequestAsync("notes.list", new { task = task.Task.Wire }, deadline.Token);
            var legacy = value.GetProperty("notes")[0];
            Require(legacy.S("id") == legacyId && legacy.GetProperty("items")[0].S("id") == legacyItem && legacy.S("body") == NoteBody(panel).Text,
                "legacy content and IDs survive editing");
            Click(Descendants(panel).OfType<Button>().Single(b => b.Name == "AddNote")); window.UpdateLayout();
            var editor = NoteBody(panel);
            const string memoText = "다음 업데이트 준비\n\n프로필을 전환해도 메모와 체크 항목이 함께 유지되는지 확인한다.";
            editor.Text = memoText;
            Click(Descendants(panel).OfType<Button>().Single(b => b.Name == "AddNoteCheck")); window.UpdateLayout();
            var item = ItemInputs(panel).Single();
            item.Text = "프로필 전환 후 메모 유지 확인";
            var check = Descendants(panel).OfType<CheckBox>().Single(); check.IsChecked = true; Click(check);
            await panel.FlushAsync();
            value = await client.RequestAsync("notes.list", new { task = task.Task.Wire }, deadline.Token);
            var mixed = value.GetProperty("notes")[1];
            Require(value.GetProperty("notes").GetArrayLength() == 2 && mixed.S("kind") == "text", "adding a check does not create a separate note");
            Require(mixed.S("body") == memoText && mixed.GetProperty("items")[0].GetProperty("done").GetBoolean(), "text and check completion persist in one note");
            Require(mixed.GetProperty("items")[0].S("text") == item.Text, "check text persisted");
            Click(Descendants(panel).OfType<Button>().Single(b => Equals(b.ToolTip, "항목 삭제"))); window.UpdateLayout();
            await panel.FlushAsync();
            value = await client.RequestAsync("notes.list", new { task = task.Task.Wire });
            Require(value.GetProperty("notes")[1].S("body") == memoText && value.GetProperty("notes")[1].GetProperty("items").GetArrayLength() == 0,
                "removing the last check retains note text");
            Click(Descendants(panel).OfType<Button>().Single(b => b.Name == "AddNoteCheck")); window.UpdateLayout();
            ItemInputs(panel).Single().Text = "프로필 전환 후 메모 유지 확인";
            check = Descendants(panel).OfType<CheckBox>().Single(); check.IsChecked = true; Click(check);
            await panel.SelectTaskAsync(other); await panel.FlushAsync();
            Require((await client.RequestAsync("notes.list", new { task = other.Task.Wire })).GetProperty("notes").GetArrayLength() == 0, "notes do not leak to another task");
            await panel.SelectTaskAsync(new(task.Task, "이름이 변경된 작업")); window.UpdateLayout();
            Click(Descendants(panel).OfType<Button>().Single(b => Equals(b.Content, "메모 2"))); window.UpdateLayout();
            Require(NoteBody(panel).Text == memoText && ItemInputs(panel).Single().Text == item.Text && Descendants(panel).OfType<CheckBox>().Single().IsChecked == true,
                "returning restores mixed note and completion");
            value = await client.RequestAsync("notes.list", new { task = task.Task.Wire });
            Require(value.GetProperty("notes").GetArrayLength() == 2, "tabs preserve legacy and new notes");
            var textNote = value.GetProperty("notes")[1];
            var drafts = new NoteDrafts(root);
            var draftNote = textNote.Deserialize<TaskNote>(NoteDrafts.Json)!;
            draftNote.Body = "강제 종료 전에 저장한 초안";
            drafts.Write(task.Task, draftNote, 3);
            var stale = textNote.Deserialize<TaskNote>(NoteDrafts.Json)!;
            drafts.Write(task.Task, stale, 2); drafts.Remove(task.Task, stale.Id, 2);
            Require(drafts.Recover(task.Task).Single().Body == draftNote.Body, "older save cannot overwrite or remove newer close draft");
            await panel.SelectTaskAsync(other); await panel.SelectTaskAsync(task); await panel.FlushAsync();
            value = await client.RequestAsync("notes.list", new { task = task.Task.Wire });
            Require(value.GetProperty("notes")[1].GetProperty("body").GetString() == draftNote.Body, "draft recovers after reopening");
            Require(value.GetProperty("notes")[1].GetProperty("items")[0].GetProperty("done").GetBoolean(), "mixed checklist survives draft recovery");
            // Test optimistic conflict with an independent edit while this panel is open.
            Click(Descendants(panel).OfType<Button>().Single(b => Equals(b.Content, "메모 2"))); window.UpdateLayout();
            mixed = value.GetProperty("notes")[1];
            await client.RequestAsync("notes.save", new { task = task.Task.Wire, note_id = mixed.S("id"), revision = mixed.GetProperty("revision").GetInt64(), title = "다른 창의 변경", kind = "text", body = "다른 창에서 적은 내용", items = mixed.GetProperty("items") });
            NoteBody(panel).Text = memoText;
            await panel.FlushAsync();
            value = await client.RequestAsync("notes.list", new { task = task.Task.Wire });
            Require(value.GetProperty("notes").GetArrayLength() == 3, "simultaneous edits preserve both versions");
            Require(value.GetProperty("notes").EnumerateArray().Any(n => n.S("title") == "다른 창의 변경"), "conflicting remote edit retained");
            Require(value.GetProperty("notes").EnumerateArray().Any(n => n.S("body") == memoText && n.GetProperty("items")[0].GetProperty("done").GetBoolean()), "conflict copy retains both text and checklist");
            window.UpdateLayout();
            var layout = (FrameworkElement)panel;
            var visual = new DrawingVisual();
            using (var drawing = visual.RenderOpen())
                drawing.DrawRectangle(new VisualBrush(layout), null, new Rect(0, 0, layout.ActualWidth, layout.ActualHeight));
            var bitmap = new RenderTargetBitmap((int)Math.Ceiling(layout.ActualWidth), (int)Math.Ceiling(layout.ActualHeight), 96, 96, PixelFormats.Pbgra32); bitmap.Render(visual);
            var png = Path.ChangeExtension(report, ".png");
            using (var file = File.Create(png)) { var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); encoder.Save(file); }
            // Same task in a different profile is intentionally the same storage key.
            var service = await client.RequestAsync("supervisor.status");
            Require(service.S("engine") == "rust" && service.S("backend_status") == "not_started", "Rust notes run without account adapter");
            await VerifySaveBeforeForkAsync(root, supersede: false);
            await VerifySaveBeforeForkAsync(root, supersede: true);
            await VerifySharedNotesAsync(root, client);
            await VerifySplitSaveAsync(root);
            await VerifySplitRestoreAsync(root);
            await VerifyStaleRefreshAsync(root);
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, checks, root, png, service = "Rust named pipe + real WPF controls; no account or original app access" }));
        }
        finally
        {
            await client.RequestAsync("supervisor.retire", cancellationToken: deadline.Token);
            window.Close(); await client.DisposeAsync();
        }
    }
    private static void SeedEmptyNotes(string root, NoteTask task)
    {
        var directory = Path.Combine(root, "work", "control-center", "notes");
        Directory.CreateDirectory(directory);
        var key = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            JsonSerializer.SerializeToUtf8Bytes(task.Wire))).ToLowerInvariant();
        File.WriteAllText(Path.Combine(directory, key + ".json"),
            JsonSerializer.Serialize(new { version = 1, task = task.Wire, notes = Array.Empty<object>() }));
    }
    private static void SeedSharedNotes(string root, string groupId, NoteTask task, TaskNote[]? notes = null)
    {
        SeedEmptyNotes(root, task);
        var key = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(task.Wire))).ToLowerInvariant();
        File.WriteAllText(Path.Combine(root, "work", "control-center", "notes", key + ".json"),
            JsonSerializer.Serialize(new { version = 2, task = task.Wire, group_id = groupId, notes = Array.Empty<object>() }));
        if (notes is null) return;
        var directory = Path.Combine(root, "work", "control-center", "note-groups");
        Directory.CreateDirectory(directory);
        File.WriteAllText(Path.Combine(directory, groupId + ".json"),
            JsonSerializer.Serialize(new { version = 1, group_id = groupId, notes }, NoteDrafts.Json));
    }
    private static async Task WaitUntilAsync(Func<bool> condition)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        while (!condition()) await Task.Delay(10, deadline.Token);
    }
    private static async Task VerifySharedNotesAsync(string root, ManagerClient client)
    {
        var parent = new SelectedTask(new("local", Guid.NewGuid().ToString()), "공유 원본");
        var child = new SelectedTask(new("local", Guid.NewGuid().ToString()), "공유 분기");
        var group = Guid.NewGuid().ToString();
        var note = new TaskNote { Title = "함께 쓰는 메모", Body = "공유 시작" };
        SeedSharedNotes(root, group, parent.Task, [note]); SeedSharedNotes(root, group, child.Task);
        var childPanel = new TaskNotesPanel(root, (command, args) => client.RequestAsync(command, args));
        var parentPanel = new TaskNotesPanel(root, (command, args) => client.RequestAsync(command, args));
        var row = new StackPanel { Orientation = Orientation.Horizontal }; row.Children.Add(childPanel); row.Children.Add(parentPanel);
        childPanel.Width = parentPanel.Width = 380;
        var fixture = new Window { Content = row, Width = 800, Height = 650, Left = -28000, Top = -28000, ShowActivated = false, ShowInTaskbar = false };
        try
        {
            fixture.Show(); await childPanel.SelectTaskAsync(child); await parentPanel.SelectTaskAsync(parent); fixture.UpdateLayout();
            var split = Descendants(childPanel).OfType<Button>().Single(b => b.Name == "ForkNote");
            Require(split.IsVisible && Equals(split.Content, "메모 분리"), "shared notes expose explicit separation action");
            Require(Descendants(childPanel).OfType<TextBlock>().Single(t => t.Name == "NoteSharing").Text.StartsWith("공유 메모"), "sharing mode is visible separately from save status");
            NoteBody(childPanel).Text = "분기에서 함께 수정"; await childPanel.FlushAsync(); await parentPanel.RefreshSharedAsync();
            Require(NoteBody(parentPanel).Text == "분기에서 함께 수정", "idle parent panel refreshes child edits without reopening");
            Click(split); await WaitUntilAsync(() => childPanel.IsEnabled && split.Visibility == Visibility.Collapsed);
            var childValue = await client.RequestAsync("notes.list", new { task = child.Task.Wire });
            Require(!childValue.B("shared") && childValue.GetProperty("notes")[0].S("id") != note.Id, "explicit UI split creates independent note IDs");
            NoteBody(childPanel).Text = "분리 후 나만 수정"; await childPanel.FlushAsync(); await parentPanel.RefreshSharedAsync();
            Require(NoteBody(parentPanel).Text == "분기에서 함께 수정", "editing separated child leaves original shared notes intact");
            var parentValue = await client.RequestAsync("notes.list", new { task = parent.Task.Wire });
            var current = parentValue.GetProperty("notes")[0];
            await client.RequestAsync("notes.delete", new { task = parent.Task.Wire, note_id = note.Id, revision = current.GetProperty("revision").GetInt64() });
            await parentPanel.RefreshSharedAsync();
            Require(Descendants(parentPanel).OfType<Button>().Single(b => b.Name == "ForkNote").IsVisible,
                "separation remains available when shared notes are all deleted");
            var empty = new SelectedTask(new("local", Guid.NewGuid().ToString()), "빈 공유 메모");
            SeedSharedNotes(root, Guid.NewGuid().ToString(), empty.Task, []);
            await childPanel.SelectTaskAsync(empty); fixture.UpdateLayout();
            Require(split.IsVisible, "empty shared notes can be separated before adding content");
            Click(split); await WaitUntilAsync(() => childPanel.IsEnabled && split.Visibility == Visibility.Collapsed);
            var emptyValue = await client.RequestAsync("notes.list", new { task = empty.Task.Wire });
            Require(!emptyValue.B("shared") && emptyValue.GetProperty("notes").GetArrayLength() == 0, "empty split persists independent ownership");
        }
        finally { fixture.Close(); }
    }
    private static async Task VerifySplitSaveAsync(string root)
    {
        var task = new SelectedTask(new("local", Guid.NewGuid().ToString()), "저장 후 분리");
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var saved = "원본"; var shared = true; var noteId = Guid.NewGuid().ToString(); long revision = 0; var calls = new List<string>();
        var panel = new TaskNotesPanel(root, async (command, args) =>
        {
            var wire = JsonSerializer.SerializeToElement(args); calls.Add(command);
            if (command == "notes.save")
            {
                entered.TrySetResult(); await release.Task;
                saved = wire.S("body");
                return JsonSerializer.SerializeToElement(new { state = "saved", note = new { revision = ++revision } });
            }
            if (command == "notes.fork")
            {
                shared = false; noteId = Guid.NewGuid().ToString(); revision = 0;
                return JsonSerializer.SerializeToElement(new { state = "forked", shared = false });
            }
            if (command != "notes.list") throw new InvalidOperationException("Unexpected fixture command");
            return JsonSerializer.SerializeToElement(new { shared, notes = new[] { new TaskNote { Id = noteId, Title = "원본 메모", Body = saved, Revision = revision } } }, NoteDrafts.Json);
        });
        var fixture = new Window { Content = panel, Width = 440, Height = 600, Left = -28000, Top = -28000, ShowActivated = false, ShowInTaskbar = false };
        try
        {
            fixture.Show(); await panel.SelectTaskAsync(task); fixture.UpdateLayout();
            NoteBody(panel).Text = "분리 직전 초안";
            var save = panel.FlushAsync(); await entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            var split = Descendants(panel).OfType<Button>().Single(b => b.Name == "ForkNote"); Click(split);
            Require(!panel.IsEnabled && !calls.Contains("notes.fork"), "split disables edits and waits for pending save");
            release.TrySetResult(); await save;
            await WaitUntilAsync(() => panel.IsEnabled && split.Visibility == Visibility.Collapsed);
            Require(calls.Count(c => c == "notes.fork") == 1 && NoteBody(panel).Text == "분리 직전 초안", "split keeps latest saved draft and runs once");
        }
        finally { release.TrySetResult(); fixture.Close(); }
    }
    private static async Task VerifySplitRestoreAsync(string root)
    {
        var task = new SelectedTask(new("local", Guid.NewGuid().ToString()), "복구 후 분리");
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var note = new TaskNote { Title = "삭제된 메모", Body = "보존할 내용", Deleted = true };
        var shared = true; var splits = 0;
        var panel = new TaskNotesPanel(root, async (command, args) =>
        {
            if (command == "notes.restore")
            {
                entered.TrySetResult(); await release.Task; note.Deleted = false; note.Revision++;
                return JsonSerializer.SerializeToElement(new { state = "saved", note }, NoteDrafts.Json);
            }
            if (command == "notes.fork")
            {
                splits++; shared = false; note.Id = Guid.NewGuid().ToString(); note.Revision = 0;
                return JsonSerializer.SerializeToElement(new { state = "forked", shared });
            }
            if (command != "notes.list") throw new InvalidOperationException("Unexpected restore fixture command");
            return JsonSerializer.SerializeToElement(new { shared, notes = new[] { note } }, NoteDrafts.Json);
        });
        var fixture = new Window { Content = panel, Width = 440, Height = 600, Left = -28000, Top = -28000, ShowActivated = false, ShowInTaskbar = false };
        try
        {
            fixture.Show(); await panel.SelectTaskAsync(task); fixture.UpdateLayout();
            var restore = (Task)typeof(TaskNotesPanel).GetMethod("RestoreAsync", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)!.Invoke(panel, null)!;
            await entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            var split = Descendants(panel).OfType<Button>().Single(b => b.Name == "ForkNote");
            Click(split); Require(!split.IsEnabled && splits == 0, "in-flight restore prevents separation even on duplicate routed click");
            release.TrySetResult(); await restore; fixture.UpdateLayout();
            Require(split.IsEnabled && NoteBody(panel).Text == "보존할 내용", "restore completes before separation becomes available");
            Click(split); await WaitUntilAsync(() => panel.IsEnabled && split.Visibility == Visibility.Collapsed);
            Require(splits == 1 && NoteBody(panel).Text == "보존할 내용", "separation retains restored note without stale IDs");
        }
        finally { release.TrySetResult(); fixture.Close(); }
    }
    private static async Task VerifyStaleRefreshAsync(string root)
    {
        var task = new SelectedTask(new("local", Guid.NewGuid().ToString()), "늦은 새로고침");
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var reads = 0; var id = Guid.NewGuid().ToString();
        var panel = new TaskNotesPanel(root, async (command, args) =>
        {
            if (command == "notes.save") return JsonSerializer.SerializeToElement(new { state = "saved", note = new { revision = 2 } });
            if (command != "notes.list") throw new InvalidOperationException("Unexpected refresh fixture command");
            if (++reads > 1) await release.Task;
            return JsonSerializer.SerializeToElement(new { shared = true, notes = new[] { new TaskNote { Id = id, Title = "공유 메모", Body = "이전 내용", Revision = 1 } } }, NoteDrafts.Json);
        });
        var fixture = new Window { Content = panel, Width = 440, Height = 600, Left = -28000, Top = -28000, ShowActivated = false, ShowInTaskbar = false };
        try
        {
            fixture.Show(); await panel.SelectTaskAsync(task); fixture.UpdateLayout();
            var pending = panel.RefreshSharedAsync(); Require(reads == 2 && !pending.IsCompleted, "idle refresh request may remain in flight");
            NoteBody(panel).Text = "새로 입력한 초안"; release.TrySetResult(); await pending;
            Require(NoteBody(panel).Text == "새로 입력한 초안", "late refresh cannot replace edits made after the request");
            await panel.FlushAsync();
        }
        finally { release.TrySetResult(); fixture.Close(); }
    }
    private static async Task VerifySaveBeforeForkAsync(string root, bool supersede)
    {
        var parent = new SelectedTask(new("local", Guid.NewGuid().ToString()), "원본");
        var child = new SelectedTask(new("local", Guid.NewGuid().ToString()), "분기");
        var latest = new SelectedTask(new("local", Guid.NewGuid().ToString()), "다음 분기");
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var reads = new List<string>();
        var saved = "저장된 원본"; long revision = 0;
        var noteId = Guid.NewGuid().ToString();
        var panel = new TaskNotesPanel(root, async (command, args) =>
        {
            var wire = JsonSerializer.SerializeToElement(args);
            if (command == "notes.save")
            {
                entered.TrySetResult();
                await release.Task;
                saved = wire.GetProperty("body").GetString()!;
                return JsonSerializer.SerializeToElement(new { state = "saved", note = new { revision = ++revision } });
            }
            if (command != "notes.list") throw new InvalidOperationException("Unexpected note fixture command");
            var thread = wire.GetProperty("task").GetProperty("thread_id").GetString()!;
            reads.Add(thread);
            return JsonSerializer.SerializeToElement(new { shared = false,
                notes = new[] { new TaskNote { Id = noteId, Title = "원본 메모", Body = saved, Revision = revision } } }, NoteDrafts.Json);
        });
        var fixture = new Window { Content = panel, Width = 440, Height = 600,
            Left = -28000, Top = -28000, ShowActivated = false, ShowInTaskbar = false };
        try
        {
            fixture.Show();
            await panel.SelectTaskAsync(parent); fixture.UpdateLayout();
            NoteBody(panel).Text = "저장 중인 초안";
            var pendingSave = panel.FlushAsync();
            await entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
            NoteBody(panel).Text = "분기 직전 최신 초안";
            var opening = panel.SelectTaskAsync(child);
            var newest = supersede ? panel.SelectTaskAsync(latest) : Task.CompletedTask;
            Require(!opening.IsCompleted && reads.SequenceEqual(new[] { parent.Task.ThreadId }),
                "fork read waits asynchronously for an in-flight parent save");
            release.TrySetResult();
            await Task.WhenAll(pendingSave, opening, newest).WaitAsync(TimeSpan.FromSeconds(5));
            Require(NoteBody(panel).Text == "분기 직전 최신 초안", "fork receives the latest draft including edits during an earlier save");
            Require(reads.SequenceEqual(new[] { parent.Task.ThreadId, (supersede ? latest : child).Task.ThreadId }),
                "superseded selection cannot load or join the wrong task");
        }
        finally { release.TrySetResult(); fixture.Close(); }
    }
    private static TextBox NoteBody(TaskNotesPanel panel) => Descendants(panel).OfType<TextBox>().Single(t => t.Name == "NoteBody");
    private static IEnumerable<TextBox> ItemInputs(TaskNotesPanel panel) => Descendants(panel).OfType<TextBox>().Where(t => t.Tag is string);
    private static void Click(ButtonBase button) => button.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
    private static void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); checks++; }
    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (var i=0; i<VisualTreeHelper.GetChildrenCount(parent); i++)
        { var child = VisualTreeHelper.GetChild(parent,i); yield return child; foreach (var next in Descendants(child)) yield return next; }
    }
}
