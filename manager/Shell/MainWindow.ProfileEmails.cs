using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

public sealed partial class MainWindow
{
    // Window-local opt-in only. Never save emails or the reveal preference.
    private readonly Button _profileEmailToggle = new() { Name = "ProfileEmailToggle", FontSize = 11,
        Width = 82, Height = 30, Padding = new Thickness(0), Margin = new Thickness(0),
        Background = Brushes.Transparent, BorderThickness = new Thickness(0) };
    private readonly Dictionary<string, (string Binding, string Text)> _profileEmails = [];
    private readonly SemaphoreSlim _profileEmailSlots = new(2);
    private bool _profileEmailsVisible;
    private int _profileEmailEpoch;

    private Button ProfileEmailButton()
    {
        UpdateProfileEmailButton();
        // Keep hiding available even while a slow CLI status request is running.
        _profileEmailToggle.Click += async (_, _) => await Safe(ToggleProfileEmailsAsync);
        return _profileEmailToggle;
    }

    private void UpdateProfileEmailButton()
    {
        var label = _profileEmailsVisible ? "이메일 숨기기" : "이메일 표시";
        _profileEmailToggle.Content = label;
        _profileEmailToggle.ToolTip = _profileEmailsVisible ? "모든 계정 이메일 숨기기" : "계정 이메일 표시 · 앱을 다시 열면 숨겨집니다";
        AutomationProperties.SetName(_profileEmailToggle, label);
    }

    private static bool HasProfileEmail(JsonElement profile) => profile.ValueKind == JsonValueKind.Object
        && profile.S("auth_mode") != "external" && !profile.B("view_only") && profile.S("removed_at") == "";

    private static string ProfileEmailBinding(JsonElement profile) => JsonSerializer.Serialize(new[]
    {
        profile.S("id"), profile.S("auth_mode"), profile.S("home"), profile.S("source_home"),
        profile.S("account_fingerprint"), profile.S("claude_account_identity"), profile.S("login_state"),
        profile.Get("claude_status").S("state"), profile.Get("claude_status").B("logged_in").ToString()
    });

    private string ProfileEmailText(JsonElement profile)
    {
        if (!_profileEmailsVisible || !HasProfileEmail(profile)) return "";
        return _profileEmails.TryGetValue(profile.S("id"), out var entry) && entry.Binding == ProfileEmailBinding(profile)
            ? entry.Text : "이메일 미확인";
    }

    private void RefreshProfileEmails()
    {
        var rendering = _rendering;
        _rendering = true;
        try { Fill(_profiles, _profiles.Items.OfType<Choice>().Select(choice =>
        {
            var profile = _state.Arr("profiles").FirstOrDefault(item => item.S("id") == choice.Id);
            return choice with { Data = profile, ProfileEmail = ProfileEmailText(profile) };
        }), _selectedProfile); }
        finally { _rendering = rendering; }
    }

    private async Task ToggleProfileEmailsAsync()
    {
        _profileEmailsVisible = !_profileEmailsVisible;
        var epoch = ++_profileEmailEpoch;
        _profileEmails.Clear();
        UpdateProfileEmailButton();
        var profiles = _profileEmailsVisible ? _state.Arr("profiles").Where(HasProfileEmail).ToArray() : [];
        foreach (var profile in profiles)
            _profileEmails[profile.S("id")] = (ProfileEmailBinding(profile), "이메일 확인 중…");
        RefreshProfileEmails();
        await Task.WhenAll(profiles.Select(profile => ReadProfileEmailAsync(profile, epoch)));
    }

    private async Task ReadProfileEmailAsync(JsonElement profile, int epoch)
    {
        await _profileEmailSlots.WaitAsync();
        try
        {
            if (_closing || !_profileEmailsVisible || epoch != _profileEmailEpoch) return;
            var label = "이메일 확인 안 됨";
            try
            {
                var result = await Request("profile.email", new { profile_id = profile.S("id"), reveal = true });
                var email = result.S("email");
                if (result.S("profile_id") == profile.S("id") && result.S("state") == "available"
                    && email.Length is > 0 and <= 320 && email.Count(c => c == '@') == 1
                    && !email.Any(c => char.IsControl(c) || char.IsWhiteSpace(c))) label = email;
            }
            catch (Codex.ControlCenter.Shared.ManagerException error) when (error.Code == "unknown_command")
            { label = "관리 서비스 업데이트 필요"; }
            catch (Exception) { /* Request logs fixed diagnostics; never render raw provider data here. */ }
            if (_closing || !_profileEmailsVisible || epoch != _profileEmailEpoch) return;
            _profileEmails[profile.S("id")] = (ProfileEmailBinding(profile), label);
            RefreshProfileEmails();
        }
        finally { _profileEmailSlots.Release(); }
    }
}
