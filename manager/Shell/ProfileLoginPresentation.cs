using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class ProfileLoginPresentation
{
    internal static bool RequiresRestartForLogin(JsonElement profile, JsonElement login) =>
        profile.S("status") == "running" && profile.S("runtime_channel") != "packaged" &&
        !profile.B("native_login_pending") && login.S("state") is not ("credential_saved" or "signed_in");
    public static bool NeedsLogin(JsonElement profile) => profile.Get("login_health").B("blocks_launch");
    public static bool ShowRecovery(JsonElement profile) => NeedsLogin(profile) && profile.S("status") != "running";
    public static string Message(JsonElement profile) => "프로필 " + profile.S("alias") + " · 로그인 확인 필요\n\n" +
        profile.Get("login_health").Message("이 프로필에 다시 로그인하세요.") +
        "\n\n위의 ‘이 프로필에 로그인’을 누르세요. 다른 프로필은 왼쪽에서 계속 열 수 있습니다.";
}
