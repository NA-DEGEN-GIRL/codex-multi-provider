using System.Diagnostics;
using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class PackageUpdateAfterExit
{
    internal static async Task BeforeStartAsync(string root)
    {
        var journal = Path.Combine(root, "work", "control-center", "updates", "transaction.json");
        if (!File.Exists(journal)) return;
        using var document = JsonDocument.Parse(File.ReadAllText(journal));
        var state = document.RootElement;
        if (state.S("mode") != "official_package_only" ||
            !(state.S("status") == "registration_pending" ||
              (state.TryGetProperty("registration_finalize", out var finalizing) && finalizing.ValueKind == JsonValueKind.True &&
               state.S("status") is "preparing" or "installing" or "recovery_required"))) return;

        var python = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "python-path.txt")).Trim();
        var script = Path.Combine(AppContext.BaseDirectory, "scripts", "finish_package_update.py");
        if (!Path.IsPathFullyQualified(python) || !File.Exists(python) || !File.Exists(script))
            throw new InvalidOperationException("업데이트 완료 확인에 필요한 파일이 없습니다. 작업 공간 설치를 확인해 주세요.");
        var waiting = new System.Windows.Window
        {
            Title = "Codex 업데이트 적용", Width = 480, Height = 165,
            ResizeMode = System.Windows.ResizeMode.NoResize,
            WindowStartupLocation = System.Windows.WindowStartupLocation.CenterScreen,
            Content = new System.Windows.Controls.TextBlock
            {
                Text = "준비된 Codex 업데이트를 적용하고 있습니다.\n완료되면 작업 공간이 자동으로 열립니다.",
                TextWrapping = System.Windows.TextWrapping.Wrap, FontSize = 16,
                Margin = new System.Windows.Thickness(28)
            }
        };
        bool finished = false;
        waiting.Closing += (_, args) => args.Cancel = !finished;
        waiting.Show();
        try
        {
            var start = new ProcessStartInfo(python) { UseShellExecute = false, CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden, WorkingDirectory = root, RedirectStandardOutput = true,
                RedirectStandardError = true, StandardOutputEncoding = System.Text.Encoding.UTF8 };
            foreach (var argument in new[] { "-X", "utf8", script, "--root", root, "--before-start" })
                start.ArgumentList.Add(argument);
            using var process = Process.Start(start) ?? throw new InvalidOperationException("업데이트 확인을 시작하지 못했습니다.");
            var output = process.StandardOutput.ReadToEndAsync();
            var errors = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromMinutes(20));
            await errors; // Do not expose raw installer output in the UI.
            if (process.ExitCode != 0) throw new InvalidOperationException("업데이트 종료 확인에 실패했습니다. 진행 기록을 확인해 주세요.");
            using var result = JsonDocument.Parse(await output);
            if (result.RootElement.S("status") == "startup_blocked")
                throw new InvalidOperationException(result.RootElement.S("message"));
        }
        finally { finished = true; waiting.Close(); }
    }

    // Called only after full shutdown has verified the management service exit.
    // Ordinary window close never schedules package registration.
    internal static void Queue(string root, Action<string> log)
    {
        try
        {
            var journal = Path.Combine(root, "work", "control-center", "updates", "transaction.json");
            if (!File.Exists(journal)) return;
            using var document = JsonDocument.Parse(File.ReadAllText(journal));
            var state = document.RootElement;
            if (state.S("mode") != "official_package_only" || state.S("status") != "registration_pending" ||
                state.S("install_outcome") is not ("command_completed" or "registration_busy") ||
                !Guid.TryParse(state.S("transaction_id"), out _)) return;
            var python = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "python-path.txt")).Trim();
            var script = Path.Combine(AppContext.BaseDirectory, "scripts", "finish_package_update.py");
            if (!Path.IsPathFullyQualified(python) || !File.Exists(python) || !File.Exists(script)) return;
            using var parent = Process.GetCurrentProcess();
            var start = new ProcessStartInfo(python) { UseShellExecute = false, CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden, WorkingDirectory = root };
            foreach (var argument in new[] { "-X", "utf8", script, "--root", root, "--transaction", state.S("transaction_id"),
                "--parent-pid", Environment.ProcessId.ToString(), "--parent-created", parent.StartTime.ToFileTimeUtc().ToString() })
                start.ArgumentList.Add(argument);
            using var worker = Process.Start(start);
            log("완전 종료 후 준비된 공식 Codex 업데이트를 자동 적용합니다.");
        }
        catch (Exception error) when (error is IOException or JsonException or System.ComponentModel.Win32Exception or InvalidOperationException)
        { log("종료 후 업데이트 준비를 완료하지 못했습니다. 다음 실행에서 다시 적용할 수 있습니다."); }
    }
}
