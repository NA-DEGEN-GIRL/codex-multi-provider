using System.IO;
using System.Windows;

namespace Codex.ControlCenter.Shell;

public partial class App : Application
{
    private Mutex? workspaceInstance;
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
                Shutdown(0); return;
            }
            var replacement = InstalledManagerRelease.Replacement(root, Environment.ProcessPath
                ?? throw new InvalidOperationException("현재 관리 앱 실행 경로를 확인하지 못했습니다."));
            if (replacement is not null)
            {
                var launch = new System.Diagnostics.ProcessStartInfo(replacement) { UseShellExecute = false };
                launch.ArgumentList.Add("--root");
                launch.ArgumentList.Add(root);
                if (notificationUri is not null) { launch.ArgumentList.Add("--workspace-notification"); launch.ArgumentList.Add(notificationUri); }
                workspaceInstance.ReleaseMutex(); workspaceInstance.Dispose(); workspaceInstance = null;
                using var next = System.Diagnostics.Process.Start(launch)
                    ?? throw new InvalidOperationException("최신 관리 앱을 시작하지 못했습니다.");
                Shutdown(0);
                return;
            }
            var workspace = new MainWindow(root);
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
