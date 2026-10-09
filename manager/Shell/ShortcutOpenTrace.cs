using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// One task-shortcut click, from the click to its outcome: the phase its card
// and the status line show, and one structured record of every phase (ms
// after the click) for the performance trace. Phases come from what the
// service answered; launch phases use the timings it measured itself.
internal sealed class ShortcutOpenTrace
{
    private readonly Func<long> clock;
    private readonly long started;
    private readonly List<(string Phase, long At)> phases = [];
    private long requestSent = -1;
    internal string ShortcutId { get; }
    internal string ProfileId { get; }
    /// <summary>The SSH alias of the task's host, or "" for a local task.</summary>
    internal string Host { get; }
    internal bool ProfileRunning { get; }
    /// <summary>This open repeats one whose app exited after its link (never repeated again).</summary>
    internal bool ExitRetry { get; init; }
    internal string Phase { get; private set; } = "click";
    internal string? Outcome { get; private set; }
    internal bool Finished => Outcome is not null;
    internal long Elapsed => clock() - started;
    internal IReadOnlyList<(string Phase, long At)> Phases => phases;

    internal ShortcutOpenTrace(string shortcutId, string profileId, string hostId, bool profileRunning, Func<long>? clock = null)
    {
        this.clock = clock ?? (() => Environment.TickCount64);
        started = this.clock();
        ShortcutId = shortcutId; ProfileId = profileId; ProfileRunning = profileRunning;
        Host = hostId.StartsWith("ssh:", StringComparison.Ordinal) ? hostId[4..] : hostId is "" or "local" ? "" : hostId;
        phases.Add(("click", 0));
    }

    /// <summary>Record a phase once (now, or at a measured offset after the click).</summary>
    internal void Mark(string phase, long? at = null)
    {
        if (Finished || phases.Any(p => p.Phase == phase)) return;
        var when = Math.Max(0, at ?? Elapsed);
        phases.Add((phase, when));
        phases.Sort((a, b) => a.At.CompareTo(b.At));
        Phase = phases[^1].Phase;
        if (phase == "request_sent") requestSent = when;
    }

    /// <summary>Phases from one service answer to conversation.open or conversation.navigate.</summary>
    internal void Observe(JsonElement result)
    {
        var launch = result.Get("launch");
        if (launch.S("state") == "launched" && requestSent >= 0)
        {
            // The service measured its admission wait and the launch itself.
            var admitted = requestSent + Math.Max(0, launch.N("admission_ms"));
            Mark("launch_start", admitted);
            Mark("launch_ready", admitted + Math.Max(0, launch.N("launch_ms")));
        }
        switch (result.S("state"))
        {
            case "waiting_for_reader":
                switch (result.S("reason"))
                {
                    case "remote_connection_starting":
                        if (Phase is "reader_wait" or "app_relaunch") Mark("reader_ready");
                        Mark("ssh_wait");
                        break;
                    case "app_relaunching":
                        Mark("app_relaunch");
                        break;
                    default:
                        Mark("reader_wait");
                        break;
                }
                break;
            case "request_sent":
                if (Phase is "reader_wait" or "app_relaunch") Mark("reader_ready");
                if (Phase == "ssh_wait") Mark("ssh_ready");
                Mark("navigation_sent");
                break;
        }
    }

    /// <summary>What the card and the status line say while the open runs.</summary>
    internal string Label => Phase switch
    {
        "click" or "request_sent" => ProfileRunning ? "대화 여는 중" : "앱 시작 중",
        "launch_start" or "launch_ready" or "reader_wait" => "앱 시작 중",
        "app_relaunch" => "앱 다시 시작 중",
        "ssh_wait" => Host.Length > 0 ? "SSH 연결 중 · " + Host : "SSH 연결 중",
        _ => "대화 여는 중",
    };
    internal string Text => Elapsed >= 1000 ? $"{Label} · {Elapsed / 1000}초" : Label;

    /// <summary>The structured record of this click, once; null when already finished.</summary>
    internal object? Finish(string outcome, string? reason = null)
    {
        if (Finished) return null;
        if (outcome is "selected" or "recovered") Mark("confirmed");
        Outcome = outcome;
        return new
        {
            shortcut_id = ShortcutId, profile_id = ProfileId, host = Host.Length > 0 ? "ssh" : "local",
            profile_running = ProfileRunning, outcome, reason, total_ms = Elapsed,
            phases = phases.Select(p => new { phase = p.Phase, at_ms = p.At }).ToArray(),
        };
    }

    /// <summary>One log line: outcome, total and each phase's offset in seconds.</summary>
    internal string Summary(string? reason = null) =>
        $"작업 열기 기록 · {Outcome ?? "진행 중"}" + (string.IsNullOrEmpty(reason) ? "" : " · " + reason) +
        $" · {Elapsed / 1000.0:0.0}초 · " + string.Join(" → ", phases.Select(p => $"{p.Phase} {p.At / 1000.0:0.0}"));
}
