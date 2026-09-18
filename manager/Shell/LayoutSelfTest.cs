using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
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
            {"id":"fixture-01","alias":"01 · 개인 개발","status":"ready","policy":{"enabled":true,"model_ids":["a","b"],"desired_revision":1,"effective_revision":1},"usage":{"windows":[{"label":"5시간","remaining_percent":72},{"label":"주간","remaining_percent":46}]}},
            {"id":"fixture-02","alias":"02 · 작업용","status":"running","policy":{"enabled":false,"model_ids":[],"desired_revision":1,"effective_revision":1},"usage":{"windows":[{"label":"5시간","remaining_percent":28},{"label":"주간","remaining_percent":81}]}},
            {"id":"fixture-03","alias":"03 · 외부 모델","auth_mode":"external","external_model_name":"Example Model","status":"stopped","policy":{"enabled":true,"selection_mode":"external_only","model_ids":["a"],"desired_revision":2,"effective_revision":1},"usage":{"windows":[]}}
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
        var window = new MainWindow(root, fixture: true) { WindowState = WindowState.Normal, Width = 1440, Height = 960,
            Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false };
        window.UseFixture(fixture.RootElement.Clone());
        window.Show();
        await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
        var layout = (FrameworkElement)window.Content;
        var controls = Descendants(layout).ToArray();
        var badges = controls.OfType<TextBlock>().Where(block => block.Text.StartsWith("하위 에이전트 · ")).ToArray();
        if (badges.Length != 2 || !badges.Any(b => b.Text == "하위 에이전트 · 혼합") || !badges.Any(b => b.Text == "하위 에이전트 · 외부 전용 · 대기"))
            throw new InvalidOperationException("Profile policy badges were not rendered by the real list template.");
        foreach (var badge in badges) AssertVisible(badge, layout);
        foreach (var label in new[] { "프로필 관리  ▾", "바로가기 관리  ▾", "진행 기록", "로그 복사" })
            AssertVisible(controls.OfType<Button>().Single(button => Equals(button.Content, label)), layout);
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
        var bitmap = new RenderTargetBitmap((int)Math.Ceiling(layout.ActualWidth), (int)Math.Ceiling(layout.ActualHeight), 96, 96, PixelFormats.Pbgra32);
        bitmap.Render(layout);
        var png = Path.ChangeExtension(report, ".png");
        using (var file = File.Create(png)) { var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); encoder.Save(file); }
        window.Width = 1024; window.Height = 840;
        await window.Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
        foreach (var label in new[] { "프로필 관리  ▾", "바로가기 관리  ▾", "진행 기록", "로그 복사" })
            AssertVisible(Descendants(layout).OfType<Button>().Single(button => Equals(button.Content, label)), layout);
        settings.IsExpanded = true;
        window.UpdateLayout();
        AssertVisible(update, layout);
        foreach (var badge in Descendants(layout).OfType<TextBlock>().Where(block => block.Text.StartsWith("하위 에이전트 · ")))
            AssertVisible(badge, layout);
        File.WriteAllText(report, JsonSerializer.Serialize(new { ok = true, width = layout.ActualWidth, height = layout.ActualHeight, sidebar_dip = 272,
            grouped_actions_reachable = true, secondary_panels_collapsed = true, minimum_window_checked = true,
            source = "synthetic WPF controls only; no live profile or Codex process", png }, new JsonSerializerOptions { WriteIndented = true }));
        window.Close();
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
