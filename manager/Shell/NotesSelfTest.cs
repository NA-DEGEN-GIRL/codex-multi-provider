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
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, checks, root, png, service = "Rust named pipe + real WPF controls; no account or original app access" }));
        }
        finally
        {
            await client.RequestAsync("supervisor.retire", cancellationToken: deadline.Token);
            window.Close(); await client.DisposeAsync();
        }
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
