using System.IO;
using System.Windows;

namespace Codex.ControlCenter.Shell;

public partial class App : Application
{
    private Mutex? workspaceInstance;
    private System.Diagnostics.ProcessStartInfo? administratorRestart;

    internal void RestartAdministratorAfterExit(string root)
    {
        var authority = Codex.ControlCenter.Shared.WindowsExecutionIdentity.Current();
        if (!authority.Known) throw new InvalidOperationException("현재 Windows 계정을 확인하지 못했습니다.");
        var current = Environment.ProcessPath
            ?? throw new InvalidOperationException("관리 앱 실행 경로를 확인하지 못했습니다.");
        var executable = InstalledManagerRelease.Replacement(root, current) ?? current;
        administratorRestart = WorkspaceExecutionMode.ElevatedStart(executable, root, authority.UserSid!, null);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // The new shell must not forward to the window that has just closed.
        if (workspaceInstance is not null)
        {
            try { workspaceInstance.ReleaseMutex(); } catch (ApplicationException) { }
            workspaceInstance.Dispose(); workspaceInstance = null;
        }
        base.OnExit(e);
        if (administratorRestart is not { } start) return;
        try { using var process = System.Diagnostics.Process.Start(start) ?? throw new InvalidOperationException("관리 앱 프로세스를 시작하지 못했습니다."); }
        catch (System.ComponentModel.Win32Exception error) when (error.NativeErrorCode == 1223)
        { MessageBox.Show("관리자 실행이 취소되었습니다. Open-Control-Center-Admin.cmd로 다시 실행할 수 있습니다.", "관리자 실행"); }
        catch (Exception error) { MessageBox.Show("관리자 실행을 시작하지 못했습니다. " + error.Message, "관리자 실행"); }
    }
    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        if (e.Args.Contains("--write-shell-compatibility"))
        {
            // Build-time metadata comes from the actual compiled shell. No
            // workspace/service connection or UI is created on this path.
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            try
            {
                using var output = new FileStream(Path.Combine(AppContext.BaseDirectory, "shell-compatibility.json"), FileMode.CreateNew);
                System.Text.Json.JsonSerializer.Serialize(output, new
                {
                    version = 1, revision = WorkspaceBuild.Revision,
                    service_protocol = Codex.ControlCenter.Shared.ManagerProtocol.Version
                });
                Shutdown(0);
            }
            catch { Shutdown(1); }
            return;
        }
        if (e.Args.Contains("--remote-updates-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var report = Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required.");
            try { await RemoteUpdatesSelfTest.RunAsync(report); Shutdown(0); }
            catch (Exception error) { File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { passed = false, error = error.ToString() })); Shutdown(1); }
            return;
        }
        if (e.Args.Contains("--personal-skills-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var report = Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required.");
            try { await PersonalSkillsSelfTest.RunAsync(report); Shutdown(0); }
            catch (Exception error) { File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { passed = false, error = error.ToString() })); Shutdown(1); }
            return;
        }
        if (e.Args.Contains("--profile-order-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var report = Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required.");
            try { await ProfileOrderingSelfTest.RunAsync(report); Shutdown(0); }
            catch (Exception error) { File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { passed = false, error = error.ToString() })); Shutdown(1); }
            return;
        }
        if (e.Args.Contains("--notification-activation-fixture"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var fixtureRoot = Argument(e.Args, "--root") ?? "";
            var fixtureTicket = WorkspaceActivation.Ticket(fixtureRoot, Argument(e.Args, "--workspace-notification"));
            Shutdown(fixtureTicket is not null && await WorkspaceActivation.ForwardAsync(fixtureRoot, fixtureTicket) ? 0 : 1);
            return;
        }
        if (e.Args.Contains("--recover-viewport"))
        {
            Shutdown(NativeViewportRecovery.Run(Argument(e.Args, "--recover-viewport") ?? ""));
            return;
        }
        if (e.Args.Contains("--notes-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var report = Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required.");
            try { await NotesSelfTest.RunAsync(report); Shutdown(0); }
            catch (Exception error) { File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { passed = false, error = error.ToString() })); Shutdown(1); }
            return;
        }
        if (e.Args.Contains("--notification-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var report = Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required.");
            try { await NotificationActivationSelfTest.RunAsync(report); Shutdown(0); }
            catch (Exception error)
            {
                File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { passed = false, error = error.ToString() }));
                Shutdown(1);
            }
            return;
        }
        if (e.Args.Contains("--responsiveness-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var report = Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required.");
            try { await ResponsivenessSelfTest.RunAsync(report, Dispatcher); Shutdown(0); }
            catch (Exception error) { File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { passed = false, error = error.ToString() })); Shutdown(1); }
            return;
        }
        if (e.Args.Contains("--window-controls-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            var report = Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required.");
            try { WindowControlsSelfTest.Run(Argument(e.Args, "--root") ?? FindRoot(), report); Shutdown(0); }
            catch (Exception error)
            {
                File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { passed = false, error = error.ToString() }));
                Shutdown(1);
            }
            return;
        }
        if (e.Args.Contains("--layout-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            try
            {
                await LayoutSelfTest.RunAsync(Argument(e.Args, "--root") ?? FindRoot(), Argument(e.Args, "--report") ?? throw new ArgumentException("--report is required."));
                Shutdown(0);
            }
            catch (Exception ex)
            {
                var report = Argument(e.Args, "--report");
                if (report is not null) File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { ok = false, error = ex.Message }));
                Shutdown(1);
            }
            return;
        }
        if (e.Args.Contains("--native-host-fixture"))
        {
            Shutdown(NativeHostSelfTest.RunFixture());
            return;
        }
        if (e.Args.Contains("--native-host-self-test"))
        {
            ShutdownMode = ShutdownMode.OnExplicitShutdown;
            try
            {
                var result = await NativeHostSelfTest.RunAsync();
                var report = Argument(e.Args, "--report");
                if (report is not null) File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(result, new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
                Shutdown(result.Passed ? 0 : 1);
            }
            catch (Exception ex)
            {
                var report = Argument(e.Args, "--report");
                if (report is not null) File.WriteAllText(report, System.Text.Json.JsonSerializer.Serialize(new { ok = false, error = ex.Message }));
                Shutdown(1);
            }
            return;
        }
        try
        {
            var root = Argument(e.Args, "--root") ?? FindRoot();
            var authority = Codex.ControlCenter.Shared.WindowsExecutionIdentity.Current();
            if (!authority.Known) throw new InvalidOperationException("현재 Windows 실행 권한을 확인하지 못했습니다. 같은 계정으로 작업공간을 다시 열어 주세요.");
            WorkspaceExecutionMode.RequireSameUser(Argument(e.Args, "--expected-user-sid"), authority.UserSid!);
            string? notificationUri = Argument(e.Args, "--workspace-notification");
            string? ticket = notificationUri is null ? null : WorkspaceActivation.Ticket(root, notificationUri)
                ?? throw new ArgumentException("올바른 작업공간 알림 주소가 아닙니다.");
            // Forward before the release redirect: a toast may refer to an
            // older installed build while the new manager is already running.
            workspaceInstance = new Mutex(true, @"Local\" + WorkspaceActivation.Scheme(root), out bool first);
            if (!first)
            {
                if (!await WorkspaceActivation.ForwardAsync(root, ticket ?? ""))
                    throw new InvalidOperationException("작업공간이 시작 중입니다. 잠시 후 알림을 다시 눌러 주세요.");
                if (e.Args.Contains("--require-administrator"))
                    MessageBox.Show("이미 열린 작업공간으로 이동했습니다.\n관리자 권한으로 바꾸려면 작업을 마친 뒤 ‘설정 및 관리 → 완전 종료 후 관리자 실행…’을 누르세요.", "관리자 실행", MessageBoxButton.OK, MessageBoxImage.Information);
                Shutdown(0); return;
            }
            bool administratorPending = false;
            if (WorkspaceExecutionMode.RequiresElevation(WorkspaceExecutionMode.ReadAdministrator(root),
                    e.Args.Contains("--require-administrator"), authority.Elevated == true))
            {
                var existing = await Codex.ControlCenter.Shared.ManagerClient.ProbeExistingAuthorityAsync(root);
                administratorPending = WorkspaceExecutionMode.DeferElevation(authority, existing);
                if (!administratorPending)
                {
                    var executable = Environment.ProcessPath ?? throw new InvalidOperationException("관리 앱 실행 경로를 확인하지 못했습니다.");
                    workspaceInstance.ReleaseMutex(); workspaceInstance.Dispose(); workspaceInstance = null;
                    try
                    {
                        using var elevated = System.Diagnostics.Process.Start(
                            WorkspaceExecutionMode.ElevatedStart(executable, root, authority.UserSid!, notificationUri))
                            ?? throw new InvalidOperationException("관리자 실행을 시작하지 못했습니다.");
                        Shutdown(0);
                    }
                    catch (System.ComponentModel.Win32Exception error) when (error.NativeErrorCode == 1223)
                    {
                        MessageBox.Show("관리자 실행이 취소되었습니다. 진행 중인 작업은 그대로 유지됩니다.", "관리자 실행", MessageBoxButton.OK, MessageBoxImage.Information);
                        Shutdown(1);
                    }
                    return;
                }
            }
            var replacement = InstalledManagerRelease.Replacement(root, Environment.ProcessPath
                ?? throw new InvalidOperationException("현재 관리 앱 실행 경로를 확인하지 못했습니다."));
            if (replacement is not null)
            {
                var launch = new System.Diagnostics.ProcessStartInfo(replacement) { UseShellExecute = false };
                launch.ArgumentList.Add("--root");
                launch.ArgumentList.Add(root);
                launch.ArgumentList.Add("--expected-user-sid"); launch.ArgumentList.Add(authority.UserSid!);
                if (e.Args.Contains("--require-administrator")) launch.ArgumentList.Add("--require-administrator");
                if (notificationUri is not null) { launch.ArgumentList.Add("--workspace-notification"); launch.ArgumentList.Add(notificationUri); }
                workspaceInstance.ReleaseMutex(); workspaceInstance.Dispose(); workspaceInstance = null;
                using var next = System.Diagnostics.Process.Start(launch)
                    ?? throw new InvalidOperationException("최신 관리 앱을 시작하지 못했습니다.");
                Shutdown(0);
                return;
            }
            var workspace = new MainWindow(root, administratorPending: administratorPending);
            MainWindow = workspace;
            MainWindow.Show();
            if (ticket is not null) workspace.ActivateWorkspaceNotification(ticket);
        }
        catch (Exception ex) { MessageBox.Show(ex.Message, "Codex 관리 앱", MessageBoxButton.OK, MessageBoxImage.Error); Shutdown(1); }
    }

    private static string? Argument(string[] args, string name)
    {
        var index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    private static string FindRoot()
    {
        var candidate = new DirectoryInfo(AppContext.BaseDirectory);
        while (candidate is not null)
        {
            if (File.Exists(Path.Combine(candidate.FullName, "scripts", "control_center.py"))) return candidate.FullName;
            candidate = candidate.Parent;
        }
        throw new InvalidOperationException("저장소를 찾지 못했습니다. Open-Control-Center.cmd로 열어 주세요.");
    }
}
