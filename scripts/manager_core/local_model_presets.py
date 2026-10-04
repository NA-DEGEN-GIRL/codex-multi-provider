"""Verified model specifications, independent of any local inference server.

These presets do not select an endpoint, discover a served model name, or prove
that a server can provide the checkpoint's context. Output recommendations are
deliberately separate from output limits. All public results are fresh ordinary
dictionaries; callers cannot mutate the shared specifications.
"""
from copy import deepcopy


_VERIFIED_AT = '2026-09-29'
_PRESETS = {
    'qwen3.8-flash-next': {
        'id': 'qwen3.8-flash-next',
        'display_name': 'Qwen3.8-Flash-Next',
        'aliases': ['Qwen 3.8 Flash Next', 'Qwen3.8 Flash Next'],
        'model_id': 'Qwen/Qwen3.8-Flash-Next',
        'verified_at': _VERIFIED_AT,
        'native_context_tokens': 262144,
        'max_context_tokens': 1000000,
        'context_extension_required': True,
        'context_extension': {
            'method': 'yarn', 'factor': 4.0,
            'original_max_position_embeddings': 262144,
            'applied_in_checkpoint': False,
        },
        'default_reasoning_effort': 'max',
        'native_reasoning_effort': 'xhigh',
        'native_reasoning_levels': ['low', 'medium', 'xhigh'],
        'max_output_tokens': None,
        'output_recommendation': {
            'kind': 'separate_reasoning_and_final_budgets',
            'reasoning_tokens': 262144, 'final_tokens': 131072,
            'context_tokens': 1000000,
            'requires_separate_budget_support': True,
        },
        'engine_support': [{
            'engine': 'vllm', 'documented_version': '0.29.0+',
            'build_note': 'The recipe requires the qwen38-flash-next Docker image; a version number alone is insufficient.',
            'tool_call_parser': 'qwen3_coder', 'reasoning_parser': 'qwen3',
            'enable_auto_tool_choice': True,
        }],
        'caveats': [
            'The logical max choice maps to native xhigh; the official template rejects the literal max value.',
            'Current official templates support enable_thinking=false; older engine documentation may claim thinking is always enabled.',
            'The 1,000,000-token limit requires explicit server-side YaRN and context configuration. It is not the native default.',
            'Output lengths are recommendations for engines with separate budgets, not a verified hard output limit.',
            'The hosted Qwen3.8-Flash service and other Qwen3.8 checkpoints are distinct from this exact open-weight model.',
        ],
        'sources': [
            {'title': 'Official model card', 'url': 'https://huggingface.co/Qwen/Qwen3.8-Flash-Next'},
            {'title': 'Official chat template', 'url': 'https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/chat_template.jinja'},
            {'title': 'vLLM deployment recipe', 'url': 'https://recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next'},
        ],
    },
    'deepseek-v4.1-flash': {
        'id': 'deepseek-v4.1-flash',
        'display_name': 'DeepSeek-V4.1-Flash',
        'aliases': ['DeepSeek V4.1 Flash', 'DeepSeek 4.1 Flash'],
        'model_id': 'deepseek-ai/DeepSeek-V4.1-Flash',
        'verified_at': _VERIFIED_AT,
        # "Native" denotes the released checkpoint limit here. Its own config
        # already includes the trained extension from the original 64K range.
        'native_context_tokens': 1048576,
        'max_context_tokens': 1048576,
        'context_extension_required': False,
        'context_extension': {
            'method': 'yarn', 'factor': 16,
            'original_max_position_embeddings': 65536,
            'applied_in_checkpoint': True,
        },
        'default_reasoning_effort': 'max',
        'native_reasoning_effort': 100,
        'native_reasoning_levels': {'minimum': 1, 'maximum': 100},
        'max_output_tokens': None,
        'output_recommendation': {
            'kind': 'minimum_generation_budget',
            'minimum_generation_tokens': 262144,
            'context_tokens': 1048576,
        },
        'engine_support': [{
            'engine': 'vllm', 'documented_version': '0.30.0+',
            'build_note': 'Use a verified nightly containing DeepSeek V4.1 support; the recipe does not promise support from a generic pip installation.',
            'tokenizer_mode': 'deepseek_v41',
            'tool_call_parser': 'deepseek_v41', 'reasoning_parser': 'deepseek_v41',
            'enable_auto_tool_choice': True,
        }],
        'caveats': [
            'Use numeric effort 100 for the logical max choice. Lower named aliases differ between the reference encoder and engine recipes.',
            'The effort prefix is applied in thinking mode at the beginning of a conversation; enabling thinking alone does not request maximum effort.',
            'The release has no Jinja chat template. A compatible DeepSeek encoder/tokenizer and DSML tool parser are required.',
            'The checkpoint already includes YaRN from 65,536 to 1,048,576 positions; do not apply that factor a second time.',
            'The recommended generation budget of at least 256K is not a hard output maximum.',
        ],
        'sources': [
            {'title': 'Official model card', 'url': 'https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash'},
            {'title': 'Official checkpoint configuration', 'url': 'https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/config.json'},
            {'title': 'Official prompt encoding documentation', 'url': 'https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/README.md?code=true'},
            {'title': 'Official protocol conversion library', 'url': 'https://github.com/deepseek-ai/deepseek-recipe'},
            {'title': 'vLLM deployment recipe', 'url': 'https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4.1-Flash'},
        ],
    },
    'glm-5.3-flash': {
        'id': 'glm-5.3-flash',
        'display_name': 'GLM-5.3-Flash',
        'aliases': ['GLM 5.3 Flash'],
        'model_id': 'zai-org/GLM-5.3-Flash',
        'verified_at': _VERIFIED_AT,
        'native_context_tokens': 1048576,
        'max_context_tokens': 1048576,
        'context_extension_required': False,
        'context_extension': None,
        'default_reasoning_effort': 'max',
        'native_reasoning_effort': 'max',
        'native_reasoning_levels': ['low', 'high', 'max'],
        'max_output_tokens': None,
        'output_recommendation': None,
        'engine_support': [{
            'engine': 'vllm', 'documented_version': '0.29.0+',
            'build_note': 'Use a verified GLM-5.3-Flash Docker build; the recipe lists integration and FlashInfer prerequisites.',
            'tool_call_parser': 'glm47', 'reasoning_parser': 'glm47',
            'enable_auto_tool_choice': True,
        }, {
            'engine': 'sglang', 'documented_version': None,
            'build_note': 'Verify the exact supported build; auto parsers resolve to the documented names below.',
            'tool_call_parser': 'glm47', 'reasoning_parser': 'glm45',
        }],
        'caveats': [
            'The official template supports low/high/max, defaults to max, and injects the corresponding effort instruction.',
            'The current official generation template always opens a thinking block. Do not assume a generic thinking=false option disables it.',
            'clear_thinking controls previous reasoning retention, not whether the new response reasons.',
            'A hard output maximum or general output recommendation was not verified. Benchmark-specific generation lengths are not model limits.',
            'Parser names differ by engine: vLLM reasoning glm47, SGLang reasoning glm45; both document tool parser glm47.',
        ],
        'sources': [
            {'title': 'Official model card', 'url': 'https://huggingface.co/zai-org/GLM-5.3-Flash'},
            {'title': 'Official checkpoint configuration', 'url': 'https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/config.json'},
            {'title': 'Official chat template', 'url': 'https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/chat_template.jinja'},
            {'title': 'vLLM deployment recipe', 'url': 'https://recipes.vllm.ai/zai-org/GLM-5.3-Flash'},
            {'title': 'SGLang deployment recipe', 'url': 'https://docs.sglang.io/cookbook/autoregressive/GLM/GLM-5.3-Flash'},
        ],
    },
}


def list_presets():
    """Return all verified presets in stable display order, without I/O."""
    return deepcopy(list(_PRESETS.values()))


def get_preset(preset_id):
    """Return one preset by exact stable ID; reject aliases and unknown models.

    Display aliases and upstream model IDs are metadata, not lookup keys. This
    avoids selecting a different checkpoint when a server uses a similar name.
    Invalid types and unknown values raise ValueError without echoing input.
    """
    if not isinstance(preset_id, str) or preset_id not in _PRESETS:
        raise ValueError('Unknown local model preset ID.')
    return deepcopy(_PRESETS[preset_id])
