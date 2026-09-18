using System.Windows;

namespace Codex.ControlCenter.Shell;

// Profiles retain independent native input queues while their viewports switch.
// A selection changes visibility without parenting or discarding native layout.
internal sealed class NativeHostDeck(Action<NativeWindowHost> created)
{
    private readonly Dictionary<string, NativeWindowHost> hosts = [];
    public NativeWindowHost Current { get; private set; } = null!;
    public IEnumerable<NativeWindowHost> Hosts => hosts.Values;
    public NativeWindowHost? Find(string id) => hosts.GetValueOrDefault(id);

    public NativeWindowHost Ensure(string id)
    {
        if (!hosts.TryGetValue(id, out var host))
        {
            host = new NativeWindowHost { Visibility = Visibility.Hidden };
            hosts.Add(id, host);
            created(host);
        }
        return host;
    }

    public NativeWindowHost Select(string id)
    {
        var host = Ensure(id);
        if (Current != host)
        {
            HideCurrent();
            Current = host;
        }
        return host;
    }

    public void HideCurrent()
    {
        // Hidden retains the client area. Collapsed can leave a 1x1 host while
        // the native compositor is being made visible again.
        if (Current is not null) Current.Visibility = Visibility.Hidden;
    }
}
