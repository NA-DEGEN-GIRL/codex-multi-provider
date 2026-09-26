namespace Codex.ControlCenter.Shell;

// The independent Codex viewport is not a WPF child. Only an explicit action
// from its foreground manager may transfer activation to the verified viewport.
internal static class ConversationCaptureActivation
{
    internal static nint Prepare(nint targetRoot, nint initialForeground, nint manager,
        Func<nint> foreground, Func<bool> managerIsValid, Func<bool> selectionIsCurrent,
        Action verifyTarget, Func<nint, bool> activate)
    {
        if (targetRoot == 0 || initialForeground == 0 || !selectionIsCurrent())
            throw new InvalidOperationException("선택한 Codex 창이 변경되었습니다. 작업을 다시 선택해 주세요.");
        verifyTarget();
        if (foreground() != initialForeground)
            throw new InvalidOperationException("다른 창으로 이동하여 작업 링크 확인을 취소했습니다.");
        if (initialForeground == targetRoot) return targetRoot;
        if (initialForeground != manager || !managerIsValid())
            throw new InvalidOperationException("작업 공간 창에서 해당 프로필을 선택한 뒤 다시 눌러 주세요.");
        // Recheck after validation, immediately before the one activation attempt.
        if (!selectionIsCurrent() || foreground() != initialForeground)
            throw new InvalidOperationException("선택한 창이 변경되어 작업 링크 확인을 취소했습니다.");
        if (!activate(targetRoot) || foreground() != targetRoot)
            throw new InvalidOperationException("Codex 입력 창으로 전환하지 못했습니다. 대화 화면을 확인한 뒤 다시 눌러 주세요.");
        verifyTarget();
        if (!selectionIsCurrent())
            throw new InvalidOperationException("선택한 작업이 변경되어 작업 링크 확인을 취소했습니다.");
        return targetRoot;
    }
}
