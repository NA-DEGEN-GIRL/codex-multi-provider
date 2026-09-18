using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace Codex.ControlCenter.Shell;

internal static class PersonalSkillsSelfTest
{
    internal static async Task RunAsync(string report)
    {
        int checks = 0;
        void Require(bool value, string name) { if (!value) throw new InvalidOperationException(name); checks++; }
        var rows = new Dictionary<string, bool> { ["codex-handoff"] = true, ["3d-assets"] = true, ["game-audio"] = true, ["orient-repo"] = false };
        var deleted = new Dictionary<string, bool>();
        var descriptions = new Dictionary<string, string> { ["codex-handoff"] = "작업 진행 상황을 저장하고 다른 작업에서 이어갑니다.", ["3d-assets"] = "3D 에셋 생성 · 편집 · 리깅 · 애니메이션", ["game-audio"] = "게임용 음악과 효과음, 대사를 제작합니다.", ["orient-repo"] = "저장소 구조와 실행 방법을 확인합니다." };
        var requests = new List<string>();
        Task<JsonElement> Request(string command, object args)
        {
            requests.Add(command);
            var input = JsonSerializer.SerializeToElement(args);
            if (command == "skills.personal.set") rows[input.GetProperty("skill_id").GetString()!] = input.GetProperty("enabled").GetBoolean();
            if (command == "skills.personal.delete") { var id = input.GetProperty("skill_id").GetString()!; deleted[id] = rows[id]; rows.Remove(id); }
            if (command == "skills.personal.restore") { var id = input.GetProperty("deleted_id").GetString()!; rows[id] = deleted[id]; deleted.Remove(id); }
            return Task.FromResult(JsonSerializer.SerializeToElement(new {
                skills = rows.Select(r => new { id = r.Key, name = r.Key, enabled = r.Value, description = descriptions[r.Key], path = "fixture/" + r.Key }),
                deleted = deleted.Keys.Select(id => new { id, name = id }), sync = new { applied = 6, errors = Array.Empty<object>() } }));
        }
        var window = new PersonalSkillsWindow(Request, _ => true) { WindowStartupLocation = WindowStartupLocation.Manual, Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        try
        {
            window.Show(); await window.LoadAsync(); window.UpdateLayout();
            Button Action(string name, string id) => Descendants(window).OfType<Button>().Single(b => b.Name == name && Equals(b.Tag, id));
            Require(Descendants(window).OfType<Button>().Count(b => b.Name == "ToggleSkill") == 4, "personal skills show one toggle per row");
            Action("ToggleSkill", "orient-repo").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Require(rows["orient-repo"] && requests[^1] == "skills.personal.set", "disabled skill can be enabled");
            Action("ToggleSkill", "orient-repo").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Require(!rows["orient-repo"], "enabled skill can be disabled");
            var search = Descendants(window).OfType<TextBox>().Single(t => t.Name == "SkillSearch");
            search.Text = "game-audio"; window.UpdateLayout();
            Require(Descendants(window).OfType<Button>().Count(b => b.Name == "ToggleSkill") == 1, "search filters skill cards");
            search.Text = "";
            Action("DeleteSkill", "orient-repo").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Require(!rows.ContainsKey("orient-repo") && deleted.ContainsKey("orient-repo"), "delete enters recoverable list");
            Descendants(window).OfType<Expander>().Single().IsExpanded = true; window.UpdateLayout();
            Action("RestoreSkill", "orient-repo").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Require(rows.ContainsKey("orient-repo") && !rows["orient-repo"] && deleted.Count == 0, "restore keeps disabled state");
            window.Width = 560; window.UpdateLayout();
            foreach (var button in Descendants(window).OfType<Button>().Where(b => b.Name is "ToggleSkill" or "DeleteSkill"))
            {
                var point = button.TranslatePoint(new Point(), window);
                Require(point.X >= 0 && point.X + button.ActualWidth < window.ActualWidth, "actions stay inside narrow window");
            }
            window.Width = 760; window.UpdateLayout();
            var content = (FrameworkElement)window.Content;
            var visual = new DrawingVisual();
            using (var drawing = visual.RenderOpen()) drawing.DrawRectangle(new VisualBrush(content), null, new Rect(0, 0, content.ActualWidth, content.ActualHeight));
            var bitmap = new RenderTargetBitmap((int)content.ActualWidth, (int)content.ActualHeight, 96, 96, PixelFormats.Pbgra32); bitmap.Render(visual);
            var png = Path.ChangeExtension(report, ".png");
            using (var output = File.Create(png)) { var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); encoder.Save(output); }
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = true, checks, png, isolation = "Synthetic skills; no live profiles or skills modified" }));
        }
        finally { window.Close(); }
    }
    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        { var child = VisualTreeHelper.GetChild(parent, i); yield return child; foreach (var next in Descendants(child)) yield return next; }
    }
}
