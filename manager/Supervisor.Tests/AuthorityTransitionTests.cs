using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;
using Codex.ControlCenter.Shared;

internal static partial class Program
{
    // Opt-in real UAC coverage. All services use synthetic Python and unique
    // temporary roots. Never pass the user's actual workspace to this fixture.
    private static string FixtureArgument(string[] args, string key)
    {
        int index = Array.IndexOf(args, key);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : throw new ArgumentException(key);
    }

    private static async Task<int> VerifyUacTransitionAsync(string[] args)
    {
        var report = FixtureArgument(args, "--report");
        Check(!WindowsExecutionIdentity.IsElevated, "coordinator must start at normal authority");
        var root = FixtureRoot();
        await using var client = await ManagerClient.ConnectAsync(root);
        var before = await client.RequestAsync("supervisor.status");
        RegisterStatusProcesses(before);
        var start = new ProcessStartInfo(Environment.ProcessPath!)
        { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden };
        start.ArgumentList.Add("--admin-transition-fixture");
        start.ArgumentList.Add("--normal-root"); start.ArgumentList.Add(root);
        start.ArgumentList.Add("--report"); start.ArgumentList.Add(report);
        try
        {
            using var elevated = Process.Start(start) ?? throw new InvalidOperationException("No UAC fixture process");
            await elevated.WaitForExitAsync().WaitAsync(TimeSpan.FromMinutes(3));
            Check(elevated.ExitCode == 0, "elevated fixture passed and cleaned up its own service");
            var after = await client.RequestAsync("supervisor.status");
            Check(after.GetProperty("supervisor_pid").GetInt32() == before.GetProperty("supervisor_pid").GetInt32(),
                "mixed-authority rejection preserves the original normal service");
            Console.WriteLine(File.ReadAllText(report));
            Console.WriteLine("PASS: real UAC transition; existing normal fixture service preserved.");
            return 0;
        }
        catch (System.ComponentModel.Win32Exception error) when (error.NativeErrorCode == 1223)
        {
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = false, cancelled = true }));
            Console.WriteLine("UAC cancelled; no administrator test was run.");
            return 2;
        }
    }

    private static async Task<int> AdminTransitionFixtureAsync(string[] args)
    {
        var report = FixtureArgument(args, "--report");
        try
        {
            var normalRoot = Path.GetFullPath(FixtureArgument(args, "--normal-root"));
            var fixturePrefix = Path.Combine(Path.GetTempPath(), "CodexControlCenter.Tests") + Path.DirectorySeparatorChar;
            Check(normalRoot.StartsWith(fixturePrefix, StringComparison.OrdinalIgnoreCase)
                && File.ReadAllText(Path.Combine(normalRoot, "scripts", "control_center.py")) == PythonFixture,
                "only a disposable synthetic normal fixture is permitted");
            var current = WindowsExecutionIdentity.Current();
            Check(current.Known && current.Elevated == true, "real elevated token was granted by Windows");
            var normal = await ManagerClient.ProbeExistingAuthorityAsync(normalRoot);
            Check(normal is { Known: true, Elevated: false } && normal.Value.UserSid == current.UserSid,
                "preflight can identify the same user despite a different UAC default owner");

            bool rejected = false;
            try { await using var ignored = await ManagerClient.ConnectAsync(normalRoot); }
            catch (ManagerException error) when (error.Code == "authority_mismatch") { rejected = true; }
            Check(rejected, "mixed elevation is rejected by OS-token preflight before an RPC");

            // Reproduce revision 85's misleading owner check on a separate
            // connection, then verify a fresh elevated service can be used.
            bool legacyRejected = false;
            using (var legacy = new NamedPipeClientStream(".", ManagerProtocol.PipeName(normalRoot), PipeDirection.InOut,
                       PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly))
            {
                try { await legacy.ConnectAsync(2000); }
                catch (UnauthorizedAccessException) { legacyRejected = true; }
            }
            Check(legacyRejected, "legacy CurrentUserOnly reproduces the UAC owner mismatch");
            await RunServiceAuthorityFixtureAsync();
            File.WriteAllText(report, JsonSerializer.Serialize(new
            { passed = true, elevated = true, legacy_owner_error_reproduced = true, normal_service_preserved = true, checks }));
            return 0;
        }
        catch (Exception error)
        {
            File.WriteAllText(report, JsonSerializer.Serialize(new { passed = false, error = error.GetType().Name, message = error.Message }));
            return 1;
        }
    }
}
