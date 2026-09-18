namespace Codex.ControlCenter.Shell;

// Successful polling is background traffic. Always show the first failure;
// repeated identical failures are counted and reported at most twice a minute.
internal sealed class RpcLogFilter
{
    private readonly Dictionary<string, (DateTime At, int Suppressed)> errors = [];
    public bool Accept(string generation, string method, string state, string reason, long code,
        DateTime now, out int repeated)
    {
        repeated = 0;
        var key = generation + ":" + method;
        if (state == "failed")
        {
            key += ":" + reason + ":" + code;
            if (errors.TryGetValue(key, out var previous))
            {
                if (now - previous.At < TimeSpan.FromSeconds(30))
                {
                    errors[key] = (previous.At, previous.Suppressed + 1);
                    return false;
                }
                repeated = previous.Suppressed;
            }
            if (errors.Count > 256) errors.Clear();
            errors[key] = (now, 0);
            return true;
        }
        return method is not ("thread/list" or "thread/read" or "configRequirements/read" or "project/list" or "account/read" or
            "account/rateLimits/read" or "thread/turns/list" or "thread/items/list" or "thread/timeline/list");
    }
}
