using System.ComponentModel;
using System.Runtime.InteropServices;
using Codex.ControlCenter.Shell;

// The clipboard belongs to a window station. This test creates a private,
// noninteractive station/desktop before creating any HWND. It cannot read or
// overwrite the user's interactive clipboard, and never activates a desktop.
internal static class IsolatedClipboardTest
{
    public static void Run()
    {
        nint originalStation=GetProcessWindowStation(), originalDesktop=GetThreadDesktop(GetCurrentThreadId());
        nint station=0, desktop=0, owner=0;
        try
        {
            station=CreateWindowStationW("CodexClipboardFixture-"+Guid.NewGuid().ToString("N"),0,0x37F,0);
            Require(station!=0,"create private window station");
            Require(SetProcessWindowStation(station),"select private window station");
            desktop=CreateDesktopW("ClipboardFixture",null,0,0,0x0183,0);
            Require(desktop!=0,"create private desktop");
            Require(SetThreadDesktop(desktop),"select private desktop");
            Require(GetProcessWindowStation()==station && GetThreadDesktop(GetCurrentThreadId())==desktop,
                "clipboard test must stay in its private station and desktop");
            owner=CreateWindowExW(0,"STATIC","Clipboard fixture",0,0,0,0,0,(nint)(-3),0,0,0);
            Require(owner!=0,"create private message-only owner");
            const string text="창·설정 진행 기록\r\n04 · 작업 열기 완료 🎯\r\n최근 전체 로그\r\nRPC -32600";
            var first=ClipboardTextCopy.Write(owner,text);
            if(!first.Success) throw new Exception(first.Error);
            Require(Read(owner)==text,"actual Unicode clipboard content must match the Korean log");
            var large=string.Concat(Enumerable.Repeat(text+"\r\n",1000));
            var second=ClipboardTextCopy.Write(owner,large);
            if(!second.Success) throw new Exception(second.Error);
            Require(Read(owner)==large,"large clipboard snapshot must not be empty or truncated");
            Require(DestroyWindow(owner),"destroy clipboard owner"); owner=0;
            Require(Read(0)==large,"clipboard text must survive owner destruction without delayed rendering");
            Console.WriteLine($"PASS: real Unicode clipboard write/read, {large.Length:N0} characters, owner lifetime; private noninteractive window station only.");
        }
        finally
        {
            if(owner!=0) DestroyWindow(owner);
            SetThreadDesktop(originalDesktop);
            SetProcessWindowStation(originalStation);
            if(desktop!=0) CloseDesktop(desktop);
            if(station!=0) CloseWindowStation(station);
        }
    }
    private static string? Read(nint owner)
    {
        Require(OpenClipboard(owner),"open private clipboard for verification");
        try
        {
            var memory=GetClipboardData(13); Require(memory!=0,"Unicode clipboard format must exist");
            var pointer=GlobalLock(memory); Require(pointer!=0,"lock Unicode clipboard buffer");
            try { return Marshal.PtrToStringUni(pointer); } finally { GlobalUnlock(memory); }
        }
        finally {CloseClipboard();}
    }
    private static void Require(bool condition,string operation)
    { if(!condition) {int error=Marshal.GetLastPInvokeError();throw new Win32Exception(error,operation+" · Win32 "+error); } }
    [DllImport("user32.dll")] private static extern nint GetProcessWindowStation();
    [DllImport("user32.dll")] private static extern nint GetThreadDesktop(uint thread);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern nint CreateWindowStationW(string name,uint flags,uint access,nint security);
    [DllImport("user32.dll",SetLastError=true)] private static extern bool SetProcessWindowStation(nint station);
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern nint CreateDesktopW(string name,string? device,nint mode,uint flags,uint access,nint security);
    [DllImport("user32.dll",SetLastError=true)] private static extern bool SetThreadDesktop(nint desktop);
    [DllImport("user32.dll")] private static extern bool CloseDesktop(nint desktop);
    [DllImport("user32.dll")] private static extern bool CloseWindowStation(nint station);
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] private static extern nint CreateWindowExW(uint ex,string cls,string title,uint style,int x,int y,int w,int h,nint parent,nint menu,nint instance,nint param);
    [DllImport("user32.dll",SetLastError=true)] private static extern bool DestroyWindow(nint hwnd);
    [DllImport("user32.dll",SetLastError=true)] private static extern bool OpenClipboard(nint owner);
    [DllImport("user32.dll")] private static extern bool CloseClipboard();
    [DllImport("user32.dll")] private static extern nint GetClipboardData(uint format);
    [DllImport("kernel32.dll")] private static extern nint GlobalLock(nint memory);
    [DllImport("kernel32.dll")] private static extern bool GlobalUnlock(nint memory);
}
