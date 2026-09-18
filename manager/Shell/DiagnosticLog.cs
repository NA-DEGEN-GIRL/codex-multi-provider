using System.IO;
using System.Text.RegularExpressions;

namespace Codex.ControlCenter.Shell;

/// <summary>Bounded support log; RPC bodies and command arguments never belong here.</summary>
internal sealed class DiagnosticLog
{
    private readonly List<string> _lines = [];
    private readonly List<string> _lifecycle = [];
    public string Path { get; }
    public string LifecyclePath { get; }
    public string Text => string.Join(Environment.NewLine, _lines);
    public string LifecycleText => string.Join(Environment.NewLine, _lifecycle);
    public string CopyText => "창·설정 진행 기록" + Environment.NewLine + LifecycleText +
        Environment.NewLine + Environment.NewLine + "최근 전체 로그" + Environment.NewLine + Text;
    public string? WriteError { get; private set; }
    public string Export(string? text = null)
    {
        var path = System.IO.Path.ChangeExtension(Path, "feedback.txt");
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
        File.WriteAllText(path, text ?? CopyText, new System.Text.UTF8Encoding(false));
        return path;
    }
    public DiagnosticLog(string root)
    {
        Path = System.IO.Path.Combine(root, "work", "control-center", "logs",
            $"shell-{DateTime.Now:yyyyMMdd-HHmmss}-{Environment.ProcessId}.log");
        LifecyclePath = System.IO.Path.ChangeExtension(Path, "windows.log");
    }

    public string Add(string message)
    {
        var line = $"{DateTime.Now:HH:mm:ss}  {Redact(message)}";
        _lines.Add(line);
        if (_lines.Count > 300) _lines.RemoveAt(0);
        bool lifecycle = !message.Contains(" · Codex ", StringComparison.Ordinal) ||
            message.Contains(" · Codex windowsSandbox/", StringComparison.Ordinal) ||
            message.Contains(" · 실패", StringComparison.Ordinal) ||
            message.Contains(" · Codex thread/name/set", StringComparison.Ordinal);
        if (lifecycle)
        {
            _lifecycle.Add(line);
            if (_lifecycle.Count > 300) _lifecycle.RemoveAt(0);
        }
        try
        {
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
            File.WriteAllText(Path, Text);
            if (lifecycle) File.WriteAllText(LifecyclePath, LifecycleText);
            WriteError = null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { WriteError = "로그 파일 저장 실패 · 화면에서 복사할 수 있습니다."; }
        return line;
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
