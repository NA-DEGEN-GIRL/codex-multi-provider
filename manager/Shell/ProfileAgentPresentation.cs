using System.Text.Json;

namespace Codex.ControlCenter.Shell;

// Describes the saved delegation policy, not which model is currently running.
internal static class ProfileAgentPresentation
{
    internal static string Badge(JsonElement profile)
    {
        var policy = profile.Get("policy");
        if (!policy.B("enabled")) return "";
        var mode = !policy.Arr("model_ids").Any() ? "모델 미선택"
            : policy.S("selection_mode", "automatic") == "external_only" ? "외부 전용" : "혼합";
        var pending = policy.N("desired_revision") != policy.N("effective_revision");
        return "하위 에이전트 · " + mode + (pending ? " · 대기" : "");
    }

    internal static string Hint(JsonElement profile)
    {
        var policy = profile.Get("policy");
        if (!policy.B("enabled")) return "외부 하위 에이전트 사용 안 함";
        var count = policy.Arr("model_ids").Count();
        var description = count == 0 ? "외부 모델을 선택해야 사용할 수 있습니다."
            : policy.S("selection_mode", "automatic") == "external_only"
                ? $"선택한 외부 모델 {count}개만 하위 에이전트로 허용합니다."
                : $"{(profile.S("auth_mode") == "external" ? "기본 모델" : "GPT")}과 선택한 외부 모델 {count}개 중 작업에 맞게 선택합니다.";
        if (policy.N("desired_revision") != policy.N("effective_revision"))
            description += "\n저장된 설정이며 실행 중인 프로필에는 아직 적용 대기 중입니다.";
        return description + "\n실제 호출 중인 모델 표시가 아닙니다. 우클릭 → 하위 에이전트 설정에서 변경하세요.";
    }
}
