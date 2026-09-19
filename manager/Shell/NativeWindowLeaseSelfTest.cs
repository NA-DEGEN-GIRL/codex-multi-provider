using System.IO;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class NativeWindowLeaseSelfTest
{
    internal static void Run(string directory)
    {
        const int fixturePid = 123;
        var file = Path.Combine(directory, $"{fixturePid}.json");
        using var lease = new NativeWindowLease(directory, fixturePid, (nint)456);
        void WithSharingViolation(Action change)
        {
            // Reproduce a reader without FILE_SHARE_DELETE while the shell
            // atomically replaces its command. Only this disposable file is held.
            using var reader = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.Read);
            change();
            if (lease.PublicationError is null)
                throw new InvalidOperationException("Fixture did not reproduce the lease publication failure.");
        }
        void RequireState(Func<JsonElement, bool> expected, string message)
        {
            using var document = JsonDocument.Parse(File.ReadAllText(file));
            if (!expected(document.RootElement)) throw new InvalidOperationException(message);
        }
        RequireState(s => s.GetProperty("geometryOwner").GetString() == "native" &&
            !s.GetProperty("interactiveMove").GetBoolean(), "The native host did not claim exclusive geometry ownership.");
        WithSharingViolation(() => lease.SetInteractiveMove(true));
        lease.SetInteractiveMove(true);
        RequireState(s => s.GetProperty("interactiveMove").GetBoolean(), "Interactive movement did not recover a failed lease write.");
        var unchanged = File.GetLastWriteTimeUtc(file);
        lease.SetInteractiveMove(true);
        if (File.GetLastWriteTimeUtc(file) != unchanged) throw new InvalidOperationException("Unchanged drag state rewrote the lease.");
        lease.SetInteractiveMove(false);
        RequireState(s => !s.GetProperty("interactiveMove").GetBoolean(), "Settled placement retained interactive movement.");
        WithSharingViolation(() => lease.SetVisible(true));
        RequireState(s => !s.GetProperty("visible").GetBoolean(), "Failed write unexpectedly changed the published lease.");
        lease.SetVisible(true);
        if (lease.PublicationError is not null) throw new InvalidOperationException("Successful retry retained a stale publication error.");
        RequireState(s => s.GetProperty("visible").GetBoolean() && s.GetProperty("presentationEpoch").GetInt64() == 1,
            "Reselecting the same profile failed to retry a lost show command.");
        lease.SetBounds(20, 30, 800, 600, 144);
        WithSharingViolation(() => lease.SetBounds(40, 50, 900, 700, 144));
        lease.SetBounds(40, 50, 900, 700, 144);
        RequireState(s => s.GetProperty("bounds").GetProperty("width").GetInt32() == 900,
            "Unchanged geometry failed to retry a lost bounds command.");
        WithSharingViolation(() => lease.SetVisible(false));
        lease.SetVisible(false);
        RequireState(s => !s.GetProperty("visible").GetBoolean(), "Hidden profile retained its published visible state.");
        lease.SetVisible(true);
        RequireState(s => s.GetProperty("presentationEpoch").GetInt64() == 2,
            "Retries incorrectly incremented the compositor presentation epoch.");
        using var state = JsonDocument.Parse(File.ReadAllText(file));
        void Acknowledge(long epoch) => File.WriteAllText(file + ".render.json", JsonSerializer.Serialize(new {
            version = 1, appPid = fixturePid, hwnd = "456", shellPid = Environment.ProcessId,
            token = state.RootElement.GetProperty("token").GetString(), presentationEpoch = epoch,
            rendererReady = true, shown = true
        }));
        Acknowledge(1);
        if (lease.ReadPresentationStatus() is not null) throw new InvalidOperationException("Old presentation was reported as current.");
        Acknowledge(2);
        if (lease.ReadPresentationStatus() is null) throw new InvalidOperationException("Current presentation acknowledgement was lost.");
        string initial = Path.Combine(directory, "124.json");
        File.WriteAllText(initial, "{}");
        NativeWindowLease fresh;
        using (var reader = new FileStream(initial, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            fresh = new NativeWindowLease(directory, 124, (nint)789);
            if (fresh.PublicationError is null) throw new InvalidOperationException("Initial publication race was not reproduced.");
        }
        using (fresh)
        {
            fresh.SetVisible(true);
            using var published = JsonDocument.Parse(File.ReadAllText(initial));
            if (fresh.PublicationError is not null || !published.RootElement.GetProperty("visible").GetBoolean())
                throw new InvalidOperationException("Initial publication failure could not recover the retained lease.");
        }
    }
}
