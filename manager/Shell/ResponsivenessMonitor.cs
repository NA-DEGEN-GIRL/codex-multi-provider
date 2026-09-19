using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

// UI scopes only enqueue metadata. A background sampler persists it even when
// the dispatcher is stuck. No input text, task contents or screenshots are read.
internal sealed class ResponsivenessMonitor : IDisposable
{
    internal sealed record Target(int Pid, long Hwnd);
    private sealed record Phase(string Name, long At, Phase? Previous);
    private readonly DispatcherTimer heartbeat;
    private readonly System.Threading.Timer sampler;
    private readonly ConcurrentQueue<object> records = new();
    private readonly ConcurrentQueue<string> recent = new();
    private readonly string directory;
    private volatile Phase? phase;
    private volatile Target[] targets = [];
    private volatile Target? selected;
    private long lastHeartbeat = Environment.TickCount64, lastSample, lastStall;
    private int sampling, disposed;
    internal string Path { get; }
    internal string RecentText => string.Join(Environment.NewLine, recent);

    internal ResponsivenessMonitor(string logPath, string root, Dispatcher dispatcher)
    {
        Path = System.IO.Path.ChangeExtension(logPath, "performance.jsonl");
        directory = System.IO.Path.Combine(root, "work", "control-center", "window-hosts");
        heartbeat = new DispatcherTimer(TimeSpan.FromMilliseconds(200), DispatcherPriority.Input,
            (_, _) => Interlocked.Exchange(ref lastHeartbeat, Environment.TickCount64), dispatcher);
        sampler = new System.Threading.Timer(_ => Sample(), null, 250, 250);
    }

    internal IDisposable Stage(string name, int threshold = 100)
    {
        var current = new Phase(name, Environment.TickCount64, phase);
        phase = current;
        return new Completion(() => {
            if (ReferenceEquals(phase, current)) phase = current.Previous;
            long elapsed = Environment.TickCount64 - current.At;
            if (elapsed >= threshold) Record("ui_operation", new { name, elapsed_ms = elapsed });
        });
    }

    internal IDisposable Time(string name, int threshold = 0)
    {
        long started = Environment.TickCount64;
        return new Completion(() => {
            long elapsed = Environment.TickCount64 - started;
            if (elapsed >= threshold) Record("operation", new { name, elapsed_ms = elapsed });
        });
    }

    internal void Select(Target? value, IEnumerable<Target> all)
    { selected = value; targets = all.DistinctBy(t => t.Pid).Take(24).ToArray(); }

    internal void Record(string kind, object data)
    {
        if (Volatile.Read(ref disposed) != 0 || records.Count >= 256) return;
        records.Enqueue(new { at = DateTimeOffset.Now, kind, data });
    }

    private void Sample()
    {
        if (Interlocked.Exchange(ref sampling, 1) != 0) return;
        try
        {
            if (Volatile.Read(ref disposed) != 0) { Flush(); return; }
            long now = Environment.TickCount64;
            long lag = now - Interlocked.Read(ref lastHeartbeat);
            if (lag >= 750 && now - lastStall >= 1500)
            {
                lastStall = now;
                Record("ui_stall", new { lag_ms = lag, phase = phase?.Name,
                    phase_ms = phase is { } p ? now - p.At : 0, selected_pid = selected?.Pid });
            }
            if (now - lastSample >= 2000)
            {
                lastSample = now;
                var target = selected;
                int rendererPid = 0;
                object? native = null, renderer = null, presentation = null;
                if (target is not null)
                {
                    GetWindowThreadProcessId((nint)target.Hwnd, out var pid);
                    if (pid == target.Pid)
                    {
                        var watch = Stopwatch.StartNew();
                        bool responded = SendMessageTimeoutW((nint)target.Hwnd, 0, 0, 0, 3, 100, out _) != 0;
                        GetWindowThreadProcessId(GetForegroundWindow(), out var foregroundPid);
                        uint pointerPid = 0;
                        if (GetCursorPos(out var point)) GetWindowThreadProcessId(WindowFromPoint(point), out pointerPid);
                        native = new { target.Pid, responded, elapsed_ms = watch.ElapsedMilliseconds,
                            foreground_pid = foregroundPid, pointer_window_pid = pointerPid,
                            visible = IsWindowVisible((nint)target.Hwnd), enabled = IsWindowEnabled((nint)target.Hwnd) };
                    }
                    try
                    {
                        var file = new FileInfo(System.IO.Path.Combine(directory, $"{target.Pid}.health.json"));
                        if (file.Exists && file.Length < 4096)
                        {
                            using var input = OpenSnapshot(file.FullName);
                            using var json = JsonDocument.Parse(input);
                            if (json.RootElement.GetProperty("appPid").GetInt32() == target.Pid &&
                                json.RootElement.GetProperty("hwnd").GetString() == target.Hwnd.ToString())
                            {
                                renderer = new { age_ms = (DateTime.UtcNow - file.LastWriteTimeUtc).TotalMilliseconds, health = json.RootElement.Clone() };
                                if (json.RootElement.TryGetProperty("rendererPid", out var r) && r.TryGetInt32(out var id) && id > 0) rendererPid = id;
                            }
                        }
                    }
                    catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or KeyNotFoundException or InvalidOperationException) { }
                    try
                    {
                        var file = new FileInfo(System.IO.Path.Combine(directory, $"{target.Pid}.json"));
                        if (file.Exists && file.Length < 32768)
                        {
                            using var input = OpenSnapshot(file.FullName);
                            using var json = JsonDocument.Parse(input);
                            var state = json.RootElement;
                            if (state.GetProperty("appPid").GetInt32() == target.Pid && state.GetProperty("hwnd").GetString() == target.Hwnd.ToString())
                                presentation = new { mode = state.GetProperty("mode").GetString(),
                                    visible = state.GetProperty("visible").GetBoolean(),
                                    epoch = state.GetProperty("presentationEpoch").GetInt64(),
                                    age_ms = (DateTime.UtcNow - file.LastWriteTimeUtc).TotalMilliseconds };
                        }
                    }
                    catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or KeyNotFoundException or InvalidOperationException) { }
                }
                var processes = new List<object>();
                foreach (int pid in targets.Select(t => t.Pid).Append(rendererPid).Prepend(Environment.ProcessId).Where(id => id > 0).Distinct())
                {
                    try
                    {
                        using var process = Process.GetProcessById(pid);
                        processes.Add(new { pid, private_mb = process.PrivateMemorySize64 / 1048576,
                            working_mb = process.WorkingSet64 / 1048576, handles = process.HandleCount,
                            threads = process.Threads.Count, cpu_ms = (long)process.TotalProcessorTime.TotalMilliseconds });
                    }
                    catch (Exception e) when (e is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception) { }
                }
                Record("sample", new { ui_lag_ms = lag, native, renderer, presentation, processes });
            }
            Flush();
        }
        catch (Exception error) { Record("diagnostic_error", new { type = error.GetType().Name }); }
        finally { Interlocked.Exchange(ref sampling, 0); }
    }

    private void Flush()
    {
        if (records.IsEmpty) return;
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
        if (new FileInfo(Path) is { Exists: true, Length: > 4_000_000 }) File.Move(Path, Path + ".previous", true);
        using var writer = new StreamWriter(Path, append: true);
        while (records.TryDequeue(out var record))
        {
            var line = JsonSerializer.Serialize(record); writer.WriteLine(line);
            recent.Enqueue(line); while (recent.Count > 60) recent.TryDequeue(out _);
        }
    }

    // Diagnostics must never block the writer's atomic rename on Windows.
    private static FileStream OpenSnapshot(string file) => new(file, FileMode.Open,
        FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);

    public void Dispose()
    {
        Interlocked.Exchange(ref disposed, 1); heartbeat.Stop(); sampler.Dispose();
        // Do not put disk I/O or a sampler join on the closing UI thread.
        _ = Task.Run(Sample);
    }
    private sealed class Completion(Action end) : IDisposable { private Action? action = end; public void Dispose() => Interlocked.Exchange(ref action, null)?.Invoke(); }
    [StructLayout(LayoutKind.Sequential)] private struct Point { public int X, Y; }
    [DllImport("user32.dll")] private static extern nint SendMessageTimeoutW(nint hwnd, uint message, nuint w, nint l, uint flags, uint timeout, out nuint result);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] private static extern nint WindowFromPoint(Point point);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindowEnabled(nint hwnd);
}
