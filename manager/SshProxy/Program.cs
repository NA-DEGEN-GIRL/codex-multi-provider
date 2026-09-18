namespace Codex.ControlCenter.SshProxy;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        try
        {
            var configuredPython = Environment.GetEnvironmentVariable("CODEX_MANAGER_SSH_PYTHON");
            if (string.IsNullOrWhiteSpace(configuredPython))
                configuredPython = Environment.GetEnvironmentVariable("CODEX_MANAGER_PYTHON");
            var python = RequireFile(configuredPython, ".exe");
            var script = RequireFile(Environment.GetEnvironmentVariable("CODEX_MANAGER_SSH_SCRIPT"), ".py");
            var arguments = new List<string> { "-u", script, "--" };
            arguments.AddRange(args);
            // The child receives inherited console events directly. Do not exit the job owner
            // first on Ctrl+C; an external process termination still closes and kills the job.
            ConsoleCancelEventHandler onCancel = (_, e) => e.Cancel = true;
            Console.CancelKeyPress += onCancel;
            try { return await InheritedProcess.RunAsync(python, arguments).ConfigureAwait(false); }
            finally { Console.CancelKeyPress -= onCancel; }
        }
        catch (Exception)
        {
            // In particular, do not print command arguments, configuration paths or environment.
            Console.Error.WriteLine("Codex Control Center: SSH bridge startup failed. Check its installation and configured interpreter.");
            return 127;
        }
    }

    private static string RequireFile(string? configured, string extension)
    {
        if (string.IsNullOrWhiteSpace(configured) || !Path.IsPathFullyQualified(configured)
            || !File.Exists(configured)
            || !string.Equals(Path.GetExtension(configured), extension, StringComparison.OrdinalIgnoreCase))
            throw new IOException("An exact component path is required.");
        return Path.GetFullPath(configured);
    }
}
