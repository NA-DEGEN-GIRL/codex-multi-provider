using System.Globalization;
using System.Text.Json;

namespace Codex.ControlCenter.Shell;

internal static class LocalModelPresentation
{
    internal static bool IsLocalProvider(JsonElement provider) => provider.S("deployment") == "local";
    internal static bool IsLocalModel(JsonElement model) => model.S("deployment") == "local" || model.S("local_preset_id") != "";
    internal static bool IsLocalProfile(JsonElement profile) => profile.S("external_deployment") == "local"
        || profile.Get("external_provider").S("deployment") == "local" || profile.Get("external_settings").S("local_preset_id") != "";
    internal static bool CredentialsReady(JsonElement provider) => provider.Get("credentials_ready").ValueKind is JsonValueKind.True or JsonValueKind.False
        ? provider.B("credentials_ready") : provider.B("key_saved");
    internal static string ProviderLabel(JsonElement provider) => provider.S("name") + (IsLocalProvider(provider) ? " · 로컬" : " · 클라우드")
        + (provider.S("auth_type") == "none" ? " · 키 없이 연결" : provider.B("key_saved") ? " · 키 저장됨" : " · 키 필요");
    internal static IEnumerable<JsonElement> AvailableModels(JsonElement registry, bool local) => registry.Arr("models")
        .Where(model => model.B("verified") || model.Get("capabilities").B("verified"))
        .Where(model => registry.Arr("providers").Any(provider => provider.S("id") == model.S("provider_id")
            && IsLocalProvider(provider) == local && CredentialsReady(provider) && provider.B("adapter_available")));
    internal static JsonElement Preset(JsonElement registry, JsonElement model) => registry.Arr("local_presets")
        .FirstOrDefault(preset => preset.S("id") == model.S("local_preset_id"));
    internal static JsonElement ForSettings(JsonElement registry, JsonElement model)
    {
        var provider = registry.Arr("providers").FirstOrDefault(p => p.S("id") == model.S("provider_id"));
        var values = model.EnumerateObject().ToDictionary(property => property.Name, property => property.Value);
        values["deployment"] = JsonSerializer.SerializeToElement(provider.S("deployment", "cloud"));
        var preset = Preset(registry, model);
        if (IsLocalProvider(provider) && model.S("local_preset_id") != "" && preset.ValueKind != JsonValueKind.Object)
            throw new InvalidOperationException("로컬 모델의 프리셋 사양을 읽지 못했습니다. 모델 연결 목록을 다시 확인하세요.");
        if (preset.ValueKind == JsonValueKind.Object) values["local_preset"] = preset;
        return JsonSerializer.SerializeToElement(values);
    }
    internal static string NativeEffort(JsonElement preset)
    {
        var value = preset.Get("native_reasoning_effort");
        return value.ValueKind == JsonValueKind.Number ? "max" : value.ValueKind == JsonValueKind.String ? value.GetString()! : "max";
    }
    internal static string[] PresetEfforts(JsonElement preset)
    {
        var declared = preset.Arr("supported_reasoning_efforts").Select(value => value.GetString()!).ToArray();
        if (declared.Length > 0) return declared;
        if (NativeEffort(preset) == "xhigh") return ["low", "medium", "xhigh"];
        if (preset.Get("native_reasoning_effort").ValueKind == JsonValueKind.Number) return ["max"];
        return preset.S("model_id").Contains("glm", StringComparison.OrdinalIgnoreCase) ? ["low", "high", "max"] : ["max"];
    }
    internal static int MaximumContext(JsonElement preset) => preset.Get("max_context_tokens").ValueKind == JsonValueKind.Number
        && preset.Get("max_context_tokens").TryGetInt32(out var value) && value >= 4096 ? value : 0;
    internal static string EffortLabel(string value, JsonElement preset = default) => value == NativeEffort(preset)
        ? "최대 (max) · 서버 " + (preset.Get("native_reasoning_effort").ValueKind == JsonValueKind.Number ? preset.Get("native_reasoning_effort").ToString() : value)
        : value switch { "none" => "끄기 · none", "low" => "낮음 · low", "medium" => "중간 · medium", "high" => "높음 · high", _ => value };
    internal static string PresetDetail(JsonElement preset)
    {
        if (preset.ValueKind != JsonValueKind.Object) return "서버가 지원하는 컨텍스트 한도와 추론 강도를 입력하세요.";
        var detail = $"최대 컨텍스트 {MaximumContext(preset):N0} 토큰 · 기본 {preset.N("native_context_tokens"):N0} 토큰\n"
            + (preset.B("context_extension_required") ? "서버에서 컨텍스트 확장 설정이 필요합니다. " : "")
            + $"서버도 최대 {MaximumContext(preset):N0} 토큰을 처리하도록 설정하세요. 모델 ID는 서버가 실제 제공하는 값으로 수정할 수 있습니다.";
        return detail;
    }
    internal static string PresetReferences(JsonElement preset)
    {
        var caveats = preset.Get("caveats");
        var detail = caveats.ValueKind == JsonValueKind.String ? caveats.GetString() ?? ""
            : string.Join("\n", caveats.Items().Where(item => item.ValueKind == JsonValueKind.String).Select(item => item.GetString()));
        var sources = preset.Arr("sources").Select(source => source.ValueKind == JsonValueKind.String ? source.GetString()
            : source.S("title") + " · " + source.S("url"));
        return string.Join("\n", new[] { detail }.Concat(sources).Where(value => !string.IsNullOrWhiteSpace(value)));
    }
    internal static bool ReadContext(string text, int maximum, bool fullContext, out int tokens) =>
        int.TryParse(text.Replace(",", ""), NumberStyles.Integer, CultureInfo.InvariantCulture, out tokens)
        && tokens >= 4096 && tokens <= maximum && (!fullContext || tokens == maximum);
}
