using Codex.ControlCenter.Shell;
using Codex.ControlCenter.Shared;

internal static class WorkspaceExecutionModeTests
{
    internal static void Run()
    {
        var assertions = 0;
        void Check(bool condition, string reason)
        {
            if (!condition) throw new InvalidOperationException(reason);
            assertions++;
        }
        var directory = Path.Combine(Path.GetTempPath(), "codex-authority-preference-" + Guid.NewGuid());
        try
        {
            Check(!WorkspaceExecutionMode.ReadAdministrator(directory), "Fresh installs must not request UAC.");
            WorkspaceExecutionMode.SaveAdministrator(directory, true);
            Check(WorkspaceExecutionMode.ReadAdministrator(directory), "Administrator preference was lost.");
            WorkspaceExecutionMode.SaveAdministrator(directory, false);
            Check(!WorkspaceExecutionMode.ReadAdministrator(directory), "Normal preference was lost.");
            Check(Directory.GetFiles(Path.GetDirectoryName(WorkspaceExecutionMode.PreferencePath(directory))!).Length == 1, "Save left a temporary file.");
            Check(!File.Exists(Path.Combine(directory, "work", "control-center", "state.json")), "Saving a preference touched manager state.");
            foreach (var broken in new[] { "{", "[]", "{\"version\":2,\"administrator\":true}", "{\"version\":1,\"administrator\":\"true\"}", "{\"version\":\"1\",\"administrator\":true}" })
            {
                File.WriteAllText(WorkspaceExecutionMode.PreferencePath(directory), broken);
                bool rejected = false;
                try { WorkspaceExecutionMode.ReadAdministrator(directory); } catch (InvalidOperationException) { rejected = true; }
                Check(rejected, "Corrupt preference silently changed authority.");
            }
        }
        finally { if (Directory.Exists(directory)) Directory.Delete(directory, true); }

        const string root = @"D:\Example & Work\workspace";
        const string uri = "codex-workspace-example://notification/ticket";
        var start = WorkspaceExecutionMode.ElevatedStart("example.exe", root, "user-a", uri);
        Check(start.UseShellExecute && start.Verb == "runas", "UAC consent path missing.");
        Check(start.ArgumentList[1] == root && start.WorkingDirectory == root, "Elevation lost the root.");
        Check(start.ArgumentList[4] == "user-a", "Elevation lost the requesting account.");
        Check(start.ArgumentList[6] == uri, "Elevation lost the notification.");
        WorkspaceExecutionMode.RequireSameUser("user-a", "user-a");
        bool differentUserRejected = false;
        try { WorkspaceExecutionMode.RequireSameUser("user-a", "user-b"); } catch (InvalidOperationException) { differentUserRejected = true; }
        Check(differentUserRejected, "UAC credentials switched the DPAPI user.");
        Check(WorkspaceExecutionMode.RequiresElevation(true, false, false), "Saved preference ignored.");
        Check(WorkspaceExecutionMode.RequiresElevation(false, true, false), "Explicit admin launcher ignored.");
        Check(!WorkspaceExecutionMode.RequiresElevation(true, true, true), "Elevated child would relaunch forever.");
        Check(!WorkspaceExecutionMode.RequiresElevation(false, false, false), "Ordinary launch unexpectedly elevates.");
        var normal = new ExecutionAuthority(false, "user-a");
        Check(WorkspaceExecutionMode.DeferElevation(normal, normal), "Existing normal work must remain reachable before UAC.");
        Check(!WorkspaceExecutionMode.DeferElevation(normal, null), "A cold launch should request UAC.");
        Check(!WorkspaceExecutionMode.DeferElevation(normal, new(true, "user-a")), "An existing administrator service requires elevation.");
        Check(!WorkspaceExecutionMode.DeferElevation(normal, WindowsExecutionIdentity.UnknownAuthority), "An unknown token is not proof of normal work.");
        Check(!WorkspaceExecutionMode.DeferElevation(normal, new(false, "user-b")), "A foreign account is never adopted.");
        Check(!WorkspaceExecutionMode.DeferElevation(new(true, "user-a"), normal), "An elevated process cannot silently downgrade.");
        Console.WriteLine($"Workspace execution mode: {assertions} assertions passed (no UAC or live service).");
    }
}
