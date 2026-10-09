namespace Codex.ControlCenter.Shell;

internal sealed class ProfileActionGate
{
    private readonly Dictionary<string, (string Label, TaskCompletionSource Released, CancellationTokenSource? Replace)> active = [];
    // A holder entered with a cancellation source (a task or profile open) can
    // be replaced by a later click; other actions keep refusing a second one.
    public IDisposable Enter(string profileId, string label, CancellationTokenSource? replaceable = null)
    {
        if (active.TryGetValue(profileId, out var pending))
            throw new InvalidOperationException($"이 계정의 ‘{pending.Label}’ 처리가 진행 중입니다. 결과가 나온 뒤 다시 눌러주세요.");
        var released = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        active.Add(profileId, (label, released, replaceable));
        return new Lease(() => { active.Remove(profileId); released.TrySetResult(); });
    }
    // Completes when the profile's current holder releases the gate, if that
    // holder is an action with this label; null when free or held by another.
    public Task? Held(string profileId, string label) =>
        active.TryGetValue(profileId, out var holder) && holder.Label == label ? holder.Released.Task : null;
    // Latest click wins: cancels a replaceable holder and returns the task that
    // completes once it has released the gate; null when the gate is free. A
    // holder that cannot be replaced is refused with Enter's message.
    public Task? Replace(string profileId)
    {
        if (!active.TryGetValue(profileId, out var holder)) return null;
        if (holder.Replace is null)
            throw new InvalidOperationException($"이 계정의 ‘{holder.Label}’ 처리가 진행 중입니다. 결과가 나온 뒤 다시 눌러주세요.");
        holder.Replace.Cancel();
        return holder.Released.Task;
    }
    private sealed class Lease(Action release) : IDisposable
    {
        private Action? release = release;
        public void Dispose() { var action = release; release = null; action?.Invoke(); }
    }
}
