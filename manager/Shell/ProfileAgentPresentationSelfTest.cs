using System.Reflection;
using System.Text.Json;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

internal static class ProfileAgentPresentationSelfTest
{
    internal static void Run(List<string> checks)
    {
        JsonElement Profile(bool enabled, string mode = "automatic", string auth = "chatgpt", int desired = 2, int effective = 2, string[]? models = null)
            => JsonSerializer.SerializeToElement(new { auth_mode = auth, policy = new { enabled, selection_mode = mode,
                model_ids = models ?? ["fixture-external"], desired_revision = desired, effective_revision = effective } });
        void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }
        Require(ProfileAgentPresentation.Badge(Profile(false)) == "", "Disabled external agents gained a badge from saved model IDs.");
        var mixed = Profile(true);
        Require(ProfileAgentPresentation.Badge(mixed) == "하위 에이전트 · 혼합", "Automatic delegation is not identified as mixed.");
        Require(ProfileAgentPresentation.Badge(Profile(true, "external_only")) == "하위 에이전트 · 외부 전용", "External-only selection is ambiguous.");
        Require(ProfileAgentPresentation.Badge(Profile(true, "external_only", "external", effective: 1)).EndsWith(" · 대기"), "Pending policy is reported as applied.");
        Require(ProfileAgentPresentation.Badge(Profile(true, models: [])).Contains("미선택"), "An empty selection claims external models are usable.");
        Require(!ProfileAgentPresentation.Hint(Profile(true, auth: "external")).Contains("GPT"), "External primary profile falsely promises GPT subagents.");
        var list = new ListBox();
        var fill = typeof(MainWindow).GetMethod("Fill", BindingFlags.Static | BindingFlags.NonPublic)!;
        fill.Invoke(null, [list, new[] { new Choice("same-id", "same-label", mixed) }, "same-id"]);
        fill.Invoke(null, [list, new[] { new Choice("same-id", "same-label", Profile(true, "external_only")) }, "same-id"]);
        Require(list.SelectedItem is Choice { AgentBadge: "하위 에이전트 · 외부 전용" }, "Policy-only refresh kept the old card badge.");
        checks.Add("Profile badges distinguish disabled, mixed, external-only, unselected and pending policies; policy-only refresh updates the selected card.");
    }
}
