namespace Codex.ControlCenter.Shell;

internal sealed class ProfileActionGate
{
    private readonly Dictionary<string, string> active = [];
    public IDisposable Enter(string profileId, string label)
    {
        if (active.TryGetValue(profileId, out var pending))
            throw new InvalidOperationException($"이 계정의 ‘{pending}’ 처리가 진행 중입니다. 결과가 나온 뒤 다시 눌러주세요.");
        active.Add(profileId, label);
        return new Lease(() => active.Remove(profileId));
    }
    private sealed class Lease(Action release) : IDisposable
    {
        private Action? release = release;
        public void Dispose() { var action = release; release = null; action?.Invoke(); }
    }
}
