"""Expose the configured external model's efforts instead of GPT UI defaults."""
import re

PICKER_MARKER = b'isCustomModelProvider:s=!1,models:c,useHiddenModels:l})'


def patch(data):
    marker = PICKER_MARKER
    if marker not in data:
        if b'enabled-reasoning-efforts' in data:
            raise ValueError('External reasoning picker is not verified for this desktop version.')
        return data
    before = b'let e=o?r.supportedReasoningEfforts:r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`)'
    gates = [b'.filter(({reasoningEffort:e})=>' + name + b'(e)&&i.has(e))' for name in (b'gj', b'WXn', b'UXn', b'vw', b'pye', b'zve', b'kve')]
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
    return _power_choices(data.replace(before, before.replace(b'let e=o?', b'let e=o||s?')).replace(
        gate, replacement))


def _power_choices(data):
    """Admit a catalog-advertised Ultracode in the composer's model/effort dropdown.

    The dropdown builds its rows from the already filtered model list, not from
    the picker gate above, so it needs the same single additional value.
    """
    pattern = rb'a=r\.flatMap\(\(\{reasoningEffort:e\}\)=>([A-Za-z_$][A-Za-z0-9_$]*)\(e\)&&e!==`persistent`\?\[e\]:\[\]\)'
    matches = list(re.finditer(pattern, data))
    if len(matches) != 1:
        raise ValueError('Desktop effort dropdown is not verified for this version.')
    found = matches[0].group(0)
    validator = matches[0].group(1)
    return data.replace(found, found.replace(b'=>' + validator + b'(e)&&',
                                             b'=>(' + validator + b'(e)||e===`ultracode`)&&'))


def upgrade_managed(data):
    """Upgrade only the audited 26.917 manager picker, without unpatching it."""
    marker = b'isCustomModelProvider:s=!1,models:c,useHiddenModels:l})'
    before = b'let e=o||s?r.supportedReasoningEfforts:r.supportedReasoningEfforts.filter(({reasoningEffort:e})=>e!==`ultra`)'
    gate = b'.filter(({reasoningEffort:e})=>vw(e)&&(s||i.has(e)))'
    replacement = b'.filter(({reasoningEffort:e})=>(vw(e)||s&&e===`ultracode`)&&(s||i.has(e)))'
    if any(data.count(value) != 1 for value in (marker, before, gate)) or replacement in data:
        raise ValueError('Managed reasoning picker is not the verified 26.917 baseline.')
    return _power_choices(data.replace(gate, replacement))


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
    # The compact composer renders an icon per effort; Ultracode uses the xhigh icon.
    icons = list(re.finditer(rb'\{none:([A-Za-z_$][A-Za-z0-9_$]*),minimal:\1,low:[A-Za-z_$][A-Za-z0-9_$]*,'
                             rb'medium:[A-Za-z_$][A-Za-z0-9_$]*,high:[A-Za-z_$][A-Za-z0-9_$]*,'
                             rb'xhigh:([A-Za-z_$][A-Za-z0-9_$]*),max:\2,ultra:\2,persistent:\2\}', data))
    if len(icons) != 1:
        raise ValueError('Desktop composer effort icons are not verified for this version.')
    icon = icons[0]
    return data.replace(match.group(0), choices).replace(selection, selected).replace(label,
        b'ultracode:{id:`composer.mode.local.reasoning.ultracode.label`,defaultMessage:`Ultracode`,'
        b'description:`Claude Code dynamic workflows with xhigh effort`},' + label).replace(
        icon.group(0), icon.group(0)[:-1] + b',ultracode:' + icon.group(2) + b'}')
