"""Explicit checkpoint presets; never infer a local server's capacity from its name."""
from copy import deepcopy

from .local_model_presets import get_preset


def supported_efforts(preset_id):
    get_preset(preset_id)
    return {'qwen3.8-flash-next': ['low', 'medium', 'xhigh'],
            'deepseek-v4.1-flash': ['max'],
            'glm-5.3-flash': ['low', 'high', 'max']}[preset_id]


def apply_preset(data, provider, previous=None):
    result = deepcopy(data)
    supplied = result.get('capabilities') or {}
    if not isinstance(supplied, dict):
        raise ValueError('모델 기능 설정을 확인하세요.')
    preset_id = result.get('local_preset_id', (previous or {}).get('local_preset_id'))
    if not preset_id:
        if (previous or {}).get('local_preset_id'):
            raise ValueError('기존 로컬 모델의 사양 선택을 지울 수 없습니다. 새 모델 연결을 등록하세요.')
        caps = {**(previous or {}).get('capabilities', {}), **supplied}
        if caps.get('local_model') or caps.get('local_preset_id'):
            raise ValueError('로컬 모델의 사양을 목록에서 선택하세요.')
        if provider.get('deployment') == 'local':
            if type(caps.get('context_window')) is not int:
                raise ValueError('로컬 모델의 실제 최대 컨텍스트를 입력하거나 사양 프리셋을 선택하세요.')
            if not previous and 'reasoning_effort' not in result and 'reasoning' not in result:
                result['reasoning_effort'] = 'max'
        return result
    if provider.get('deployment') != 'local':
        raise ValueError('로컬 모델 사양은 로컬 연결에서만 선택할 수 있습니다.')
    preset = get_preset(preset_id)
    maximum = preset['max_context_tokens']
    if 'context_window' in supplied and (type(supplied['context_window']) is not int
                                       or supplied['context_window'] != maximum):
        raise ValueError(f'이 모델은 최대 컨텍스트 {maximum:,} 토큰으로 설정합니다. 서버 설정을 확인하세요.')
    efforts = supported_efforts(preset_id)
    caps = {**deepcopy((previous or {}).get('capabilities', {})), **supplied}
    caps.update(local_model=True, local_preset_id=preset_id,
                published_max_context=maximum, context_window=maximum,
                reasoning_efforts=efforts, context_extension_required=preset['context_extension_required'])
    # A shorter loaded server limit is evidence to fix the server, not permission
    # to silently shrink the user's requested model window.
    loaded = caps.get('server_context_window')
    if loaded is not None and (type(loaded) is not int or loaded < maximum):
        raise ValueError(f'서버 컨텍스트를 {maximum:,} 토큰 이상으로 설정한 뒤 등록하세요. 자동 축소하지 않습니다.')
    if not previous or previous.get('local_preset_id') != preset_id:
        caps['verified'] = False
    effort = result.get('reasoning_effort', result.get('reasoning',
        (previous or {}).get('reasoning_effort', 'max')))
    if preset_id == 'qwen3.8-flash-next' and effort == 'max':
        effort = 'xhigh'
    result.update(local_preset_id=preset_id, capabilities=caps, reasoning_effort=effort)
    return result


def effort_aliases(model):
    return {'max': 'xhigh'} if model.get('local_preset_id') == 'qwen3.8-flash-next' else {}
