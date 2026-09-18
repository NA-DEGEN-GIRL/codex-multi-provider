using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace Codex.ControlCenter.Shell;

internal static class ProfileOrderingSelfTest
{
    internal static async Task RunAsync(string report)
    {
        int checks = 0, selectedChanges = 0;
        void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); checks++; }
        var list = new ListBox { ItemTemplate = ProfileOrdering.Template(), Width = 252, Height = 440, Margin = new Thickness(12) };
        foreach (var id in new[] { "04", "02", "03", "01" }) list.Items.Add(new Choice(id, $"{id}\n주간 53% 남음 · 실행 중"));
        list.SelectedItem = list.Items[1];
        list.SelectionChanged += (_, _) => selectedChanges++;
        var saved = new List<ProfileMove>();
        var ordering = new ProfileOrdering(list, move => { saved.Add(move); return Task.CompletedTask; });
        var window = new Window { Content = new AdornerDecorator { Child = list }, Width = 300, Height = 500, Left = -28000, Top = -28000, ShowActivated = false, ShowInTaskbar = false };
        try
        {
            window.Show(); window.UpdateLayout();
            Point At(int index, double fraction)
            {
                var item = (ListBoxItem)list.ItemContainerGenerator.ContainerFromIndex(index);
                return item.TranslatePoint(new Point(16, item.ActualHeight * fraction), list);
            }
            ordering.Begin("01", At(3, .5)); ordering.Update(At(0, .1));
            Require(ordering.Target(At(0, .1)) == new ProfileMove("01", "04", "before"), "drag to top inserts before first row");
            await ordering.CompleteAsync(At(0, .1));
            Require(saved.Count == 1 && !ordering.IsInteracting, "drop submits one save and releases drag state");
            ordering.Begin("04", At(0, .5)); ordering.Update(At(3, .9));
            Require(ordering.Target(At(3, .9)) == new ProfileMove("04", "01", "after"), "drag to bottom inserts after last row");
            await ordering.CompleteAsync(At(3, .9));
            ordering.Begin("03", At(2, .5)); await ordering.CompleteAsync(At(2, .5));
            Require(saved.Count == 2, "click on grip does not save or open a profile");
            ordering.Begin("03", At(2, .5)); ordering.Update(At(0, .1)); await ordering.CompleteAsync(new Point(-20, 30));
            Require(saved.Count == 2, "dropping outside cancels");
            ordering.Begin("03", At(2, .5)); ordering.Update(At(0, .1)); ordering.Cancel();
            Require(!ordering.IsInteracting && saved.Count == 2, "escape/capture loss cancels cleanly");
            await ordering.MoveByAsync("04", -1); await ordering.MoveByAsync("01", 1);
            Require(saved.Count == 2, "first and last rows cannot move beyond edges");
            await ordering.MoveByAsync("03", -1); await ordering.MoveByAsync("02", 1);
            Require(saved[^2] == new ProfileMove("03", "02", "before") && saved[^1] == new ProfileMove("02", "03", "after"), "menu and keyboard moves use current adjacent rows");
            Require(selectedChanges == 0 && ((Choice)list.SelectedItem).Id == "02", "reordering never changes selected account");
            var pending = new TaskCompletionSource();
            var delayed = new ProfileOrdering(new ListBox { Items = { new Choice("a", "a"), new Choice("b", "b") } }, _ => pending.Task);
            var request = delayed.MoveByAsync("a", 1);
            Require(delayed.IsInteracting, "refresh blocked until order save completes");
            pending.SetResult(); await request;
            Require(!delayed.IsInteracting, "refresh resumes after save");
            var visual = new DrawingVisual();
            using (var drawing = visual.RenderOpen()) drawing.DrawRectangle(new VisualBrush(list), null, new Rect(0, 0, list.ActualWidth, list.ActualHeight));
            var bitmap = new RenderTargetBitmap((int)list.ActualWidth, (int)list.ActualHeight, 96, 96, PixelFormats.Pbgra32); bitmap.Render(visual);
            var png = Path.ChangeExtension(report, ".png");
            using (var file = File.Create(png)) { var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); encoder.Save(file); }
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, checks, png, isolation = "Synthetic WPF list; no account or app access" }));
        }
        finally { window.Close(); }
    }
}
