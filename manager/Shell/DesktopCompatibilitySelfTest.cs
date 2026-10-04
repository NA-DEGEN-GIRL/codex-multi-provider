using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

internal static class DesktopCompatibilitySelfTest
{
    internal static List<string> Run(string root)
    {
        const string notice = "설치된 Codex 26.924.2738.0 호환성 확인 대기 · 검증된 26.917.9434.0 관리용 앱으로 실행합니다.";
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var window = new MainWindow(root, fixture: true, fixtureRequest: (_, _) =>
            throw new InvalidOperationException("Version presentation must not request a service operation."));
        T Field<T>(string name) => (T)typeof(MainWindow).GetField(name, flags)!.GetValue(window)!;
        static void Require(bool condition, string message)
        { if (!condition) throw new InvalidOperationException(message); }
        JsonElement State(string status = "running", string failure = "", bool login = false,
            string restart = "complete", bool needsRestart = false, bool fallback = true)
        {
            object Profile(string id) => new { id, alias = id, status, status_message = failure,
                desktop_compatibility_notice = fallback ? notice : "",
                login_health = new { blocks_launch = login },
                restart = new { phase = restart, message = "재시작 상태 확인 필요" },
                runtime_selection = new { restart_required = needsRestart, message = "새 실행 버전 적용 대기" } };
            return JsonSerializer.SerializeToElement(new { profiles = new[] { Profile("01"), Profile("02") },
                view_instances = new[] { Profile("viewer") } });
        }
        try
        {
            window.UseFixture(State());
            var attention = Field<TextBlock>("_attention");
            var compatibility = Field<TextBlock>("_desktopCompatibility");
            Require(attention.Visibility == Visibility.Collapsed && attention.Text == "",
                "A healthy fallback profile still shows an alarming main status.");
            Require(compatibility.Visibility == Visibility.Visible && compatibility.Text == "관리용 Codex 버전 안내\n" + notice &&
                Equals(compatibility.ToolTip, compatibility.Text) && !Equals(compatibility.Foreground, attention.Foreground),
                "Shared fallback versions must appear once with neutral styling and complete version details.");
            Require(compatibility.Parent is StackPanel settings && settings.Children.Contains(Field<Button>("_updateButton")),
                "The desktop version notice must be reachable beside the Codex version action in settings.");
            typeof(MainWindow).GetField("_selectedProfile", flags)!.SetValue(window, "02");
            typeof(MainWindow).GetMethod("Render", flags, Type.EmptyTypes)!.Invoke(window, []);
            Require(attention.Visibility == Visibility.Collapsed && compatibility.Text == "관리용 Codex 버전 안내\n" + notice,
                "Switching healthy profiles repeated or promoted the shared version notice.");
            foreach (var (state, expected) in new[]
            {
                (State(status: "failed", failure: "프로필 실행 실패"), "프로필 실행 실패"),
                (State(login: true), "로그인 확인 필요"),
                (State(restart: "attention"), "재시작 상태 확인 필요"),
                (State(needsRestart: true), "새 실행 버전 적용 대기")
            })
            {
                window.UseFixture(state);
                Require(attention.Visibility == Visibility.Visible && attention.Text.Contains(expected, StringComparison.Ordinal) &&
                    !attention.Text.Contains(notice, StringComparison.Ordinal), "Real profile attention was hidden: " + expected);
            }
            window.UseFixture(State(fallback: false));
            Require(compatibility.Visibility == Visibility.Collapsed && compatibility.Text == "" && attention.Visibility == Visibility.Collapsed,
                "An obsolete fallback version notice remained after it cleared.");
            return ["Shared desktop fallback versions appear once in settings with neutral styling across profile selection.",
                "Login, restart, runtime-version and execution failures remain visible as profile attention.",
                "Cleared desktop fallback notices disappear without a service request or profile restart."];
        }
        finally { window.Close(); }
    }
}
