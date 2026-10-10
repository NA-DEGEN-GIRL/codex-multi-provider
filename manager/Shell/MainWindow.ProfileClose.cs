using System.Text.Json;
using System.Windows;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

// Revision 126: close one profile's managed Codex the way 완전 종료 closes all
// of them (quit request, leftover cleanup, SSH runtime stop), and remove a
// running profile by closing it first. The service holds the profile's
// launches meanwhile (profile.close_begin/close_end), so no warmup, task open
// or reconnect reopens it between the close and the removal.
public sealed partial class MainWindow
{
    // Profiles whose app this window is closing: never (re)attached meanwhile.
    private readonly HashSet<string> _closingProfiles = [];
    // Confirmation prompts of a profile close (fixtures replace the dialog).
    internal Func<string, string, bool>? ConfirmProfileClose { get; set; }

    private bool ConfirmClose(string message, string title, bool defaultOk = false) =>
        ConfirmProfileClose?.Invoke(message, title) ?? MessageBox.Show(this, message, title, MessageBoxButton.OKCancel,
            MessageBoxImage.Warning, defaultOk ? MessageBoxResult.OK : MessageBoxResult.Cancel) == MessageBoxResult.OK;

    private Task CloseProfileAppAsync() => CloseProfileAppAsync(RequireProfile());
    private async Task CloseProfileAppAsync(string id)
    {
        using var action = _profileActions.Enter(id, "Codex 닫기");
        var (state, profile) = await FreshProfileAsync(id);
        if (profile.ValueKind != JsonValueKind.Object)
            throw new InvalidOperationException("닫을 프로필을 찾지 못했습니다. 목록을 새로 확인해 주세요.");
        if (profile.S("status") != "running")
        {
            SetStatus($"{profile.S("alias", "선택한 프로필")} · Codex가 이미 닫혀 있습니다. 프로필을 선택하면 다시 엽니다.");
            return;
        }
        await CloseProfileCoreAsync(state, profile, remove: false);
    }

    private Task RemoveProfileAsync() => RemoveProfileAsync(RequireProfile());
    private async Task RemoveProfileAsync(string id)
    {
        using var action = _profileActions.Enter(id, "계정 제거");
        var (state, profile) = await FreshProfileAsync(id);
        if (profile.S("status") == "running") { await CloseProfileCoreAsync(state, profile, remove: true); return; }
        JsonElement result;
        try { result = await Request("profile.remove", new { profile_id = id }); }
        catch (ManagerException error) when (error.Code == "profile_running")
        {
            // Its app started after the state read: offer the same close first.
            (state, profile) = await FreshProfileAsync(id);
            if (profile.S("status") != "running") throw;
            await CloseProfileCoreAsync(state, profile, remove: true);
            return;
        }
        if (_selectedProfile == id) _selectedProfile = null;
        _loginStatuses.Remove(id); await RefreshAsync(); SetStatus(result.Message());
    }

    // A state read for this action only: the shown state can be a poll old.
    private async Task<(JsonElement State, JsonElement Profile)> FreshProfileAsync(string id)
    {
        var state = _client is not null ? await Request("state") : _state;
        return (state, state.Arr("profiles").FirstOrDefault(p => p.S("id") == id));
    }

    private async Task CloseProfileCoreAsync(JsonElement state, JsonElement listed, bool remove)
    {
        var id = listed.S("id");
        var alias = listed.S("alias", id);
        if (_client?.IsConnected != true || !state.Get("capabilities").B("profile_close"))
            throw new InvalidOperationException((remove ? "이 계정의 Codex 창을 닫은 뒤 제거하세요. " : "") +
                "실행 중인 관리 서비스가 이전 버전이라 프로필별 Codex 닫기를 지원하지 않습니다. " +
                "작업을 마친 뒤 완전 종료하고 새 버전으로 다시 열어 주세요.");
        if (_closing || _shutdownInProgress) return;
        if (_hostDeck.Find(id)?.IsTransitioning == true)
            throw new InvalidOperationException("창 연결이 진행 중입니다. 연결이 끝난 뒤 다시 닫아 주세요.");
        var remoteLifecycle = state.Get("capabilities").B("remote_profile_lifecycle");
        if (!ConfirmClose(ProfileClosePresentation.Prompt(listed, remove, remoteLifecycle),
                remove ? "Codex 닫고 계정 제거" : "이 프로필의 Codex 닫기")) return;
        if (_closing || _shutdownInProgress) return;
        // Task opens still checking this profile stop; the service drops its
        // waiting opens too (profile.close_begin ends them).
        CancelShortcutOpens();
        var selected = _selectedProfile == id && !_viewingCatalog;
        _closingProfiles.Add(id);
        var begun = false;
        var closed = false;
        WindowIdentity? detached = null;
        try
        {
            var begin = await Request("profile.close_begin", new { profile_id = id, remove });
            begun = true;
            var profile = begin.Get("profile");
            if (selected)
            {
                // Late show/attach answers for this profile are dropped.
                ++_navigation; _profileRequestTicket = null; _embedRequested = false; _expectedWindowLaunch = null;
            }
            _shownProfiles.Remove(id); _detachedProfiles.Remove(id);
            if (_hostDeck.Find(id) is { } host)
            {
                if (host.IsTransitioning) throw new InvalidOperationException("창 연결이 진행 중입니다. 연결이 끝난 뒤 다시 닫아 주세요.");
                if (host.IsAttached)
                {
                    host.DetachForClose();
                    if (host.IsAttached)
                        throw new InvalidOperationException("원본 창을 안전하게 분리하지 못해 닫지 않았습니다. " + host.LastError);
                }
                _attachedWindows.Remove(host, out detached);
                _windowLaunches.Remove(host);
                host.Visibility = Visibility.Hidden;
            }
            if (selected)
            {
                _empty.Visibility = Visibility.Visible;
                _empty.Text = $"{alias} 프로필의 Codex를 닫고 있습니다…";
            }
            var pid = profile.S("status") == "running" ? (int)profile.N("process_id") : 0;
            if (pid != 0) await QuitProfileAppAsync(alias, profile, pid, detached);
            await CleanupProfileAsync(alias, id, profile.S("generation"), observed: pid != 0);
            if (detached is not null) { detached.ClearMarker(); detached = null; }
            foreach (var window in _parked.Values.Where(w => pid != 0 && w.Pid == pid).ToArray())
            { window.ClearMarker(); _parked.Remove(window.Handle); }
            closed = true;
            Log($"{alias} · 이 프로필의 로컬 Codex 종료 확인");
            var remoteNote = await StopProfileRemoteAsync(alias, profile, remove, remoteLifecycle);
            var end = await Request("profile.close_end", new { profile_id = id, remove });
            begun = false;
            _attachAttempts.Reset(id);
            if (remove)
            {
                if (_selectedProfile == id) _selectedProfile = null;
                _loginStatuses.Remove(id);
            }
            else if (selected)
            {
                _empty.Visibility = Visibility.Visible;
                _empty.Text = $"{alias} 프로필의 Codex를 닫았습니다.\n프로필을 다시 선택하면 엽니다.";
            }
            await RefreshAsync();
            SetStatus((remove ? end.Message("계정을 목록에서 제거했습니다.")
                : $"{alias} 프로필의 Codex를 닫았습니다. 프로필은 목록에 남아 있으며 다시 선택하면 엽니다.") + remoteNote);
        }
        catch (Exception error)
        {
            if (begun)
            {
                // Releases the launch hold; nothing is removed.
                try { await Request("profile.close_end", new { profile_id = id, remove = false }); }
                catch (Exception releaseError) { Log($"{alias} · 프로필 닫기 보류 해제 확인 · {releaseError.Message}"); }
            }
            if (!closed)
            {
                // The app still runs: keep its window ours so the next poll reattaches it.
                if (detached is not null)
                {
                    if (detached.MatchesLifetime) _parked[detached.Handle] = detached;
                    else detached.ClearMarker();
                }
                _attachAttempts.Reset(id);
                if (begun && selected && _selectedProfile == id && !_closing) { _closingProfiles.Remove(id); BeginAttach(); Render(); }
            }
            if (remove && begun && !closed)
                throw new InvalidOperationException($"{alias} 프로필의 Codex를 닫지 못해 계정을 제거하지 않았습니다. {error.Message}", error);
            throw;
        }
        finally { _closingProfiles.Remove(id); }
    }

    // The full exit's quit request for one app: its window is asked to quit and
    // the app gets the same graceful wait. A window still open at the end has
    // not begun to quit; that close fails and the app keeps running.
    private async Task QuitProfileAppAsync(string alias, JsonElement profile, int pid, WindowIdentity? detached)
    {
        var hwnd = (nint)profile.N("window_handle");
        var created = profile.N("process_created");
        if (hwnd == 0 && detached is { } window && window.Pid == pid) { hwnd = window.Handle; created = 0; }
        if (hwnd == 0 && _parked.Values.FirstOrDefault(w => w.Pid == pid) is { } parked) { hwnd = parked.Handle; created = 0; }
        if (hwnd == 0)
        {
            // A launch whose window never appeared: the leftover cleanup ends it,
            // exactly as 완전 종료 does for such an app.
            Log($"{alias} · 확인된 Codex 창이 없어 남은 프로세스 정리로 종료합니다 · PID {pid}");
            return;
        }
        var limit = NativeWindowShutdown.GracefulWait(1);
        Log($"{alias} · 관리 중인 Codex 앱 종료 요청 · PID {pid} · 최대 {(int)limit.TotalSeconds}초 대기");
        var results = await NativeWindowShutdown.WaitAllAsync([pid], limit,
            (_, deadline) => NativeWindowShutdown.RequestAsync(_root, pid, hwnd, profile.S("executable_path"),
                created, deadline, NativeWindowShutdown.QuitGrace),
            _ => Log($"{alias} · 관리 중인 Codex 프로세스 종료 확인 · PID {pid}"),
            (exited, elapsed) => ShowShutdownProgress($"{alias} · " + NativeWindowShutdown.Progress(exited, 1, elapsed, limit)));
        var (_, outcome, failure) = results[0];
        if (failure is not null)
            throw new InvalidOperationException($"Codex 종료를 요청하지 못했습니다: {failure.Message}", failure);
        if (outcome == NativeExitOutcome.Unresponsive)
            throw new InvalidOperationException($"Codex가 {(int)limit.TotalSeconds}초 안에 종료 요청에 응답하지 않아 닫지 않았습니다. " +
                "잠시 뒤 다시 시도해 주세요. 실행 중인 작업은 그대로 유지했습니다.");
        if (outcome == NativeExitOutcome.Quitting)
            Log($"{alias} · 창은 닫혔지만 종료 정리가 끝나지 않은 Codex · PID {pid} · 남은 프로세스 정리로 마무리합니다");
    }

    // The full exit's leftover cleanup for one profile: every managed process
    // carrying this profile's own --user-data-dir, for the closed generation.
    private async Task CleanupProfileAsync(string alias, string id, string generation, bool observed)
    {
        if (generation == "") return;
        SetStatus($"{alias} · 남은 Codex 프로세스가 없는지 확인하고 있습니다…");
        try
        {
            // Longer than the service's own 12 s reap limit (as in full exit).
            using var stopDeadline = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            var cleanup = await _client!.RequestAsync("profile.cleanup", new { profile_id = id, generation },
                cancellationToken: stopDeadline.Token);
            if (observed || cleanup.S("state") != "already_stopped") Log($"남은 Codex 프로세스 정리 · {alias}");
        }
        catch (Exception error)
        {
            var reason = error is OperationCanceledException ? "정리 확인 시간 초과" : error.Message;
            Log($"프로세스 종료 확인 실패 · {alias} · {reason}");
            if (observed) throw new InvalidOperationException($"남은 Codex 프로세스를 정리하지 못했습니다: {reason}");
        }
    }

    // The full exit's SSH stop for one profile. A remote that is not confirmed
    // stopped can be left on the server, as 완전 종료 offers. Returns a note for
    // the final status line.
    private async Task<string> StopProfileRemoteAsync(string alias, JsonElement profile, bool remove, bool remoteLifecycle)
    {
        if (profile.S("generation") == "" || !profile.Arr("remote_bindings").Any(b => b.B("prepared"))) return "";
        if (!remoteLifecycle)
        {
            Log($"{alias} · 이전 서비스의 로컬 종료만 수행 · SSH 원격 실행 유지");
            return " SSH 원격 실행은 유지됩니다.";
        }
        var refused = await StopRemoteRuntimesAsync([profile]);
        if (refused.Count == 0) return "";
        var leave = ConfirmClose($"{alias} 프로필의 다음 SSH 원격 실행은 자동으로 종료하지 못했습니다.\n\n" + string.Join("\n", refused) +
            $"\n\n원격 실행을 서버에 남겨 두고 {(remove ? "계정을 제거" : "닫기를 마치")}려면 확인을 누르세요. 로컬 Codex는 이미 닫혔고, " +
            "다음에 이 프로필을 열 때 남은 원격 실행에 다시 연결합니다." +
            (remove ? " 계정을 제거하지 않으려면 취소를 누르세요." : ""),
            "원격 실행을 서버에 남겨 두고 닫기", defaultOk: true);
        if (!leave)
        {
            if (remove)
                throw new InvalidOperationException($"{alias} 프로필의 SSH 원격 실행을 종료하지 못해 계정을 제거하지 않았습니다. 로컬 Codex는 닫았습니다.");
            return " SSH 원격 실행의 종료는 확인하지 못했습니다.";
        }
        Log($"{alias} · SSH 원격 실행을 서버에 남겨 두고 닫기");
        return " SSH 원격 실행은 서버에 남겨 두었습니다.";
    }
}

internal static class ProfileClosePresentation
{
    // The shortcut list's per-task activity (working, waiting for approval or
    // input), local and over SSH, counted for one profile.
    internal static (int Count, string Detail) RunningTasks(JsonElement profile)
    {
        var activity = ShortcutCardData.Activity([profile]);
        var counts = activity.Values.GroupBy(v => v.State).ToDictionary(g => g.Key, g => g.Count());
        var detail = string.Join(", ", new[] { ("working", "작업 중"), ("waiting_approval", "승인 대기"), ("waiting_input", "입력 대기") }
            .Where(state => counts.ContainsKey(state.Item1)).Select(state => $"{state.Item2} {counts[state.Item1]}개"));
        return (activity.Count, detail);
    }

    internal static string Prompt(JsonElement profile, bool remove, bool remoteLifecycle)
    {
        var alias = profile.S("alias", profile.S("id"));
        var text = $"{alias} 프로필의 관리용 Codex를 닫습니다.\n" + (remove
            ? "닫기가 끝나면 이 계정을 목록에서 제거합니다. 로그인과 대화는 보존되며 ‘제거한 계정 복원’으로 다시 표시할 수 있습니다."
            : "프로필은 목록에 남고, 다시 선택하면 새로 엽니다.");
        var (count, detail) = RunningTasks(profile);
        text += count > 0
            ? $"\n\n주의: 이 프로필에서 진행 중인 작업이 {count}개 있습니다 ({detail}). 닫으면 진행 중인 답변이 중단되고, 보내지 않은 입력은 사라질 수 있습니다."
            : "\n\n진행 중인 작업은 확인되지 않았습니다. 보내지 않은 입력은 사라질 수 있습니다.";
        if (profile.Arr("remote_bindings").Any(b => b.B("prepared")))
            text += remoteLifecycle
                ? "\n\nSSH에서는 이 프로필의 현재 답변이 끝나기를 기다린 뒤 원격 실행을 종료합니다."
                : "\n\n현재 관리 서비스는 SSH 원격 실행을 종료하지 않고 유지합니다.";
        return text + "\n\n다른 프로필과 원래 Codex 앱은 유지됩니다. 계속하려면 확인을 누르세요.";
    }
}
