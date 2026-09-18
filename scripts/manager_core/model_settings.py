"""Provider capabilities and validated per-profile context/reasoning preferences."""
from copy import deepcopy

EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
DEEPSEEK_EFFORTS = ('none', 'low', 'high', 'max')
DEEPSEEK_ALIASES = {'minimal': 'low', 'medium': 'high', 'xhigh': 'high', 'ultra': 'max'}


def is_deepseek(model):
    return model.get('wire_model_id', model.get('model', '')).lower().startswith(('deepseek-flash', 'deepseek-v4-'))


def supported_efforts(model):
    if is_deepseek(model):
        return list(DEEPSEEK_EFFORTS)
    values = model.get('capabilities', {}).get('reasoning_efforts')
    if values is None:
        values = [x['effort'] for x in model.get('catalog', {}).get('supported_reasoning_levels', [])]
    values = values or [model.get('reasoning_effort', 'high')]
    if not isinstance(values, list) or not values or any(v not in EFFORTS for v in values):
        raise ValueError('지원 추론 강도 목록이 올바르지 않습니다.')
    return list(dict.fromkeys(values))


def normalize_effort(model, effort):
    if is_deepseek(model):
        effort = DEEPSEEK_ALIASES.get(effort, effort)
    if effort not in supported_efforts(model):
        raise ValueError('선택한 모델이 지원하는 추론 강도를 선택하세요.')
    return effort


def context_limit(model):
    return model.get('capabilities', {}).get('context_window',
        model.get('catalog', {}).get('context_window', 1048576 if is_deepseek(model) else 32768))


def resolve(model, settings=None):
    if settings is None:
        settings = {}
    if not isinstance(settings, dict) or set(settings) - {'reasoning_effort', 'context_window', 'auto_compact_percent'}:
        raise ValueError('외부 모델 설정 항목을 확인하세요.')
    maximum = context_limit(model)
    context = settings.get('context_window', maximum)
    percent = settings.get('auto_compact_percent', model.get('auto_compact_percent', 90))
    if type(context) is not int or not 4096 <= context <= maximum:
        raise ValueError(f'컨텍스트는 4,096~{maximum:,} 토큰 범위로 입력하세요.')
    # The native runtime caps auto-compaction at 90% of the raw context window.
    if type(percent) is not int or not 10 <= percent <= 90:
        raise ValueError('자동 압축 기준은 컨텍스트의 10~90%로 설정하세요.')
    return dict(reasoning_effort=normalize_effort(model, settings.get('reasoning_effort', model['reasoning_effort'])),
                context_window=context, auto_compact_percent=percent)


def configured(model, settings=None):
    result = deepcopy(model)
    value = resolve(model, settings)
    result.update(reasoning_effort=value['reasoning_effort'], reasoning=value['reasoning_effort'],
                  forced_reasoning_effort=None, auto_compact_percent=value['auto_compact_percent'])
    result.setdefault('capabilities', {})['context_window'] = value['context_window']
    return result


def render_options(profile):
    options = {}
    if profile.get('auth_mode') == 'external':
        options.update(primary_model_id=profile['external_model_id'], primary_settings=profile.get('external_settings'))
    if profile.get('policy', {}).get('selection_mode') == 'external_only':
        options['selection_mode'] = 'external_only'
    return options
