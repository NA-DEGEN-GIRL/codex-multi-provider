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
