using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace Codex.ControlCenter.Shell;

/// <summary>Support log; RPC bodies and command arguments never belong here.
/// Every line is appended to the file of its day (a new file each day, or once
/// a file passes MaxFileBytes), so a whole day stays on disk. The window keeps
/// the latest WindowLines for display and copy. Older shell logs are pruned:
/// text logs after RetainDays or beyond TextBudgetBytes, performance traces
/// after RetainDays or beyond PerformanceBudgetBytes (newest kept first).</summary>
internal sealed class DiagnosticLog
{
    internal const int RetainDays = 7;
    internal const long TextBudgetBytes = 50L * 1024 * 1024;
    internal const long PerformanceBudgetBytes = 100L * 1024 * 1024;
    internal const long MaxFileBytes = 16L * 1024 * 1024;
    internal const int WindowLines = 300;
    private readonly List<string> _lines = [];
    private readonly List<string> _lifecycle = [];
    private readonly string _directory;
    private readonly Func<DateTime> _now;
    private DateTime _day;
    private long _written;
    public string Path { get; private set; } = "";
    public string LifecyclePath { get; private set; } = "";
    /// <summary>This process's first log file; its performance trace is named after it.</summary>
    public string FirstPath { get; }
    public string Text => string.Join(Environment.NewLine, _lines);
    public string LifecycleText => string.Join(Environment.NewLine, _lifecycle);
    public string CopyText => "창·설정 진행 기록" + Environment.NewLine + LifecycleText +
        Environment.NewLine + Environment.NewLine + "최근 전체 로그" + Environment.NewLine + Text;
    public string? WriteError { get; private set; }
    public string Export(string? text = null)
    {
        var path = System.IO.Path.ChangeExtension(Path, "feedback.txt");
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
        File.WriteAllText(path, text ?? CopyText, new UTF8Encoding(false));
        return path;
    }
    public DiagnosticLog(string root, Func<DateTime>? now = null, bool prune = true)
    {
        _now = now ?? (() => DateTime.Now);
        _directory = System.IO.Path.Combine(root, "work", "control-center", "logs");
        Open(_now());
        FirstPath = Path;
        // Off the UI thread: a week of logs is a directory listing and a few deletes.
        if (prune) PruneLater();
    }

    private void Open(DateTime at)
    {
        _day = at.Date;
        var name = $"shell-{at:yyyyMMdd-HHmmss}-{Environment.ProcessId}";
        var path = System.IO.Path.Combine(_directory, name + ".log");
        // A size rotation within the same second keeps a distinct name.
        for (var part = 2; File.Exists(path); part++)
            path = System.IO.Path.Combine(_directory, $"{name}-{part}.log");
        Path = path;
        LifecyclePath = System.IO.Path.ChangeExtension(Path, "windows.log");
        _written = 0;
    }

    public string Add(string message)
    {
        var at = _now();
        var line = $"{at:HH:mm:ss}  {Redact(message)}";
        _lines.Add(line);
        if (_lines.Count > WindowLines) _lines.RemoveAt(0);
        bool lifecycle = !message.Contains(" · Codex ", StringComparison.Ordinal) ||
            message.Contains(" · Codex windowsSandbox/", StringComparison.Ordinal) ||
            message.Contains(" · 실패", StringComparison.Ordinal) ||
            message.Contains(" · Codex thread/name/set", StringComparison.Ordinal);
        if (lifecycle)
        {
            _lifecycle.Add(line);
            if (_lifecycle.Count > WindowLines) _lifecycle.RemoveAt(0);
        }
        try
        {
            Directory.CreateDirectory(_directory);
            if (at.Date != _day || _written >= MaxFileBytes)
            {
                var previous = System.IO.Path.GetFileName(Path);
                Open(at);
                Append(Path, $"{at:HH:mm:ss}  로그 이어짐 · 이전 파일 {previous}");
                PruneLater();
            }
            Append(Path, line);
            if (lifecycle) Append(LifecyclePath, line, counted: false);
            WriteError = null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { WriteError = "로그 파일 저장 실패 · 화면에서 복사할 수 있습니다."; }
        return line;
    }

    private void Append(string path, string line, bool counted = true)
    {
        var text = line + Environment.NewLine;
        File.AppendAllText(path, text, new UTF8Encoding(false));
        if (counted) _written += Encoding.UTF8.GetByteCount(text);
    }

    private void PruneLater()
    {
        // This process's files: the current text logs, the first ones and the
        // performance trace named after the first (with its rotations).
        var keep = new[] { Path, LifecyclePath, FirstPath, System.IO.Path.ChangeExtension(FirstPath, "windows.log"),
            System.IO.Path.ChangeExtension(FirstPath, "performance.jsonl") };
        var directory = _directory;
        var now = _now();
        _ = Task.Run(() => Prune(directory, now, keep));
    }

    /// <summary>Past limit, shift path to path.1 (newest) .. path.rotations (oldest dropped).</summary>
    internal static void Rotate(string path, long limit, int rotations)
    {
        if (new FileInfo(path) is not { Exists: true } file || file.Length <= limit) return;
        for (int index = rotations; index >= 1; index--)
        {
            var source = index == 1 ? path : path + "." + (index - 1);
            if (File.Exists(source)) File.Move(source, path + "." + index, true);
        }
    }

    /// <summary>Delete old shell logs. A file whose full path starts with an entry
    /// of keep stays; other log families (backend, service) are never touched.</summary>
    internal static int Prune(string directory, DateTime now, IReadOnlyCollection<string> keep,
        long textBudget = TextBudgetBytes, long performanceBudget = PerformanceBudgetBytes)
    {
        FileInfo[] files;
        try { files = new DirectoryInfo(directory).GetFiles("shell-*"); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { return 0; }
        var kept = keep.Select(System.IO.Path.GetFullPath).ToArray();
        var cutoff = now - TimeSpan.FromDays(RetainDays);
        int deleted = 0;
        // Performance traces are large and frequent; text logs are small and
        // carry the history people ask for. Each family has its own budget.
        foreach (var family in files.GroupBy(f => f.Name.Contains(".performance.jsonl", StringComparison.OrdinalIgnoreCase)))
        {
            long budget = family.Key ? performanceBudget : textBudget, used = 0;
            foreach (var file in family.OrderByDescending(f => f.LastWriteTimeUtc))
            {
                used += file.Length;
                if (kept.Any(path => file.FullName.StartsWith(path, StringComparison.OrdinalIgnoreCase))
                    || (file.LastWriteTime >= cutoff && used <= budget)) continue;
                try { file.Delete(); deleted++; }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
            }
        }
        return deleted;
    }

    internal static string Redact(string message)
    {
        var text = message.Length > 4000 ? message[..4000] : message;
        text = Regex.Replace(text, @"(?i)(bearer\s+|(?:access_token|refresh_token|api[_-]?key)[""']?\s*[=:]\s*[""']?)[^\s,""';]+", "$1[숨김]");
        text = Regex.Replace(text, @"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[로그인 토큰 숨김]");
        text = Regex.Replace(text, @"\bsk-[A-Za-z0-9_-]+\b", "[API 키 숨김]");
        return new string(text.Where(c => !char.IsControl(c) || c == ' ').Take(1600).ToArray());
    }
}
