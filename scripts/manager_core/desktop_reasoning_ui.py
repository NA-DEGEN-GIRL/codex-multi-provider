"""Expose the configured external model's efforts instead of GPT UI defaults."""
import re

def patch(data):
    marker = b'isCustomModelProvider:s=!1,models:c,useHiddenModels:l})'
    if marker not in data:
        if b'enabled-reasoning-efforts' in data:
            raise ValueError('External reasoning picker is not verified for this desktop version.')
        return data
    before = b'let e=o?r.supportedReasoningEfforts:r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`)'
    gates = [b'.filter(({reasoningEffort:e})=>' + name + b'(e)&&i.has(e))' for name in (b'gj', b'WXn', b'UXn', b'vw', b'pye')]
    found = [gate for gate in gates if gate in data]
    if not found:
        raise ValueError('External reasoning picker is not verified for this desktop version.')
    if data.count(marker) != 1 or data.count(before) != 1 or len(found) != 1 or data.count(found[0]) != 1:
        raise ValueError('External reasoning picker is ambiguous.')
    gate = found[0]
    # Ultracode is a Claude CLI workflow mode advertised as an effort choice.
    # Keep the desktop's native-model validation and enable only this additional
    # value for a configured provider that explicitly advertises it.
    replacement = gate.replace(b'=>', b'=>(').replace(
        b'&&i.has(e)', b'||s&&e===`ultracode`)&&(s||i.has(e))')
    return data.replace(before, before.replace(b'let e=o?', b'let e=o||s?')).replace(
        gate, replacement)


def upgrade_managed(data):
    """Upgrade only the audited 26.917 manager picker, without unpatching it."""
    marker = b'isCustomModelProvider:s=!1,models:c,useHiddenModels:l})'
    before = b'let e=o||s?r.supportedReasoningEfforts:r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`)'
    gate = b'.filter(({reasoningEffort:e})=>vw(e)&&(s||i.has(e)))'
    replacement = b'.filter(({reasoningEffort:e})=>(vw(e)||s&&e===`ultracode`)&&(s||i.has(e)))'
    if any(data.count(value) != 1 for value in (marker, before, gate)) or replacement in data:
        raise ValueError('Managed reasoning picker is not the verified 26.917 baseline.')
    return data.replace(gate, replacement)


def patch_composer(data):
    """Keep a catalog-advertised Ultracode selection and render its label."""
    marker = b'reasoningEffort!==`persistent`'
    if marker not in data:
        return data
    pattern = rb'supportedReasoningEfforts\.filter\(e=>([A-Za-z_$][A-Za-z0-9_$]*)\(e.reasoningEffort\)&&e.reasoningEffort!==`persistent`\)'
    matches = list(re.finditer(pattern, data))
    if len(matches) != 1 or data.count(marker) != 1:
        raise ValueError('Desktop composer effort choices are not verified for this version.')
    match = matches[0]
    validator = match.group(1)
    selection = b'return ' + validator + b'(e)&&t.some(t=>t.reasoningEffort===e)?e:'
    label = b'persistent:{id:`composer.mode.local.reasoning.persistent.label`'
    if data.count(selection) != 1 or data.count(label) != 1:
        raise ValueError('Desktop composer effort selection or labels are not verified for this version.')
    choices = match.group(0).replace(b'e=>', b'e=>(').replace(
        b'&&e.reasoningEffort', b'||e.reasoningEffort===`ultracode`)&&e.reasoningEffort')
    selected = selection.replace(b'return ', b'return (').replace(
        b'&&t.some', b'||e===`ultracode`)&&t.some')
    return data.replace(match.group(0), choices).replace(selection, selected).replace(label,
        b'ultracode:{id:`composer.mode.local.reasoning.ultracode.label`,defaultMessage:`Ultracode`,'
        b'description:`Claude Code dynamic workflows with xhigh effort`},' + label)
