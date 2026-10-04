using System.Text.Json;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

internal static class LocalModelSelfTest
{
    internal static List<string> Run()
    {
        static void Require(bool condition, string message)
        { if (!condition) throw new InvalidOperationException(message); }
        using var document = JsonDocument.Parse("""
        {
          "local_presets": [
            {"id":"qwen3.8-flash-next","display_name":"Qwen3.8-Flash-Next","model_id":"Qwen/Qwen3.8-Flash-Next",
             "native_context_tokens":262144,"max_context_tokens":1000000,"context_extension_required":true,
             "default_reasoning_effort":"max","native_reasoning_effort":"xhigh","supported_reasoning_efforts":["low","medium","xhigh"]},
            {"id":"deepseek-v4.1-flash","display_name":"DeepSeek-V4.1-Flash","model_id":"deepseek-ai/DeepSeek-V4.1-Flash",
             "native_context_tokens":1048576,"max_context_tokens":1048576,"context_extension_required":false,
             "default_reasoning_effort":"max","native_reasoning_effort":100,"supported_reasoning_efforts":["max"]},
            {"id":"glm-5.3-flash","display_name":"GLM-5.3-Flash","model_id":"zai-org/GLM-5.3-Flash",
             "native_context_tokens":1048576,"max_context_tokens":1048576,"context_extension_required":false,
             "default_reasoning_effort":"max","native_reasoning_effort":"max","supported_reasoning_efforts":["low","high","max"]}
          ],
          "providers":[
            {"id":"local","name":"Local server","deployment":"local","auth_type":"none","execution_scope":"local","credentials_ready":true,"key_saved":false,"adapter_available":true},
            {"id":"cloud","name":"Cloud API","auth_type":"api_key","key_saved":true,"adapter_available":true},
            {"id":"locked","name":"Missing credential","deployment":"local","credentials_ready":false,"key_saved":true,"adapter_available":true},
            {"id":"unsupported","name":"No adapter","deployment":"local","credentials_ready":true,"adapter_available":false}
          ],
          "models":[
            {"id":"local-model","provider_id":"local","verified":true,"local_preset_id":"qwen3.8-flash-next","reasoning_effort":"xhigh","settings_defaults":{"context_window":1000000}},
            {"id":"cloud-model","provider_id":"cloud","verified":true},
            {"id":"locked-model","provider_id":"locked","verified":true},
            {"id":"unsupported-model","provider_id":"unsupported","verified":true},
            {"id":"unverified-model","provider_id":"local","verified":false}
          ]
        }
        """);
        var registry = document.RootElement;
        var body = new StackPanel();
        var editor = new Dialogs.ProviderEditor(body, null, default, registry, localFirst: true);
        Require((editor.Deployment.SelectedItem as Choice)?.Id == "local" && (editor.Auth.SelectedItem as Choice)?.Id == "none"
            && (editor.Scope.SelectedItem as Choice)?.Id == "local" && (editor.Protocol.SelectedItem as Choice)?.Id == "responses" && editor.Url.Text == "",
            "A new local connection must require an explicit endpoint and default to no key, Windows only and Responses.");
        Require(!editor.TryBuild(out _, out _), "The local wizard guessed a usable endpoint.");
        editor.Url.Text = "http://127.0.0.1:54321/v1";
        foreach (var preset in registry.Arr("local_presets"))
        {
            editor.Presets.SelectedItem = editor.Presets.Items.OfType<Choice>().Single(choice => choice.Id == preset.S("id"));
            int maximum = LocalModelPresentation.MaximumContext(preset);
            Require(editor.Context.IsReadOnly && editor.Context.Text == maximum.ToString() && editor.Supported.IsReadOnly &&
                (editor.Effort.SelectedItem as Choice)?.Id == LocalModelPresentation.NativeEffort(preset) &&
                ((Choice)editor.Effort.SelectedItem).Label.StartsWith("최대 (max)", StringComparison.Ordinal),
                "The local preset did not select its full context and highest native effort: " + preset.S("id"));
            Require(editor.Effort.Items.OfType<Choice>().Select(choice => choice.Id).SequenceEqual(LocalModelPresentation.PresetEfforts(preset)),
                "The local preset offered unsupported named effort values.");
            editor.ModelId.Text = "exact-served-alias";
            Require(editor.TryBuild(out var form, out var error), "Local preset form failed: " + error);
            var request = JsonSerializer.SerializeToElement(form);
            Require(request.Get("provider").S("base_url") == editor.Url.Text && request.Get("provider").S("auth_type") == "none" &&
                request.Get("model").S("wire_model_id") == "exact-served-alias" && request.Get("model").S("local_preset_id") == preset.S("id") &&
                request.Get("model").Get("capabilities").N("context_window") == maximum &&
                request.Get("model").S("reasoning_effort") == LocalModelPresentation.NativeEffort(preset),
                "Preset save changed the user's served ID/endpoint or omitted its actual enum and maximum.");
            editor.Context.Text = "32768";
            Require(!editor.TryBuild(out _, out _) && !LocalModelPresentation.ReadContext("32768", maximum, true, out _),
                "A preset context was silently lowered at registration or in profile settings.");
            editor.Context.Text = maximum.ToString();
        }
        editor.Scope.SelectedItem = editor.Scope.Items.OfType<Choice>().Single(choice => choice.Id == "all_hosts");
        editor.Auth.SelectedItem = editor.Auth.Items.OfType<Choice>().Single(choice => choice.Id == "api_key");
        Require(editor.TryBuild(out var shared, out _) && JsonSerializer.SerializeToElement(shared).Get("provider").S("execution_scope") == "all_hosts"
            && JsonSerializer.SerializeToElement(shared).Get("provider").S("auth_type") == "api_key", "Explicit SSH scope or API-key authentication was lost.");
        var manual = new Dialogs.ProviderEditor(new StackPanel(), null, default, default, localFirst: true);
        manual.Name.Text = "Manual local"; manual.Url.Text = "http://127.0.0.1:54321/v1"; manual.ModelId.Text = "served-model";
        Require((manual.Effort.SelectedItem as Choice)?.Id == "max" && manual.Context.Text == "" && !manual.TryBuild(out _, out _),
            "Manual local registration invented a context window or lowered its new-model effort default.");
        manual.Context.Text = "65536";
        Require(manual.TryBuild(out var manualForm, out _) && JsonSerializer.SerializeToElement(manualForm).Get("model").Get("capabilities").N("context_window") == 65536,
            "Manual local registration did not preserve the server capacity supplied by the user.");
        var cloud = new Dialogs.ProviderEditor(new StackPanel(), null, default, registry);
        Require((cloud.Auth.SelectedItem as Choice)?.Id == "api_key" && (cloud.Effort.SelectedItem as Choice)?.Id == "high" &&
            cloud.Context.Text == "" && !cloud.Context.IsReadOnly, "Local defaults changed the ordinary cloud registration form.");
        var savedCloud = registry.Arr("providers").First(provider => provider.S("id") == "cloud");
        var returning = new Dialogs.ProviderEditor(new StackPanel(), new Choice("cloud", "Cloud API", savedCloud), default, registry);
        returning.UseExisting.IsChecked = false;
        returning.Deployment.SelectedItem = returning.Deployment.Items.OfType<Choice>().Single(choice => choice.Id == "local");
        returning.Presets.SelectedItem = returning.Presets.Items.OfType<Choice>().First(choice => choice.Id != "");
        returning.UseExisting.IsChecked = true;
        Require((returning.Deployment.SelectedItem as Choice)?.Id == "cloud" && !returning.Context.IsReadOnly &&
            (returning.Auth.SelectedItem as Choice)?.Id == "api_key", "Reselecting the existing cloud connection retained local-only preset controls.");
        Require(LocalModelPresentation.AvailableModels(registry, true).Select(model => model.S("id")).SequenceEqual(["local-model"])
            && LocalModelPresentation.AvailableModels(registry, false).Select(model => model.S("id")).SequenceEqual(["cloud-model"]),
            "Profile selection mixed local/cloud models, rejected a ready keyless server, or admitted an unready connection.");
        var modelForSettings = LocalModelPresentation.ForSettings(registry, registry.Arr("models").First());
        Require(LocalModelPresentation.IsLocalModel(modelForSettings) && LocalModelPresentation.MaximumContext(modelForSettings.Get("local_preset")) == 1000000,
            "Profile settings lost the saved local preset's full-context specification.");
        var provider = registry.Arr("providers").First();
        Require(!LocalModelPresentation.ProviderLabel(provider).Contains("키 필요", StringComparison.Ordinal), "A no-auth local server still asks for an API key.");
        var localProfile = JsonSerializer.SerializeToElement(new { id = "local", alias = "local", auth_mode = "external", external_deployment = "local", external_model_name = "Qwen", status = "running" });
        Require(ProfileCardData.Create(localProfile, "local").ProviderBadge == "로컬", "A local profile has a cloud API badge.");
        var shortcut = ShortcutCardData.Create(JsonSerializer.SerializeToElement(new { alias = "local task", thread_id = "task" }), localProfile,
            new Dictionary<string, (string State, string Alias)>(), []);
        Require(shortcut.Agent.StartsWith("로컬", StringComparison.Ordinal) && !shortcut.Detail.Contains("외부 API", StringComparison.Ordinal),
            "The task shortcut describes a local model as a cloud API.");
        return ["All three local presets select full maximum context and native maximum effort while preserving explicit endpoint and served model ID.",
            "Local preset registration and profile settings reject reduced context; auth and SSH execution scope remain explicit.",
            "Manual local models require explicit context capacity and default to max, while ordinary cloud defaults stay unchanged.",
            "Local/cloud profile choices use credential readiness, including keyless local servers, and exclude unverified or unsupported connections.",
            "Local profile and task labels distinguish local servers from cloud APIs and do not ask keyless servers for credentials."];
    }
}
