using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class ConversationReadyWait
{
    public static async Task<JsonElement?> CompleteAsync(JsonElement result, Func<bool> stillSelected,
        Func<string, Task<JsonElement>> resume, Func<Task> delay)
    {
        while (result.S("state") == "waiting_for_reader")
        {
            if (!stillSelected()) return null;
            var token = result.S("navigation_id");
            if (token == "") throw new InvalidOperationException("대화 이동 대기 요청을 확인하지 못했습니다.");
            await delay();
            if (!stillSelected()) return null;
            result = await resume(token);
        }
        return stillSelected() ? result : null;
    }
}

// The readiness continuation is a request/response IPC: a server-side wait would
// hold one of the backend's few request workers per waiting open. Instead the
// first checks follow quickly (most warm waits end within a second) and back
// off to MaxMs; a new waiting reason (the reader is up, now SSH) starts over.
internal sealed class ReadyPollBackoff
{
    internal const int FirstMs = 150, MaxMs = 600;
    private int next = FirstMs;
    private string reason = "";
    internal int Next() { var value = next; next = Math.Min(MaxMs, next * 3 / 2); return value; }
    internal void Observe(string waitingReason)
    {
        if (waitingReason == reason) return;
        reason = waitingReason;
        next = FirstMs;
    }
}
