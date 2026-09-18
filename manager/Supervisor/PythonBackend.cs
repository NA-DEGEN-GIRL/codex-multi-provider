using System.Diagnostics;
using System.Text.Json;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Supervisor;

internal sealed class PythonBackend : IAsyncDisposable
{
    private readonly string root;
    private readonly SemaphoreSlim requests = new(1, 1);
    private Process? process;
    private JsonLineReader? reader;
    private string? fault;
    private int pending;
    public int PendingRequests => Volatile.Read(ref pending);
    public bool IsFaulted => fault is not null;
    public int? ProcessId
    {
        get
        {
            var current = process;
            try { return current is { HasExited: false } ? current.Id : null; }
            catch (InvalidOperationException) { return null; }
        }
    }
    public string Status => fault is not null ? "faulted" : process is null ? "not_started" : ProcessId is null ? "exited" : PendingRequests > 0 ? "busy" : "ready";

    public PythonBackend(string root) { this.root = root; }

    public async Task<bool> ReconnectAsync()
    {
        Interlocked.Increment(ref pending);
        await requests.WaitAsync().ConfigureAwait(false);
        try
        {
            if (process is not null)
            {
                try { process.StandardInput.Close(); } catch (IOException) { }
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                try { await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false); }
                catch (OperationCanceledException) { return false; }
                process.Dispose();
            }
            process = null;
            reader = null;
            fault = null;
            // A subsequent state request starts a fresh backend. No previous command is replayed.
            return true;
        }
        catch (Exception e) when (e is IOException or InvalidOperationException) { return false; }
        finally { requests.Release(); Interlocked.Decrement(ref pending); }
    }

    public async Task<string> RequestAsync(string request, string id)
    {
        Interlocked.Increment(ref pending);
        await requests.WaitAsync().ConfigureAwait(false);
        try
        {
            if (fault is not null) return Error(id, "backend_unavailable", fault);
            if (process is null) await StartAsync().ConfigureAwait(false);
            if (process!.HasExited) throw new IOException("The backend exited.");
            // Once dispatched, this response is drained even if its UI pipe disconnects.
            await JsonLineReader.WriteAsync(process.StandardInput.BaseStream, request, ManagerProtocol.MaxRequestBytes).ConfigureAwait(false);
            var line = await reader!.ReadAsync().ConfigureAwait(false)
                ?? throw new IOException("The backend closed its response stream.");
            using var document = JsonDocument.Parse(line);
            var response = document.RootElement;
            if (response.ValueKind != JsonValueKind.Object || !response.TryGetProperty("id", out var returnedId)
                || returnedId.ValueKind != JsonValueKind.String || returnedId.GetString() != id
                || !response.TryGetProperty("ok", out var ok) || ok.ValueKind is not (JsonValueKind.True or JsonValueKind.False)
                || (ok.GetBoolean() ? !response.TryGetProperty("result", out _) : !response.TryGetProperty("error", out _)))
                throw new IOException("The backend response did not match its request.");
            return line;
        }
        catch (Exception e) when (e is IOException or JsonException or InvalidOperationException or System.ComponentModel.Win32Exception or UnauthorizedAccessException)
        {
            // Do not replay or restart an uncertain mutation. Original Codex processes are never killed.
            fault = "백엔드 연결에 문제가 생겼습니다. 이미 시작한 작업은 계속될 수 있습니다. 관리자 창을 닫았다가 열고 상태를 확인해 주세요.";
            return Error(id, "backend_unavailable", fault);
        }
        finally
        {
            requests.Release();
            Interlocked.Decrement(ref pending);
        }
    }

    private async Task StartAsync()
    {
        var bundled = File.Exists(Path.Combine(AppContext.BaseDirectory, "runtime-manifest.json"));
        var script = Path.GetFullPath(Path.Combine(bundled ? AppContext.BaseDirectory : root, "scripts", "control_center.py"));
        var expectedPrefix = Path.TrimEndingDirectorySeparator(root) + Path.DirectorySeparatorChar;
        if (!script.StartsWith(expectedPrefix, StringComparison.OrdinalIgnoreCase) || !File.Exists(script))
            throw new IOException("The manager backend was not found inside the selected workspace.");
        var python = await FindPythonAsync().ConfigureAwait(false);
        var start = new ProcessStartInfo(python)
        {
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        start.ArgumentList.Add("-u");
        start.ArgumentList.Add(script);
        start.ArgumentList.Add("--serve");
        start.ArgumentList.Add("--root");
        start.ArgumentList.Add(root);
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        start.Environment["PYTHONUTF8"] = "1";
        start.Environment["CODEX_MANAGER_PROTOCOL_VERSION"] = ManagerProtocol.Version.ToString();
        process = Process.Start(start) ?? throw new IOException("Python did not start.");
        reader = new JsonLineReader(process.StandardOutput.BaseStream, ManagerProtocol.MaxResponseBytes);
        // Never copy diagnostics to a log: third-party messages can contain credentials.
        _ = DrainDiagnosticsAsync(process.StandardError.BaseStream);
    }

    private static async Task DrainDiagnosticsAsync(Stream stream)
    {
        try
        {
            var buffer = new byte[4096];
            while (await stream.ReadAsync(buffer).ConfigureAwait(false) > 0) { }
        }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
    }

    private static async Task<string> FindPythonAsync()
    {
        var candidates = new List<(string Path, bool Launcher)>();
        var windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        candidates.Add((Path.Combine(windows, "py.exe"), true));
        var systemDrive = Path.GetPathRoot(windows) ?? "C:\\";
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        foreach (var version in new[] { "314", "313", "312", "311" })
        {
            candidates.Add((Path.Combine(systemDrive, "Python" + version, "python.exe"), false));
            candidates.Add((Path.Combine(local, "Programs", "Python", "Python" + version, "python.exe"), false));
            candidates.Add((Path.Combine(programFiles, "Python" + version, "python.exe"), false));
        }
        foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            var path = directory.Trim().Trim('"');
            if (!Path.IsPathFullyQualified(path) || path.Contains("Microsoft\\WindowsApps", StringComparison.OrdinalIgnoreCase)) continue;
            candidates.Add((Path.Combine(path, "python.exe"), false));
            candidates.Add((Path.Combine(path, "py.exe"), true));
        }
        foreach (var candidate in candidates.DistinctBy(c => c.Path, StringComparer.OrdinalIgnoreCase))
        {
            if (!File.Exists(candidate.Path)) continue;
            var executable = await ProbePythonAsync(candidate.Path, candidate.Launcher).ConfigureAwait(false);
            if (executable is not null) return executable;
        }
        throw new IOException("Python 3.11 or newer was not found.");
    }

    private static async Task<string?> ProbePythonAsync(string executable, bool launcher)
    {
        try
        {
            var start = new ProcessStartInfo(executable)
            {
                UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true, RedirectStandardError = true,
            };
            if (launcher) start.ArgumentList.Add("-3");
            start.ArgumentList.Add("-I");
            start.ArgumentList.Add("-c");
            start.ArgumentList.Add("import sys; print(sys.executable if sys.version_info >= (3, 11) else '')");
            using var probe = Process.Start(start);
            if (probe is null) return null;
            _ = DrainDiagnosticsAsync(probe.StandardError.BaseStream);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            string? output;
            try
            {
                output = await new JsonLineReader(probe.StandardOutput.BaseStream, 8192).ReadAsync(timeout.Token).ConfigureAwait(false);
                await probe.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // Only this isolated discovery probe is ours to terminate, never a backend or app tree.
                try { probe.Kill(); } catch (InvalidOperationException) { }
                return null;
            }
            output = output?.Trim();
            if (probe.ExitCode != 0 || string.IsNullOrWhiteSpace(output) || !Path.IsPathFullyQualified(output)
                || !File.Exists(output) || !string.Equals(Path.GetExtension(output), ".exe", StringComparison.OrdinalIgnoreCase))
                return null;
            return Path.GetFullPath(output);
        }
        catch (Exception e) when (e is IOException or System.ComponentModel.Win32Exception or UnauthorizedAccessException or InvalidOperationException)
        { return null; }
    }

    public static string Error(string? id, string code, string message) => JsonSerializer.Serialize(new
    {
        id, ok = false, error = new { code, message = ManagerProtocol.SafeMessage(message) }
    });

    public async ValueTask DisposeAsync()
    {
        if (process is null) return;
        try
        {
            process.StandardInput.Close();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (Exception e) when (e is InvalidOperationException or IOException or OperationCanceledException) { }
        // Process.Dispose releases our handles only. Detached original Codex instances remain alive.
        process.Dispose();
    }
}
