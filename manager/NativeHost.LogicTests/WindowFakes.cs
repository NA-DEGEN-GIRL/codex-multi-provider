using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

// Compile-time stand-ins: no Dispatcher, HWND, native window or desktop is created.
namespace System.Windows
{
    public struct Rect { }
    public enum Visibility { Visible, Hidden, Collapsed }
}
namespace System.Windows.Input
{
    public class TraversalRequest { }
}
namespace System.Windows.Threading
{
    public enum DispatcherPriority { Background }
    public class Dispatcher { public void VerifyAccess() { } }
    public class DispatcherTimer
    {
        public DispatcherTimer(TimeSpan interval, DispatcherPriority priority, EventHandler callback, Dispatcher dispatcher) { }
        public void Stop() { }
        public void Start() { }
    }
}
namespace System.Windows.Interop
{
    public delegate nint HwndSourceHook(nint hwnd, int message, nint wp, nint lp, ref bool handled);
    public class HwndSource
    {
        public nint Handle => 100;
        public void AddHook(HwndSourceHook hook) { }
        public void RemoveHook(HwndSourceHook hook) { }
    }
    public abstract class HwndHost
    {
        public System.Windows.Visibility Visibility { get; set; }
        public bool Focusable { get; set; }
        public System.Windows.Threading.Dispatcher Dispatcher { get; } = new();
        public event EventHandler? GotKeyboardFocus;
        protected abstract HandleRef BuildWindowCore(HandleRef parent);
        protected abstract void DestroyWindowCore(HandleRef hwnd);
        protected virtual void OnWindowPositionChanged(System.Windows.Rect bounds) { }
        protected virtual bool TabIntoCore(System.Windows.Input.TraversalRequest request) => false;
        protected virtual nint WndProc(nint hwnd, int msg, nint wParam, nint lParam, ref bool handled) => 0;
        public void LoadFakeHost() => BuildWindowCore(new HandleRef(this, (nint)100));
        public void DeliverLayoutCallback() => OnWindowPositionChanged(default);
        public void DeliverFocusCallback() => GotKeyboardFocus?.Invoke(this, EventArgs.Empty);
        public (nint Result, bool Handled) DeliverMessage(int message)
        { bool handled=false; var result=WndProc((nint)200,message,0,0,ref handled); return (result,handled); }
    }
}
namespace Codex.ControlCenter.Shell
{
    internal static class NativeWindowInterop
    {
        internal const int GwlStyle=-16, GwlExStyle=-20, SwRestore=9, WmSetFocus=7, WmMouseActivate=0x21;
        internal const long WsChild=0x40000000, WsVisible=0x10000000, WsPopup=0x80000000L,
            WsClipChildren=0x02000000, WsClipSiblings=0x04000000, WsCaption=0xC00000,
            WsThickFrame=0x40000, WsMinimizeBox=0x20000, WsMaximizeBox=0x10000,
            WsSysMenu=0x80000, WsMinimize=0x20000000, WsMaximize=0x01000000,
            WsExAppWindow=0x40000, WsExWindowEdge=0x100, WsExClientEdge=0x200, WsExStaticEdge=0x20000;
        internal const uint GaParent=1, GaRoot=2, GwOwner=4, StillActive=259,
            ProcessQueryLimitedInformation=0x1000, SwpNoZOrder=4, SwpNoActivate=0x10, SwpFrameChanged=0x20;
        internal struct Rect { internal int Left, Top, Right, Bottom; }
        internal struct WindowPlacement { internal uint Length, ShowCmd; }
        internal static readonly nint Child=42, Container=200, Desktop=1;
        internal static int Pid => Environment.ProcessId + 1000;
        internal const string Executable = "C:\\fixture\\ChatGPT.exe";
        internal static nint Parent, Style, ExStyle;
        internal static bool Alive=true, Visible=true, AmbiguousGetParent, Enabled=true, Hung;
        internal static nint Focused, Popup, Foreground;
        internal static int ResizeCalls, FocusCalls, ActivationCalls;
        internal static string ChildClass = "";
        internal delegate bool EnumWindowsCallback(nint hwnd, nint param);
        internal static bool EnumChildWindows(nint parent, EnumWindowsCallback callback, nint param)
            => callback(43, param);
        internal static int GetClassNameW(nint hwnd, StringBuilder name, int length)
        { name.Append(ChildClass); return ChildClass.Length; }
        internal static Action? DuringActivation;
        internal const uint SwpAsyncWindowPos = 0x4000;
        internal static bool RejectResize;
        internal static uint LastResizeFlags;
        internal static int ClientWidth, ClientHeight;
        internal static bool Zoomed;
        internal static int RestoreCalls;
        internal static Rect ChildBounds;
        internal static Action? BeforeReparent, AfterReparent, DuringResize, DuringFocus;
        internal static nint? RejectNextParent;
        private static readonly Dictionary<string,nint> Properties=[];
        internal static void Reset()
        {
            Parent=0; Style=(nint)(WsVisible|WsCaption|WsThickFrame|WsSysMenu); ExStyle=(nint)WsExAppWindow;
            Alive=Visible=true; AmbiguousGetParent=false; ResizeCalls=FocusCalls=0;
            BeforeReparent=AfterReparent=DuringResize=null; RejectNextParent=null; Properties.Clear();
            DuringFocus=null; Focused=Popup=0; Foreground=Container; Enabled=true; Hung=false;
            ClientWidth=800; ClientHeight=600; RejectResize=false; LastResizeFlags=0;
            Zoomed=false; RestoreCalls=0;
            ActivationCalls=0; DuringActivation=null;
            ChildClass="";
            ChildBounds=new(){Left=10,Top=20,Right=650,Bottom=500};
        }
        internal static nint CreateWindowExW(uint ex, string cls, string title, uint style,
            int x,int y,int w,int h,nint parent,nint menu,nint instance,nint param) => Container;
        internal static bool DestroyWindow(nint hwnd) => true;
        internal static bool IsWindow(nint hwnd) => hwnd == Child ? Alive : hwnd != 0;
        internal static bool IsWindowVisible(nint hwnd) => Visible;
        internal static bool IsWindowEnabled(nint hwnd) => Enabled;
        internal static bool IsHungAppWindow(nint hwnd) => Hung;
        internal static nint GetLastActivePopup(nint hwnd) => Popup;
        internal static nint GetFocus() => Focused;
        internal static nint GetForegroundWindow() => Foreground;
        internal static bool SetForegroundWindow(nint hwnd) { ActivationCalls++; Foreground=hwnd; DuringActivation?.Invoke(); return true; }
        internal static bool IsChild(nint parent,nint child) => parent==Child && child==43;
        internal static bool IsIconic(nint hwnd) => false;
        internal static bool IsZoomed(nint hwnd) => Zoomed;
        internal static bool ShowWindow(nint hwnd,int command) { Visible=command!=0; if(command==SwRestore){RestoreCalls++;Zoomed=false;} return true; }
        internal static uint GetWindowThreadProcessId(nint hwnd,out uint pid) { pid=(uint)Pid; return 1; }
        internal static nint GetWindow(nint hwnd,uint command) => 0;
        internal static nint GetParent(nint hwnd) => AmbiguousGetParent ? 0 : Parent;
        internal static nint GetAncestor(nint hwnd,uint flags) => flags == GaParent ? (Parent == 0 ? Desktop : Parent) : (Parent == 0 ? Child : Container);
        internal static nint ReadStyle(nint hwnd,int index) => index == GwlStyle ? Style : ExStyle;
        internal static bool RejectChildBeforeParent;
        internal static void WriteStyle(nint hwnd,int index,nint value)
        {
            if(index==GwlStyle)
                Style=RejectChildBeforeParent && Parent==0 && (value.ToInt64()&WsChild)!=0 ?
                    (nint)((value.ToInt64()&~WsChild)|WsCaption|WsThickFrame|WsMinimizeBox|WsMaximizeBox) : value;
            else ExStyle=value;
        }
        internal static void Reparent(nint hwnd,nint parent)
        {
            var before=BeforeReparent; BeforeReparent=null; before?.Invoke();
            if (RejectNextParent==parent) { RejectNextParent=null; throw new InvalidOperationException("fixture parent change rejected"); }
            Parent=parent;
            var after=AfterReparent; AfterReparent=null; after?.Invoke();
        }
        internal static bool GetClientRect(nint hwnd,out Rect rect) { rect=new(){Right=ClientWidth,Bottom=ClientHeight}; return true; }
        internal static bool GetWindowRect(nint hwnd,out Rect rect) { rect=hwnd==Container ? new(){Right=ClientWidth,Bottom=ClientHeight} : ChildBounds; return true; }
        internal static bool GetWindowPlacement(nint hwnd,ref WindowPlacement placement) { placement.ShowCmd=Visible?1u:0u; return true; }
        internal static bool SetWindowPlacement(nint hwnd,in WindowPlacement placement) { Visible=placement.ShowCmd!=0; return true; }
        internal static bool SetWindowPos(nint hwnd,nint after,int x,int y,int cx,int cy,uint flags)
        {
            ResizeCalls++;
            LastResizeFlags=flags;
            if (!RejectResize) ChildBounds=new(){Left=x,Top=y,Right=x+cx,Bottom=y+cy};
            var callback=DuringResize; DuringResize=null; callback?.Invoke(); return true;
        }
        internal static nint SetFocus(nint hwnd) { FocusCalls++; DuringFocus?.Invoke(); Focused=hwnd; return hwnd; }
        internal static nint GetWindowDpiAwarenessContext(nint hwnd) => 2;
        internal static bool AreDpiAwarenessContextsEqual(nint first,nint second) => first==second;
        internal static int GetAwarenessFromDpiAwarenessContext(nint context) => 2;
        internal static uint GetDpiForWindow(nint hwnd) => 144;
        internal static bool SetPropW(nint hwnd,string name,nint value) { Properties[name]=value; return true; }
        internal static nint GetPropW(nint hwnd,string name) => Properties.GetValueOrDefault(name);
        internal static nint RemovePropW(nint hwnd,string name) { Properties.Remove(name,out var value); return value; }
        internal static SafeProcessHandle OpenProcess(uint access,bool inherit,uint pid) => new((nint)987,false);
        internal static bool QueryFullProcessImageNameW(SafeProcessHandle process,uint flags,StringBuilder name,ref uint size)
        { name.Append(Executable); return true; }
        internal static bool GetExitCodeProcess(SafeProcessHandle process,out uint code) { code=StillActive; return true; }
    }
}
