using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// Per profile card, for the selected task: whether its prompt cache is likely
// still warm on that profile's account and provider, minutes left and the rough
// cost of the first request there. scripts/manager_core/cache_warmth.py computes
// every value (state "cache_warmth"); this file only finds the entry. The ledger
// never stores thread ids, so the lookup key is the same salted digest as
// serve_ledger.thread_hash.
internal static class ProfileCacheLine
{
    internal static string ThreadHash(string threadId)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes("codex-manager-thread-v1\0" + threadId.Trim().ToLowerInvariant()));
        return Convert.ToHexString(bytes, 0, 12).ToLowerInvariant();
    }

    internal static JsonElement Entry(JsonElement state, SelectedTask? task, string? profileId)
    {
        if (task is null || string.IsNullOrEmpty(profileId)) return default;
        return state.Get("cache_warmth").Get("threads").Get(ThreadHash(task.Task.ThreadId)).Get("profiles").Get(profileId);
    }

    internal static Choice Apply(Choice choice, JsonElement state, SelectedTask? task)
    {
        if (ClaudeProfilePresentation.IsClaude(choice.Data)) return choice with { ProfileCache = "", ProfileCacheTone = "" };
        var entry = Entry(state, task, choice.Id);
        return choice with { ProfileCache = entry.S("line"), ProfileCacheTone = entry.S("tone") };
    }

    // One status-line notice per (task, cold profile, warm profile) and at most
    // every 30 minutes, so the 4 s state poll never repeats it.
    internal static string? Notice(JsonElement state, SelectedTask? task, string? profileId,
        Dictionary<string, DateTime> shown, DateTime now)
    {
        if (ClaudeProfilePresentation.IsClaude(state.Arr("profiles").FirstOrDefault(profile => profile.S("id") == profileId))) return null;
        var entry = Entry(state, task, profileId);
        var notice = entry.S("notice");
        if (notice.Length == 0 || task is null) return null;
        var key = task.Task.Key + "/" + profileId + "/" + entry.S("warm_profile");
        if (shown.TryGetValue(key, out var at) && now - at < TimeSpan.FromMinutes(30)) return null;
        shown[key] = now;
        if (shown.Count > 256) shown.Remove(shown.MinBy(item => item.Value).Key);
        return notice;
    }
}
