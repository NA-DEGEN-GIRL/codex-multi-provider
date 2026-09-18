using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// A startup replacement already owns launch admission. Selecting that account
// waits for its replacement window, without sending a competing launch request.
internal static class AutomaticProfileUpdate
{
    public static bool IsReplacing(JsonElement profile)
    {
        var job = profile.Get("restart");
        return job.S("automatic_key") != "" && job.S("phase") is
            "acquiring" or "closing" or "opening" or "releasing" or "recovering" or "connecting";
    }

    public static bool IsClosing(JsonElement profile) => IsReplacing(profile) &&
        profile.Get("restart").S("phase") is "acquiring" or "closing";
}
