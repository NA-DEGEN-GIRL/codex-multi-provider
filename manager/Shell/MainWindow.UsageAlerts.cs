using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// Revision 124: account usage alerts and automatic-continuation log lines that the backend
// adds to each state poll. The backend already deduplicates per window and reset period;
// this window only shows each alert once, and never alerts that predate its own start.
public sealed partial class MainWindow
{
    private readonly HashSet<string> _deliveredUsageAlerts = [];
    private readonly HashSet<string> _loggedContinuations = [];
    private readonly double _usageAlertsSince = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000d - 60;

    internal static double UsageAlertAt(JsonElement value)
        => value.Get("at") is { ValueKind: JsonValueKind.Number } at && at.TryGetDouble(out var seconds) ? seconds : 0;

    // A stopped local task can be opened from its toast; SSH routes are owned by the desktop.
    internal static NotificationTarget? UsageAlertTarget(JsonElement alert)
        => alert.S("host_id") == "local" && Guid.TryParse(alert.S("thread_id"), out _) && Guid.TryParse(alert.S("profile_id"), out _)
            ? new NotificationTarget(alert.S("profile_id"), alert.S("thread_id"), "local", alert.S("title"), 0) : null;

    private void DeliverUsageAlerts()
    {
        foreach (var alert in _state.Arr("usage_alerts"))
        {
            var id = alert.S("id");
            if (id.Length == 0 || UsageAlertAt(alert) < _usageAlertsSince || !_deliveredUsageAlerts.Add(id)) continue;
            var alias = alert.S("alias", "프로필");
            Log($"사용량 알림 · {alias} · {alert.S("title")} · {alert.S("body")}");
            if (_workspaceNotifications is { } notifications)
                _ = notifications.ShowAccountAlertAsync(alias, alert.S("title"), alert.S("body"), id, UsageAlertTarget(alert));
        }
        foreach (var item in _state.Arr("usage_continuation_events"))
        {
            var id = item.S("id");
            if (id.Length == 0 || UsageAlertAt(item) < _usageAlertsSince || !_loggedContinuations.Add(id)) continue;
            var profile = item.S("profile_id");
            Log($"자동 이어하기 · {(ProfileExists(profile) ? ProfileAlias(profile) : "제거된 프로필")} · {item.S("message")}");
        }
        // The backend keeps a short recent list; an id it no longer lists never comes back. An
        // empty list (also a failed evaluation) keeps the delivered ids.
        if (_state.Arr("usage_alerts").Any())
            _deliveredUsageAlerts.IntersectWith(_state.Arr("usage_alerts").Select(alert => alert.S("id")));
        if (_state.Arr("usage_continuation_events").Any())
            _loggedContinuations.IntersectWith(_state.Arr("usage_continuation_events").Select(item => item.S("id")));
    }
}
