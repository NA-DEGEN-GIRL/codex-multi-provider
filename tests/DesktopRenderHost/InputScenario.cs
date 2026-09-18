using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using Codex.ControlCenter.Shell;

// Actual mouse/keyboard input, only in a disposable, unauthenticated fixture.
// Every input is conditional on this test's foreground HWND. No chat is sent.
internal static class InputScenario
{
    [DllImport("user32.dll")] static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] static extern nint WindowFromPoint(Point p);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] static extern nint GetWindow(nint hwnd,uint command);
    [DllImport("user32.dll")] static extern nint GetParent(nint hwnd);
    [DllImport("user32.dll")] static extern nint GetWindowLongPtrW(nint hwnd,int index);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(nint hwnd);
    [DllImport("user32.dll")] static extern nint GetAncestor(nint hwnd,uint flag);
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint from,uint to,bool attach);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out Point p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(nint hwnd, out Rect r);
    [DllImport("user32.dll")] static extern bool GetClientRect(nint hwnd, out Rect r);
    [DllImport("user32.dll")] static extern bool ClientToScreen(nint hwnd,ref Point point);
    static bool GetClientScreenRect(nint hwnd,out Rect r) {var p=new Point();GetClientRect(hwnd,out r);ClientToScreen(hwnd,ref p);r.Right+=p.X;r.Bottom+=p.Y;r.Left=p.X;r.Top=p.Y;return true;}
    [DllImport("user32.dll")] static extern uint SendInput(uint count, Input[] inputs, int size);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool PostMessageW(nint hwnd,uint message,nuint wParam,nint lParam);
    [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint thread, ref Gui info);
    [DllImport("user32.dll")] static extern nint GetKeyboardLayout(uint thread);
    [DllImport("user32.dll")] static extern int GetKeyboardLayoutList(int count, [Out] nint[] layouts);
    [DllImport("imm32.dll")] static extern nint ImmGetDefaultIMEWnd(nint hwnd);
    [DllImport("user32.dll")] static extern nint SendMessageTimeoutW(nint hwnd,uint msg,nuint wp,nint lp,uint flags,uint timeout,out nuint result);
    [StructLayout(LayoutKind.Sequential)] struct Point { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct Gui { public uint Size, Flags; public nint Active, Focus, Capture, Menu, Move, Caret; public Rect Rect; }
    [StructLayout(LayoutKind.Sequential)] struct Input { public uint Type; public Payload Data; }
    [StructLayout(LayoutKind.Explicit)] struct Payload
    {
        [FieldOffset(0)] public Mouse Mouse;
        [FieldOffset(0)] public Key Key;
    }
    [StructLayout(LayoutKind.Sequential)] struct Mouse { public int X,Y; public uint Data,Flags,Time; public nuint Extra; }
    [StructLayout(LayoutKind.Sequential)] struct Key { public ushort Vk,Scan; public uint Flags,Time; public nuint Extra; }

    internal static int Run(string[] args)
    {
        string root = Path.GetFullPath(args[0]);
        var entries = JsonSerializer.Deserialize<Entry[]>(File.ReadAllText(Path.Combine(root,"input-fixtures.json")))!;
        if (entries.Length != 3 || entries.Any(e => !Path.GetFullPath(e.Run).StartsWith(root + Path.DirectorySeparatorChar)))
            throw new InvalidOperationException("Only three private fixtures are accepted.");
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        var grid = new Grid(); var panel = new StackPanel { Orientation = Orientation.Horizontal };
        var body = new Grid(); grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(45) });
        grid.RowDefinitions.Add(new RowDefinition()); grid.Children.Add(panel); Grid.SetRow(body,1); grid.Children.Add(body);
        var window = new Window { Title = "Disposable Codex input test", Content = grid, Width=850, Height=600, Left=60,Top=60, ShowActivated=false };
        nint previous=GetForegroundWindow(); GetCursorPos(out var cursor);
        var hosts=new List<NativeWindowHost>(); var log=new List<string>(); var checks=new List<object>();
        object? mouse=null; int selected=0; bool passed=false; nint rootHwnd=0,inputForeground=0;
        void Select(int index) { for(int i=0;i<hosts.Count;i++) hosts[i].Visibility=i==index?Visibility.Visible:Visibility.Hidden; selected=index; }
        foreach(var entry in entries)
        {
            int index=hosts.Count;
            var host=new NativeWindowHost { Visibility=Visibility.Hidden };
            typeof(NativeWindowHost).GetProperty("WindowStateDirectory",BindingFlags.Instance|BindingFlags.NonPublic)!.SetValue(host,Path.Combine(entry.Run,"work/control-center/window-hosts"));
            host.Diagnostic+=s=>log.Add($"{index}: {s}"); hosts.Add(host); body.Children.Add(host);
            var button=new Button { Content=$"Profile {index+1}", Width=150 };
            button.Click+=(_,_)=>Select(index); panel.Children.Add(button);
        }
        window.Loaded+=async(_,_)=>
        {
            try
            {
                rootHwnd=new WindowInteropHelper(window).Handle;
                inputForeground=rootHwnd;
                for(int i=0;i<entries.Length;i++)
                {
                    var e=entries[i]; nint handle=0; var deadline=DateTime.UtcNow.AddSeconds(12);
                    while(handle==0&&DateTime.UtcNow<deadline)
                    {
                        try { using var doc=JsonDocument.Parse(File.ReadAllText(Path.Combine(e.Run,"status.json"))); handle=nint.Parse(doc.RootElement.GetProperty("hwnd").GetString()!); }
                        catch(Exception error) when(error is IOException or JsonException or KeyNotFoundException){}
                        if(handle==0) await Task.Delay(50);
                    }
                    Select(i); body.UpdateLayout();
                    var layoutDeadline=DateTime.UtcNow.AddSeconds(3);
                    while(!hosts[i].IsLayoutReady && DateTime.UtcNow<layoutDeadline) await Task.Delay(50);
                    if(!hosts[i].Attach(handle,e.Pid,Path.Combine(e.Run,"app/ChatGPT.exe"),out _,allowHidden:true)) throw new Exception(hosts[i].LastError);
                }
                await Task.Delay(3500);
                ActivateFixture(rootHwnd);
                await Task.Delay(300);
                passed=true;
                if(!args.Contains("--ime-only"))
                {
                foreach(int index in new[]{0,1,2,0,2,1})
                {
                    // Real sidebar click, then a real click inside the test editor.
                    await ClickElement((FrameworkElement)panel.Children[index]); await Task.Delay(1600);
                    if(selected!=index) throw new Exception("Profile sidebar failed to switch.");
                    var host=hosts[index]; GetClientScreenRect(host.AttachedHandle,out var bounds);
                    checks.Add(new{index,hit=WindowFromPoint(new Point{X=bounds.Left+190,Y=bounds.Top+180}).ToString(),expected=host.AttachedHandle.ToString(),visible=IsWindowVisible(host.AttachedHandle),foreground=GetForegroundWindow().ToString(),next=GetWindow(host.AttachedHandle,3).ToString(),root=rootHwnd.ToString(),left=bounds.Left,top=bounds.Top,width=bounds.Right-bounds.Left,height=bounds.Bottom-bounds.Top});
                    await Click(bounds.Left+190,bounds.Top+180); await Task.Delay(350);
                    var before=Read(index);
                    Type("abc"); await Task.Delay(350);
                    var after=Read(index); var gui=new Gui { Size=(uint)Marshal.SizeOf<Gui>() };
                    GetGUIThreadInfo(GetWindowThreadProcessId(host.AttachedHandle,out _),ref gui);
                    checks.Add(new { index,before,after,active=gui.Active.ToString(),focus=gui.Focus.ToString(),diagnostic=host.InputDiagnostic() });
                }
                passed=true;
                foreach(int i in Enumerable.Range(0,3))
                {
                    var data=Read(i); if(data.GetProperty("value").GetString()!="abcabc" || data.GetProperty("clicks").GetInt32()<2) passed=false;
                }
                // Editing, Unicode and a later resize must retain the same OS
                // input route; cursor movement must not activate a hidden tab.
                if(!args.Contains("--no-unicode"))
                {
                window.Width+=60; await Task.Delay(500);
                CheckForeground();
                SendKey(0x11,false);SendKey(0x41,false);SendKey(0x41,true);SendKey(0x11,true);
                foreach(char c in "한글")
                {
                    Input[] events=[new(){Type=1,Data=new(){Key=new(){Scan=c,Flags=4}}},new(){Type=1,Data=new(){Key=new(){Scan=c,Flags=6}}}];
                    SendInput(2,events,Marshal.SizeOf<Input>());
                }
                await Task.Delay(350);
                var unicode=Read(selected);checks.Add(new{unicode});
                if(unicode.GetProperty("value").GetString()!="한글")passed=false;
                SendKey(0x25,false);SendKey(0x25,true);SendKey(0x08,false);SendKey(0x08,true);
                await Task.Delay(350);
                var edited=Read(selected);checks.Add(new{edited});
                if(edited.GetProperty("value").GetString()!="글")passed=false;
                }
                }
                if(args.Contains("--ime"))
                {
                    var layouts=new nint[16];int count=GetKeyboardLayoutList(layouts.Length,layouts);
                    var korean=layouts.Take(count).FirstOrDefault(h=>(h.ToInt64()&0xffff)==0x412);
                    checks.Add(new{layouts=layouts.Take(count).Select(h=>h.ToString("X")).ToArray(),korean=korean.ToString("X")});
                    if(korean==0)throw new Exception("Korean keyboard is not installed.");
                    var handles=hosts.Select(h=>h.AttachedHandle).ToArray();
                    foreach(int index in new[]{0,1,2,0,2,1})
                    {
                        var target=handles[index];
                        await ClickElement((FrameworkElement)panel.Children[index]);await Task.Delay(1200);
                        PostMessageW(target,0x50,0,korean);await Task.Delay(250);
                        var ime=ImmGetDefaultIMEWnd(target);
                        var host=hosts[index];GetClientScreenRect(target,out var bounds);
                        await Click(bounds.Left+190,bounds.Top+180);await Task.Delay(150);
                        SendKey(0x11,false);SendKey(0x41,false);SendKey(0x41,true);SendKey(0x11,true);
                        // IMM_SETOPENSTATUS does not describe TSF conversion
                        // mode reliably. Determine the real mode with physical
                        // input in this disposable editor, then normalize it.
                        Type("a ");await Task.Delay(300);
                        var probe=Read(index).GetProperty("value").GetString();
                        checks.Add(new{index,modeProbe=probe});
                        if(probe=="ㅁ ")ToggleIme();
                        else if(probe!="a ")throw new Exception("Unexpected fixture IME mode: "+probe);
                        SendKey(0x11,false);SendKey(0x41,false);SendKey(0x41,true);SendKey(0x11,true);
                        int compositionCount = Read(index).GetProperty("compositions").GetArrayLength();
                        ToggleIme();await Task.Delay(150);
                        foreach(char c in "gks") { Type(c.ToString());await Task.Delay(100); }
                        await Task.Delay(350);
                        var preedit=Read(index);checks.Add(new{index,preedit});
                        if(index==2) { File.WriteAllText(Path.Combine(entries[index].Run,"capture-preedit"), "capture"); await Task.Delay(250); }
                        if(args.Contains("--inline-ime") && (preedit.GetProperty("value").GetString()!="한" ||
                            !preedit.GetProperty("compositions").EnumerateArray().Skip(compositionCount).Any(e=>e[0].GetString()=="compositionupdate" && e[1].GetString()=="한")))passed=false;
                        SendKey(0x08,false);SendKey(0x08,true);await Task.Delay(150);
                        if(Read(index).GetProperty("value").GetString()!="하")passed=false;
                        Type("s");await Task.Delay(150);
                        if(Read(index).GetProperty("value").GetString()!="한")passed=false;
                        if(index==0 && checks.Count(c=>c.ToString()?.Contains("preedit")==true)==1)
                        { window.Width+=20;window.Left+=5;await Task.Delay(300); }
                        foreach(char c in "rmf") { Type(c.ToString());await Task.Delay(100); }
                        Type(" ");await Task.Delay(350);
                        var composed=Read(index);
                        SendMessageTimeoutW(ime,0x283,5,0,2,300,out var open);
                        checks.Add(new{index,composed,ime=ime.ToString(),open=(ulong)open,layout=GetKeyboardLayout(GetWindowThreadProcessId(target,out _)).ToString("X"),hostLayout=GetKeyboardLayout(0).ToString("X")});
                        if(composed.GetProperty("value").GetString()!="한글 ")passed=false;
                        ToggleIme();Type("abc");await Task.Delay(300);
                        var english=Read(index);checks.Add(new{index,english});
                        if(english.GetProperty("value").GetString()!="한글 abc")passed=false;
                    }
                }
                if(args.Contains("--lifecycle"))
                {
                    var native = hosts[selected].AttachedHandle;
                    foreach (var h in hosts) if(GetParent(h.AttachedHandle)!=0) throw new Exception("Cross-process parent or owner was installed.");
                    var managerGui=new Gui{Size=(uint)Marshal.SizeOf<Gui>()};
                    GetGUIThreadInfo(GetWindowThreadProcessId(rootHwnd,out _),ref managerGui);
                    if(managerGui.Focus==native) throw new Exception("Manager and native input queues were joined.");
                    window.Left+=65;window.Top+=30;window.Width+=60;await Task.Delay(500);
                    var container=(nint)typeof(NativeWindowHost).GetProperty("ContainerHandle",BindingFlags.Instance|BindingFlags.NonPublic)!.GetValue(hosts[selected])!;
                    GetWindowRect(container,out var slot);GetClientScreenRect(native,out var positioned);
                    if(slot.Left!=positioned.Left||slot.Top!=positioned.Top||slot.Right!=positioned.Right||slot.Bottom!=positioned.Bottom)
                        throw new Exception("Viewport did not follow manager movement and resize.");
                    Type("x");await Task.Delay(200);
                    if(Read(selected).GetProperty("value").GetString()!="한글 abcx")throw new Exception("Resize lost native keyboard focus.");
                    checks.Add(new{lifecycle="move/resize retains native input and independent queues"});
                    await ClickElement((FrameworkElement)panel.Children[selected]);await Task.Delay(250);
                    window.IsEnabled=false;await Task.Delay(250);
                    if(IsWindowVisible(native))throw new Exception("Viewport covered a disabled manager/modal.");
                    window.IsEnabled=true;await Task.Delay(300);
                    var other=new Window{Title="Disposable other foreground window",Width=220,Height=100,Left=90,Top=80,ShowActivated=true};
                    other.Show();ActivateFixture(new WindowInteropHelper(other).Handle);await Task.Delay(300);
                    if(!IsWindowVisible(native)||(GetWindowLongPtrW(native,-20).ToInt64()&8)!=0)
                        throw new Exception("Viewport was blanked or left topmost outside its foreground group.");
                    other.Close();ActivateFixture(rootHwnd);await Task.Delay(400);
                    if(!IsWindowVisible(native)||GetForegroundWindow()!=rootHwnd)throw new Exception("Return to manager lost presentation or stole foreground.");
                    window.WindowState=WindowState.Minimized;await Task.Delay(300);
                    if(hosts.Any(h=>IsWindowVisible(h.AttachedHandle)))throw new Exception("Minimized manager left a profile visible.");
                    window.WindowState=WindowState.Normal;ActivateFixture(rootHwnd);await Task.Delay(500);
                    GetClientScreenRect(native,out var b);await Click(b.Left+190,b.Top+180);await Task.Delay(200);
                    Type("y");await Task.Delay(200);
                    if(Read(selected).GetProperty("value").GetString()?.Replace("y", "")!="한글 abcx")throw new Exception("Restore lost native editor input.");
                    checks.Add(new{lifecycle="modal, other foreground, minimize/restore, no focus theft"});
                }
            }
            catch(Exception error){passed=false;log.Add(error.ToString());}
            finally
            {
                bool restoreForeground = IsFixtureForeground();
                (mouse as IDisposable)?.Dispose();
                foreach(var host in hosts)host.DetachForClose();
                if(restoreForeground) { SetForegroundWindow(previous);SetCursorPos(cursor.X,cursor.Y); }
                File.WriteAllText(Path.Combine(root,"input-report.json"),JsonSerializer.Serialize(new{passed,checks,log},new JsonSerializerOptions{WriteIndented=true}));
                window.Close();app.Shutdown(passed?0:1);
            }
        };
        JsonElement Read(int index)
        {
            // The fixture publishes a complete snapshot with atomic rename.
            // Share deletion so a concurrent publish cannot lock out this reader.
            using var stream=new FileStream(Path.Combine(entries[index].Run,"input-state.json"),
                FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete);
            using var doc=JsonDocument.Parse(stream);return doc.RootElement.Clone();
        }
        void ActivateFixture(nint target)
        {
            SetForegroundWindow(target);
            if(GetForegroundWindow()==target)return;
            var current=GetCurrentThreadId();var foreground=GetWindowThreadProcessId(GetForegroundWindow(),out _);
            bool attached=AttachThreadInput(current,foreground,true);
            try { SetForegroundWindow(target); }
            finally { if(attached)AttachThreadInput(current,foreground,false); }
        }
        bool IsFixtureForeground() => GetForegroundWindow()==inputForeground || (hosts.Count>selected && GetForegroundWindow()==hosts[selected].AttachedHandle);
        void CheckForeground(){if(!IsFixtureForeground())throw new Exception($"Fixture lost foreground; refusing to send input elsewhere. Expected={inputForeground}, actual={GetForegroundWindow()}, process={GetForegroundPid()}");}
        uint GetForegroundPid(){GetWindowThreadProcessId(GetForegroundWindow(),out var id);return id;}
        void SendKey(ushort key,bool up){CheckForeground();Input[] events=[new(){Type=1,Data=new(){Key=new(){Vk=key,Flags=up?2u:0u}}}];SendInput(1,events,Marshal.SizeOf<Input>());}
        void ToggleIme()
        {
            CheckForeground();
            if(args.Contains("--right-alt")||args.Contains("--scan-hangul"))
            {
                ushort scan=(ushort)(args.Contains("--right-alt")?0x38:0x72);
                Input[] events=[new(){Type=1,Data=new(){Key=new(){Scan=scan,Flags=9}}},new(){Type=1,Data=new(){Key=new(){Scan=scan,Flags=11}}}];
                SendInput(2,events,Marshal.SizeOf<Input>());
            }
            else { SendKey(0x15,false);SendKey(0x15,true); }
        }
        Task ClickElement(FrameworkElement element){var p=element.PointToScreen(new System.Windows.Point(element.ActualWidth/2,element.ActualHeight/2));return Click((int)p.X,(int)p.Y);}
        async Task Click(int x,int y)
        {
            CheckForeground(); SetCursorPos(x,y); await Task.Delay(100); CheckForeground();
            // A physical click spans separate input frames. Sending down/up in
            // the same zero-duration batch leaves no activation time before release.
            Input[] down=[new(){Data=new(){Mouse=new(){Flags=2}}}];
            Input[] up=[new(){Data=new(){Mouse=new(){Flags=4}}}];
            SendInput(1,down,Marshal.SizeOf<Input>());
            try { await Task.Delay(60); }
            finally { SendInput(1,up,Marshal.SizeOf<Input>()); }
        }
        void Type(string text){CheckForeground();foreach(char c in text){CheckForeground();Input[] events=[new(){Type=1,Data=new(){Key=new(){Vk=(ushort)char.ToUpperInvariant(c)}}},new(){Type=1,Data=new(){Key=new(){Vk=(ushort)char.ToUpperInvariant(c),Flags=2}}}];SendInput(2,events,Marshal.SizeOf<Input>());}}
        window.Show();return app.Run();
    }
    private sealed record Entry(string Run,int Pid);
}
