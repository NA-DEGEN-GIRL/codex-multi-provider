namespace Codex.ControlCenter.Shell;

// Retry short-lived Chromium startup races without repeated user clicks or an
// unbounded detach/reparent loop. A replacement window gets a new budget.
internal sealed class WindowAttachmentAttempts
{
    private readonly Dictionary<string, (string Window, int Count, DateTime Next)> entries = [];
    public bool Begin(string profile, string window, DateTime now, out int attempt)
    {
        var entry = entries.GetValueOrDefault(profile);
        if (entry.Window != window) entry = (window, 0, DateTime.MinValue);
        attempt = entry.Count;
        if (entry.Count >= 3 || now < entry.Next) return false;
        attempt = entry.Count + 1;
        entries[profile] = (window, attempt, now.AddSeconds(2));
        return true;
    }
    public void Reset(string profile) => entries.Remove(profile);
}
