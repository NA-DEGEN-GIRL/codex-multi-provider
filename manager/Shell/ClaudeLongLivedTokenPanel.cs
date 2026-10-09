using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Codex.ControlCenter.Shell;

/// <summary>
/// "SSH·위임 작업용 장기 토큰" in the Claude profile dialog. The token comes from
/// the password field or the clipboard, is sent once with claude.token.save and
/// is never shown, logged or kept: the field is cleared on submit, and the
/// clipboard is cleared when it still holds the saved token. Every other request
/// carries only the profile ID.
/// </summary>
internal sealed class ClaudeLongLivedTokenPanel
{
    internal const string ServiceUpdateMessage = "관리 서비스가 새 버전으로 바뀐 뒤 사용할 수 있습니다.";
    internal const string RevocationText =
        "해지: ‘장기 토큰 삭제’는 이 PC에서만 지웁니다. Claude 계정에서 토큰이 해지되지는 않으며 토큰은 만료일까지 유효합니다. " +
        "Anthropic 공식 문서에는 setup-token 해지 방법이 안내되어 있지 않습니다. 사용자 보고에 따르면 claude.ai 설정의 ‘Claude Code’ 항목에서 " +
        "발급된 토큰을 해지할 수 있으니, 유출이 의심되면 그 화면을 확인하세요. 이미 진행 중인 SSH 작업은 끝날 때까지 기존 토큰을 계속 사용합니다.";
    internal const string HelpText =
        "장기 토큰 발급 (Claude 계정마다 한 번)\n" +
        "1. 이 창의 ‘로그인 상태 확인’과 ‘계정 이메일 보기’로 이 프로필의 Claude 계정을 확인합니다.\n" +
        "2. 기본 브라우저의 claude.ai가 같은 계정으로 로그인되어 있는지 확인합니다. 여러 계정을 쓰면 다른 계정에서 로그아웃하거나, 브라우저가 자동으로 열리지 않을 때 표시되는 주소를 그 계정의 브라우저 창에 붙여넣습니다.\n" +
        "3. ‘발급 창 열기’를 누르면 인증 환경 변수를 지운 새 콘솔 창에서 claude setup-token이 실행됩니다. 이 프로필의 로그인 폴더는 쓰지 않으며, 관리 앱은 그 창의 내용을 읽지 않습니다. 직접 실행한다면 CLAUDE_CODE_OAUTH_TOKEN·ANTHROPIC_API_KEY가 설정되지 않은 새 PowerShell 창을 쓰세요.\n" +
        "4. 브라우저 승인 화면의 계정(이메일·조직)이 1번과 같은지 확인한 뒤 승인합니다. 다르면 승인하지 말고 계정을 바꿔 다시 실행합니다.\n" +
        "5. 터미널에 ‘Long-lived authentication token created successfully!’, 유효 기간(valid for …)과 sk-ant-oat… 토큰이 표시됩니다. 토큰 전체를 복사합니다. 여러 줄로 나뉘어 복사되어도 줄바꿈과 공백은 자동으로 제거됩니다. ‘발급 창 열기’로 연 창은 setup-token이 끝난 뒤에도 닫히지 않으니 서두르지 않아도 됩니다.\n" +
        "6. ‘클립보드에서 붙여넣기’를 누르거나 토큰 칸에 붙여넣고, 터미널에 표시된 유효 기간과 계정 확인란을 선택한 뒤 저장합니다. 채팅·에이전트·파일에는 붙여넣지 마세요.\n" +
        "7. 저장하면 클립보드에 남은 토큰을 지웁니다. 클립보드 기록(Win+V)이나 클라우드 클립보드 동기화를 쓰면 그 항목도 삭제하고, 발급 창은 직접 닫습니다(창 닫기 또는 exit 입력). 창을 닫기 전까지 토큰이 화면에 남아 있습니다.\n" +
        "8. 다른 Claude 프로필도 각자의 계정으로 따로 발급합니다.\n\n" +
        "토큰이 노출될 수 있는 곳에서는 발급하지 마세요.\n" +
        "• IDE나 에이전트가 읽을 수 있는 터미널 (예: Claude Code 데스크톱 앱의 터미널 패널)\n" +
        "• ‘이전 세션 복원’이 켜진 Windows Terminal: 화면 내용이 디스크에 저장될 수 있습니다. Windows 기본 터미널이 Windows Terminal이면 ‘발급 창 열기’도 그 앱에서 열립니다.\n" +
        "• PowerShell 기록(transcription)이 켜진 세션\n" +
        "• 화면 공유·녹화 중\n\n" +
        "토큰은 발급 화면에 표시된 기간(보통 1년) 동안 유효합니다. 만료 30일 전부터 알림이 표시되고, 만료 하루 전부터는 사용하지 않습니다. 같은 절차로 새로 발급해 ‘교체’하세요. " +
        "Claude 구독이 필요하며, 조직 정책이 금지하면 발급할 수 없습니다. 이때는 계속 이 PC의 로그인 토큰(약 8시간)을 빌려 씁니다. " +
        "토큰은 SSH 서버에서 실행하는 이 계정의 Claude 작업에만 쓰이며 사용량은 같은 구독 한도에서 차감됩니다.\n\n" + RevocationText;

    // S1c: the SSH host hardening narrows, but does not close, every way a same-user process
    // can reach a running turn's token, so the note keeps saying that it can.
    internal const string SecurityNote =
        "토큰은 이 Windows 사용자 전용(DPAPI)으로 암호화해 이 PC에만 저장합니다. 다른 Windows 사용자와 디스크 복사본으로부터는 보호되지만, 이 Windows 사용자로 실행되는 프로그램으로부터는 보호되지 않습니다. " +
        "SSH 서버에서는 토큰을 파일이나 환경 변수에 남기지 않고 실행 중인 Claude에 소켓으로만 전달합니다. " +
        "그래도 SSH 서버의 같은 사용자 계정으로 실행되는 프로세스(에이전트 포함)는 작업 중 이 토큰을 읽을 수 있습니다.";
    internal const string LoginHint = "이 프로필의 Claude 로그인을 먼저 확인하세요 (로그인 상태 확인).";
    private static readonly Brush Muted = new SolidColorBrush(Color.FromRgb(166, 176, 192));
    private static readonly Brush Warning = Brushes.Orange;
    private readonly Window _owner;
    private readonly string _profileId;
    private readonly Func<string, object, Task<JsonElement>> _request;
    private JsonElement _presentation;
    private bool _loggedIn;
    private bool _busy;
    // The login the attestation checkbox names: the masked account ("" when none is shown).
    private string _account = "";
    private bool? _accountLoggedIn;

    internal readonly Expander Section;
    internal readonly TextBlock Status, Ssh, Format, Result, Email, Hint;
    internal readonly PasswordBox Token;
    internal readonly CheckBox Attested;
    internal readonly ComboBox Validity;
    internal readonly TextBox ValidityDays;
    internal readonly DatePicker Minted;
    internal readonly Button Paste, Save, Retry, Remove, Issue, ShowEmail;
    // Replaceable by the self-test; the real ones use the Windows clipboard.
    internal Func<string?> ReadClipboard = () =>
    {
        try { return Clipboard.ContainsText() ? Clipboard.GetText() : null; }
        catch (ExternalException) { return null; }
    };
    internal Func<string, bool> ClearClipboardIfHolds = token =>
    {
        try
        {
            if (!Clipboard.ContainsText() || ClaudeProfilePresentation.NormalizeToken(Clipboard.GetText()) != token) return false;
            Clipboard.Clear();
            return true;
        }
        catch (ExternalException) { return false; }
    };
    internal Func<string, bool> Confirm;

    internal bool HasUnsavedToken => Token.Password.Length > 0;

    internal ClaudeLongLivedTokenPanel(Window owner, Panel body, JsonElement profile, Func<string, object, Task<JsonElement>> request)
    {
        _owner = owner; _profileId = profile.S("id"); _request = request;
        Confirm = message => MessageBox.Show(_owner, message, "장기 토큰 삭제", MessageBoxButton.YesNo, MessageBoxImage.Warning) == MessageBoxResult.Yes;
        var content = new StackPanel();
        Section = new Expander { Content = content, Foreground = Brushes.LightGray, Margin = new Thickness(0, 8, 0, 14) };
        body.Children.Add(Section);
        content.Children.Add(Note("SSH 서버에서 실행하는 이 계정의 Claude 작업(위임 작업 포함)에 1년짜리 토큰을 사용합니다. 저장하지 않으면 지금처럼 이 PC의 Claude 로그인 토큰(약 8시간)을 빌려 씁니다. 이 PC에서 실행하는 Claude 작업에는 영향이 없습니다."));
        content.Children.Add(Note("장기 토큰도 이 PC의 Claude 로그인이 같은 계정으로 확인된 동안에만 사용합니다. 로그아웃하거나 다시 로그인하는 동안에는 SSH 작업에 쓰이지 않습니다."));
        Status = Note(""); Status.FontWeight = FontWeights.SemiBold; content.Children.Add(Status);
        Ssh = Note(""); content.Children.Add(Ssh);
        content.Children.Add(Note(SecurityNote));

        content.Children.Add(new TextBlock { Text = "장기 토큰 (화면에 표시하지 않습니다)" });
        var tokenRow = new DockPanel { LastChildFill = true };
        Paste = new Button { Content = "클립보드에서 붙여넣기", Margin = new Thickness(6, 0, 0, 0) };
        DockPanel.SetDock(Paste, Dock.Right); tokenRow.Children.Add(Paste);
        Token = new PasswordBox { MaxLength = ClaudeProfilePresentation.TokenMaxLength };
        tokenRow.Children.Add(Token); content.Children.Add(tokenRow);
        Format = Note(""); content.Children.Add(Format);
        // A single-line field would keep only the first line of a wrapped copy.
        DataObject.AddPastingHandler(Token, (_, e) =>
        {
            e.CancelCommand();
            ApplyPaste(e.DataObject.GetDataPresent(DataFormats.UnicodeText) ? e.DataObject.GetData(DataFormats.UnicodeText) as string : null);
        });
        Token.PasswordChanged += (_, _) => UpdateFormat();
        Paste.Click += (_, _) => ApplyPaste(ReadClipboard());

        content.Children.Add(new TextBlock { Text = "유효 기간 (발급 화면의 ‘valid for …’)" });
        Validity = new ComboBox();
        Validity.Items.Add(new Choice("365", "1년 · 기본"));
        Validity.Items.Add(new Choice("custom", "직접 입력 · 표시된 일수"));
        Validity.SelectedIndex = 0; content.Children.Add(Validity);
        ValidityDays = new TextBox { Text = "365", IsEnabled = false, MaxLength = 3 }; content.Children.Add(ValidityDays);
        Validity.SelectionChanged += (_, _) => { ValidityDays.IsEnabled = ((Choice)Validity.SelectedItem).Id == "custom"; UpdateButtons(); };
        ValidityDays.TextChanged += (_, _) => UpdateButtons();
        content.Children.Add(new TextBlock { Text = "발급일 · 오늘 발급했다면 그대로 두세요", Margin = new Thickness(0, 8, 0, 0) });
        Minted = new DatePicker { SelectedDate = DateTime.Today, DisplayDateEnd = DateTime.Today }; content.Children.Add(Minted);

        Attested = new CheckBox { Margin = new Thickness(0, 10, 0, 4) }; content.Children.Add(Attested);
        Attested.Checked += (_, _) => UpdateButtons(); Attested.Unchecked += (_, _) => UpdateButtons();
        // A new token needs its own attestation: the box confirms who issued this token.
        Token.PasswordChanged += (_, _) => Attested.IsChecked = false;
        var emailRow = new StackPanel { Orientation = Orientation.Horizontal };
        ShowEmail = new Button { Content = "계정 이메일 보기" }; emailRow.Children.Add(ShowEmail);
        Email = Note(""); Email.Margin = new Thickness(8, 0, 0, 0); Email.VerticalAlignment = VerticalAlignment.Center; emailRow.Children.Add(Email);
        content.Children.Add(emailRow);
        ShowEmail.Click += async (_, _) => await Run(ShowEmail, ShowEmailAsync);

        var actions = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 10, 0, 0) };
        Save = new Button { Content = "장기 토큰 저장" }; actions.Children.Add(Save);
        Retry = new Button { Content = "다시 시도", Visibility = Visibility.Collapsed, ToolTip = "거부 기록을 지우고 다음 SSH 작업부터 이 토큰을 다시 사용합니다." }; actions.Children.Add(Retry);
        Remove = new Button { Content = "장기 토큰 삭제", Visibility = Visibility.Collapsed }; actions.Children.Add(Remove);
        content.Children.Add(actions);
        // WPF shows no tooltip on a disabled button unless asked; the hint line says it anyway (U4).
        ToolTipService.SetShowOnDisabled(Save, true);
        Hint = Note(""); Hint.Foreground = Warning; Hint.Margin = new Thickness(0, 4, 0, 0); content.Children.Add(Hint);
        Save.Click += async (_, _) => await Run(Save, SaveAsync);
        Retry.Click += async (_, _) => await Run(Retry, RetryAsync);
        Remove.Click += async (_, _) => await Run(Remove, RemoveAsync);
        Issue = new Button { Content = "발급 창 열기", ToolTip = "인증 환경 변수를 지운 새 콘솔 창에서 claude setup-token을 실행합니다. 관리 앱은 그 창을 읽지 않습니다." };
        content.Children.Add(Issue);
        Issue.Click += async (_, _) => await Run(Issue, IssueAsync);
        Result = Note(""); content.Children.Add(Result);
        content.Children.Add(new Expander
        {
            Header = "setup-token 발급 방법", Foreground = Brushes.LightGray, Margin = new Thickness(0, 4, 0, 0),
            Content = new TextBlock { Text = HelpText, TextWrapping = TextWrapping.Wrap, Foreground = Muted, Margin = new Thickness(0, 6, 0, 0) }
        });

        UpdateLogin(ClaudeProfilePresentation.Status(profile));
        Apply(ClaudeProfilePresentation.LongLived(profile));
        // Collapsed when nothing is saved and no SSH binding or preset runs this account (U8).
        Section.IsExpanded = _presentation.ValueKind == JsonValueKind.Object && _presentation.B("expanded");
    }

    private static TextBlock Note(string text) => new() { Text = text, TextWrapping = TextWrapping.Wrap, Foreground = Muted, Margin = new Thickness(0, 0, 0, 10) };

    /// <summary>Called with the CLI login status shown by this dialog.</summary>
    internal void UpdateLogin(JsonElement status)
    {
        var loggedIn = status.B("logged_in");
        var masked = status.S("masked_email");
        // The attestation names one account: another login, or none, makes it stale.
        if (_accountLoggedIn is { } before && (before != loggedIn || _account != masked)) Attested.IsChecked = false;
        _loggedIn = loggedIn; _accountLoggedIn = loggedIn; _account = masked;
        Attested.Content = masked.Length > 0
            ? $"이 토큰은 {masked} 계정으로 발급했습니다"
            : "이 토큰은 이 프로필과 같은 Claude 계정으로 로그인한 브라우저에서 발급했습니다";
        UpdateButtons();
    }

    internal void Apply(JsonElement presentation)
    {
        _presentation = presentation;
        if (presentation.ValueKind != JsonValueKind.Object)
        {
            Status.Text = "장기 토큰 상태를 확인하지 못했습니다 · " + ServiceUpdateMessage;
            Section.Header = "SSH·위임 작업용 장기 토큰";
            Ssh.Text = ""; UpdateButtons(); return;
        }
        var saved = presentation.B("saved");
        Status.Text = presentation.S("label", "장기 토큰 · 없음");
        Status.Foreground = presentation.S("tone") == "warning" ? Warning : presentation.S("tone") == "ready" ? Brushes.LightGreen : Muted;
        Ssh.Text = presentation.Get("ssh").S("text");
        Section.Header = "SSH·위임 작업용 장기 토큰 · " + (saved ? presentation.S("label").Split(" · ")[0] : "없음");
        Remove.Visibility = saved ? Visibility.Visible : Visibility.Collapsed;
        Retry.Visibility = presentation.B("retry_available") ? Visibility.Visible : Visibility.Collapsed;
        UpdateButtons();
    }

    internal void ApplyPaste(string? text)
    {
        var token = ClaudeProfilePresentation.NormalizeToken(text);
        text = null;
        if (token.Length > Token.MaxLength)
        {
            Token.Clear();
            Format.Text = ClaudeProfilePresentation.TokenLengthMessage; Format.Foreground = Warning;
            return;
        }
        Token.Password = token;
        UpdateFormat();
    }

    private void UpdateFormat()
    {
        var length = Token.Password.Length;
        var error = length == 0 ? null : ClaudeProfilePresentation.TokenFormatError(Token.Password);
        Format.Text = length == 0 ? "" : error ?? $"형식 확인됨 · {length}자";
        Format.Foreground = error is null ? Muted : Warning;
        UpdateButtons();
    }

    private bool TryValidity(out int days)
    {
        days = 365;
        if (Validity.SelectedItem is Choice { Id: "365" }) return true;
        return int.TryParse(ValidityDays.Text.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out days) && days is >= 1 and <= 366;
    }

    private void UpdateButtons()
    {
        var formatOk = Token.Password.Length > 0 && ClaudeProfilePresentation.TokenFormatError(Token.Password) is null;
        // Disabled until the profile is logged in, so a token is never pasted only to be refused (U4).
        Save.IsEnabled = !_busy && _loggedIn && formatOk && Attested.IsChecked == true && TryValidity(out _)
            && _presentation.ValueKind == JsonValueKind.Object;
        Save.ToolTip = _loggedIn ? "토큰 형식과 계정 확인란, 유효 기간을 확인하면 저장할 수 있습니다."
            : "이 프로필의 Claude 로그인이 확인되어야 저장할 수 있습니다. ‘로그인 상태 확인’을 눌러 주세요.";
        Hint.Text = _loggedIn ? "" : LoginHint;
        Hint.Visibility = _loggedIn ? Visibility.Collapsed : Visibility.Visible;
        // From the saved state, also after a click restored the button's own label.
        if (!_busy) Save.Content = _presentation.ValueKind == JsonValueKind.Object && _presentation.B("saved") ? "교체" : "장기 토큰 저장";
    }

    private async Task Run(Button button, Func<Task> action)
    {
        if (_busy) return;
        _busy = true; var label = button.Content; button.IsEnabled = false; button.Content = label + " · 처리 중…";
        try { await action(); }
        catch (Codex.ControlCenter.Shared.ManagerException error) when (error.Code == "unknown_command") { Show(ServiceUpdateMessage, true); }
        catch (Exception error) { Show(error.Message, true); }
        finally { _busy = false; button.Content = label; button.IsEnabled = true; UpdateButtons(); }
    }

    private void Show(string message, bool warning = false)
    {
        Result.Text = message; Result.Foreground = warning ? Warning : Muted;
    }

    internal async Task SaveAsync()
    {
        // Read once, clear the field at once; the reference is dropped after the request.
        // Clearing the field also clears the attestation, so a failed save is attested again.
        var attested = Attested.IsChecked == true;
        var account = _account;
        var token = ClaudeProfilePresentation.NormalizeToken(Token.Password);
        Token.Clear();
        try
        {
            if (ClaudeProfilePresentation.TokenFormatError(token) is { } error) { Show(error, true); return; }
            if (!attested) { Show("토큰을 발급한 계정이 이 프로필의 계정인지 확인하고 확인란을 선택하세요.", true); return; }
            if (!TryValidity(out var days)) { Show("유효 기간은 발급 화면에 표시된 대로 1~366일로 입력하세요.", true); return; }
            // The manager refuses the save when the login checked then is not this attested account.
            var args = new Dictionary<string, object?> { ["profile_id"] = _profileId, ["token"] = token, ["attested"] = true,
                ["attested_account"] = account, ["validity_days"] = days };
            if (Minted.SelectedDate is { } minted && minted.Date != DateTime.Today)
                args["minted_on"] = minted.Date.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            JsonElement result;
            try { result = await _request("claude.token.save", args); }
            finally { args["token"] = null; }
            var cleared = ClearClipboardIfHolds(token);
            Attested.IsChecked = false;
            Apply(result.Get("long_lived"));
            Show(result.Message("장기 토큰을 저장했습니다.") + (cleared
                ? " 클립보드에 남은 토큰을 지웠습니다. 클립보드 기록(Win+V)이나 클라우드 클립보드 동기화를 쓰면 그 항목도 삭제하세요."
                : " 클립보드 기록(Win+V)에 토큰이 남아 있다면 삭제하세요."));
        }
        finally { token = null; }
    }

    internal async Task RemoveAsync()
    {
        if (!Confirm("이 PC에서만 장기 토큰을 삭제합니다.\n\n" + RevocationText + "\n\n삭제할까요?")) return;
        var result = await _request("claude.token.remove", new { profile_id = _profileId });
        Apply(result.Get("long_lived"));
        Show(result.Message("장기 토큰을 삭제했습니다."));
    }

    internal async Task RetryAsync()
    {
        var result = await _request("claude.token.retry", new { profile_id = _profileId });
        Apply(result.Get("long_lived"));
        Show(result.Message("거부 기록을 지웠습니다."));
    }

    internal async Task IssueAsync()
    {
        var result = await _request("claude.token.issue", new { profile_id = _profileId });
        Show(result.Message("새 콘솔 창에서 claude setup-token을 시작했습니다."));
    }

    private async Task ShowEmailAsync()
    {
        // Shown only in this window, never kept or rendered on cards.
        var result = await _request("profile.email", new { profile_id = _profileId, reveal = true });
        var email = result.S("email");
        Email.Text = result.S("profile_id") == _profileId && result.S("state") == "available"
            && email.Length is > 0 and <= 320 && email.Count(c => c == '@') == 1
            && !email.Any(c => char.IsControl(c) || char.IsWhiteSpace(c)) ? "계정 · " + email : "이메일 확인 안 됨";
    }

    /// <summary>U4: an unsaved token is never dropped silently when the dialog closes.</summary>
    internal bool ConfirmDiscard()
    {
        if (!HasUnsavedToken) return true;
        if (MessageBox.Show(_owner, "장기 토큰 칸에 저장하지 않은 토큰이 있습니다. 저장하지 않고 계속할까요?\n계속하면 입력한 토큰은 지워집니다.",
                "장기 토큰 저장 안 됨", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return false;
        Token.Clear();
        return true;
    }
}
