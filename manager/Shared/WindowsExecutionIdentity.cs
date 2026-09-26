using System.IO.Pipes;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Codex.ControlCenter.Shared;

/// <summary>
/// Token evidence read from the OS for one process or pipe peer. ``Elevated``
/// is null when the elevation query failed: an unreadable elevation is never
/// reported as a normal token.
/// </summary>
public readonly record struct ExecutionAuthority(bool? Elevated, string? UserSid)
{
    /// <summary>True only when both the elevation and user SID were read.</summary>
    public bool Known => Elevated is not null && !string.IsNullOrEmpty(UserSid);

    public string Mode => Elevated switch { true => "admin", false => "normal", _ => "unknown" };
}

/// <summary>
/// Actual Windows token probes for the current process and a connected named
/// pipe peer. Values always come from the OS handle, never from manager JSON:
/// a peer's own "pid" or "elevated" field is not evidence.
/// </summary>
public static class WindowsExecutionIdentity
{
    private const uint TokenQuery = 0x0008;
    private const uint ProcessQueryLimitedInformation = 0x1000;
    private const int TokenUserInformation = 1;
    private const int TokenElevationInformation = 20;
    private static readonly ExecutionAuthority Unknown = new(null, null);

    public static ExecutionAuthority UnknownAuthority => Unknown;

    /// <summary>True only when this process's elevated (UAC) token was proven.</summary>
    public static bool IsElevated => ProbeCurrent().Elevated == true;

    /// <summary>This process's elevation and user SID, read from its token.</summary>
    public static ExecutionAuthority Current() => ProbeCurrent();

    public static ExecutionAuthority ProbeCurrent() => OperatingSystem.IsWindows()
        ? ProbeToken(GetCurrentProcess())
        : Unknown;

    public static ExecutionAuthority ProbeProcess(int processId)
    {
        if (!OperatingSystem.IsWindows() || processId <= 0) return Unknown;
        using var process = OpenProcess(ProcessQueryLimitedInformation, false, processId);
        return process.IsInvalid ? Unknown : ProbeToken(process.DangerousGetHandle());
    }

    /// <summary>Resolves the connected pipe's server process and probes its token.</summary>
    public static ExecutionAuthority ProbePipeServer(SafePipeHandle? pipe) => ProbePipe(pipe);

    /// <summary>
    /// Pure comparison used before any manager request: the connected service
    /// must run as the same Windows user and at the same elevation. Unknown
    /// evidence is a mismatch, never a silent pass.
    /// </summary>
    public static void RequireMatchingAuthority(ExecutionAuthority current, ExecutionAuthority peer,
                                               string component = "관리 서비스")
    {
        if (current.Known && peer.Known
            && string.Equals(current.UserSid, peer.UserSid, StringComparison.OrdinalIgnoreCase)
            && current.Elevated == peer.Elevated)
            return;
        throw new ManagerException("authority_mismatch", MismatchMessage(current, peer, component));
    }

    /// <summary>Compares a peer against this process; returns the verified peer.</summary>
    public static ExecutionAuthority RequireCurrentMatches(ExecutionAuthority peer, string component = "관리 서비스")
    {
        var current = ProbeCurrent();
        RequireMatchingAuthority(current, peer, component);
        return peer;
    }

    private static ExecutionAuthority ProbePipe(SafePipeHandle? pipe)
    {
        if (!OperatingSystem.IsWindows() || pipe is null || pipe.IsInvalid || pipe.IsClosed) return Unknown;
        return GetNamedPipeServerProcessId(pipe, out var processId) && processId is > 0 and <= int.MaxValue
            ? ProbeProcess((int)processId)
            : Unknown;
    }

    private static ExecutionAuthority ProbeToken(IntPtr processHandle)
    {
        if (!OpenProcessToken(processHandle, TokenQuery, out var raw)) return Unknown;
        using var token = new SafeTokenHandle(raw);
        var elevated = ReadElevation(token);
        var user = ReadUserSid(token);
        // Any failed OS query leaves the whole authority unknown so a missing
        // elevation answer can never be mistaken for a normal token.
        return elevated is null || user is null ? Unknown : new ExecutionAuthority(elevated, user);
    }

    private static bool? ReadElevation(SafeTokenHandle token)
    {
        var buffer = Marshal.AllocHGlobal(sizeof(int));
        try
        {
            if (!GetTokenInformation(token, TokenElevationInformation, buffer, sizeof(int), out _)) return null;
            return Marshal.ReadInt32(buffer) != 0;
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static string? ReadUserSid(SafeTokenHandle token)
    {
        GetTokenInformation(token, TokenUserInformation, IntPtr.Zero, 0, out var size);
        if (size <= 0) return null;
        var buffer = Marshal.AllocHGlobal(size);
        try
        {
            if (!GetTokenInformation(token, TokenUserInformation, buffer, size, out _)) return null;
            var user = Marshal.PtrToStructure<TokenUser>(buffer);
            if (user.User.Sid == IntPtr.Zero || !ConvertSidToStringSidW(user.User.Sid, out var text)) return null;
            try { return Marshal.PtrToStringUni(text); }
            finally { LocalFree(text); }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static string ModeText(ExecutionAuthority value) =>
        value.Known ? (value.Elevated == true ? "관리자" : "일반") : "확인할 수 없는";

    private static string MismatchMessage(ExecutionAuthority current, ExecutionAuthority peer, string component) =>
        $"실행 권한이 다릅니다. 현재 창은 {ModeText(current)} 권한이고 연결된 {component}는 {ModeText(peer)} 권한입니다. " +
        "기존 작업은 그대로 유지됩니다. 관리 서비스는 한 번에 한 실행 권한으로만 동작하므로, 실행 중인 작업을 모두 마친 뒤 " +
        "'완전 종료'로 관리 서비스를 끝내고 원하는 권한으로 다시 열어 주세요.";

    private sealed class SafeTokenHandle : SafeHandleZeroOrMinusOneIsInvalid
    {
        public SafeTokenHandle(IntPtr handle) : base(true) => SetHandle(handle);

        protected override bool ReleaseHandle() => CloseHandle(handle);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SidAndAttributes
    {
        public IntPtr Sid;
        public uint Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct TokenUser
    {
        public SidAndAttributes User;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeProcessHandle OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit,
                                                        int processId);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetTokenInformation(SafeTokenHandle token, int informationClass, IntPtr information,
                                                   int length, out int returned);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint processId);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ConvertSidToStringSidW(IntPtr sid, out IntPtr text);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);
}
