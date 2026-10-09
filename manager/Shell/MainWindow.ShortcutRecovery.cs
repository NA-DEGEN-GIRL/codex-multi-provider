using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

public sealed partial class MainWindow
{
    private async Task VerifyShortcutNavigationAsync(int ticket, JsonElement result, JsonElement item, ShortcutOpenTrace trace)
    {
        if (!ShortcutNavigationRecovery.TryTarget(result, item, out var target)) { FinishShortcutTrace(trace, "sent", "selection_not_verifiable"); return; }
        var expected = WindowLaunchIdentity.From(result.Get("profile"));
        if (!Guid.TryParse(expected.ProfileId, out _) || !Guid.TryParse(expected.Generation, out _) || expected.ProcessId <= 0)
        { FinishShortcutTrace(trace, "sent", "launch_identity_unknown"); return; }
        var host = _host;
        string? initialRoute = null;
        bool superseded = false;
        bool Current() => !superseded && !_closing && ticket == _navigation && _selectedProfile == expected.ProfileId
            && !_viewingCatalog && _embedRequested && expected.Matches(Latest(Profile()));
        bool Attached() => _host == host && host.HasLiveAttachment
            && _windowLaunches.TryGetValue(host, out var launch) && launch == expected
            && _attachedWindows.TryGetValue(host, out var window) && window.MatchesLifetime;
        // Why a verification stopped early, for the log (first failing check of Current, then Attached).
        string CancelReason() => _closing ? "관리창 종료" : superseded ? "화면에서 다른 대화로 이동"
            : ticket != _navigation ? "새 이동 요청" : _selectedProfile != expected.ProfileId || _viewingCatalog ? "다른 화면 선택"
            : !_embedRequested ? "창 표시 해제" : !expected.Matches(Latest(Profile())) ? "창 실행 정보 변경"
            : !Attached() ? "창 연결 해제" : "확인 조건 변경";
        string CancelCode() => _closing ? "closing" : superseded ? "other_task_selected"
            : ticket != _navigation ? "replaced" : _selectedProfile != expected.ProfileId || _viewingCatalog ? "other_view"
            : !_embedRequested ? "view_released" : !expected.Matches(Latest(Profile())) ? "launch_changed"
            : !Attached() ? "window_detached" : "changed";
        async Task<bool> Selected()
        {
            try
            {
                var path = Path.Combine(_root, "work", "control-center", "instances", expected.ProfileId, "active-task.json");
                await using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 4096, true);
                if (file.Length > 8192) return false;
                using var doc = await JsonDocument.ParseAsync(file);
                if (!Current() || !Attached() || !ShortcutNavigationRecovery.SameWindow(doc.RootElement, expected, host.AttachedHandle.ToInt64())) return false;
                if (ShortcutNavigationRecovery.Matches(doc.RootElement, expected, host.AttachedHandle.ToInt64(), target)) return true;
                var route = doc.RootElement.S("host_id", "local") + "/" + doc.RootElement.S("thread_id");
                if (initialRoute is not null && route != initialRoute) superseded = true;
                initialRoute ??= route;
                return false;
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { return false; }
        }
        try
        {
            var outcome = await ShortcutNavigationRecovery.RunAsync(Current, Attached, Selected,
                token => host.NavigateAsync(target, token));
            if (outcome == ShortcutRecoveryResult.Cancelled)
            {
                var code = CancelCode();
                Log("대화 이동 · 화면 선택 확인 취소 · " + CancelReason());
                FinishShortcutTrace(trace, code == "replaced" ? "replaced" : "cancelled", code);
                return;
            }
            if (!Current()) { FinishShortcutTrace(trace, "cancelled", CancelCode()); return; }
            if (outcome is ShortcutRecoveryResult.Selected or ShortcutRecoveryResult.Recovered)
            {
                SetStatus(outcome == ShortcutRecoveryResult.Recovered
                    ? "작업 화면 이동을 복구했습니다. 진행 중인 작업은 유지됩니다."
                    : "선택한 작업을 열었습니다.");
                Log(outcome == ShortcutRecoveryResult.Recovered ? "대화 이동 · 직접 전달로 화면 선택 확인" : "대화 이동 · 화면 선택 확인");
                FinishShortcutTrace(trace, outcome == ShortcutRecoveryResult.Recovered ? "recovered" : "selected");
            }
            else if (outcome == ShortcutRecoveryResult.Unverified)
            {
                SetStatus("작업 화면 이동을 확인하지 못했습니다. 앱을 재시작하지 않았으며 진행 중인 작업은 유지됩니다.", true);
                Log("대화 이동 · 화면 선택 미확인 · 자동 재시도 종료");
                FinishShortcutTrace(trace, "unverified");
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidOperationException or JsonException)
        {
            if (Current()) SetStatus("작업 화면 이동을 확인하지 못했습니다. 진행 중인 작업은 유지됩니다.", true);
            FinishShortcutTrace(trace, "unverified", FailureCode(error));
        }
    }
}
