using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// A profile can keep its ID while its process and window are replaced. Pin the
// show response until attached; an older state response must not select a window.
internal sealed record WindowLaunchIdentity(string ProfileId, string Generation, long ProcessId)
{
    public static WindowLaunchIdentity From(JsonElement profile) =>
        new(profile.S("id"), profile.S("generation"), profile.N("process_id"));

    public bool Matches(JsonElement profile) => ProfileId == profile.S("id") &&
        Generation == profile.S("generation") && ProcessId == profile.N("process_id");

    // A discovery poll can report another top-level HWND from the same Electron
    // process. A verified live child is authoritative until it actually closes.
    public bool Retains(NativeWindowHost host, JsonElement profile) => Matches(profile) && host.HasLiveAttachment;
}
