"""Bounded JSONL v1 and normalization of public Claude stream-json messages."""
import json
import math


PROTOCOL = 1
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MAX_COMPACT_SUMMARY_BYTES = 20000


class ProtocolError(ValueError):
    pass


def read_message(stream):
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError('JSONL message exceeds the size limit.')
    try:
        message = json.loads(line.decode('utf-8') if isinstance(line, bytes) else line)
    except (ValueError, UnicodeError) as error:
        raise ProtocolError('Invalid UTF-8 JSONL message.') from error
    if not isinstance(message, dict) or not isinstance(message.get('type'), str):
        raise ProtocolError('JSONL message must be an object with a type.')
    return message


def encode_message(message):
    line = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError('JSONL message exceeds the size limit.')
    return line


def bounded_text(value, limit=MAX_TEXT):
    if not isinstance(value, str):
        return ''
    return value if len(value) <= limit else value[:limit] + '\n[Output truncated by Claude bridge.]'


def compact_summary_fits(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode('utf-8')) <= MAX_COMPACT_SUMMARY_BYTES
    except UnicodeError:
        return False


def usage_fields(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0:
            result[key] = number
    creation = value.get('cache_creation')
    if isinstance(creation, dict):
        result['cache_creation'] = {key: value for key, value in creation.items()
                                    if key in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')
                                    and isinstance(value, int) and value >= 0}
    return result


def _object(value):
    return value if isinstance(value, dict) else {}


def normalize(message):
    """Return display events only; never return raw init/settings/auth output."""
    kind = message.get('type')
    parent = message.get('parent_tool_use_id')
    if kind == 'system':
        subtype = message.get('subtype')
        if subtype == 'init':
            return [dict(kind='init', model=bounded_text(message.get('model'), 160),
                         permission_mode=bounded_text(message.get('permissionMode'), 80))]
        if subtype == 'compact_boundary':
            metadata = _object(message.get('compact_metadata'))
            return [dict(kind='compact', message='Claude compacted its session; a portable summary has not been received.',
                         summary_available=False, trigger=metadata.get('trigger') if metadata.get('trigger') in ('auto', 'manual') else None,
                         pre_tokens=metadata.get('pre_tokens') if isinstance(metadata.get('pre_tokens'), int) else None)]
        if subtype == 'api_retry':
            return [dict(kind='retry', message='Claude is retrying a request.',
                         attempt=message.get('attempt') if isinstance(message.get('attempt'), int) else None)]
        if subtype == 'task_started' and message.get('task_type') == 'local_workflow' and not message.get('ambient'):
            name = bounded_text(message.get('workflow_name') or message.get('description'), 200)
            return [dict(kind='subagent', message='Claude started a background workflow' + (f': {name}' if name else '.'))]
        if subtype == 'task_notification' and message.get('status') in ('completed', 'failed', 'stopped'):
            summary = bounded_text(message.get('summary'), 400)
            return [dict(kind='subagent', message=f"Claude background task {message['status']}"
                         + (f': {summary}' if summary else '.'))]
        return []
    if kind == 'stream_event':
        event = _object(message.get('event'))
        if parent:
            return []
        if event.get('type') == 'content_block_delta':
            delta = _object(event.get('delta'))
            if delta.get('type') == 'text_delta':
                return [dict(kind='text_delta', text=bounded_text(delta.get('text')))]
            if delta.get('type') == 'thinking_delta':
                return [dict(kind='reasoning', text=bounded_text(delta.get('thinking')))]
        return []
    if kind in ('assistant', 'user'):
        content = _object(message.get('message')).get('content', [])
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list):
            content = []
        events = []
        if kind == 'user' and (message.get('isCompactSummary') is True or message.get('is_compact_summary') is True):
            summary = '\n'.join(block.get('text', '') for block in content
                                if isinstance(block, dict) and block.get('type') == 'text'
                                and isinstance(block.get('text'), str))
            if summary:
                if not compact_summary_fits(summary):
                    return [dict(kind='compact', summary_available=False,
                                 message='Claude supplied a compaction hint beyond the portable summary limit; the original history is retained.')]
                return [dict(kind='compact', summary=summary, summary_available=True,
                             message='Claude supplied a portable compaction summary.')]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            block_type = block.get('type')
            if block_type == 'text' and kind == 'assistant' and not parent:
                events.append(dict(kind='text', text=bounded_text(block.get('text')), id=message.get('uuid')))
            elif (block_type == 'tool_use' and isinstance(block.get('id'), str)
                  and isinstance(block.get('name'), str) and isinstance(block.get('input'), dict)):
                events.append(dict(kind='tool_start', id=block.get('id'), tool=block.get('name'),
                                   input=block.get('input') or {}, parent_tool_use_id=parent))
            elif block_type == 'tool_result':
                output = block.get('content', '')
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                events.append(dict(kind='tool_end', id=block.get('tool_use_id'),
                                   output=bounded_text(output), is_error=bool(block.get('is_error')),
                                   parent_tool_use_id=parent))
        if kind == 'user' and not any(e['kind'] == 'tool_end' for e in events):
            events.append(dict(kind='user_ack'))
        return events
    if kind == 'rate_limit_event':
        info = _object(message.get('rate_limit_info'))
        event = dict(kind='rate_limit', message='Claude usage limit status changed.')
        if info.get('status') in ('allowed', 'allowed_warning', 'rejected'):
            event['status'] = info['status']
        if info.get('errorCode') == 'credits_required':
            event['errorCode'] = 'credits_required'
        for key in ('resetsAt', 'utilization'):
            value = info.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                event[key] = value
        return [event]
    return []
