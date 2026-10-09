using Microsoft.Win32.SafeHandles;

namespace Codex.ControlCenter.Shell;

// Why a managed Codex app went away, for the log: its exit code once the
// process has ended, and what that code usually means. Never kills or waits on
// the UI thread; a process that outlives its window is reported as running.
internal sealed class AppExit
{
    private const uint Synchronize = 0x00100000;
    internal static readonly TimeSpan Wait = TimeSpan.FromSeconds(15);
    internal int Pid { get; }
    /// <summary>The exit code, or null while the process still runs after Wait.</summary>
    internal Task<uint?> Code { get; }
    private AppExit(int pid, Task<uint?> code) { Pid = pid; Code = code; }

    /// <summary>Watch the process of an attachment that is still held open. A
    /// second handle opened while the first is held names the same lifetime,
    /// never a reused PID.</summary>
    internal static AppExit? Watch(int pid, SafeProcessHandle held, TimeSpan? wait = null)
    {
        if (pid <= 0 || held.IsClosed || held.IsInvalid) return null;
        var process = NativeWindowInterop.OpenProcess(Synchronize | NativeWindowInterop.ProcessQueryLimitedInformation, false, (uint)pid);
        if (process.IsInvalid) { process.Dispose(); return null; }
        return new AppExit(pid, WaitAsync(process, wait ?? Wait));
    }

    private static Task<uint?> WaitAsync(SafeProcessHandle process, TimeSpan wait)
    {
        var done = new TaskCompletionSource<uint?>(TaskCreationOptions.RunContinuationsAsynchronously);
        var handle = new ManualResetEvent(false) { SafeWaitHandle = new SafeWaitHandle(process.DangerousGetHandle(), false) };
        RegisteredWaitHandle? registration = null;
        registration = ThreadPool.RegisterWaitForSingleObject(handle, (_, timedOut) =>
        {
            uint? code = null;
            try
            {
                if (!timedOut && NativeWindowInterop.GetExitCodeProcess(process, out var exit) && exit != NativeWindowInterop.StillActive)
                    code = exit;
            }
            finally
            {
                registration?.Unregister(null);
                handle.Dispose();
                process.Dispose();
                done.TrySetResult(code);
            }
        }, null, wait, executeOnlyOnce: true);
        return done.Task;
    }

    /// <summary>A short Korean reading of a Windows exit code (Electron/Chromium and NT status values).</summary>
    internal static string Describe(uint? code) => code switch
    {
        null => "창이 닫혔지만 프로세스는 계속 실행 중",
        0 => "정상 종료",
        1 => "앱 오류 종료 또는 외부 종료 요청",
        0xC0000005 => "메모리 접근 위반으로 비정상 종료",
        0xC0000409 => "보안 검사 실패로 비정상 종료",
        0xC0000374 => "힙 손상으로 비정상 종료",
        0xC00000FD => "스택 오버플로로 비정상 종료",
        0x80000003 => "치명적 오류(중단점)로 비정상 종료",
        0xE0000008 => "메모리 부족으로 비정상 종료",
        0xC0000017 => "메모리 부족으로 비정상 종료",
        0xC000013A => "콘솔 중단 신호로 종료",
        0x40010004 => "외부에서 강제 종료",
        0xC0000142 => "DLL 초기화 실패로 시작 중 종료",
        _ => "기타 종료 코드"
    };

    internal static string Format(uint? code) => code is { } value
        ? $"종료 코드 {value} (0x{value:X8}) · {Describe(value)}" : Describe(null);
}
