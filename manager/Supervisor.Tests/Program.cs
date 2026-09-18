using System.Diagnostics;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using Codex.ControlCenter.Shared;

// Real transport against a disposable, synthetic Python backend. No original Codex,
// credentials, SSH connection, package updater, or actual manager state is touched.
internal static class Program
{
    private static int checks;
    private static readonly List<string> roots = [];
    private static readonly Dictionary<int, Process> ownedProcesses = [];

    private static async Task<int> Main(string[] args)
    {
        try
        {
            if (args.Contains("--clipboard-isolated"))
            {
                IsolatedClipboardTest.Run();
                return 0;
            }
            await TestFramesAsync();
            TestSupportLog();
            await TestLogCopyAsync();
            var root = FixtureRoot();
            await TestConcurrentClientsAsync(root);
            await TestNotesBypassSlowRequestAsync(root);
            await TestProtocolRejectionAsync(root);
            await TestDisconnectDrainsAsync(root);
            await TestAppSurvivalAsync(root);
            await TestServiceRetirementAsync(FixtureRoot());
            await TestBackendFaultAsync(FixtureRoot());
            if (args.Contains("--idle")) await TestIdleShutdownAsync(FixtureRoot());
            Console.WriteLine($"PASS: {checks} assertions; fixture-only transport integration.");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"FAIL: {exception.GetType().Name}: {exception.Message}");
            return 1;
        }
        finally { foreach (var root in roots) await CleanupAsync(root); }
    }

    private static void TestSupportLog()
    {
        var root = FixtureRoot();
        var log = new Codex.ControlCenter.Shell.DiagnosticLog(root);
        log.Add("Bearer private-bearer; api_key=private-key; \"access_token\":\"private-access\"");
        Check(!log.Text.Contains("private-"), "support logs redact credential-shaped values");
        for (var i = 0; i < 310; i++) log.Add("entry " + i);
        Check(log.Text.Split(Environment.NewLine).Length == 300, "support logs retain a bounded rolling window");
        Check(File.ReadAllText(log.Path) == log.Text, "copyable support log matches the saved file");
        log.Add("창 연결 완료 · Bearer private-lifecycle");
        log.Add("03 · Codex windowsSandbox/setupCompleted · Windows 설정 완료");
        for (var i = 0; i < 310; i++) log.Add("03 · Codex thread/list · 완료");
        Check(!log.Text.Contains("창 연결 완료"), "test reproduces routine RPC log eviction");
        Check(log.LifecycleText.Contains("창 연결 완료") && !log.LifecycleText.Contains("private-lifecycle"),
            "window lifecycle survives RPC polling with secrets redacted");
        Check(log.CopyText.Contains("Windows 설정 완료"), "copied feedback includes Windows setup lifecycle");
        Check(File.ReadAllText(log.LifecyclePath) == log.LifecycleText, "window diagnostics are saved separately");
        var exported=log.Export();
        Check(File.ReadAllText(exported)==log.CopyText,"export contains the same nonempty support snapshot as copy");
        var snapshot=log.CopyText; log.Add("later event"); log.Export(snapshot);
        Check(File.ReadAllText(exported)==snapshot,"clipboard failure fallback preserves the exact snapshot");
    }

    private static async Task TestLogCopyAsync()
    {
        const string text="창·설정 진행 기록\r\n04 · 대화 열기 완료\r\n최근 전체 로그";
        int writes=0, waits=0;
        await Codex.ControlCenter.Shell.ClipboardTextCopy.CopyAsync(text,value=>
        {
            Check(value==text,"clipboard retry keeps exact nonempty Unicode snapshot");
            return ++writes<3 ? new(false,true,"busy") : new(true,false);
        },()=>{waits++;return Task.CompletedTask;});
        Check(writes==3&&waits==2,"temporarily busy clipboard recovers with bounded asynchronous retries");
        writes=0;waits=0;
        await ThrowsAsync<InvalidOperationException>(()=>Codex.ControlCenter.Shell.ClipboardTextCopy.CopyAsync(text,value=>
        {writes++;return new(false,true,"busy");},()=>{waits++;return Task.CompletedTask;}),"busy clipboard reports failure instead of success");
        Check(writes==5&&waits==4,"clipboard contention cannot block indefinitely");
        writes=0;
        await ThrowsAsync<InvalidOperationException>(()=>Codex.ControlCenter.Shell.ClipboardTextCopy.CopyAsync(text,value=>
        {writes++;return new(false,false,"readback mismatch");}),"unverified content is never reported as copied");
        Check(writes==1,"nonretryable writes never erase the clipboard repeatedly");
        await ThrowsAsync<ArgumentException>(()=>Codex.ControlCenter.Shell.ClipboardTextCopy.CopyAsync(" ",value=>
        {throw new Exception("must not touch clipboard");}),"empty feedback is refused before clipboard access");
    }

    private static async Task TestFramesAsync()
    {
        using var stream = new MemoryStream(Encoding.UTF8.GetBytes("가나\r\n{}\n"));
        var reader = new JsonLineReader(stream, 7);
        Check(await reader.ReadAsync() == "가나", "UTF-8 and CRLF preserved");
        Check(await reader.ReadAsync() == "{}", "buffered second frame preserved");
        Check(await reader.ReadAsync() is null, "clean EOF accepted");
        await ThrowsAsync<IOException>(async () => await new JsonLineReader(new MemoryStream(Encoding.UTF8.GetBytes("가나\n")), 5).ReadAsync(), "byte limit, not character limit");
        await ThrowsAsync<IOException>(async () => await new JsonLineReader(new MemoryStream(Encoding.UTF8.GetBytes("incomplete")), 20).ReadAsync(), "partial frame EOF rejected");
        await ThrowsAsync<IOException>(async () => await new JsonLineReader(new MemoryStream(new byte[] { 0xC3, 0x28, 10 }), 10).ReadAsync(), "invalid UTF-8 rejected");
        await ThrowsAsync<IOException>(async () => await JsonLineReader.WriteAsync(new MemoryStream(), "literal\nnewline", 100), "literal newline rejected");
        await ThrowsAsync<IOException>(async () => await JsonLineReader.WriteAsync(new MemoryStream(), "가나", 5), "outbound UTF-8 byte limit");
        Console.WriteLine("PASS: bounded JSONL frames");
    }

    private static async Task TestConcurrentClientsAsync(string root)
    {
        var clients = await Task.WhenAll(Enumerable.Range(0, 8).Select(_ => ManagerClient.ConnectAsync(root)));
        try
        {
            var replies = await Task.WhenAll(clients.Select((client, index) => client.RequestAsync("state", new { token = index, delay = 0.02 })));
            Check(replies.Select((reply, index) => reply.GetProperty("token").GetInt32() == index).All(x => x), "concurrent replies correlate to callers");
            var sameClientReplies = await Task.WhenAll(Enumerable.Range(0, 8).Select(index => clients[0].RequestAsync("state", new { token = index })));
            Check(sameClientReplies.Select((reply, index) => reply.GetProperty("token").GetInt32() == index).All(x => x), "one client correlates simultaneous calls");
            var statuses = await Task.WhenAll(clients.Select(client => client.RequestAsync("supervisor.status")));
            foreach (var status in statuses) RegisterStatusProcesses(status);
            Check(statuses.Select(x => x.GetProperty("supervisor_pid").GetInt32()).Distinct().Count() == 1, "startup race results in one supervisor");
            Check(statuses.Select(x => x.GetProperty("backend_pid").GetInt32()).Distinct().Count() == 1, "one compatibility backend serves clients");
        }
        finally { foreach (var client in clients) await client.DisposeAsync(); }
        Console.WriteLine("PASS: concurrent clients and startup race");
    }

    private static async Task TestProtocolRejectionAsync(string root)
    {
        await using (var pipe = await ConnectRawAsync(root))
        {
            var reader = new JsonLineReader(pipe, ManagerProtocol.MaxResponseBytes);
            await SendRawAsync(pipe, reader, new { id = "version", version = 999, command = "state", args = new { } }, "protocol_version");
            await SendRawAsync(pipe, reader, new { id = "unknown", version = ManagerProtocol.Version, command = "shell.exec", args = new { } }, "unknown_command");
            await SendRawAsync(pipe, reader, new { id = "args", version = ManagerProtocol.Version, command = "state", args = "wrong" }, "invalid_request");
            await SendRawAsync(pipe, reader, new { id = "type", version = "wrong", command = "state", args = new { } }, "invalid_request");
            await JsonLineReader.WriteAsync(pipe, "{", ManagerProtocol.MaxRequestBytes);
            using var malformed = JsonDocument.Parse(await reader.ReadAsync() ?? throw new Exception("missing malformed response"));
            Check(malformed.RootElement.GetProperty("error").GetProperty("code").GetString() == "invalid_request", "malformed JSON rejected");
        }
        await using (var pipe = await ConnectRawAsync(root))
        {
            var oversized = new byte[ManagerProtocol.MaxRequestBytes + 8192];
            Array.Fill(oversized, (byte)'x');
            try { await pipe.WriteAsync(oversized); await pipe.FlushAsync(); } catch (IOException) { }
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            try { Check(await pipe.ReadAsync(new byte[1], deadline.Token) == 0, "oversized client disconnected"); }
            catch (IOException) { Check(true, "oversized client disconnected"); }
        }
        await using var client = await ManagerClient.ConnectAsync(root);
        var state = await client.RequestAsync("state");
        Check(state.GetProperty("profiles").ValueKind == JsonValueKind.Array, "server survives bad clients");
        Check(state.GetProperty("manager_protocol").GetString() == ManagerProtocol.Version.ToString(), "backend receives supervisor protocol needed for background update lifetime");
        Check((await client.RequestAsync("supervisor.status")).GetProperty("version").GetInt32() == ManagerProtocol.Version, "supervisor reports the shared protocol version");
        var catalog = await client.RequestAsync("catalog.show", new { profile_id = "fixture-profile", token = "fixture-catalog" });
        Check(catalog.GetProperty("token").GetString() == "fixture-catalog", "catalog.show reaches the synthetic backend through the allowlist");
        Check(File.ReadAllLines(Path.Combine(root, "backend-requests.log")).Last() == "catalog.show", "catalog.show dispatched without creating a real viewer");
        foreach (var command in new[] { "catalog.resolve", "handoff.preview", "conversation.continue", "profile.login", "profile.login_status" })
        {
            var reply = await client.RequestAsync(command, new { token = command });
            Check(reply.GetProperty("token").GetString() == command, command + " reaches only the fixture backend");
            Check(File.ReadAllLines(Path.Combine(root, "backend-requests.log")).Last() == command, command + " is explicitly allowed");
        }
        Console.WriteLine("PASS: protocol and oversized request rejection");
    }

    private static async Task TestNotesBypassSlowRequestAsync(string root)
    {
        await using var client = await ManagerClient.ConnectAsync(root);
        var slow = client.RequestAsync("profile.add", new { token = "notes-parallel", delay = 1.2 });
        await WaitFileAsync(Path.Combine(root, "started-notes-parallel"));
        var timer = Stopwatch.StartNew();
        var task = new { host_id = "local", thread_id = Guid.NewGuid().ToString() };
        var saved = await client.RequestAsync("notes.save", new { task, note_id = Guid.NewGuid().ToString(), revision = 0, title = "연결 시험", kind = "checklist", body = "", items = new[] { new { id = Guid.NewGuid().ToString(), text = "같은 연결의 저장", done = true } } });
        Check(saved.GetProperty("note").GetProperty("items")[0].GetProperty("done").GetBoolean(), "out-of-order note response reaches its own request");
        Check(timer.Elapsed < TimeSpan.FromMilliseconds(800) && !slow.IsCompleted, "notes bypass a slow profile request on the same C# connection");
        Check((await client.RequestAsync("supervisor.status")).GetProperty("engine").GetString() == "rust", "Rust service reports its engine");
        await slow;
        Check((await client.RequestAsync("notes.list", new { task })).GetProperty("notes").GetArrayLength() == 1, "later response cannot overwrite the note result");
        Console.WriteLine("PASS: same-connection out-of-order replies and independent note saving");
    }

    private static async Task TestDisconnectDrainsAsync(string root)
    {
        var client = await ManagerClient.ConnectAsync(root);
        using var cancel = new CancellationTokenSource();
        var mutation = client.RequestAsync("profile.add", new { token = "cancelled-mutation", delay = 0.8 }, cancel.Token);
        await WaitFileAsync(Path.Combine(root, "started-cancelled-mutation"));
        cancel.Cancel();
        await ThrowsAsync<OperationCanceledException>(async () => await mutation, "client cancellation does not report backend cancellation");
        await client.DisposeAsync();
        await using var replacement = await ManagerClient.ConnectAsync(root);
        var result = await replacement.RequestAsync("state", new { token = "after-cancel" });
        Check(result.GetProperty("token").GetString() == "after-cancel", "old mutation response drained before next client response");
        Check(File.ReadAllLines(Path.Combine(root, "mutations.log")).Count(x => x == "cancelled-mutation") == 1, "cancelled mutation executed exactly once, no retry");

        await using (var pipe = await ConnectRawAsync(root))
        {
            await JsonLineReader.WriteAsync(pipe, JsonSerializer.Serialize(new { id = "disconnect", version = ManagerProtocol.Version, command = "profile.add", args = new { token = "disconnected-mutation", delay = 0.5 } }), ManagerProtocol.MaxRequestBytes);
            await WaitFileAsync(Path.Combine(root, "started-disconnected-mutation"));
        }
        result = await replacement.RequestAsync("state", new { token = "after-disconnect" });
        Check(result.GetProperty("token").GetString() == "after-disconnect", "disconnected client response drained");
        Check(File.ReadAllLines(Path.Combine(root, "mutations.log")).Count(x => x == "disconnected-mutation") == 1, "disconnected mutation executed once");
        Console.WriteLine("PASS: cancellation/disconnection preserve mutation and drain responses");
    }

    private static async Task TestAppSurvivalAsync(string root)
    {
        int supervisorPid, backendPid, childPid;
        await using (var client = await ManagerClient.ConnectAsync(root))
        {
            var child = await client.RequestAsync("profile.show");
            childPid = child.GetProperty("process_id").GetInt32();
            RegisterFixtureProcess(childPid);
            var status = await client.RequestAsync("supervisor.status");
            RegisterStatusProcesses(status);
            supervisorPid = status.GetProperty("supervisor_pid").GetInt32();
            backendPid = status.GetProperty("backend_pid").GetInt32();
        }
        await Task.Delay(200);
        Check(Alive(supervisorPid) && Alive(backendPid) && Alive(childPid), "UI disconnect preserves supervisor, backend and tracked process");
        await using var reconnect = await ManagerClient.ConnectAsync(root);
        var statusAgain = await reconnect.RequestAsync("supervisor.status");
        Check(statusAgain.GetProperty("supervisor_pid").GetInt32() == supervisorPid, "reopen reuses surviving supervisor");
        Console.WriteLine("PASS: disconnect never kills tracked processes");
    }

    private static async Task TestServiceRetirementAsync(string root)
    {
        await using var client = await ManagerClient.ConnectAsync(root);
        var child = await client.RequestAsync("profile.show");
        var childPid = child.GetProperty("process_id").GetInt32();
        RegisterFixtureProcess(childPid);
        var status = await client.RequestAsync("supervisor.status");
        RegisterStatusProcesses(status);
        var supervisorPid = status.GetProperty("supervisor_pid").GetInt32();
        var backendPid = status.GetProperty("backend_pid").GetInt32();
        await using (var second = await ManagerClient.ConnectAsync(root))
            await ThrowsManagerAsync(() => client.RequestAsync("supervisor.retire"), "backend_busy", "active manager window must block retirement");
        await Task.Delay(150);
        Check((await client.RequestAsync("supervisor.retire")).GetProperty("retiring").GetBoolean(), "idle service accepts retirement");
        var deadline = Stopwatch.StartNew();
        while ((Alive(supervisorPid) || Alive(backendPid)) && deadline.Elapsed < TimeSpan.FromSeconds(6)) await Task.Delay(50);
        Check(!Alive(supervisorPid) && !Alive(backendPid), "retired service drains and exits its backend");
        Check(Alive(childPid), "service replacement must not terminate a managed app");
        await using var replacement = await ManagerClient.ConnectAsync(root);
        var next = await replacement.RequestAsync("supervisor.status");
        RegisterStatusProcesses(next);
        Check(next.GetProperty("supervisor_pid").GetInt32() != supervisorPid, "one fresh service takes over the same workspace endpoint");
        Check(Alive(childPid), "replacement service leaves existing app alive");
        Console.WriteLine("PASS: service replacement preserves running app and retires old backend");
    }

    private static async Task TestBackendFaultAsync(string root)
    {
        await using var client = await ManagerClient.ConnectAsync(root);
        await ThrowsManagerAsync(() => client.RequestAsync("providers.save", new { invalid_response = true }), "backend_unavailable", "mismatched backend response rejected");
        RegisterStatusProcesses(await client.RequestAsync("supervisor.status"));
        await ThrowsManagerAsync(() => client.RequestAsync("state"), "backend_unavailable", "faulted mutation is not automatically replayed");
        Check(File.ReadAllLines(Path.Combine(root, "backend-requests.log")).Length == 1, "fault prevents duplicate backend dispatch");
        var recovery = await client.RequestAsync("supervisor.reconnect");
        Check(recovery.GetProperty("reconnected").GetBoolean(), "explicit reconnect resets faulty backend transport");
        Check((await client.RequestAsync("state")).GetProperty("profiles").ValueKind == JsonValueKind.Array, "fresh state works after reconnect");
        RegisterStatusProcesses(await client.RequestAsync("supervisor.status"));
        Check(File.ReadAllLines(Path.Combine(root, "backend-requests.log")).SequenceEqual(new[] { "providers.save", "state" }), "recovery reads state without mutation replay");
        await ThrowsManagerAsync(() => client.RequestAsync("providers.save", new { invalid_utf8 = true }), "backend_unavailable", "invalid backend UTF-8 faults transport");
        Check((await client.RequestAsync("supervisor.reconnect")).GetProperty("reconnected").GetBoolean(), "invalid UTF-8 transport recovers explicitly");
        Check((await client.RequestAsync("state")).GetProperty("profiles").ValueKind == JsonValueKind.Array, "state works after invalid UTF-8 recovery");
        RegisterStatusProcesses(await client.RequestAsync("supervisor.status"));
        Console.WriteLine("PASS: protocol fault does not replay uncertain mutation");
    }

    private static async Task TestIdleShutdownAsync(string root)
    {
        int supervisorPid, backendPid;
        await using (var client = await ManagerClient.ConnectAsync(root))
        {
            await client.RequestAsync("state");
            var status = await client.RequestAsync("supervisor.status");
            RegisterStatusProcesses(status);
            supervisorPid = status.GetProperty("supervisor_pid").GetInt32();
            backendPid = status.GetProperty("backend_pid").GetInt32();
        }
        Console.WriteLine("Waiting for genuine 60-second idle policy (up to 90 seconds).");
        var timer = Stopwatch.StartNew();
        while (Alive(supervisorPid) && timer.Elapsed < TimeSpan.FromSeconds(90)) await Task.Delay(500);
        Check(!Alive(supervisorPid), "empty supervisor exits after idle grace");
        Check(!Alive(backendPid), "idle backend receives EOF and exits normally");
        Console.WriteLine("PASS: idle shutdown");
    }

    private static async Task SendRawAsync(Stream pipe, JsonLineReader reader, object request, string error)
    {
        await JsonLineReader.WriteAsync(pipe, JsonSerializer.Serialize(request), ManagerProtocol.MaxRequestBytes);
        using var response = JsonDocument.Parse(await reader.ReadAsync() ?? throw new Exception("missing protocol response"));
        Check(response.RootElement.GetProperty("error").GetProperty("code").GetString() == error, $"rejected as {error}");
    }

    private static async Task<NamedPipeClientStream> ConnectRawAsync(string root)
    {
        var pipe = new NamedPipeClientStream(".", ManagerProtocol.PipeName(root), PipeDirection.InOut, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        await pipe.ConnectAsync(5000);
        return pipe;
    }

    private static string FixtureRoot()
    {
        var root = Path.Combine(Path.GetTempPath(), "CodexControlCenter.Tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "scripts"));
        File.WriteAllText(Path.Combine(root, "scripts", "control_center.py"), PythonFixture, new UTF8Encoding(false));
        roots.Add(root);
        return root;
    }

    private static async Task WaitFileAsync(string path)
    {
        var timer = Stopwatch.StartNew();
        while (!File.Exists(path) && timer.Elapsed < TimeSpan.FromSeconds(5)) await Task.Delay(20);
        Check(File.Exists(path), "fixture mutation started");
    }

    private static async Task CleanupAsync(string root)
    {
        // Only exact PIDs verified through this unique fixture endpoint/files are terminated.
        try
        {
            await using var pipe = await ConnectRawAsync(root);
            var reader = new JsonLineReader(pipe, ManagerProtocol.MaxResponseBytes);
            await JsonLineReader.WriteAsync(pipe, JsonSerializer.Serialize(new { id = "cleanup", version = ManagerProtocol.Version, command = "supervisor.status", args = new { } }), ManagerProtocol.MaxRequestBytes);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            using var response = JsonDocument.Parse(await reader.ReadAsync(timeout.Token) ?? "{}");
            var status = response.RootElement.GetProperty("result");
            RegisterStatusProcesses(status);
            if (status.GetProperty("backend_pid").ValueKind == JsonValueKind.Number) StopFixtureProcess(status.GetProperty("backend_pid").GetInt32());
            StopFixtureProcess(status.GetProperty("supervisor_pid").GetInt32());
        }
        catch (Exception exception) when (exception is IOException or TimeoutException or JsonException or OperationCanceledException or KeyNotFoundException) { }
        foreach (var filename in new[] { "backend.pid", "child.pid" })
            if (File.Exists(Path.Combine(root, filename)) && int.TryParse(File.ReadAllText(Path.Combine(root, filename)), out var pid))
            {
                // A previously opened handle remains tied to its original process if a PID is reused.
                RegisterFixtureProcess(pid);
                StopFixtureProcess(pid);
            }
        var resolved = Path.GetFullPath(root);
        var expected = Path.GetFullPath(Path.Combine(Path.GetTempPath(), "CodexControlCenter.Tests")) + Path.DirectorySeparatorChar;
        if (!resolved.StartsWith(expected, StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Unexpected fixture cleanup path.");
        try { Directory.Delete(resolved, true); } catch (IOException) { }
    }

    private static void StopFixtureProcess(int pid)
    {
        if (!ownedProcesses.TryGetValue(pid, out var process)) return;
        try { if (!process.HasExited) { process.Kill(false); process.WaitForExit(3000); } }
        catch (ArgumentException) { }
        catch (InvalidOperationException) { }
    }

    private static void RegisterStatusProcesses(JsonElement status)
    {
        RegisterFixtureProcess(status.GetProperty("supervisor_pid").GetInt32());
        if (status.GetProperty("backend_pid").ValueKind == JsonValueKind.Number)
            RegisterFixtureProcess(status.GetProperty("backend_pid").GetInt32());
    }

    private static void RegisterFixtureProcess(int pid)
    {
        if (ownedProcesses.ContainsKey(pid)) return;
        try
        {
            var process = Process.GetProcessById(pid);
            _ = process.Handle;
            ownedProcesses.Add(pid, process);
        }
        catch (ArgumentException) { }
        catch (InvalidOperationException) { }
    }

    private static bool Alive(int pid)
    {
        try { using var process = Process.GetProcessById(pid); return !process.HasExited; }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
    }

    private static void Check(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
        checks++;
    }

    private static async Task ThrowsAsync<T>(Func<Task> action, string message) where T : Exception
    {
        try { await action(); } catch (T) { checks++; return; }
        throw new Exception(message + " (expected " + typeof(T).Name + ")");
    }

    private static async Task ThrowsManagerAsync(Func<Task<JsonElement>> action, string code, string message)
    {
        try { await action(); } catch (ManagerException exception) when (exception.Code == code) { checks++; return; }
        throw new Exception(message);
    }

    private const string PythonFixture = """
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(__file__).resolve().parent.parent
(root / 'backend.pid').write_text(str(os.getpid()))
for line in sys.stdin:
    req = json.loads(line)
    args = req.get('args', {})
    with (root / 'backend-requests.log').open('a') as log:
        log.write(req['command'] + '\n')
    if req['command'] == 'profile.add':
        token = args['token']
        (root / ('started-' + token)).write_text('started')
        time.sleep(args.get('delay', 0))
        with (root / 'mutations.log').open('a') as log:
            log.write(token + '\n')
    else:
        time.sleep(args.get('delay', 0))
    profiles = [{'process_id': None, 'status': 'not_started'}]
    if req['command'] == 'profile.show':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(240)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        (root / 'child.pid').write_text(str(child.pid))
    if (root / 'child.pid').exists():
        profiles = [{'process_id': int((root / 'child.pid').read_text()), 'status': 'running'}]
    result = {'token': args.get('token'), 'profiles': profiles, 'manager_protocol': os.environ.get('CODEX_MANAGER_PROTOCOL_VERSION')}
    if req['command'] == 'profile.show':
        result['process_id'] = child.pid
    if args.get('invalid_utf8'):
        sys.stdout.buffer.write(bytes([0xC3, 0x28, 10]))
        sys.stdout.buffer.flush()
        continue
    response = {'id': 'wrong-id' if args.get('invalid_response') else req['id'], 'ok': True, 'result': result}
    print(json.dumps(response), flush=True)
""";
}
