using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace Codex.ControlCenter.SshProxy;

/// <summary>
/// Own a transport-only process tree and pass its exact inherited stdio handles.
/// This job must never own a detached remote app-server or another profile's process.
/// </summary>
internal static class InheritedProcess
{
    private const uint StartfUseStdHandles = 0x100;
    private const uint CreateSuspended = 0x4;
    private const uint ExtendedStartupInfoPresent = 0x80000;
    private const uint CreateNoWindow = 0x08000000;
    private const uint DuplicateSameAccess = 2;
    private const uint JobObjectLimitKillOnJobClose = 0x2000;
    private const int JobObjectExtendedLimitInformation = 9;
    private static readonly nint HandleListAttribute = 0x20002;
    private static readonly nint JobListAttribute = 0x2000D;

    public static async Task<int> RunAsync(string executable, IReadOnlyList<string> arguments)
    {
        var handles = new List<nint>();
        nint attributeList = 0, handleList = 0, jobList = 0, job = 0;
        var attributesInitialized = false;
        var childFinished = false;
        ProcessInformation process = default;
        try
        {
            // A null security descriptor makes this unnamed job handle non-inheritable.
            // Only this bootstrap owns it, so TerminateProcess on ssh.exe cannot orphan SSH.
            job = CreateJobObject(0, null);
            if (job == 0) throw LastError();
            var limits = new JobExtendedLimits
            {
                BasicLimitInformation = new JobBasicLimits { LimitFlags = JobObjectLimitKillOnJobClose },
            };
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, ref limits,
                (uint)Marshal.SizeOf<JobExtendedLimits>())) throw LastError();

            foreach (var kind in new[] { -10, -11, -12 })
            {
                var original = GetStdHandle(kind);
                if (original == 0 || original == -1)
                    throw new IOException("A required standard handle is unavailable.");
                if (!DuplicateHandle(GetCurrentProcess(), original, GetCurrentProcess(), out var inherited,
                    0, true, DuplicateSameAccess)) throw LastError();
                handles.Add(inherited);
            }
            nuint required = 0;
            InitializeProcThreadAttributeList(0, 2, 0, ref required);
            attributeList = Marshal.AllocHGlobal(checked((nint)required));
            if (!InitializeProcThreadAttributeList(attributeList, 2, 0, ref required)) throw LastError();
            attributesInitialized = true;
            handleList = Marshal.AllocHGlobal(handles.Count * IntPtr.Size);
            for (var index = 0; index < handles.Count; index++)
                Marshal.WriteIntPtr(handleList, index * IntPtr.Size, handles[index]);
            if (!UpdateProcThreadAttribute(attributeList, 0, HandleListAttribute, handleList,
                (nuint)(handles.Count * IntPtr.Size), 0, 0)) throw LastError();
            // Windows 10+ assigns this job atomically when the process is created. A separate
            // AssignProcessToJobObject call would leave a kill window containing an orphaned,
            // suspended Python process that still holds all three protocol pipe handles.
            jobList = Marshal.AllocHGlobal(IntPtr.Size);
            Marshal.WriteIntPtr(jobList, job);
            if (!UpdateProcThreadAttribute(attributeList, 0, JobListAttribute, jobList,
                (nuint)IntPtr.Size, 0, 0)) throw LastError();
            var startup = new StartupInfoEx
            {
                StartupInfo = new StartupInfo
                {
                    Size = (uint)Marshal.SizeOf<StartupInfoEx>(),
                    Flags = StartfUseStdHandles,
                    StdInput = handles[0], StdOutput = handles[1], StdError = handles[2],
                },
                AttributeList = attributeList,
            };
            // Windows argv quoting only; neither cmd.exe nor PowerShell parses this text.
            var commandLine = new StringBuilder(string.Join(" ", new[] { executable }.Concat(arguments).Select(QuoteArgument)));
            var flags = CreateSuspended | ExtendedStartupInfoPresent | (GetConsoleWindow() == 0 ? CreateNoWindow : 0);
            if (!CreateProcess(executable, commandLine, 0, 0, true, flags, 0,
                Environment.CurrentDirectory, ref startup, out process)) throw LastError();

            // Creation already assigned the job. Incompatible enclosing job constraints fail
            // CreateProcess atomically; no child can run or survive outside our ownership.
            if (ResumeThread(process.Thread) == uint.MaxValue) throw LastError();
            foreach (var handle in handles) CloseHandle(handle);
            handles.Clear();
            CloseHandle(process.Thread);
            process.Thread = 0;
            var wait = await Task.Run(() => WaitForSingleObject(process.Process, uint.MaxValue)).ConfigureAwait(false);
            if (wait != 0) throw LastError();
            childFinished = true;
            if (!GetExitCodeProcess(process.Process, out var exitCode)) throw LastError();
            return unchecked((int)exitCode);
        }
        finally
        {
            // Handle any startup failure explicitly; closing the job also handles all
            // suspended or already-running descendants, including on owner termination.
            if (process.Process != 0 && !childFinished) TerminateProcess(process.Process, 127);
            if (job != 0) CloseHandle(job);
            foreach (var handle in handles) CloseHandle(handle);
            if (process.Thread != 0) CloseHandle(process.Thread);
            if (process.Process != 0) CloseHandle(process.Process);
            if (attributeList != 0)
            {
                if (attributesInitialized) DeleteProcThreadAttributeList(attributeList);
                Marshal.FreeHGlobal(attributeList);
            }
            if (handleList != 0) Marshal.FreeHGlobal(handleList);
            if (jobList != 0) Marshal.FreeHGlobal(jobList);
        }
    }

    private static Win32Exception LastError() => new(Marshal.GetLastWin32Error());

    private static string QuoteArgument(string argument)
    {
        var result = new StringBuilder("\"");
        var slashes = 0;
        foreach (var character in argument)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"')
            {
                result.Append('\\', slashes * 2 + 1);
                result.Append('"');
            }
            else
            {
                result.Append('\\', slashes);
                result.Append(character);
            }
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfo
    {
        public uint Size;
        public nint Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute, Flags;
        public ushort ShowWindow, Reserved2Size;
        public nint Reserved2, StdInput, StdOutput, StdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfoEx { public StartupInfo StartupInfo; public nint AttributeList; }
    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation { public nint Process, Thread; public uint ProcessId, ThreadId; }
    [StructLayout(LayoutKind.Sequential)]
    private struct JobBasicLimits
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public nuint MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public nuint Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct JobExtendedLimits
    {
        public JobBasicLimits BasicLimitInformation;
        public IoCounters IoInfo;
        public nuint ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", SetLastError = true)] private static extern nint GetStdHandle(int kind);
    [DllImport("kernel32.dll")] private static extern nint GetCurrentProcess();
    [DllImport("kernel32.dll")] private static extern nint GetConsoleWindow();
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool DuplicateHandle(nint sourceProcess, nint sourceHandle,
        nint targetProcess, out nint targetHandle, uint desiredAccess, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint options);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool InitializeProcThreadAttributeList(nint list, int count, uint flags, ref nuint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool UpdateProcThreadAttribute(nint list, uint flags,
        nint attribute, nint value, nuint size, nint previousValue, nint returnSize);
    [DllImport("kernel32.dll")] private static extern void DeleteProcThreadAttributeList(nint list);
    [DllImport("kernel32.dll", EntryPoint = "CreateProcessW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool CreateProcess(string application, StringBuilder commandLine,
        nint processAttributes, nint threadAttributes, [MarshalAs(UnmanagedType.Bool)] bool inheritHandles, uint flags,
        nint environment, string currentDirectory, ref StartupInfoEx startup, out ProcessInformation process);
    [DllImport("kernel32.dll", EntryPoint = "CreateJobObjectW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint CreateJobObject(nint securityAttributes, string? name);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SetInformationJobObject(nint job, int informationClass,
        ref JobExtendedLimits information, uint informationLength);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint ResumeThread(nint thread);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint WaitForSingleObject(nint handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool GetExitCodeProcess(nint process, out uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool TerminateProcess(nint process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool CloseHandle(nint handle);
}
