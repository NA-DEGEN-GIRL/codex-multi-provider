namespace Codex.ControlCenter.Shell;

internal sealed class ProfileActionGate
{
    private readonly Dictionary<string, (string Label, TaskCompletionSource Released)> active = [];
    public IDisposable Enter(string profileId, string label)
    {
        if (active.TryGetValue(profileId, out var pending))
            throw new InvalidOperationException($"이 계정의 ‘{pending.Label}’ 처리가 진행 중입니다. 결과가 나온 뒤 다시 눌러주세요.");
        var released = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        active.Add(profileId, (label, released));
        return new Lease(() => { active.Remove(profileId); released.TrySetResult(); });
    }
    // Completes when the profile's current holder releases the gate, if that
    // holder is an action with this label; null when free or held by another.
    public Task? Held(string profileId, string label) =>
        active.TryGetValue(profileId, out var holder) && holder.Label == label ? holder.Released.Task : null;
    private sealed class Lease(Action release) : IDisposable
    {
        private Action? release = release;
        public void Dispose() { var action = release; release = null; action?.Invoke(); }
    }
}
