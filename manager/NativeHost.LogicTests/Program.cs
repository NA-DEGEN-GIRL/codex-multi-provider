using System.Reflection;
using System.Text.Json;
using Codex.ControlCenter.Shell;
using static Codex.ControlCenter.Shell.NativeWindowInterop;

// Only production lifecycle logic runs. OS/window/event boundaries are deterministic fakes.
var tests = new (string Name, Action Body)[]
{
    ("renderer acknowledgement belongs to the current window lease", () =>
    {
        var directory=Path.Combine(Path.GetTempPath(),"codex-render-lease-"+Guid.NewGuid());
        try
        {
            using var lease=new NativeWindowLease(directory,1234,(nint)456);
            var path=Path.Combine(directory,"1234.json");
            using var state=JsonDocument.Parse(File.ReadAllText(path));
            var token=state.RootElement.GetProperty("token").GetString();
            void Write(string? match, bool shown=true) => File.WriteAllText(path+".render.json",
                JsonSerializer.Serialize(new {version=1,appPid=1234,hwnd="456",shellPid=Environment.ProcessId,
                    token=match,rendererReady=true,shown}));
            Write("old attachment"); True(lease.ReadPresentationStatus() is null,"stale renderer ack accepted");
            Write(token); True(lease.ReadPresentationStatus()?.Contains("초기화 완료")==true,"ready renderer ack missing");
            Write(token,false); True(lease.ReadPresentationStatus()?.Contains("초기화 실패")==true,"blank renderer reported ready");
            File.WriteAllText(path+".render.json","{");
            True(lease.ReadPresentationStatus() is null,"partial write escaped reader");
        }
        finally { if(Directory.Exists(directory))Directory.Delete(directory,true); }
    }),
    ("private desktop lease spans exactly the verified native attachment", () =>
    {
        var directory=Path.Combine(Path.GetTempPath(),"codex-host-lease-"+Guid.NewGuid());
        var host=NewHost(); host.WindowStateDirectory=directory;
        try
        {
            Attach(host);
            var path=Path.Combine(directory,$"{Pid}.json");
            using(var state=JsonDocument.Parse(File.ReadAllText(path)))
            {
                Equal(state.RootElement.GetProperty("appPid").GetInt32(),Pid);
                Equal(state.RootElement.GetProperty("hwnd").GetString()!,Child.ToString());
            }
            host.Detach(); True(!File.Exists(path),"detached window retained embedded-only behavior");
            RejectNextParent=Container;
            True(!host.Attach(Child,Pid,Executable,out _),"fixture parent rejection ignored");
            True(!File.Exists(path),"failed attach retained lease");
        }
        finally { host.Detach(); if(Directory.Exists(directory))Directory.Delete(directory,true); }
    }),
    ("login recovery is scoped to the stopped account and preserves live windows", () =>
    {
        var failed=JsonSerializer.SerializeToElement(new {alias="03",status="not_started",
            login_health=new {blocks_launch=true,message="등록된 계정과 다릅니다."}});
        var live=JsonSerializer.SerializeToElement(new {alias="03",status="running",
            login_health=new {blocks_launch=true}});
        var healthy=JsonSerializer.SerializeToElement(new {alias="04",status="running",
            login_health=new {blocks_launch=false}});
        True(ProfileLoginPresentation.ShowRecovery(failed),"stopped account did not show recovery");
        True(!ProfileLoginPresentation.ShowRecovery(live),"live window was replaced by login notice");
        True(!ProfileLoginPresentation.ShowRecovery(healthy),"another profile was blocked");
        True(ProfileLoginPresentation.Message(failed).Contains("프로필 03"),"missing account identity");
    }),
    ("live profile window wins over alternating discovery handles", () =>
    {
        var host=NewHost(); Attach(host);
        var current=WindowLaunchIdentity.From(Profile("03","same-run",(int)Pid));
        var resizeCount=ResizeCalls;
        for(int poll=0;poll<20;poll++)
        {
            var observed=JsonSerializer.SerializeToElement(new {
                id="03", generation="same-run", process_id=Pid, window_handle=poll%2==0?999:888 });
            True(current.Retains(host,observed),"a helper HWND displaced the live attached main window");
        }
        Equal(host.AttachedHandle,Child); Equal(ResizeCalls,resizeCount);
        True(!current.Retains(host,Profile("03","new-run",(int)Pid)),"replacement generation retained old attachment");
        Alive=false;
        True(!current.Retains(host,Profile("03","same-run",(int)Pid)),"closed window was retained");
    }),
    ("unmeasured host waits without parenting or shrinking the app", () =>
    {
        var host=NewHost(); ClientWidth=ClientHeight=1;
        True(!host.IsLayoutReady,"placeholder reported a measured area");
        True(!host.Attach(Child,Pid,Executable,out _,allowHidden:true),"attached into 1x1 placeholder");
        Equal(Parent,(nint)0); Equal(ResizeCalls,0); True(!host.IsAttached,"failed layout retained attachment");
        ClientWidth=1000; ClientHeight=700;
        Attach(host); Equal(Parent,Container);
    }),
    ("temporary zero-size layout does not reset the embedded compositor", () =>
    {
        var host=NewHost(); Attach(host); var before=ResizeCalls;
        ClientWidth=0; ClientHeight=1;
        True(host.SynchronizeLayout(),"temporary layout destroyed attachment");
        Equal(ResizeCalls,before); True(host.HasLiveAttachment,"temporary layout detached the window");
        ClientWidth=1200; ClientHeight=850;
        True(host.SynchronizeLayout(),"real client area was not restored");
        Equal(ResizeCalls,before+1);
    }),
    ("Chromium top-level style rejection succeeds on the first embedded attach", () =>
    {
        var host=NewHost(); RejectChildBeforeParent=true;
        try
        {
            Attach(host); Equal(Parent,Container);
            True((Style.ToInt64()&WsChild)!=0,"style did not settle after parenting");
            True((Style.ToInt64()&WsCaption)==0,"caption remained on embedded window");
        }
        finally {RejectChildBeforeParent=false;}
    }),
    ("one-time Chromium style rewrite during resize does not detach the window", () =>
    {
        var host=NewHost(); DuringResize=()=>Style=(nint)((Style.ToInt64()&~WsChild)|WsCaption);
        Attach(host); Equal(Parent,Container); True(host.IsAttached,"style change caused a standalone window");
    }),
    ("asynchronous startup style rewrite is repaired without a restart or reparent", () =>
    {
        var host=NewHost(); Attach(host); var messages=new List<string>(); host.Diagnostic+=messages.Add;
        // Reproduce the live 0x10C70000 style observed AFTER Attach had returned.
        Style=(nint)0x10C70000; ExStyle=(nint)WsExAppWindow;
        int before=ResizeCalls; Callbacks(host);
        Equal(Parent,Container); True(host.HasLiveAttachment,"late style recovery detached the app");
        True((Style.ToInt64()&WsChild)!=0,"lost WS_CHILD not restored");
        True((Style.ToInt64()&WsCaption)==0,"caption not removed"); Equal(ExStyle,(nint)0);
        Equal(ResizeCalls,before+1); Equal(FocusCalls,0);
        Callbacks(host); Equal(ResizeCalls,before+1);
        Equal(messages.Count(m=>m.Contains("실행 중 내부 창 스타일 복원")),1);
    }),
    ("repeated native frame rejection cannot become a redraw loop", () =>
    {
        long now=0; var host=NewHost(); host.ResizeClock=()=>now; Attach(host);
        var messages=new List<string>(); host.Diagnostic+=messages.Add;
        int before=ResizeCalls;
        for(int i=0;i<100;i++) { now+=1000; Style=(nint)0x10C70000; Callbacks(host); }
        Equal(ResizeCalls,before+3); Equal(Parent,Container);
        True(host.IsAttached,"native style disagreement closed or detached user's task");
        Equal(messages.Count(m=>m.Contains("반복 조절을 멈췄습니다")),1);
    }),
    ("matching dimensions at the wrong origin are corrected", () =>
    {
        long now=0; var host=NewHost(); host.ResizeClock=()=>now; Attach(host);
        ChildBounds=new(){Left=100,Top=200,Right=100+ClientWidth,Bottom=200+ClientHeight};
        now=1000; int before=ResizeCalls; host.SynchronizeLayout();
        Equal(ResizeCalls,before+1); Equal(ChildBounds.Left,0); Equal(ChildBounds.Top,0);
    }),
    ("hidden managed windows attach without showing a standalone window", () =>
    {
        var host=NewHost(); Visible=false;
        True(host.Attach(Child,Pid,Executable,out var error,allowHidden:true),error);
        Equal(Parent,Container); True(host.IsAttached,"hidden window was not adopted");
    }),
    ("managed attachment failure keeps the external window hidden", () =>
    {
        var host=NewHost(); RejectNextParent=Container;
        True(!host.Attach(Child,Pid,Executable,out _,allowHidden:true),"fixture rejection ignored");
        True(!Visible,"failed attach escaped as a separate window"); Equal(Parent,(nint)0);
    }),
    ("background account containers preserve the selected profile", () =>
    {
        var deck=new NativeHostDeck(_=>{});
        var current=deck.Select("03"); current.Visibility=System.Windows.Visibility.Visible;
        var background=deck.Ensure("02");
        True(ReferenceEquals(current,deck.Current),"background update changed selected account");
        Equal(background.Visibility,System.Windows.Visibility.Hidden);
        True(ReferenceEquals(background,deck.Ensure("02")),"background host duplicated");
    }),
    ("transient attach retry is bounded and replacement windows get a fresh budget", () =>
    {
        var attempts=new WindowAttachmentAttempts(); var now=DateTime.UtcNow;
        True(attempts.Begin("03","old-window",now,out _),"first attach blocked");
        True(!attempts.Begin("03","old-window",now,out _),"retry has no backoff");
        True(attempts.Begin("03","old-window",now.AddSeconds(2),out _),"second try missing");
        True(attempts.Begin("03","old-window",now.AddSeconds(4),out _),"third try missing");
        True(!attempts.Begin("03","old-window",now.AddHours(1),out _),"reparent loop is unbounded");
        True(attempts.Begin("03","new-window",now.AddSeconds(5),out _),"replacement did not attach automatically");
    }),
    ("installed newer package cannot offer install and its blocker is visible", () =>
    {
        var result=JsonSerializer.SerializeToElement(new{status="installed_newer",message="설치된 앱이 더 최신입니다."});
        True(!UpdatePresentation.CanInstall(result),"downgrade action offered");
        var failed=JsonSerializer.SerializeToElement(new{status="blocked",message="적용 중단",blockers=new[]{new{message="installed_newer"}}});
        True(UpdatePresentation.Summary(failed).Contains("installed_newer"),"blocking reason hidden");
        True(UpdatePresentation.CanInstall(JsonSerializer.SerializeToElement(new{status="available"})),"verified available update disabled");
    }),
    ("old manager shortcut resolves installed release once without a redirect loop", () =>
    {
        var root = Path.Combine(Path.GetTempPath(), "codex-manager-selection-" + Guid.NewGuid());
        var folder = Path.Combine(root, "artifacts", "manager", "releases", "new");
        var shell = Path.Combine(folder, "Codex.ControlCenter.exe");
        var old = Path.Combine(root, "work", "old", "Codex.ControlCenter.exe");
        Directory.CreateDirectory(folder);
        try
        {
            True(InstalledManagerRelease.Replacement(root, old) is null, "missing selection must permit development startup");
            File.WriteAllText(shell, "fixture executable; never launched");
            var pointer = Path.Combine(root, "artifacts", "manager", "current.json");
            File.WriteAllText(pointer, JsonSerializer.Serialize(new { shell }));
            Equal(InstalledManagerRelease.Replacement(root, old), shell);
            True(InstalledManagerRelease.Replacement(root, shell) is null, "latest version redirected to itself");
            File.WriteAllText(pointer, JsonSerializer.Serialize(new { shell = old }));
            bool refused = false;
            try { InstalledManagerRelease.Replacement(root, shell); }
            catch (InvalidOperationException) { refused = true; }
            True(refused, "selection must remain in installed releases");
        }
        finally { Directory.Delete(root, recursive: true); }
    }),
    ("selecting an automatically updating account waits without launching twice", () =>
    {
        JsonElement Profile(string phase, bool automatic = true) => JsonSerializer.SerializeToElement(
            new { restart = new { automatic_key = automatic ? "release" : "", phase } });
        foreach (var phase in new[] { "acquiring", "closing", "opening", "releasing", "recovering", "connecting" })
            True(AutomaticProfileUpdate.IsReplacing(Profile(phase)), "replacement should keep its one pending selection");
        foreach (var phase in new[] { "waiting", "complete", "attention", "superseded" })
            True(!AutomaticProfileUpdate.IsReplacing(Profile(phase)), "work or recovery should remain accessible");
        True(!AutomaticProfileUpdate.IsReplacing(Profile("opening", false)), "manual actions retain their existing flow");
        True(AutomaticProfileUpdate.IsClosing(Profile("closing")), "do not attach the window being closed");
        True(!AutomaticProfileUpdate.IsClosing(Profile("opening")), "new window can attach while restoration completes");
    }),
    ("one shortcut click continues after reader readiness without a second open", () =>
    {
        var waiting=JsonSerializer.SerializeToElement(new {state="waiting_for_reader",navigation_id="opaque"});
        int polls=0;
        var result=ConversationReadyWait.CompleteAsync(waiting,()=>true, token=>
        {
            Equal(token,"opaque"); polls++;
            return Task.FromResult(polls<3 ? waiting : JsonSerializer.SerializeToElement(new {state="request_sent"}));
        },()=>Task.CompletedTask).GetAwaiter().GetResult();
        Equal(polls,3); Equal(result!.Value.S("state"),"request_sent");
    }),
    ("switching profiles while warming cancels the old deferred navigation", () =>
    {
        var waiting=JsonSerializer.SerializeToElement(new {state="waiting_for_reader",navigation_id="opaque"});
        bool selected=true; int polls=0;
        var result=ConversationReadyWait.CompleteAsync(waiting,()=>selected, token=>
        { polls++; return Task.FromResult(waiting); },()=> {selected=false;return Task.CompletedTask;}).GetAwaiter().GetResult();
        True(result is null,"stale navigation result applied"); Equal(polls,0);
    }),
    ("rejected Chromium sizes cannot flood layout, logs, or native messages", () =>
    {
        long now=0; var host=NewHost(); host.ResizeClock=()=>now;
        var messages=new List<string>(); host.Diagnostic+=messages.Add;
        RejectResize=true; Attach(host);
        for(var i=0;i<10000;i++) host.SynchronizeLayout();
        Equal(ResizeCalls,1); True((LastResizeFlags & SwpAsyncWindowPos)!=0,"cross-thread resize blocks the dispatcher");
        now=1000; host.SynchronizeLayout(); now=2000; host.SynchronizeLayout();
        now=3000; for(var i=0;i<10000;i++) host.SynchronizeLayout();
        Equal(ResizeCalls,3);
        Equal(messages.Count(m=>m.Contains("표시 영역 크기 요청")),1);
        Equal(messages.Count(m=>m.Contains("반복 조절을 멈췄습니다")),1);
        True(host.IsAttached,"size disagreement detached the user's app");
        ClientWidth+=100; RejectResize=false; host.SynchronizeLayout();
        Equal(ResizeCalls,4); Equal(ChildBounds.Right,ClientWidth);
    }),
    ("same profile actions do not overlap but other profiles stay independent", () =>
    {
        var gate=new ProfileActionGate(); var first=gate.Enter("04","대화 열기");
        bool rejected=false;
        try { using var duplicate=gate.Enter("04","설정 적용"); }
        catch(InvalidOperationException ex) { rejected=ex.Message.Contains("대화 열기"); }
        True(rejected,"restart overlapped pending navigation");
        using(var other=gate.Enter("02","계정 열기")) { }
        first.Dispose(); first.Dispose();
        using(var next=gate.Enter("04","설정 적용")) { }
    }),
    ("routine polling stays quiet while failures remain visible and scoped", () =>
    {
        var filter=new RpcLogFilter(); var at=DateTime.UtcNow;
        for(var i=0;i<100;i++)
        {
            True(!filter.Accept("03:one","thread/list","started","",0,at,out _),"poll start leaked");
            True(!filter.Accept("03:one","thread/list","completed","",0,at,out _),"poll completion leaked");
        }
        True(filter.Accept("03:one","thread/list","failed","cursor",-32600,at,out _),"first error hidden");
        True(!filter.Accept("03:one","thread/list","failed","cursor",-32600,at.AddSeconds(1),out _),"duplicate flooded log");
        True(filter.Accept("02:one","thread/list","failed","cursor",-32600,at,out _),"other account hidden");
        True(filter.Accept("03:two","thread/list","failed","cursor",-32600,at,out _),"new launch hidden");
        True(filter.Accept("03:one","thread/list","failed","cursor",-32600,at.AddSeconds(31),out var repeats),"recurring error hidden");
        Equal(repeats,1);
        True(filter.Accept("03:one","thread/name/set","completed","",0,at,out _),"user rename hidden");
    }),
    ("switching profiles keeps the existing native parent and viewport", () =>
    {
        var deck=new NativeHostDeck(host=>host.LoadFakeHost());
        var first=deck.Select("03"); Attach(first); ResizeCalls=0;
        var style=Style;
        for(var i=0;i<100;i++)
        {
            deck.Select("02");
            Equal(first.Visibility,System.Windows.Visibility.Hidden);
            True(first.IsAttached,"switch detached the original child");
            True(ReferenceEquals(deck.Select("03"),first),"switch created another host");
        }
        Equal(Parent,Container); Equal(Style,style); Equal(ResizeCalls,0); Equal(deck.Hosts.Count(),2);
        first.Detach(); True(!first.IsAttached,"manual detach stopped working");
    }),
    ("a reused maximized window is restored before embedding", () =>
    {
        Zoomed=true; Style=(nint)(Style.ToInt64()|WsMaximize);
        var host=NewHost(); Attach(host);
        Equal(RestoreCalls,1); True(!Zoomed,"maximized compositor state retained");
        Equal(ChildBounds.Right-ChildBounds.Left,ClientWidth);
    }),
    ("late client size change is fitted without a profile restart", () =>
    {
        var host=NewHost(); Attach(host); ResizeCalls=0;
        ClientWidth=1200; ClientHeight=850;
        typeof(NativeWindowHost).GetMethod("CheckAttachedWindow",BindingFlags.Instance|BindingFlags.NonPublic)!.Invoke(host,null);
        Equal(ChildBounds.Right,1200); Equal(ChildBounds.Bottom,850); Equal(ResizeCalls,1);
        Equal(Parent,Container); True(host.IsAttached,"late layout replaced the window");
    }),
    ("mouse activation preserves click and Chromium focus", () =>
    {
        var host=NewHost(); Attach(host); Focused=43;
        var result=host.DeliverMessage(WmMouseActivate);
        True(result.Handled,"mouse activation was forwarded to WPF"); Equal(result.Result,(nint)1);
        Equal(FocusCalls,0); Equal(Focused,(nint)43);
    }),
    ("embedded click raises manager without changing caret or reentering focus", () =>
    {
        var host=NewHost(); Attach(host); Foreground=999; Focused=43;
        DuringActivation=host.DeliverFocusCallback;
        True(host.ActivateFromEmbeddedClick(43),"embedded editor click did not activate manager");
        Equal(Foreground,Container); Equal(ActivationCalls,1); Equal(FocusCalls,0); Equal(Focused,(nint)43);
        host.ActivateFromEmbeddedClick(43); Equal(ActivationCalls,1);
    }),
    ("other apps, hidden profiles, detached windows and modals cannot activate manager", () =>
    {
        var host=NewHost(); Attach(host); Foreground=999;
        True(!host.ActivateFromEmbeddedClick(999),"click on covering browser activated manager");
        host.Visibility=System.Windows.Visibility.Hidden;
        True(!host.ActivateFromEmbeddedClick(43),"hidden profile activated manager");
        host.Visibility=System.Windows.Visibility.Visible; Enabled=false;
        True(!host.ActivateFromEmbeddedClick(43),"modal disabled host activated");
        Enabled=true; Parent=999;
        True(!host.ActivateFromEmbeddedClick(43),"detached window activated manager");
        Parent=Container; Alive=false;
        True(!host.ActivateFromEmbeddedClick(43),"dead/recycled child activated manager");
        Equal(ActivationCalls,0); Equal(FocusCalls,0); Equal(Foreground,(nint)999);
    }),
    ("editor click moves OS keyboard focus from the already active WPF root", () =>
    {
        var host=NewHost(); Attach(host); Foreground=Container; Focused=Container;
        DuringFocus=host.DeliverFocusCallback;
        True(host.ActivateFromEmbeddedClick(43),"native focus was not transferred");
        Equal(FocusCalls,1); Equal(Focused,(nint)42); Equal(ActivationCalls,0);
        True(host.InputDiagnostic().Contains("키보드 대상 내부 Codex"),"OS focus not reported");
        host.ActivateFromEmbeddedClick(43); Equal(FocusCalls,1);
    }),
    ("only fresh mouse down packets qualify across pointer sizes and timer wrap", () =>
    {
        foreach(int pointers in new[]{4,8})
        {
            var bytes=new byte[8+2*pointers+24]; int flags=8+2*pointers+4;
            foreach(ushort down in new ushort[]{1,4,16,64,256})
            {
                BitConverter.GetBytes(down).CopyTo(bytes,flags);
                True(EmbeddedMouseActivation.IsFreshButtonDown(bytes,pointers,100,90),"mouse down ignored");
                True(EmbeddedMouseActivation.IsFreshButtonDown(bytes,pointers,10,uint.MaxValue-5),"tick wrap rejected");
                True(!EmbeddedMouseActivation.IsFreshButtonDown(bytes,pointers,1000,90),"stale click could steal focus");
            }
            foreach(ushort other in new ushort[]{0,2,8,32,128,512,1024,2048})
            {
                BitConverter.GetBytes(other).CopyTo(bytes,flags);
                True(!EmbeddedMouseActivation.IsFreshButtonDown(bytes,pointers,100,90),"movement/release/wheel activated");
            }
            bytes[flags]=1; bytes[0]=1;
            True(!EmbeddedMouseActivation.IsFreshButtonDown(bytes,pointers,100,90),"keyboard packet activated");
            True(!EmbeddedMouseActivation.IsFreshButtonDown(bytes.AsSpan(0,10),pointers,100,90),"short packet accepted");
        }
    }),
    ("Chromium render widget receives IME focus instead of its outer frame", () =>
    {
        var host=NewHost(); Attach(host); Foreground=Container; Focused=Child;
        ChildClass="Chrome_RenderWidgetHostHWND";
        True(host.ActivateFromEmbeddedClick(43),"renderer input activation failed");
        Equal(Focused,(nint)43); Equal(FocusCalls,1);
        host.ActivateFromEmbeddedClick(43); Equal(FocusCalls,1);
    }),
    ("keyboard focus inside Chromium is preserved", () =>
    {
        var host=NewHost(); Attach(host); Focused=43; host.DeliverFocusCallback(); Equal(FocusCalls,0);
    }),
    ("synchronous focus notification cannot recursively focus again", () =>
    {
        var host=NewHost(); Attach(host); Focused=Container; DuringFocus=host.DeliverFocusCallback;
        host.DeliverFocusCallback(); Equal(FocusCalls,1);
    }),
    ("WPF sidebar focus cannot be pulled back into Codex", () =>
    {
        var host=NewHost(); Attach(host); Focused=100;
        host.DeliverFocusCallback(); Equal(FocusCalls,0); Equal(Focused,(nint)100);
        Focused=Container; host.Visibility=System.Windows.Visibility.Hidden;
        host.DeliverFocusCallback(); Equal(FocusCalls,0);
        host.Visibility=System.Windows.Visibility.Visible; Foreground=999;
        host.DeliverFocusCallback(); Equal(FocusCalls,0);
    }),
    ("blocked or unresponsive window is not focused", () =>
    {
        var host=NewHost(); Attach(host); Enabled=false; host.DeliverFocusCallback(); Equal(FocusCalls,0);
        True(host.InputDiagnostic().Contains("Codex 차단"),"blocked input was not reported");
        Enabled=true; Hung=true; host.DeliverFocusCallback(); Equal(FocusCalls,0);
        True(host.InputDiagnostic().Contains("Windows 응답 없음"),"hang was not reported");
    }),
    ("modal-disabled window is rejected before reparenting", () =>
    {
        var host=NewHost(); Enabled=false;
        True(!host.Attach(Child,Pid,Executable,out var error),"disabled window was embedded");
        True(error.Contains("대화상자"),"modal explanation missing"); Equal(Parent,(nint)0);
    }),
    ("log-triggered layout before SetParent does not detach", () =>
    {
        var host=NewHost();
        host.Diagnostic += message => { if(message.Contains("부모 창 연결 시작")) Callbacks(host); };
        Attach(host); Equal(Parent,Container); True(host.IsAttached,"lost attachment");
    }),
    ("layout and watchdog reenter during SetParent", () =>
    {
        var host=NewHost(); BeforeReparent=()=>Callbacks(host); AfterReparent=()=>Callbacks(host);
        Attach(host); Equal(Parent,Container); Equal(FocusCalls,0);
    }),
    ("detach restoration does not recursively restore twice", () =>
    {
        var host=NewHost(); var style=Style; var exStyle=ExStyle; Attach(host);
        var notifications=0; host.AttachmentChanged+=(_,_)=>notifications++;
        AfterReparent=()=>Callbacks(host); DuringResize=()=>Callbacks(host);
        host.Detach(); True(!host.IsAttached,"detach retained ownership");
        Equal(Parent,(nint)0); Equal(Style,style); Equal(ExStyle,exStyle); Equal(notifications,1);
    }),
    ("size callbacks during SetWindowPos are coalesced", () =>
    {
        var host=NewHost(); Attach(host); ResizeCalls=0; DuringResize=host.DeliverLayoutCallback;
        ClientWidth=1000;
        host.DeliverLayoutCallback(); Equal(ResizeCalls,1); True(host.IsAttached,"resize lost ownership");
    }),
    ("unchanged log layouts do not repaint the native frame", () =>
    {
        var host=NewHost(); Attach(host); ResizeCalls=0;
        for(var i=0;i<100;i++) host.DeliverLayoutCallback();
        Equal(ResizeCalls,0); True(host.IsAttached,"unchanged layout detached the window");
        ClientHeight=900; host.DeliverLayoutCallback(); Equal(ResizeCalls,1);
    }),
    ("external size changes are repaired even when the host did not resize", () =>
    {
        long now=0; var host=NewHost(); host.ResizeClock=()=>now; Attach(host); ResizeCalls=0; now=1000;
        ChildBounds.Right=400; host.DeliverLayoutCallback();
        Equal(ResizeCalls,1); Equal(ChildBounds.Right-ChildBounds.Left,ClientWidth);
    }),
    ("structural parent is used even if GetParent is ambiguous", () =>
    {
        var host=NewHost(); Attach(host); AmbiguousGetParent=true; ResizeCalls=0;
        ClientWidth=1000;
        host.DeliverLayoutCallback(); Equal(ResizeCalls,1); True(host.IsAttached,"owner result replaced structural parent");
    }),
    ("genuine parent loss after attach is still restored", () =>
    {
        var host=NewHost(); var style=Style; Attach(host); Parent=0;
        var reports=0; var losses=0;
        host.AttachmentLost += (hwnd, parentChanged) =>
        {
            Equal(hwnd,Child); True(parentChanged,"parent change not reported");
            True(!host.IsTransitioning,"loss arrived during restore"); losses++;
            Callbacks(host);
        };
        host.Diagnostic += message => { if(message.Contains("연결 후 부모")) { reports++; if(reports==1) Callbacks(host); } };
        Callbacks(host);
        True(!host.IsAttached,"genuine parent loss ignored"); Equal(Style,style);
        Equal(reports,1); Equal(losses,1);
        for(var i=0;i<100;i++) Callbacks(host);
        Equal(losses,1);
    }),
    ("explicit detach does not report an unexpected attachment loss", () =>
    {
        var host=NewHost(); Attach(host); var losses=0;
        host.AttachmentLost += (_,_) => losses++;
        host.Detach(); Callbacks(host); Equal(losses,0);
    }),
    ("nested attach and detach cannot interrupt the current transition", () =>
    {
        var host=NewHost(); var nestedRejected=false;
        host.Diagnostic += message =>
        {
            if(!message.Contains("부모 창 연결 시작")) return;
            True(host.IsTransitioning,"transition was not exposed to the shell close guard");
            host.Detach();
            nestedRejected=!host.Attach(Child,Pid,Executable,out _);
        };
        Attach(host); True(nestedRejected,"nested attach accepted");
        True(!host.IsTransitioning,"transition guard did not release"); Equal(Parent,Container);
    }),
    ("attach failure rolls back and permits retry", () =>
    {
        var host=NewHost(); var style=Style; RejectNextParent=Container;
        True(!host.Attach(Child,Pid,Executable,out var error),"failed parent accepted");
        True(error.Contains("fixture parent"),"failure cause lost");
        True(!host.IsAttached,"failed attach retained ownership"); Equal(Style,style); Equal(Parent,(nint)0);
        Attach(host);
    }),
    ("failed detach preserves the attached window for retry", () =>
    {
        var host=NewHost(); Attach(host); RejectNextParent=0; host.Detach();
        True(host.IsAttached,"failed detach dropped ownership"); Equal(Parent,Container);
        host.Detach(); True(!host.IsAttached,"detach retry failed");
    }),
    ("close detaches without showing a small restored window", () =>
    {
        var host=NewHost(); Attach(host); AfterReparent=()=>Callbacks(host);
        host.DetachForClose(); True(!host.IsAttached,"close retained attachment");
        Equal(Parent,(nint)0); True(!Visible,"close restored a standalone window");
        True((Style.ToInt64()&WsVisible)==0,"visible style restored during close");
    }),
    ("failed close detach restores visibility for recovery", () =>
    {
        var host=NewHost(); Attach(host); RejectNextParent=0;
        host.DetachForClose(); True(host.IsAttached,"failed close dropped attachment");
        Equal(Parent,Container); True(Visible,"failed close left the attached window hidden");
    }),
    ("closed window is forgotten", () =>
    {
        var host=NewHost(); Attach(host); var losses=0;
        host.AttachmentLost += (hwnd,parentChanged) =>
        {
            Equal(hwnd,Child); True(!parentChanged,"ordinary exit treated as parent loss");
            True(!host.IsTransitioning,"exit reported during transition"); losses++;
        };
        Alive=false; Callbacks(host);
        True(!host.IsAttached,"closed window retained");
        Equal(losses,1);
    }),
    ("restart response rejects delayed snapshots even if a PID was reused", () =>
    {
        var target=WindowLaunchIdentity.From(Profile("03","new",300));
        var old=Profile("03","old",300);
        True(!target.Matches(old),"reused PID from previous generation accepted");
        True(!target.Matches(Profile("03","new",200)),"old PID accepted");
        True(!target.Matches(Profile("04","new",300)),"another profile accepted");
        True(target.Matches(Profile("03","new",300)),"fresh launch rejected");
    })
};
var failed=0;
foreach(var test in tests)
{
    Reset();
    try { test.Body(); Console.WriteLine("PASS: "+test.Name); }
    catch(Exception ex) { failed++; Console.WriteLine("FAIL: "+test.Name+" — "+(ex.InnerException??ex).Message); }
}
Console.WriteLine($"{tests.Length-failed}/{tests.Length} passed; no native window APIs called.");
return failed==0?0:1;

static NativeWindowHost NewHost() { var host=new NativeWindowHost(); host.LoadFakeHost(); return host; }
static JsonElement Profile(string id,string generation,int processId) => JsonSerializer.SerializeToElement(new { id,generation,process_id=processId });
static void Attach(NativeWindowHost host) => True(host.Attach(Child,Pid,Executable,out var error),error);
static void Callbacks(NativeWindowHost host)
{
    host.DeliverLayoutCallback(); host.DeliverFocusCallback();
    typeof(NativeWindowHost).GetMethod("CheckAttachedWindow",BindingFlags.Instance|BindingFlags.NonPublic)!.Invoke(host,null);
}
static void True(bool value,string error) { if(!value) throw new Exception(error); }
static void Equal<T>(T actual,T expected) where T:notnull => True(actual.Equals(expected),$"expected {expected}, actual {actual}");
