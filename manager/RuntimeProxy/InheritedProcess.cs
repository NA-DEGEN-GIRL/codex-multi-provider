using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace Codex.ControlCenter.RuntimeProxy;

/// <summary>Pass the exact inherited stdio handles, without creating a second protocol reader.</summary>
internal static class InheritedProcess
{
    private const uint StartfUseStdHandles = 0x100;
    private const uint ExtendedStartupInfoPresent = 0x80000;
    private const uint CreateNoWindow = 0x08000000;
    private const uint DuplicateSameAccess = 2;
    private static readonly nint HandleListAttribute = 0x20002;

    public static async Task<int> RunAsync(string executable, IReadOnlyList<string> arguments)
    {
        var handles = new List<nint>();
        nint attributeList = 0;
        var attributesInitialized = false;
        nint handleList = 0;
        ProcessInformation process = default;
        try
        {
            foreach (var standardHandle in new[] { -10, -11, -12 })
            {
                var original = GetStdHandle(standardHandle);
                if (original == 0 || original == -1) throw new IOException("A required standard handle is unavailable.");
                if (!DuplicateHandle(GetCurrentProcess(), original, GetCurrentProcess(), out var inherited,
                    0, true, DuplicateSameAccess)) throw new Win32Exception(Marshal.GetLastWin32Error());
                handles.Add(inherited);
            }
            nuint required = 0;
            InitializeProcThreadAttributeList(0, 1, 0, ref required);
            attributeList = Marshal.AllocHGlobal(checked((nint)required));
            if (!InitializeProcThreadAttributeList(attributeList, 1, 0, ref required))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            attributesInitialized = true;
            handleList = Marshal.AllocHGlobal(handles.Count * IntPtr.Size);
            for (var index = 0; index < handles.Count; index++) Marshal.WriteIntPtr(handleList, index * IntPtr.Size, handles[index]);
            if (!UpdateProcThreadAttribute(attributeList, 0, HandleListAttribute, handleList,
                (nuint)(handles.Count * IntPtr.Size), 0, 0)) throw new Win32Exception(Marshal.GetLastWin32Error());
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
            // This is Windows argv quoting, never cmd.exe or PowerShell command text.
            var commandLine = new StringBuilder(string.Join(" ", new[] { executable }.Concat(arguments).Select(QuoteArgument)));
            var flags = ExtendedStartupInfoPresent | (GetConsoleWindow() == 0 ? CreateNoWindow : 0);
            if (!CreateProcess(executable, commandLine, 0, 0, true, flags, 0,
                Environment.CurrentDirectory, ref startup, out process)) throw new Win32Exception(Marshal.GetLastWin32Error());
            // Close only our duplicates promptly, so they cannot keep a redirected pipe alive.
            foreach (var handle in handles) CloseHandle(handle);
            handles.Clear();
            CloseHandle(process.Thread);
            process.Thread = 0;
            var wait = await Task.Run(() => WaitForSingleObject(process.Process, uint.MaxValue)).ConfigureAwait(false);
            if (wait != 0 || !GetExitCodeProcess(process.Process, out var exitCode))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return unchecked((int)exitCode);
        }
        finally
        {
            foreach (var handle in handles) CloseHandle(handle);
            if (process.Thread != 0) CloseHandle(process.Thread);
            if (process.Process != 0) CloseHandle(process.Process);
            if (attributeList != 0)
            {
                if (attributesInitialized) DeleteProcThreadAttributeList(attributeList);
                Marshal.FreeHGlobal(attributeList);
            }
            if (handleList != 0) Marshal.FreeHGlobal(handleList);
        }
    }

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
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint WaitForSingleObject(nint handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool GetExitCodeProcess(nint process, out uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)] private static extern bool CloseHandle(nint handle);
}
