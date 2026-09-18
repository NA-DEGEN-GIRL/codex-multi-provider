namespace Codex.ControlCenter.RuntimeProxy;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        try
        {
            var root = FindRoot();
            var bundled = File.Exists(Path.Combine(AppContext.BaseDirectory, "runtime-manifest.json"));
            var script = Path.Combine(bundled ? AppContext.BaseDirectory : root, "scripts", "manager_core", "runtime_proxy.py");
            if (!File.Exists(script)) return Fail("Runtime bridge component was not found.");
            var python = FindPython();
            var arguments = new List<string>();
            if (python.IsLauncher) arguments.Add("-3");
            arguments.Add("-u");
            arguments.Add(script);
            arguments.Add("--");
            arguments.AddRange(args);
            Environment.SetEnvironmentVariable("CODEX_MANAGER_ROOT", root);
            Environment.SetEnvironmentVariable("PYTHONIOENCODING", "utf-8");
            Environment.SetEnvironmentVariable("PYTHONUTF8", "1");
            // Console control events already reach an inherited console process group. Avoid
            // exiting first or sending a duplicate event; the Python bridge owns child cleanup.
            ConsoleCancelEventHandler onCancel = (_, e) => e.Cancel = true;
            Console.CancelKeyPress += onCancel;
            try { return await InheritedProcess.RunAsync(python.Executable, arguments).ConfigureAwait(false); }
            finally { Console.CancelKeyPress -= onCancel; }
        }
        catch (Exception)
        {
            // Never include argv, inherited environment, credentials or backend diagnostics.
            return Fail("Runtime bridge startup failed. Check the manager installation and Python path.");
        }
    }

    private static int Fail(string message)
    {
        Console.Error.WriteLine("Codex Control Center: " + message);
        return 127;
    }

    private static string FindRoot()
    {
        var configured = Environment.GetEnvironmentVariable("CODEX_MANAGER_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            if (!Path.IsPathFullyQualified(configured)) throw new IOException("An absolute workspace path is required.");
            var root = Path.GetFullPath(configured);
            if (!Directory.Exists(root)) throw new IOException("The workspace path was not found.");
            return root;
        }
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        for (var level = 0; level < 8 && directory is not null; level++, directory = directory.Parent)
        {
            if (File.Exists(Path.Combine(directory.FullName, "scripts", "manager_core", "runtime_proxy.py")))
                return directory.FullName;
        }
        throw new IOException("The workspace path was not found.");
    }

    private static (string Executable, bool IsLauncher) FindPython()
    {
        var configured = Environment.GetEnvironmentVariable("CODEX_MANAGER_PYTHON");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            if (!Path.IsPathFullyQualified(configured) || !File.Exists(configured)
                || !string.Equals(Path.GetExtension(configured), ".exe", StringComparison.OrdinalIgnoreCase))
                throw new IOException("An exact Python executable path is required.");
            return (Path.GetFullPath(configured), string.Equals(Path.GetFileName(configured), "py.exe", StringComparison.OrdinalIgnoreCase));
        }
        var windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        var launcher = Path.Combine(windows, "py.exe");
        if (File.Exists(launcher)) return (launcher, true);
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var systemDrive = Path.GetPathRoot(windows) ?? "C:\\";
        foreach (var version in new[] { "314", "313", "312", "311" })
        {
            foreach (var candidate in new[]
            {
                Path.Combine(systemDrive, "Python" + version, "python.exe"),
                Path.Combine(local, "Programs", "Python", "Python" + version, "python.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Python" + version, "python.exe"),
            }) if (File.Exists(candidate)) return (candidate, false);
        }
        throw new IOException("Python was not found. Configure the exact interpreter path in the manager.");
    }
}
