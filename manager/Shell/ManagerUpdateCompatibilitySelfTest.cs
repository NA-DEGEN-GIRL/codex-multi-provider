using System.IO;
using System.Text.Json;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal static class ManagerUpdateCompatibilitySelfTest
{
    internal static int Run()
    {
        // Empty executable placeholders and synthetic manifests only. Inspect
        // must never start a shell or contact a real management service.
        var fixture = Directory.CreateTempSubdirectory("codex-manager-compatibility-");
        var root = fixture.FullName;
        var manager = Path.Combine(root, "artifacts", "manager");
        var pointer = Path.Combine(manager, "current.json");
        int checks = 0;
        const int protocol = ManagerProtocol.Version;
        string serviceA = new('a', 64), serviceB = new('b', 64), serviceC = new('c', 64);

        string Release(string name, int revision, string serviceRevision)
        {
            var directory = Path.Combine(manager, "releases", name);
            Directory.CreateDirectory(directory);
            var executable = Path.Combine(directory, "Codex.ControlCenter.exe");
            File.WriteAllBytes(executable, []);
            File.WriteAllText(Path.Combine(directory, "runtime-manifest.json"), JsonSerializer.Serialize(new
            {
                version = 1, service_revision = serviceRevision,
                shell_compatibility = new { version = 1, revision, service_protocol = protocol }
            }));
            return executable;
        }

        void Select(string executable) => File.WriteAllText(pointer, JsonSerializer.Serialize(new { shell = executable }));
        static JsonElement Live(string serviceRevision, int liveProtocol = protocol, bool preserves = true) =>
            JsonSerializer.SerializeToElement(new { version = liveProtocol, service_revision = serviceRevision, preserves_background_profiles = preserves });
        void Expect(string executable, int revision, JsonElement? live, ManagerUpdateState expected, string scenario)
        {
            var before = Directory.GetFiles(root, "*", SearchOption.AllDirectories)
                .ToDictionary(path => path, File.ReadAllBytes);
            var actual = ManagerUpdateCompatibility.Inspect(root, executable, revision, live);
            if (actual.State != expected)
                throw new InvalidOperationException($"Manager compatibility: {scenario}. Expected {expected}, got {actual.State}: {actual.Title}");
            checks++;
            if (Directory.GetFiles(root, "*", SearchOption.AllDirectories).Length != before.Count ||
                before.Any(file => !File.Exists(file.Key) || !file.Value.SequenceEqual(File.ReadAllBytes(file.Key))))
                throw new InvalidOperationException("Compatibility inspection changed its synthetic release files.");
            checks++;
        }

        try
        {
            var shellA = Release("shell-a", 10, serviceA);
            var shellB = Release("shell-b", 11, serviceB);
            var shellC = Release("shell-c", 12, serviceC);
            var shellOnly = Release("shell-only", 13, serviceA);
            var liveA = Live(serviceA);

            Select(shellB);
            Expect(shellA, 10, liveA, ManagerUpdateState.NeedsStop,
                "a newer shell that requires service B cannot be Ready while service A remains alive");
            Expect(shellB, 11, liveA, ManagerUpdateState.NeedsStop,
                "reopening shell B alone does not clear the pending service replacement");
            Select(shellC);
            Expect(shellB, 11, liveA, ManagerUpdateState.NeedsStop,
                "publishing shell C does not hide the live service mismatch behind a newer-shell notice");
            Expect(shellC, 12, liveA, ManagerUpdateState.NeedsStop,
                "another shell-only reopen still requires the original service A to stop");

            Select(shellOnly);
            Expect(shellA, 10, liveA, ManagerUpdateState.Ready,
                "a shell-only update that retains service A supports reopening while work continues");
            Expect(shellOnly, 13, liveA, ManagerUpdateState.Current,
                "matching active shell and live service are current");

            Select(shellB);
            Expect(shellA, 10, Live(serviceB), ManagerUpdateState.Ready,
                "a newer shell is Ready once its exact target service is already running");
            Expect(shellB, 11, Live(serviceB), ManagerUpdateState.Current,
                "a completed service replacement clears the full-stop requirement");
            Expect(shellA, 10, Live(serviceB, protocol + 1), ManagerUpdateState.NeedsStop,
                "equal service revisions cannot override an incompatible live protocol");
            Expect(shellB, 11, Live(serviceB, preserves: false), ManagerUpdateState.NeedsStop,
                "a service without background-profile preservation cannot advertise a safe shell restart");
            Expect(shellA, 10, JsonSerializer.SerializeToElement(new { version = protocol, service_revision = serviceB }),
                ManagerUpdateState.NeedsStop, "a legacy service without a preservation capability still requires a full stop");

            foreach (var live in new JsonElement?[]
            {
                null, default(JsonElement), JsonSerializer.SerializeToElement<object?>(null),
                JsonSerializer.SerializeToElement(new { }), JsonSerializer.SerializeToElement("not-service-metadata")
            })
                Expect(shellA, 10, live, ManagerUpdateState.Unknown, "missing or non-object live metadata stays unknown");

            foreach (var (field, invalid) in new (string Field, object? Value)[]
            {
                ("version", null), ("version", "27"), ("version", 0),
                ("service_revision", null), ("service_revision", ""), ("service_revision", 42),
                ("service_revision", "not-a-hash"), ("service_revision", new string('g', 64)),
                ("preserves_background_profiles", "true")
            })
            {
                var live = new Dictionary<string, object?>
                {
                    ["version"] = protocol, ["service_revision"] = serviceB,
                    ["preserves_background_profiles"] = true
                };
                if (invalid is null) live.Remove(field);
                else live[field] = invalid;
                Expect(shellA, 10, JsonSerializer.SerializeToElement(live), ManagerUpdateState.Unknown,
                    "missing or invalid " + field + " cannot advertise a safe shell restart");
            }
            var malformedTarget = Release("invalid-service-revision", 14, "not-a-hash");
            Select(malformedTarget);
            Expect(shellB, 11, Live(serviceB), ManagerUpdateState.Unknown,
                "an invalid target service revision cannot advertise compatibility");
            return checks;
        }
        finally
        {
            // Delete only the unique directory created by this fixture.
            var temporaryRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(Path.GetTempPath()));
            if (fixture.Parent is not null && string.Equals(fixture.Parent.FullName, temporaryRoot, StringComparison.OrdinalIgnoreCase)
                && fixture.Name.StartsWith("codex-manager-compatibility-", StringComparison.Ordinal))
                fixture.Delete(recursive: true);
        }
    }
}
