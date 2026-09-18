using System.IO;
using System.Text.Json;
using System.Windows.Threading;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal sealed record NoteTask(string HostId, string ThreadId)
{
    public object Wire => new { host_id = HostId, thread_id = ThreadId };
    public string Key => HostId + "/" + ThreadId;
}
internal sealed record SelectedTask(NoteTask Task, string Title);

internal sealed class TaskContextWatcher : IDisposable
{
    private readonly string directory;
    private readonly Dispatcher dispatcher;
    private readonly Action<SelectedTask?> changed;
    private readonly FileSystemWatcher watcher;
    private JsonElement profile;
    private long ticket;
    private string selection = "";
    private bool disposed;
    private SelectedTask? current;

    internal TaskContextWatcher(string root, Dispatcher dispatcher, Action<SelectedTask?> changed)
    {
        directory = Path.Combine(root, "work", "control-center", "instances");
        Directory.CreateDirectory(directory);
        this.dispatcher = dispatcher; this.changed = changed;
        watcher = new FileSystemWatcher(directory, "active-task.json") { IncludeSubdirectories = true, NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.FileName };
        watcher.Changed += OnChanged; watcher.Created += OnChanged; watcher.Renamed += (_, e) => OnChanged(this, e);
        watcher.EnableRaisingEvents = true;
    }
    internal void Select(JsonElement next)
    {
        profile = next;
        string identity = next.S("id") + "/" + next.S("generation") + "/" + next.N("process_id");
        if (selection != identity) { selection = identity; current = null; changed(null); }
        _ = ReadAsync(++ticket);
    }
    private void OnChanged(object? sender, FileSystemEventArgs e) => dispatcher.BeginInvoke(new Action(() =>
    {
        if (!disposed && Path.GetFileName(Path.GetDirectoryName(e.FullPath)) == profile.S("id")) _ = ReadAsync(++ticket);
    }));
    private async Task ReadAsync(long request)
    {
        if (!Guid.TryParse(profile.S("id"), out _) || profile.S("generation") == "") return;
        var expected = profile;
        try
        {
            var path = Path.Combine(directory, expected.S("id"), "active-task.json");
            await using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 4096, true);
            if (file.Length > 8192) return;
            using var doc = await JsonDocument.ParseAsync(file);
            var value = doc.RootElement;
            if (disposed || request != ticket || value.S("generation") != expected.S("generation") || value.N("app_pid") != expected.N("process_id") || value.S("profile_id") != expected.S("id")) return;
            if (expected.N("window_handle") > 0 && (!long.TryParse(value.S("hwnd"), out var hwnd) || hwnd != expected.N("window_handle"))) return;
            SelectedTask? selected = null;
            if (Guid.TryParse(value.S("thread_id"), out var thread))
            {
                var title = value.S("title");
                var opened = expected.Get("runtime_state").Get("opened_task");
                if (opened.S("thread_id") == thread.ToString() && opened.S("title") != "") title = opened.S("title");
                if (title is "" or "ChatGPT" or "Codex") title = "작업 " + thread.ToString()[..8];
                selected = new(new(value.S("host_id", "local"), thread.ToString()), title);
            }
            if (current != selected) { current = selected; changed(selected); }
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException) { }
    }
    public void Dispose() { disposed = true; ++ticket; watcher.Dispose(); }
}
