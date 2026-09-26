using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

internal static partial class Dialogs
{
    internal static bool? ExecutionMode(Window owner, bool administrator, string currentStatus)
    {
        var window = Create(owner, "Windows 실행 권한", 570, 455);
        var body = Body(window);
        body.Children.Add(new TextBlock { Text = "Windows 관리자 실행", FontSize = 22, Margin = new Thickness(0, 0, 0, 16) });
        body.Children.Add(Note(currentStatus));
        var preference = new CheckBox { Content = "다음 실행부터 관리자 권한 사용", IsChecked = administrator, Margin = new Thickness(0, 0, 0, 18) };
        body.Children.Add(preference);
        body.Children.Add(Note("Hyper-V 관리처럼 Windows 관리자 권한이 필요한 작업에 사용합니다. 실행할 때 Windows 권한 허용 창이 나타납니다."));
        body.Children.Add(Note("작업을 모두 마친 뒤 설정 및 관리의 ‘완전 종료 후 관리자 실행…’을 누르면 종료 확인 후 관리자 모드로 다시 열립니다. 제목줄의 X는 작업과 서비스 권한을 유지합니다."));
        body.Children.Add(Note("Codex의 전체 액세스 설정과 별개입니다. SSH 서버의 sudo 권한은 바뀌지 않습니다."));
        bool? result = null;
        var save = Button(body, "다음 실행 설정 저장", () => { result = preference.IsChecked == true; window.DialogResult = true; });
        save.IsDefault = true;
        var cancel = Button(body, "취소", window.Close); cancel.IsCancel = true;
        window.ShowDialog();
        return result;
    }
}
