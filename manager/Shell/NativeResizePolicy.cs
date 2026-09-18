namespace Codex.ControlCenter.Shell;

// Chromium can reject a requested size or apply it later. Layout notifications
// must not turn that disagreement into an unbounded SetWindowPos/render loop.
internal sealed class NativeResizePolicy
{
    private int width, height, attempts;
    private long lastAttempt;
    private bool reported;

    public bool Request(int nextWidth, int nextHeight, bool frameChanged, bool matches,
        long now, out bool report)
    {
        report = false;
        if (frameChanged || width != nextWidth || height != nextHeight)
        {
            width = nextWidth; height = nextHeight; attempts = 1;
            lastAttempt = now; reported = false;
            return true;
        }
        // A settled window ends this recovery episode. Settings/native layout
        // may change it later; earlier successful repairs must not exhaust the
        // new episode's budget for the rest of this process lifetime.
        if (matches) { attempts = 0; reported = false; return false; }
        if (attempts > 0 && now - lastAttempt < 1000) return false;
        if (attempts >= 3)
        {
            report = !reported; reported = true;
            return false;
        }
        attempts++; lastAttempt = now;
        return true;
    }
}
