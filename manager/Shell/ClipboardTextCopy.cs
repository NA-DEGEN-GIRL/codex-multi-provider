using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace Codex.ControlCenter.Shell;

// Publish an owned Unicode buffer immediately. No delayed OLE data provider or
// dependency on the focus/message loop of the embedded application's thread.
internal static class ClipboardTextCopy
{
    internal sealed record Attempt(bool Success, bool Retryable, string? Error = null);

    internal static async Task CopyAsync(string text, Func<string, Attempt> write,
        Func<Task>? wait = null)
    {
        if (string.IsNullOrWhiteSpace(text) || text.Contains('\0'))
            throw new ArgumentException("복사할 로그가 비어 있거나 올바르지 않습니다.");
        wait ??= () => Task.Delay(80);
        for (int attempt = 0; ; attempt++)
        {
            var result = write(text);
            if (result.Success) return;
            if (!result.Retryable || attempt == 4)
                throw new InvalidOperationException(result.Error ?? "클립보드에 로그를 복사하지 못했습니다.");
            await wait();
        }
    }

    internal static Attempt Write(nint owner, string text)
    {
        nint memory = 0;
        bool opened = false;
        try
        {
            // Allocate before clearing the previous clipboard contents.
            byte[] bytes = Encoding.Unicode.GetBytes(text + '\0');
            memory = GlobalAlloc(0x0002 /* GMEM_MOVEABLE */, (nuint)bytes.Length);
            if (memory == 0) throw new Win32Exception(Marshal.GetLastPInvokeError());
            nint buffer = GlobalLock(memory);
            if (buffer == 0) throw new Win32Exception(Marshal.GetLastPInvokeError());
            try { Marshal.Copy(bytes, 0, buffer, bytes.Length); }
            finally { GlobalUnlock(memory); }
            if (owner == 0) return new(false, false, "관리창의 클립보드 연결을 확인하지 못했습니다.");
            if (!OpenClipboard(owner)) return new(false, true, "다른 앱이 클립보드를 사용 중입니다. 저장된 로그 파일을 열 수 있습니다.");
            opened = true;
            if (!EmptyClipboard()) throw new Win32Exception(Marshal.GetLastPInvokeError());
            if (SetClipboardData(13 /* CF_UNICODETEXT */, memory) == 0)
                throw new Win32Exception(Marshal.GetLastPInvokeError());
            memory = 0; // Windows now owns the allocation.

            // Verify our eager buffer while still holding the clipboard lock.
            // This never asks another application's delayed renderer for data.
            nint published = GetClipboardData(13);
            buffer = published == 0 ? 0 : GlobalLock(published);
            if (buffer == 0) throw new InvalidOperationException("복사된 로그를 확인하지 못했습니다.");
            try
            {
                if (Marshal.PtrToStringUni(buffer, text.Length) != text)
                    throw new InvalidOperationException("복사된 로그가 원문과 다릅니다. 저장된 로그 파일을 열어 주세요.");
            }
            finally { GlobalUnlock(published); }
            return new(true, false);
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
        { return new(false, false, "로그 복사 실패 · " + ex.Message); }
        finally
        {
            if (opened) CloseClipboard();
            if (memory != 0) GlobalFree(memory);
        }
    }

    [DllImport("user32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool OpenClipboard(nint owner);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseClipboard();
    [DllImport("user32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool EmptyClipboard();
    [DllImport("user32.dll", SetLastError = true)] private static extern nint SetClipboardData(uint format, nint data);
    [DllImport("user32.dll")] private static extern nint GetClipboardData(uint format);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern nint GlobalAlloc(uint flags, nuint size);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern nint GlobalLock(nint memory);
    [DllImport("kernel32.dll")] [return: MarshalAs(UnmanagedType.Bool)] private static extern bool GlobalUnlock(nint memory);
    [DllImport("kernel32.dll")] private static extern nint GlobalFree(nint memory);
}
