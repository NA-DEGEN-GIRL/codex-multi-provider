using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows;
using Microsoft.Win32.SafeHandles;

namespace Codex.ControlCenter.Shell;

public sealed record NativeConversationCaptureResult(string? ThreadId, string? Error, bool ClipboardRestored)
{
    public bool Success => ThreadId is not null && Error is null;
}

/// <summary>
/// An explicit user action only. Never call this from a timer, discovery, selection, or a test
/// against a real Codex instance. From the foreground manager it may activate only
/// the verified current viewport. It never activates an unrelated window or submits a message.
/// </summary>
public static class NativeConversationCapture
{
    private static readonly SemaphoreSlim CaptureGate = new(1, 1);
    private static readonly Regex ThreadLink = new(
        @"\Acodex://threads/(?<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\z",
        RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
    private static readonly HashSet<string> MemoryFormats = new(StringComparer.Ordinal)
    {
        "HTML Format", "Rich Text Format", "Rich Text Format Without Objects", "PNG",
        "UniformResourceLocator", "UniformResourceLocatorW", "Preferred DropEffect",
        "Chromium Web Custom MIME Data Format", "CanIncludeInClipboardHistory",
        "CanUploadToCloudClipboard", "ExcludeClipboardContentFromMonitorProcessing"
    };

    public static async Task<NativeConversationCaptureResult> CaptureAsync(nint hwnd, int pid, string expectedExecutable,
        nint manager = 0, Func<bool>? selectionIsCurrent = null)
    {
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher is null || dispatcher.HasShutdownStarted)
            return Failure("Capture requires the running WPF application dispatcher.");
        if (!dispatcher.CheckAccess())
            return await dispatcher.InvokeAsync(() => CaptureAsync(hwnd, pid, expectedExecutable, manager, selectionIsCurrent)).Task.Unwrap();
        if (!await CaptureGate.WaitAsync(0))
            return Failure("Another conversation capture is already in progress.");

        nint clipboardWindow = 0;
        var saved = new List<SavedFormat>();
        try
        {
            if (hwnd == 0 || pid <= 0 || pid == Environment.ProcessId ||
                string.IsNullOrWhiteSpace(expectedExecutable) || !Path.IsPathFullyQualified(expectedExecutable))
                return Failure("Capture requires an external HWND, its PID, and an absolute executable path.");
            using SafeProcessHandle process = OpenProcess(0x1000, false, (uint)pid);
            if (process.IsInvalid)
                return Failure(Win32Error("Cannot verify the selected Codex process"));
            VerifyIdentity(hwnd, pid, expectedExecutable, process);
            nint foreground = GetForegroundWindow();
            bool CurrentSelection() => selectionIsCurrent?.Invoke() ?? manager == 0;
            bool ValidManager()
            {
                GetWindowThreadProcessId(manager, out uint managerPid);
                return manager != 0 && managerPid == Environment.ProcessId && IsWindowVisible(manager)
                    && IsWindowEnabled(manager) && GetAncestor(manager, 2) == manager;
            }
            void VerifyTarget()
            {
                VerifyIdentity(hwnd, pid, expectedExecutable, process);
                if (!IsWindowVisible(hwnd)) throw new InvalidOperationException("선택한 Codex 창이 보이지 않습니다.");
            }
            if (!CurrentSelection() || foreground == 0 || !IsWindowVisible(hwnd) ||
                (GetAncestor(hwnd, 2) != foreground && (foreground != manager || !ValidManager())))
                return Failure("작업 공간 창에서 해당 프로필을 선택한 뒤 다시 눌러 주세요.");

            // An old clipboard owner may hang while rendering a promised format. A dedicated
            // background STA can finish its read/cleanup later, but can never send input or
            // write the clipboard after the UI has timed out.
            ClipboardSnapshot snapshot = await ReadSnapshotAsync();
            saved.AddRange(snapshot.Formats);
            uint baselineSequence = snapshot.Sequence;
            clipboardWindow = CreateClipboardWindow();

            // Clipboard acquisition can yield to another window/profile. Verify the
            // original selection again before activating the independent viewport.
            foreground = ConversationCaptureActivation.Prepare(GetAncestor(hwnd, 2), foreground, manager,
                GetForegroundWindow, ValidManager, CurrentSelection, VerifyTarget, SetForegroundWindow);
            FocusVerifiedWindow(hwnd, foreground);
            if (!OwnsKeyboardFocus(hwnd, foreground) || GetClipboardSequenceNumber() != baselineSequence)
                return Failure("Focus or clipboard changed before capture. Nothing was sent to Codex.");
            if (new[] { 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x4C }.Any(key => (GetAsyncKeyState(key) & 0x8000) != 0))
                return Failure("Release Ctrl, Alt, Shift, Windows, and L before capturing the conversation.");

            VerifyIdentity(hwnd, pid, expectedExecutable, process);
            if (!CurrentSelection() || !OwnsKeyboardFocus(hwnd, foreground) || GetClipboardSequenceNumber() != baselineSequence)
                return Failure("Focus or clipboard changed immediately before capture. Nothing was sent to Codex.");
            Input[] chord = [Key(0x11), Key(0x12), Key(0x4C), Key(0x4C, true), Key(0x12, true), Key(0x11, true)];
            Marshal.SetLastPInvokeError(0);
            uint sent = SendInput((uint)chord.Length, chord, Marshal.SizeOf<Input>());
            int inputError = Marshal.GetLastPInvokeError();
            if (sent != chord.Length)
            {
                bool released = ReleaseInsertedKeys(chord, sent);
                return Failure($"Windows accepted {sent}/{chord.Length} shortcut events (error {inputError}). Input may be blocked by process privilege isolation; capture did not retry the shortcut." +
                    (released ? "" : " Windows also rejected modifier cleanup; physically press and release Ctrl and Alt to clear the key state."));
            }

            // Poll only for a fresh clipboard update. Old links are never accepted. The bounded
            // wait does not turn native delayed-rendering clipboard APIs into cancellable APIs.
            long deadline = Environment.TickCount64 + 1800;
            while (Environment.TickCount64 < deadline)
            {
                await Task.Delay(40);
                VerifyIdentity(hwnd, pid, expectedExecutable, process);
                if (!CurrentSelection() || GetForegroundWindow() != foreground)
                    return Failure("Foreground changed during capture. No window was reactivated and clipboard restoration was skipped.");
                if (GetClipboardSequenceNumber() == baselineSequence)
                    continue;
                if (!OpenClipboard(clipboardWindow))
                    continue;
                try
                {
                    uint capturedSequence = GetClipboardSequenceNumber();
                    if (capturedSequence == baselineSequence) continue;
                    nint owner = GetClipboardOwner();
                    GetWindowThreadProcessId(owner, out uint ownerPid);
                    if (owner == 0 || ownerPid != (uint)pid)
                        return Failure("Another process changed the clipboard. Its contents were kept; no conversation was captured.");
                    string? link = ReadUnicodeText();
                    string? threadId = ParseThreadId(link);
                    if (threadId is null)
                        return Failure("Codex did not copy a strict codex://threads/<UUID> link. This app version or screen may not support Copy chat link; clipboard contents were kept.");

                    // The comparison and replacement share the same clipboard lock. A later
                    // third-party copy cannot be overwritten between these two operations.
                    if (GetClipboardSequenceNumber() != capturedSequence || ReadUnicodeText() != link ||
                        GetClipboardOwner() != owner || !CurrentSelection() || GetForegroundWindow() != foreground)
                        return new(threadId, "Capture succeeded, but the clipboard or foreground changed; restoration was skipped.", false);
                    string? restoreError = RestoreClipboard(saved);
                    return new(threadId, restoreError, restoreError is null);
                }
                finally { CloseClipboard(); }
            }
            return Failure("Codex did not copy a conversation link within 1.8 seconds. The shortcut was sent once; this screen or app version may not support it.");
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException or ArgumentException or IOException or UnauthorizedAccessException or NotSupportedException or TimeoutException)
        {
            return Failure(ex.Message);
        }
        finally
        {
            foreach (SavedFormat format in saved) format.Dispose();
            if (clipboardWindow != 0) DestroyWindow(clipboardWindow);
            CaptureGate.Release();
        }
    }

    internal static string? ParseThreadId(string? text)
    {
        if (text is null || text.Length > 80) return null;
        Match match = ThreadLink.Match(text);
        return match.Success && Guid.TryParseExact(match.Groups["id"].Value, "D", out Guid id)
            ? id.ToString("D") : null;
    }

    private static NativeConversationCaptureResult Failure(string error) => new(null, error, false);
    private static string Win32Error(string message) => $"{message} (Windows error {Marshal.GetLastPInvokeError()}).";

    private static void VerifyIdentity(nint hwnd, int pid, string expected, SafeProcessHandle process)
    {
        GetWindowThreadProcessId(hwnd, out uint actualPid);
        if (!IsWindow(hwnd) || actualPid != (uint)pid || !GetExitCodeProcess(process, out uint exit) || exit != 259)
            throw new InvalidOperationException("The selected Codex window or process no longer has the expected identity.");
        var path = new StringBuilder(32768);
        uint length = (uint)path.Capacity;
        if (!QueryFullProcessImageNameW(process, 0, path, ref length))
            throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot inspect the selected process executable.");
        if (!string.Equals(Path.GetFullPath(path.ToString()), Path.GetFullPath(expected), StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("The selected window PID does not match the expected absolute executable path.");
    }

    private static void FocusVerifiedWindow(nint hwnd, nint foreground)
    {
        if (OwnsKeyboardFocus(hwnd, foreground)) return;
        uint current = GetCurrentThreadId();
        uint target = GetWindowThreadProcessId(hwnd, out _);
        bool attached = false;
        try
        {
            if (current != target)
            {
                if (!AttachThreadInput(current, target, true))
                    throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot focus the verified Codex child window.");
                attached = true;
            }
            if (GetForegroundWindow() != foreground || GetAncestor(hwnd, 2) != foreground)
                throw new InvalidOperationException("Foreground changed while preparing capture.");
            SetFocus(hwnd);
        }
        finally
        {
            if (attached) AttachThreadInput(current, target, false);
        }
    }

    private static bool OwnsKeyboardFocus(nint hwnd, nint foreground)
    {
        if (GetForegroundWindow() != foreground || GetAncestor(hwnd, 2) != foreground) return false;
        uint thread = GetWindowThreadProcessId(foreground, out _);
        var info = new GuiThreadInfo { Size = (uint)Marshal.SizeOf<GuiThreadInfo>() };
        return GetGUIThreadInfo(thread, ref info) && (info.Focus == hwnd || IsChild(hwnd, info.Focus));
    }

    private sealed record ClipboardSnapshot(uint Sequence, List<SavedFormat> Formats);

    private static nint CreateClipboardWindow()
    {
        nint window = CreateWindowExW(0, "STATIC", "Codex explicit link capture", 0,
            0, 0, 0, 0, (nint)(-3), 0, 0, 0);
        if (window == 0)
            throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot create the private clipboard owner.");
        return window;
    }

    private static async Task<ClipboardSnapshot> ReadSnapshotAsync()
    {
        var completion = new TaskCompletionSource<ClipboardSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        var worker = new Thread(() =>
        {
            nint owner = 0;
            bool clipboardOpen = false, transferred = false;
            var formats = new List<SavedFormat>();
            try
            {
                owner = CreateClipboardWindow();
                for (int attempt = 0; attempt < 5 && !completion.Task.IsCompleted; attempt++)
                {
                    if (OpenClipboard(owner)) { clipboardOpen = true; break; }
                    Thread.Sleep(30);
                }
                if (!clipboardOpen)
                    throw new InvalidOperationException("The clipboard is busy. Nothing was sent to Codex.");
                uint sequence = GetClipboardSequenceNumber();
                SnapshotClipboard(formats, () => completion.Task.IsCompleted);
                if (GetClipboardSequenceNumber() != sequence)
                    throw new InvalidOperationException("The clipboard changed while being saved. Nothing was sent to Codex.");
                CloseClipboard();
                clipboardOpen = false;
                transferred = completion.TrySetResult(new ClipboardSnapshot(sequence, formats));
            }
            catch (Exception ex)
            {
                completion.TrySetException(ex);
            }
            finally
            {
                if (clipboardOpen) CloseClipboard();
                if (!transferred) foreach (SavedFormat format in formats) format.Dispose();
                if (owner != 0) DestroyWindow(owner);
            }
        }) { IsBackground = true, Name = "Codex read-only clipboard snapshot" };
        worker.SetApartmentState(ApartmentState.STA);
        worker.Start();
        Task finished = await Task.WhenAny(completion.Task, Task.Delay(2000));
        if (finished != completion.Task && completion.TrySetCanceled())
            throw new TimeoutException("The clipboard owner did not provide its contents within 2 seconds. Nothing was sent to Codex; the background reader will clean up when Windows releases it.");
        return await completion.Task;
    }

    private static void SnapshotClipboard(List<SavedFormat> saved, Func<bool> cancelled)
    {
        var formats = new List<uint>();
        uint format = 0;
        while (true)
        {
            if (cancelled()) throw new TimeoutException("Clipboard snapshot was cancelled before input.");
            Marshal.SetLastPInvokeError(0);
            format = EnumClipboardFormats(format);
            if (format == 0)
            {
                if (Marshal.GetLastPInvokeError() != 0)
                    throw new Win32Exception(Marshal.GetLastPInvokeError(), "Cannot enumerate the clipboard.");
                break;
            }
            if (formats.Count >= 64 || !IsSupportedFormat(format))
                throw new NotSupportedException($"Clipboard format {FormatName(format)} cannot be preserved safely. Capture stopped before sending a shortcut. Copy plain text before trying again.");
            formats.Add(format);
        }
        ulong bytes = 0;
        foreach (uint item in formats)
        {
            if (cancelled()) throw new TimeoutException("Clipboard snapshot was cancelled before input.");
            nint source = GetClipboardData(item);
            if (source == 0)
                throw new InvalidOperationException($"Clipboard format {FormatName(item)} could not be materialized. Nothing was sent to Codex.");
            nint duplicate;
            if (item == 14) duplicate = CopyEnhMetaFileW(source, null);
            else
            {
                if (item is not (2 or 3 or 9))
                {
                    nuint size = GlobalSize(source);
                    if (size == 0 || (bytes += (ulong)size) > 64 * 1024 * 1024)
                        throw new NotSupportedException("The clipboard cannot be preserved within the 64 MiB capture limit. Nothing was sent to Codex.");
                }
                duplicate = OleDuplicateData(source, (ushort)item, 2 /* GMEM_MOVEABLE */);
            }
            if (duplicate == 0)
                throw new InvalidOperationException($"Clipboard format {FormatName(item)} could not be duplicated. Nothing was sent to Codex.");
            saved.Add(new SavedFormat(item, duplicate));
        }
    }

    private static bool IsSupportedFormat(uint format) => format is 1 or 2 or 3 or 7 or 8 or 9 or 13 or 14 or 15 or 16 or 17 ||
        format >= 0xC000 && MemoryFormats.Contains(FormatName(format));

    private static string FormatName(uint format)
    {
        var name = new StringBuilder(256);
        return GetClipboardFormatNameW(format, name, name.Capacity) > 0 ? name.ToString() : format.ToString();
    }

    private static string? ReadUnicodeText()
    {
        if (!IsClipboardFormatAvailable(13)) return null;
        nint data = GetClipboardData(13);
        nuint size = data == 0 ? 0 : GlobalSize(data);
        if (size < 2 || size > 4096 || size % 2 != 0) return null;
        nint pointer = GlobalLock(data);
        if (pointer == 0) return null;
        try
        {
            int length = checked((int)size / 2);
            string value = Marshal.PtrToStringUni(pointer, length) ?? "";
            int end = value.IndexOf('\0');
            return end < 0 ? null : value[..end];
        }
        finally { GlobalUnlock(data); }
    }

    private static string? RestoreClipboard(List<SavedFormat> saved)
    {
        if (!EmptyClipboard())
            return Win32Error("Conversation captured, but the previous clipboard could not be restored");
        var failed = new List<string>();
        foreach (SavedFormat format in saved)
        {
            if (SetClipboardData(format.Format, format.Handle) == 0)
                failed.Add(FormatName(format.Format));
            else format.TransferToSystem();
        }
        return failed.Count == 0 ? null : "Conversation captured, but Windows restored only part of the previous clipboard. Failed formats: " + string.Join(", ", failed);
    }

    private static Input Key(ushort key, bool up = false) => new() { Type = 1, Data = new InputUnion { Keyboard = new KeyboardInput { VirtualKey = key, Flags = up ? 2u : 0u } } };

    private static bool ReleaseInsertedKeys(Input[] chord, uint inserted)
    {
        var down = new HashSet<ushort>();
        foreach (Input input in chord.Take((int)Math.Min(inserted, (uint)chord.Length)))
        {
            if ((input.Data.Keyboard.Flags & 2) != 0) down.Remove(input.Data.Keyboard.VirtualKey);
            else down.Add(input.Data.Keyboard.VirtualKey);
        }
        // Only key-up events for keys this operation successfully pressed. Never replay the
        // chord or send a key-down after focus/privilege failure.
        Input[] release = down.Reverse().Select(key => Key(key, true)).ToArray();
        return release.Length == 0 || SendInput((uint)release.Length, release, Marshal.SizeOf<Input>()) == release.Length;
    }

    private sealed class SavedFormat(uint format, nint handle) : IDisposable
    {
        public uint Format { get; } = format;
        public nint Handle { get; private set; } = handle;
        public void TransferToSystem() => Handle = 0;
        public void Dispose()
        {
            if (Handle == 0) return;
            if (Format is 2 or 9) DeleteObject(Handle);
            else if (Format == 14) DeleteEnhMetaFile(Handle);
            else
            {
                if (Format == 3)
                {
                    nint pointer = GlobalLock(Handle);
                    if (pointer != 0)
                    {
                        try { DeleteMetaFile(Marshal.PtrToStructure<MetafilePicture>(pointer).Metafile); }
                        finally { GlobalUnlock(Handle); }
                    }
                }
                GlobalFree(Handle);
            }
            Handle = 0;
        }
    }

    [StructLayout(LayoutKind.Sequential)] private struct Input { public uint Type; public InputUnion Data; }
    [StructLayout(LayoutKind.Explicit)] private struct InputUnion
    {
        [FieldOffset(0)] public KeyboardInput Keyboard;
        [FieldOffset(0)] public MouseInput Mouse;
    }
    [StructLayout(LayoutKind.Sequential)] private struct KeyboardInput { public ushort VirtualKey, Scan; public uint Flags, Time; public nuint Extra; }
    [StructLayout(LayoutKind.Sequential)] private struct MouseInput { public int X, Y; public uint Data, Flags, Time; public nuint Extra; }
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct GuiThreadInfo
    {
        public uint Size, Flags;
        public nint Active, Focus, Capture, MenuOwner, MoveSize, Caret;
        public Rect CaretRect;
    }
    [StructLayout(LayoutKind.Sequential)] private struct MetafilePicture { public int MappingMode, XExt, YExt; public nint Metafile; }

    [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint count, Input[] input, int size);
    [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool SetForegroundWindow(nint hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool IsWindowEnabled(nint hwnd);
    [DllImport("user32.dll")] private static extern nint GetAncestor(nint hwnd, uint flags);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool IsChild(nint parent, nint child);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [DllImport("user32.dll")] private static extern nint SetFocus(nint hwnd);
    [DllImport("user32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool AttachThreadInput(uint from, uint to, [MarshalAs(UnmanagedType.Bool)] bool attach);
    [DllImport("user32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool GetGUIThreadInfo(uint thread, ref GuiThreadInfo info);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll", SetLastError = true)] private static extern SafeProcessHandle OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool GetExitCodeProcess(SafeProcessHandle process, out uint exit);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool QueryFullProcessImageNameW(SafeProcessHandle process, uint flags, StringBuilder name, ref uint size);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern nint CreateWindowExW(uint exStyle, string className, string name, uint style, int x, int y, int width, int height, nint parent, nint menu, nint instance, nint param);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool DestroyWindow(nint hwnd);
    [DllImport("user32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool OpenClipboard(nint owner);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool CloseClipboard();
    [DllImport("user32.dll", SetLastError = true)] private static extern uint EnumClipboardFormats(uint previous);
    [DllImport("user32.dll")] private static extern uint GetClipboardSequenceNumber();
    [DllImport("user32.dll")] private static extern nint GetClipboardOwner();
    [DllImport("user32.dll")] private static extern nint GetClipboardData(uint format);
    [DllImport("user32.dll", SetLastError = true)] private static extern nint SetClipboardData(uint format, nint data);
    [DllImport("user32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool EmptyClipboard();
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool IsClipboardFormatAvailable(uint format);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClipboardFormatNameW(uint format, StringBuilder name, int max);
    [DllImport("ole32.dll")] private static extern nint OleDuplicateData(nint source, ushort format, uint flags);
    [DllImport("kernel32.dll")] private static extern nuint GlobalSize(nint memory);
    [DllImport("kernel32.dll")] private static extern nint GlobalLock(nint memory);
    [DllImport("kernel32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool GlobalUnlock(nint memory);
    [DllImport("kernel32.dll")] private static extern nint GlobalFree(nint memory);
    [DllImport("gdi32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool DeleteObject(nint handle);
    [DllImport("gdi32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool DeleteMetaFile(nint handle);
    [DllImport("gdi32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool DeleteEnhMetaFile(nint handle);
    [DllImport("gdi32.dll", CharSet = CharSet.Unicode)] private static extern nint CopyEnhMetaFileW(nint source, string? fileName);
}
