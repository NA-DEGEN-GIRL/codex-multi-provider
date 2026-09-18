"""Expose the configured external model's efforts instead of GPT UI defaults."""

def patch(data):
    marker = b'isCustomModelProvider:s=!1,models:c,useHiddenModels:l})'
    if marker not in data:
        if b'enabled-reasoning-efforts' in data:
            raise ValueError('External reasoning picker is not verified for this desktop version.')
        return data
    before = b'let e=o?r.supportedReasoningEfforts:r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`)'
    gates = [b'.filter(({reasoningEffort:e})=>' + name + b'(e)&&i.has(e))' for name in (b'gj', b'WXn')]
    found = [gate for gate in gates if gate in data]
    if data.count(marker) != 1 or data.count(before) != 1 or len(found) != 1 or data.count(found[0]) != 1:
        raise ValueError('External reasoning picker is ambiguous.')
    gate = found[0]
    return data.replace(before, before.replace(b'let e=o?', b'let e=o||s?')).replace(
        gate, gate.replace(b'&&i.has(e)', b'&&(s||i.has(e))'))
