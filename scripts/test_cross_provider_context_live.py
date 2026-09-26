"""Cross-provider portable context: GPT compaction -> external API model -> GPT.

One disposable shared record home and disposable CODEX_HOMEs:

  1. G (OpenAI family, shared execution, its config also defines the external
     provider so the portable-summary gate is on) starts a paginated thread,
     plants two synthetic facts, then asks for a one-line acknowledgement.
     In the summary-path scenario G gets one more turn that carries only
     synthetic filler text and asks for a one-word reply.
  2. G compacts the thread with thread/compact/start. The checkpoint must carry
     origin openai, a ready portable summary and native encrypted state.
  3. X (the external profile's provider) resumes the same thread and asks for the
     facts. X talks to its provider through a 127.0.0.1 recording proxy that
     forwards bytes unchanged and keeps only a structural summary of each request
     body; headers (Authorization included) are forwarded, never stored.
  4. G resumes again and asks what the other model was asked.
  5. Cross-account mode only: G2, a second ChatGPT account, resumes a fork of
     the thread taken right after compaction and replays G's native encrypted
     compaction (see CROSS-ACCOUNT MODE).

SCENARIOS
- Full transcript (default): the conversation is small, so X is given the whole
  transcript (rebuild step 2); the portable summary is produced but not used.
- Summary path (--summary-path, or --external-context-window / --padding-tokens):
  X's scratch config and model binding declare a context window of N tokens
  (default 32,768) with auto-compaction at 90% of it, the highest the runtime
  honours; X runs one turn inside a rebuilt context sized below that limit, so
  it does not compact (checked). Before compaction G gets a filler turn of about
  N tokens (common single-token English words; by default sized so its rebuild
  estimate is 1.25x the window). The runtime sizes rebuilds conservatively
  (portable_context/budget.rs: ASCII 3 bytes/token, other text 2 bytes/token)
  against min(90%, 95%) of the window minus the base instructions, an eighth for
  tools (at most 8,000), a tenth for the reply (rebuild.rs) and the largest
  context bundle: at most 22,856 tokens for 32,768, and about 13.4k after X's
  ~7k-token instructions (the runtime logs it; the report keeps it). The default
  filler (18,500 words, rebuild estimate ~42k) is 1.28x the window and about 3x
  that budget, so X must be rebuilt from the portable summary: step 3 (summary +
  newest user messages; the filler takes the whole user-message allowance, so
  the planted facts reach X only through the summary) or step 4 (with a context
  note). The filler is capped so G's own resume still fits its native
  checkpoint (step 1) on ~100k-window GPT models. --plan-only prints the
  expected requests and approximate input tokens and exits.

NEGATIVE CONTROL (--no-summary-gate, fixture only): G's config omits the
external provider, so compaction writes no portable summary. The run reports
NEGATIVE_CONTROL_OK when exactly the expected checks fail: the summary checks,
plus in the summary-path scenario X's summary and fact checks (X gets only
recent items and a context note).

FIXTURE MODE (the default) is offline: both providers are one loopback fixture
whose replies are derived only from what each request carried. No credential is
read and nothing leaves 127.0.0.1. --fixture-second-account accept|reject runs
the G2 step against a fixture that accepts or rejects another account's
encrypted items.

LIVE MODE MAKES REAL, BILLABLE MODEL REQUESTS on the ChatGPT account of one
registered profile and the API key of one external profile: 6 requests in the
full-transcript scenario, 7 in the summary-path scenario (8 with G2). It runs
only with --live --confirm-real-model-calls and both profile ids, and prints its
plan (requests, approximate input tokens) before starting. It never writes into
the manager state; it only reads it.
- GPT: only the current access token and account id are read, in memory, from
  the profile's login (its source home, or its own home for a native login),
  matched against the profile's registered account fingerprint, and handed to
  the test runtime with account/login/start chatgptAuthTokens and an ephemeral
  credential store. Refresh requests are answered by re-reading the access
  token, as AuthProxy does; refresh tokens are never read out or sent.
- External: the key is unlocked the way the manager does it and passed only in
  the X runtime's environment. It never touches disk.

CROSS-ACCOUNT MODE (--second-gpt-profile ID --confirm-second-account, live only)
NEEDS THE USER'S EXPLICIT APPROVAL FOR THAT SECOND ACCOUNT: it borrows a second
ChatGPT profile's access token (same rules as G) and sends that account G's
native encrypted compaction, which was made on the first account. G forks the
thread right after compaction (thread/fork, no model request) and G2 resumes
that fork after G's last turn, so steps 1-4 (X included) are tested as usual,
G's thread never carries the second account's items, and G2's request carries
no first-account item but the compaction (no reasoning from G's later turns;
the report lists the encrypted items the fork replays, and the fixture checks
the wire). The report says whether G2's request succeeded or was rejected for
its encrypted content.

Evidence keeps no secrets: request bodies are summarized, encrypted payloads and
filler text are reduced to length and hash, every evidence file (report,
proxy-requests.json, redacted rollouts) is scrubbed of the secret values and of
JWT, API-key, Bearer and refresh-token patterns, then every file the run wrote is
scanned; a file that still matches is deleted and the run fails. Other files in
the output directory are left alone. The report is written as FAIL with a
pending scan until the scan comes back clean. Raw homes are deleted even when
the evidence cannot be written, and a run whose raw homes survive fails.

Not part of the unittest suite.
"""
import argparse
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import ssl
import stat
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_shared_editing_headless import Client, ROOT  # noqa: E402
from manager_core.external_profile import ExternalProfile  # noqa: E402
from manager_core.providers import ProviderRegistry  # noqa: E402
from manager_core.proxy_auth import LoginNeededError, account_fingerprint, read_existing_tokens  # noqa: E402
from manager_core.runtime_build import resolve  # noqa: E402

CODEWORD = 'PLANET-7431'
COLOUR = '청록'
PLANT = f'테스트용 가상 정보다. 코드워드는 {CODEWORD}, 색깔은 {COLOUR}. 도구를 쓰지 말고 "기억함"이라고만 답해.'
ACK = '방금 정한 두 정보를 한 줄로 확인해 줘.'
ASK_X = '앞에서 정한 코드워드와 색깔이 뭐였지? 한 줄로.'
ASK_G = '방금 다른 모델에게 무엇을 물어봤고, 코드워드는 뭐였지? 한 줄로.'
ASK_G2 = '처음에 정한 코드워드가 뭐였지? 한 줄로.'
PROMPTS = (PLANT, ACK, ASK_X, ASK_G, ASK_G2)
PAD_HEAD = ('[컨텍스트 크기 시험용 합성 채움 글이다. 아무 정보도 없으니 기억하거나 요약하지 말고, '
            '도구를 쓰지 말고 "확인"이라고만 답해.]')
PAD_TAIL = '[채움 글 끝. 도구를 쓰지 말고 "확인"이라고만 답해.]'
PAD_REPLY = '확인'
# Common nouns that are one token each in the GPT and DeepSeek tokenizers; no colours,
# nothing resembling the planted facts or a credential.
FILLER_WORDS = tuple((
    'river stone garden window paper table chair cloud forest bridge mountain village market '
    'letter candle basket ladder pencil blanket kettle lantern harbor meadow orchard pebble '
    'feather thunder compass anchor station ticket engine island valley desert canyon harvest '
    'pillow mirror wallet jacket button pocket bottle spoon plate bread butter cheese apple lemon '
    'onion carrot garlic pepper salt sugar coffee water fire earth wind rain snow storm season '
    'winter summer autumn morning evening night').split())
SUMMARY_MARK = 'Another language model started to solve this problem'
COMPACT_PROMPT_MARK = 'You are performing a CONTEXT CHECKPOINT COMPACTION'
NOTE_MARK = '<context_note>'
PLACEHOLDER_MARK = 'it was encrypted for a different model provider'
REBUILD_LOG_MARK = 'rebuilt model context for provider family'
FORBIDDEN_OUTPUT = [ROOT / 'work/control-center', ROOT / 'artifacts/manager',
                    ROOT / 'artifacts/manager-runtime', ROOT / 'artifacts/remote']
SECRET_PATTERNS = {name: re.compile(pattern) for name, pattern in (
    ('jwt', r'eyJ[A-Za-z0-9_-]{10,}'),
    ('api_key', r'\bsk-[A-Za-z0-9_-]{8,}'),
    ('bearer', r'(?i)bearer\s+(?!\[REDACTED\])[A-Za-z0-9._-]{8,}'),
    ('refresh_token', r'(?i)"?refresh_token"?\s*[:=]\s*"(?!\[REDACTED\])[^"]{4,}"'))}
SENSITIVE_KEYS = {'access_token', 'refresh_token', 'id_token', 'api_key', 'authorization', 'openai_api_key'}
ENCRYPTED_ERROR = re.compile(r'(?i)encrypt')
NATIVE_COMPACTION_TYPES = ('compaction', 'compaction_summary', 'context_compaction')

# Summary-path sizing. X's rebuild budget (rebuild.rs, budget.rs) is below
# min(90%, 95%) of the window minus base instructions, min(8000, limit/8) for tools and
# limit/10 for the reply, minus the largest recorded context bundle.
DEFAULT_SUMMARY_WINDOW = 32_768
PADDING_WINDOW_MARGIN = 1.25       # filler rebuild estimate / X's window
# Keeps G's own resume on its native checkpoint (rebuild step 1) for ~100k-window GPT models,
# far below G's auto-compaction and native compaction's 64k retained-message budget.
MAX_PADDING_RUNTIME_TOKENS = 36_000
# Observed on the 2026-09-26 live run (gpt-6-astra, deepseek-flash); estimates only.
X_BASE_INSTRUCTION_TOKENS = 7_200  # ~21.4k chars of instructions, rebuild estimate
CONTEXT_BUNDLE_TOKENS = 2_600      # skills, multi-agent and environment messages, rebuild estimate
SUMMARY_ALLOWANCE_TOKENS = 1_000
G_TURN_INPUT = 9_000               # instructions, tools and context bundle per GPT turn
G_RESUME_INPUT = 13_500            # a resumed GPT turn also carries a model-switch message
G_COMPACTION_INPUT = 6_000
G_SUMMARY_INPUT = 9_000
X_TURN_INPUT = 9_000
PORTABLE_USER_MESSAGE_MAX_TOKENS = 20_000


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()[:12]


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def message_text(item):
    return ''.join(part.get('text', '') for part in item.get('content') or [] if isinstance(part, dict))


def item_label(item):
    kind = item.get('type') or ('message' if 'role' in item else '?')
    return kind + (':' + item['role'] if kind == 'message' and item.get('role') else '')


def is_summary_message(item):
    return item.get('role') == 'user' and message_text(item).startswith(SUMMARY_MARK)


def request_kind(items):
    if any(item.get('type') == 'compaction_trigger' for item in items):
        return 'native_compaction'
    users = [message_text(item) for item in items if item.get('role') == 'user']
    if users and users[-1].startswith(COMPACT_PROMPT_MARK):
        return 'summary_prompt'
    return 'turn'


def prompt_label(text):
    if text in PROMPTS:
        return text
    if text.startswith(COMPACT_PROMPT_MARK):
        return '[compaction summary prompt]'
    if text.startswith(PAD_HEAD):
        return f'[padding filler, {len(text)} chars]'
    return '[other text sha256 ' + digest(text) + ']'


# Python ports of the runtime's estimators (codex_utils_string::approx_token_count and
# portable_context::budget::text_token_count).
def runtime_tokens(text):
    return (len(text.encode('utf-8')) + 3) // 4


def rebuild_tokens(text):
    data = text.encode('utf-8')
    estimate = (len(data) + 3) // 4
    return estimate + estimate // 3 + sum(1 for byte in data if byte >= 0x80) // 6


def filler(words):
    """Deterministic filler of `words` common single-token words between the instructions."""
    state, chosen = 7431, []
    for _ in range(words):
        state = (state * 1103515245 + 12345) & 0x7fffffff
        chosen.append(FILLER_WORDS[(state >> 16) % len(FILLER_WORDS)])
    lines = [' '.join(chosen[index:index + 16]) for index in range(0, words, 16)]
    return PAD_HEAD + '\n' + '\n'.join(lines) + '\n' + PAD_TAIL


def default_padding_words(window):
    sample = 4000
    per_word = (rebuild_tokens(filler(sample)) - rebuild_tokens(filler(0))) / sample
    words = math.ceil(window * PADDING_WINDOW_MARGIN / per_word / 500) * 500
    while rebuild_tokens(filler(words)) < window * PADDING_WINDOW_MARGIN:
        words += 500
    return words


def x_limits(window):
    """X's compaction limit and the upper bound of its rebuild budget, before instructions."""
    limit = min(window * 9 // 10, window * 95 // 100)
    return limit, limit - min(8000, limit // 8) - limit // 10


def make_plan(args):
    """Expected requests and approximate input tokens; printed before anything starts."""
    summary = args.scenario == 'summary'
    gate = not args.no_summary_gate
    second = bool(args.second_gpt_profile or args.fixture_second_account)
    g_requests = ['turn 1 (plant)', 'turn 2 (acknowledge)'] + (['turn 3 (padding)'] if summary else []) + \
        ['native compaction'] + (['portable summary'] if gate else []) + ['final turn']
    plan = {'mode': 'live' if args.live else 'fixture',
            'scenario': 'summary path' if summary else 'full transcript',
            'summary_gate': gate,
            'requests': {'G': g_requests, 'X': ['turn (ask facts)'], 'G2': ['turn (second account)'] if second else []}}
    padding_real = 0
    x_padding_real = 0
    if summary:
        text = filler(args.padding_words)
        window = args.external_context_window
        limit, upper = x_limits(window)
        estimate = upper - X_BASE_INSTRUCTION_TOKENS - CONTEXT_BUNDLE_TOKENS
        user_allowance = max(0, min(estimate - SUMMARY_ALLOWANCE_TOKENS, PORTABLE_USER_MESSAGE_MAX_TOKENS))
        padding_real = args.padding_words + rebuild_tokens(PAD_HEAD + PAD_TAIL)
        # keep_user_messages fits the kept filler by the rebuild estimate.
        x_padding_real = round(padding_real * min(1, user_allowance / max(1, rebuild_tokens(text))))
        plan['padding'] = {
            'words': args.padding_words, 'chars': len(text), 'bytes': len(text.encode('utf-8')),
            'approx_real_tokens': padding_real, 'runtime_estimate_tokens': runtime_tokens(text),
            'rebuild_estimate_tokens': rebuild_tokens(text)}
        plan['x'] = {
            'context_window': window, 'auto_compaction_limit': limit,
            'rebuild_budget_upper_bound': upper,
            'rebuild_budget_estimate': estimate,
            'full_transcript_rebuild_estimate_over_window': round(rebuild_tokens(text) / window, 2),
            'full_transcript_rebuild_estimate_over_budget_upper_bound': round(rebuild_tokens(text) / upper, 2),
            'expected_rebuild_step': '3 (portable summary + newest user messages; the filler takes the '
                                     'user-message allowance, so the facts arrive only in the summary)'}
        if estimate < 4 * SUMMARY_ALLOWANCE_TOKENS:
            plan['warnings'] = [f'The estimated X rebuild budget ({estimate}) leaves little room for the summary; '
                                'the runtime may fall back to recent items only (step 5). Use a larger '
                                '--external-context-window.']
    g_tokens = 2 * G_TURN_INPUT + G_COMPACTION_INPUT + padding_real + (G_TURN_INPUT + padding_real if summary else 0) \
        + (G_SUMMARY_INPUT + padding_real if gate else 0) + G_RESUME_INPUT + padding_real
    tokens = {'G': g_tokens, 'X': X_TURN_INPUT + x_padding_real,
              'G2': G_RESUME_INPUT + padding_real if second else 0}
    tokens['total'] = sum(tokens.values())
    plan['request_count'] = {'G': len(g_requests), 'X': 1, 'G2': int(second),
                             'total': len(g_requests) + 1 + int(second)}
    plan['approx_input_tokens'] = tokens
    plan['estimate_basis'] = ('per-request overhead observed on the 2026-09-26 live run; the filler is carried by '
                              'the padding turn, native compaction, portable summary and each resumed GPT turn '
                              '(native compaction keeps user messages up to 64k tokens), and X gets only its '
                              'user-message allowance of it. A ChatGPT login also reads account settings; '
                              'those are not model requests.')
    return plan


def summarize_request(raw, content_encoding=None):
    """Structural, secret-free summary of one Responses request body."""
    summary = {'bytes': len(raw)}
    if content_encoding and content_encoding.lower() != 'identity':
        summary['content_encoding'] = content_encoding
        return summary
    try:
        body = json.loads(raw)
    except ValueError:
        summary['unparsed'] = True
        return summary
    items = [item for item in body.get('input') or [] if isinstance(item, dict)]
    types, encrypted = {}, {}
    for item in items:
        label = item_label(item)
        types[label] = types.get(label, 0) + 1
        if item.get('encrypted_content') is not None:
            encrypted[label] = encrypted.get(label, 0) + 1
    text = '\n'.join(strings(items))
    outside = '\n'.join(strings([item for item in items if not is_summary_message(item)]))
    users = [message_text(item) for item in items if item.get('role') == 'user']
    padding = [len(value) for value in users if value.startswith(PAD_HEAD)]
    summary.update(
        kind=request_kind(items), model=body.get('model'),
        reasoning_effort=(body.get('reasoning') or {}).get('effort'), store=body.get('store'),
        tool_count=len(body.get('tools') or []), instructions_chars=len(body.get('instructions') or ''),
        input_item_count=len(items), input_types=types, input_order=[item_label(item) for item in items],
        encrypted_content_items=encrypted,
        has_native_compaction_item=any(item.get('type') in NATIVE_COMPACTION_TYPES for item in items),
        has_compaction_trigger=any(item.get('type') == 'compaction_trigger' for item in items),
        has_encrypted_reasoning=any(item.get('type') == 'reasoning' and item.get('encrypted_content')
                                    for item in items),
        portable_summary_present=SUMMARY_MARK in text,
        summary_chars=sum(len(message_text(item)) for item in items if is_summary_message(item)),
        context_note_present=NOTE_MARK in text,
        encrypted_placeholder_present=PLACEHOLDER_MARK in text, model_switch_present='<model_switch>' in text,
        codeword_occurrences=text.count(CODEWORD), colour_occurrences=text.count(COLOUR),
        codeword_outside_summary=outside.count(CODEWORD), colour_outside_summary=outside.count(COLOUR),
        plant_prompt_present=PLANT in text, x_question_present=ASK_X in text,
        padding_messages=len(padding), padding_chars=max(padding, default=0),
        last_user_text=prompt_label(users[-1]) if users else None)
    return summary


def summarize_response(raw, content_encoding=None):
    summary = {'bytes': len(raw)}
    if content_encoding and content_encoding.lower() != 'identity':
        summary['content_encoding'] = content_encoding
        return summary
    events, outputs = {}, []
    for line in raw.decode('utf-8', 'replace').splitlines():
        if not line.startswith('data:'):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get('type'))
        events[kind] = events.get(kind, 0) + 1
        if kind == 'response.output_item.done':
            item = event.get('item') or {}
            outputs.append({'type': item.get('type'), 'encrypted': item.get('encrypted_content') is not None})
        if kind in ('response.completed', 'response.failed', 'response.incomplete'):
            response = event.get('response') or {}
            usage = response.get('usage') or {}
            summary['usage'] = {key: value for key, value in usage.items() if isinstance(value, int)}
            if response.get('error'):
                summary['error'] = {key: str(value)[:200] for key, value in response['error'].items()}
    if not events:
        try:
            body = json.loads(raw)
            error = body.get('error') if isinstance(body, dict) else None
            if isinstance(error, dict):
                summary['error'] = {key: str(error.get(key))[:200] for key in ('type', 'code', 'message')}
        except ValueError:
            pass
    summary.update(events=events, output_items=outputs)
    return summary


class RecordingProxy:
    """127.0.0.1 reverse proxy: forwards every byte, stores only structural summaries."""

    def __init__(self, upstream):
        parts = urlsplit(upstream)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('The external provider base URL is not an http(s) URL.')
        self.scheme, self.host = parts.scheme, parts.hostname
        self.port = parts.port or (443 if parts.scheme == 'https' else 80)
        self.prefix = parts.path.rstrip('/')
        self.records = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'  # the response ends when the connection closes

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.forward(None)

            def do_POST(self):
                self.forward(self.rfile.read(int(self.headers.get('Content-Length') or 0)))

            def forward(self, body):
                record = {'at': now(), 'method': self.command, 'path': self.path.split('?')[0]}
                owner.records.append(record)
                if body is not None:
                    record['request'] = summarize_request(body, self.headers.get('Content-Encoding'))
                # Identity responses keep the recorded copy parseable; nothing else changes.
                headers = {key: value for key, value in self.headers.items() if key.lower() not in (
                    'host', 'connection', 'proxy-connection', 'keep-alive', 'transfer-encoding', 'accept-encoding')}
                headers['Accept-Encoding'] = 'identity'
                if owner.scheme == 'https':
                    connection = http.client.HTTPSConnection(owner.host, owner.port, timeout=300,
                                                             context=ssl.create_default_context())
                else:
                    connection = http.client.HTTPConnection(owner.host, owner.port, timeout=300)
                try:
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                except OSError as error:
                    record['upstream_error'] = type(error).__name__
                    self.send_error(502)
                    return
                record['status'] = response.status
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ('transfer-encoding', 'connection', 'content-length', 'date', 'server'):
                        self.send_header(key, value)
                self.end_headers()
                captured = bytearray()
                try:
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if len(captured) < 8 << 20:
                            captured.extend(chunk)
                finally:
                    connection.close()
                record['response'] = summarize_response(bytes(captured), response.getheader('Content-Encoding'))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}{self.prefix}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


FIXTURE_OPAQUE = re.compile(r'^FIXTURE-OPAQUE-[A-Z]+-(G2|G|X)-')


class FixtureModel:
    """Loopback Responses API for both families.

    Replies depend only on what each request carried, so an answer proves which
    context reached that provider. G (and G2) return encrypted reasoning with each
    turn and a native compaction item for a compaction trigger, like OpenAI does;
    each encrypted payload names the account that made it. With cross_account
    'reject', a request carrying another account's encrypted payload gets the 400
    error OpenAI returns for unreadable encrypted content.
    """

    def __init__(self, cross_account=None):
        self.requests = []
        self.cross_account = cross_account
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append({'provider': None, 'method': 'GET', 'path': self.path.split('?')[0]})
                self.send_error(404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                provider = {'Bearer fixture-G': 'G', 'Bearer fixture-G2': 'G2',
                            'Bearer fixture-X': 'X'}.get(self.headers.get('Authorization'))
                if provider is None or not self.path.split('?')[0].endswith('/responses'):
                    owner.requests.append({'provider': provider, 'method': 'POST', 'path': self.path.split('?')[0]})
                    self.send_error(401 if provider is None else 404)
                    return
                items = [item for item in json.loads(raw).get('input') or [] if isinstance(item, dict)]
                kind = request_kind(items)
                record = {'provider': provider, 'method': 'POST', 'path': self.path.split('?')[0],
                          'kind': kind, 'request': summarize_request(raw)}
                owner.requests.append(record)
                foreign = sorted({match.group(1) for item in items
                                  if isinstance(item.get('encrypted_content'), str)
                                  for match in [FIXTURE_OPAQUE.match(item['encrypted_content'])]
                                  if match and match.group(1) != provider})
                if owner.cross_account == 'reject' and provider in ('G', 'G2') and foreign:
                    record['rejected_foreign_encrypted_content_from'] = foreign
                    body = json.dumps({'error': {
                        'message': 'The encrypted content of an input item could not be verified for this account.',
                        'type': 'invalid_request_error', 'param': 'input',
                        'code': 'invalid_encrypted_content'}}).encode()
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                output = owner.reply(provider, kind, items)
                rid = 'resp_' + uuid4().hex
                events = [dict(type='response.created', response=dict(id=rid, status='in_progress', output=[]))]
                for index, item in enumerate(output):
                    events += [dict(type='response.output_item.added', output_index=index, item=item),
                               dict(type='response.output_item.done', output_index=index, item=item)]
                events.append(dict(type='response.completed', response=dict(
                    id=rid, status='completed', output=output,
                    usage=dict(input_tokens=len(raw) // 4, output_tokens=8, total_tokens=len(raw) // 4 + 8))))
                data = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'

    @staticmethod
    def reply(provider, kind, items):
        if kind == 'native_compaction':
            return [dict(type='compaction', id='cmp_' + uuid4().hex,
                         encrypted_content=f'FIXTURE-OPAQUE-COMPACTION-{provider}-' + uuid4().hex)]
        users = [message_text(item) for item in items if item.get('role') == 'user']
        latest = users[-1] if users else ''
        earlier = '\n'.join(strings(items)).replace(latest, '') if latest else '\n'.join(strings(items))
        facts = [label for label, value in (('코드워드 ' + CODEWORD, CODEWORD), ('색깔 ' + COLOUR, COLOUR))
                 if value in earlier]
        if kind == 'summary_prompt':
            text = '요약: 사용자가 정한 정보 - ' + (', '.join(facts) or '없음')
        elif latest == PLANT:
            text = '기억함'
        elif latest.startswith(PAD_HEAD):
            text = PAD_REPLY
        elif latest == ASK_G:
            asked = '다른 모델에게 코드워드와 색깔을 물었음' if ASK_X in earlier else '다른 모델의 질문을 모름'
            text = asked + '; ' + (facts[0] if facts and facts[0].startswith('코드워드') else '코드워드 모름')
        else:
            text = ', '.join(facts) or '모름'
        message = dict(type='message', id='msg_' + uuid4().hex, role='assistant', status='completed',
                       content=[dict(type='output_text', text=text, annotations=[])])
        if provider in ('G', 'G2'):
            return [dict(type='reasoning', id='rs_' + uuid4().hex, summary=[],
                         encrypted_content=f'FIXTURE-OPAQUE-REASONING-{provider}-' + uuid4().hex), message]
        return [message]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Secrets:
    def __init__(self):
        self.values = []

    def add(self, value):
        if isinstance(value, str) and len(value) >= 8 and value not in self.values:
            self.values.append(value)

    def scrub(self, text):
        text = str(text)
        for value in self.values:
            text = text.replace(value, '[REDACTED]')
        for pattern in SECRET_PATTERNS.values():
            text = pattern.sub('[REDACTED]', text)
        return text

    def scrub_value(self, value):
        """Scrubs every string of a JSON value, keys included; sensitive keys lose their values."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, list):
            return [self.scrub_value(child) for child in value]
        if isinstance(value, dict):
            return {self.scrub(key): '[REDACTED]' if str(key).lower() in SENSITIVE_KEYS and isinstance(child, str)
                    and child else self.scrub_value(child) for key, child in value.items()}
        return value

    def dumps(self, value, indent=2):
        return json.dumps(self.scrub_value(value), ensure_ascii=False, indent=indent)

    def scan(self, base, targets):
        """Findings in the files under `targets` (files or directories), named relative to `base`."""
        findings = []
        paths = sorted({path for target in targets if target.exists()
                        for path in ([target] if target.is_file() else target.rglob('*')) if path.is_file()})
        for path in paths:
            text = path.read_bytes().decode('utf-8', 'replace')
            matches = (['secret value'] if any(value in text for value in self.values) else []) + \
                [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]
            findings += [{'file': str(path.relative_to(base)), 'match': match} for match in matches]
        return findings


class LiveAuth:
    """Borrows a profile's current access token like AuthProxy; never a refresh token."""

    def __init__(self, auth_home, expected_fingerprint, secrets):
        self.auth_home, self.expected, self.secrets = auth_home, expected_fingerprint, secrets
        self.refreshes = 0

    def tokens(self):
        tokens = read_existing_tokens(self.auth_home)
        if account_fingerprint(tokens.account_id) != self.expected:
            raise LoginNeededError('account_mismatch')
        self.secrets.add(tokens.access_token)
        self.secrets.add(tokens.account_id)
        return tokens

    def server_request(self, message):
        if message.get('method') != 'account/chatgptAuthTokens/refresh':
            return None
        self.refreshes += 1
        try:
            return {'id': message['id'], 'result': self.tokens().refresh_result()}
        except LoginNeededError as error:
            return {'id': message['id'], 'error': {'code': -32041, 'message': 'login needed: ' + error.reason}}


def parse_rebuild_line(line):
    """Only the numbers and names of the runtime's rebuild log line; never the raw line."""
    line = re.sub(r'\x1b\[[0-9;]*m', '', line)
    fields = {'family': re.search(REBUILD_LOG_MARK + r' (\S+)', line)}
    fields.update({key: re.search(r'\b' + key + r'=(-?\w+)', line)
                   for key in ('source', 'items', 'budget_tokens', 'transcript_items')})
    result = {}
    for key, match in fields.items():
        if match:
            value = match.group(1)
            result[key] = int(value) if re.fullmatch(r'-?\d+', value) else value
    return result


class StderrLines(list):
    """Client.errors replacement that also keeps the runtime's rebuild log lines, parsed."""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def append(self, line):
        if REBUILD_LOG_MARK in line:
            self.sink.append(parse_rebuild_line(line))
        super().append(line)


# The rebuild decision is logged at info level by codex_core::session; everything else
# stays at the default error level.
RUNTIME_LOG_ENVIRONMENT = {'RUST_LOG': 'error,codex_core::session=info', 'NO_COLOR': '1'}


class ManagedClient(Client):
    """The headless Client with sanitized server-request handling and turn helpers."""

    def __init__(self, binary, home, shared, alias, *, config_text, environment=None, adapter=None,
                 auth=None, secrets=None):
        self.auth, self.secrets = auth, secrets or Secrets()
        self.server_requests = []
        self.rebuild_logs = []
        super().__init__(binary, home, None, alias, 0, str(uuid4()), canonical_home=shared,
                         config_text=config_text, extra_environment={**RUNTIME_LOG_ENVIRONMENT, **(environment or {})},
                         request_adapter=adapter)
        # Rebuilds happen on resume, after this point; earlier stderr lines stay in the old list.
        self.errors = StderrLines(self.rebuild_logs)

    def write_private(self, message):
        # Credential-bearing frames bypass the observer and the request adapter.
        self.proc.stdin.write(json.dumps(message).encode() + b'\n')
        self.proc.stdin.flush()

    def next(self, deadline):
        message = self.messages.get(timeout=max(.1, deadline - time.monotonic()))
        if message is None:
            raise RuntimeError('runtime exited: ' + self.secrets.scrub(''.join(self.errors[-3:]))[:600])
        if 'method' in message and 'id' in message:
            self.server_requests.append(message['method'])
            reply = self.auth.server_request(message) if self.auth else None
            self.write_private(reply or {'id': message['id'], 'error': {
                'code': -32601, 'message': 'The cross-provider test grants no additional permissions'}})
            return {'method': message['method']}
        self.observer.consume('server', message)
        if 'method' in message:
            self.events.append(message)
        return message

    def login(self):
        tokens = self.auth.tokens()
        identity = 'cross-provider-login-' + uuid4().hex
        self.write_private({'id': identity, 'method': 'account/login/start', 'params': tokens.login_params()})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            message = self.next(deadline)
            if message.get('id') == identity and 'method' not in message:
                if message.get('error') is not None:
                    raise RuntimeError('account/login/start rejected (code %s)' % message['error'].get('code'))
                if (message.get('result') or {}).get('type') != 'chatgptAuthTokens':
                    raise RuntimeError('account/login/start returned an unexpected result')
                return
        raise TimeoutError('account/login/start')

    def wait_turn(self, thread_id, turn_id, start, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events[start:]:
                params = event.get('params') or {}
                if event.get('method') == 'turn/completed' and (params.get('turn') or {}).get('id') == turn_id:
                    return params['turn']
            self.next(deadline)
        raise TimeoutError('turn/completed ' + turn_id)

    def notes(self, start, turn_id=None):
        """Warnings, errors and reroutes seen since `start`, scrubbed."""
        seen = []
        for event in self.events[start:]:
            method, params = event.get('method'), event.get('params') or {}
            if method in ('warning', 'configWarning', 'error', 'model/rerouted', 'deprecationNotice',
                          'modelProvider/authRecoveryStarted', 'modelProvider/authRecoveryCompleted'):
                if turn_id is None or params.get('turnId') in (None, turn_id):
                    seen.append({'method': method, 'detail': self.secrets.scrub(json.dumps(params, ensure_ascii=False))[:500]})
        return seen

    def run_turn(self, thread_id, text, *, effort=None, timeout=300):
        start = len(self.events)
        params = {'threadId': thread_id, 'input': [{'type': 'text', 'text': text, 'text_elements': []}]}
        if effort:
            params['effort'] = effort
        began = time.monotonic()
        turn_id = self.rpc('turn/start', params)['turn']['id']
        turn = self.wait_turn(thread_id, turn_id, start, timeout)
        answers = [event['params']['item'].get('text', '') for event in self.events[start:]
                   if event.get('method') == 'item/completed'
                   and (event.get('params') or {}).get('turnId') == turn_id
                   and (event['params'].get('item') or {}).get('type') == 'agentMessage']
        return {'turn_id': turn_id, 'status': turn.get('status'), 'seconds': round(time.monotonic() - began, 1),
                'error': self.secrets.scrub(json.dumps(turn.get('error'), ensure_ascii=False)) if turn.get('error') else None,
                'answer': '\n'.join(answers), 'notes': self.notes(start, turn_id)}

    def compact(self, thread_id, timeout=300):
        start = len(self.events)
        began = time.monotonic()
        self.rpc('thread/compact/start', {'threadId': thread_id})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.events[start:]:
                params = event.get('params') or {}
                if event.get('method') == 'turn/completed' and params.get('threadId') == thread_id:
                    turn = params.get('turn') or {}
                    return {'status': turn.get('status'), 'turn_id': turn.get('id'),
                            'compaction_item_completed': any(
                                e.get('method') == 'item/completed'
                                and ((e.get('params') or {}).get('item') or {}).get('type') == 'contextCompaction'
                                for e in self.events[start:]),
                            'seconds': round(time.monotonic() - began, 1),
                            'error': self.secrets.scrub(json.dumps(turn.get('error'), ensure_ascii=False))
                            if turn.get('error') else None,
                            'notes': self.notes(start)}
            self.next(deadline)
        raise TimeoutError('compaction of ' + thread_id)


def redact_item(value):
    """Reduces encrypted payloads and filler text to length and hash."""
    if isinstance(value, dict):
        return {key: {'redacted': True, 'chars': len(child), 'sha256_12': digest(child)}
                if key == 'encrypted_content' and isinstance(child, str) else redact_item(child)
                for key, child in value.items()}
    if isinstance(value, list):
        return [redact_item(child) for child in value]
    if isinstance(value, str) and PAD_HEAD in value and len(value) > 2000:
        return f'[padding filler: {len(value)} chars, sha256 {digest(value)}]'
    return value


def read_lines(path):
    """A rollout's lines; a runtime stopped mid-write can leave a partial UTF-8 sequence, so
    undecodable bytes are replaced (the damaged line then fails to parse and is skipped)."""
    return Path(path).read_bytes().decode('utf-8', 'replace').splitlines()


def rollout_records(roots, thread_ids):
    thread_ids = [thread_ids] if isinstance(thread_ids, str) else [value for value in thread_ids if value]
    paths = sorted({path for root in roots if root.exists() for path in root.rglob('*.jsonl')
                    if any(thread_id in path.name for thread_id in thread_ids)})
    records = []
    for path in paths:
        for line in read_lines(path):
            try:
                records.append((path, json.loads(line)))
            except ValueError:
                pass
    return paths, records



def history_encrypted_items(records):
    """Encrypted items, by type, in the model-visible history a resume replays: the newest
    checkpoint's replacement history plus the response items recorded after it."""
    records = [record for _, record in records]
    start = max((index for index, record in enumerate(records) if record.get('type') == 'compacted'), default=-1)
    items = []
    if start >= 0:
        items += [item for item in (records[start].get('payload') or {}).get('replacement_history') or []
                  if isinstance(item, dict)]
    items += [record.get('payload') for record in records[start + 1:]
              if record.get('type') == 'response_item' and isinstance(record.get('payload'), dict)]
    counts = {}
    for item in items:
        if item.get('encrypted_content'):
            counts[item_label(item)] = counts.get(item_label(item), 0) + 1
    return counts


def checkpoint_summary(payload):
    replacement = payload.get('replacement_history') or []
    origin = payload.get('origin') or {}
    portable = payload.get('portable')
    return {
        'origin': origin or None,
        'kind': (origin.get('kind') or {'type': 'compaction'}) if origin else None,
        'window_number': payload.get('window_number'),
        'replacement_types': [item_label(item) for item in replacement if isinstance(item, dict)],
        'native_encrypted_items': sum(1 for item in replacement if isinstance(item, dict)
                                      and item.get('type') in NATIVE_COMPACTION_TYPES
                                      and item.get('encrypted_content')),
        'portable': None if portable is None else {
            'status': portable.get('status'), 'covers': portable.get('covers'),
            'summary_chars': len(portable.get('summary') or ''),
            'summary_mentions_codeword': CODEWORD in (portable.get('summary') or ''),
            'summary_mentions_colour': COLOUR in (portable.get('summary') or ''),
            'summary': portable.get('summary')},
    }


def rollout_evidence(roots, thread_id):
    paths, records = rollout_records(roots, thread_id)
    checkpoints, turns, warnings, usage = [], [], [], 0
    for _, record in records:
        kind, payload = record.get('type'), record.get('payload') or {}
        if kind == 'compacted':
            checkpoints.append(checkpoint_summary(payload))
        elif kind == 'turn_context':
            turns.append({'turn_id': payload.get('turn_id'), 'model': payload.get('model'),
                          'model_provider': payload.get('model_provider'), 'effort': payload.get('effort')})
        elif kind == 'token_usage_record':
            usage += 1
        elif kind == 'event_msg' and payload.get('type') in ('warning', 'error', 'stream_error'):
            warnings.append({'type': payload.get('type'), 'message': str(payload.get('message'))[:500]})
    return {'rollout_files': [str(path) for path in paths], 'record_count': len(records),
            'checkpoints': checkpoints, 'turn_contexts': turns, 'token_usage_records': usage,
            'warnings': warnings}


def write_redacted_rollouts(paths, target, secrets):
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for path in paths:
        rows = []
        for line in read_lines(path):
            try:
                rows.append(secrets.dumps(redact_item(json.loads(line)), indent=None))
            except ValueError:
                rows.append('"[unparsed line]"')
        destination = target / (Path(path).stem + '.redacted.jsonl')
        destination.write_text('\n'.join(rows) + '\n', encoding='utf-8')
        written.append(destination.name)
    return written


def provider_block(config_text, provider_id):
    match = re.search(r'(?ms)^\[model_providers\.' + re.escape(provider_id) + r'\]\n.*?(?=^\[|\Z)', config_text)
    if not match:
        raise RuntimeError('The rendered external config has no provider block.')
    return match.group(0).rstrip() + '\n'


# No tools, plugin/app sync or analytics: besides this test's model requests, a ChatGPT
# login still reads its account settings from the backend.
BASE_SETTINGS = ('approval_policy = "never"\ncli_auth_credentials_store = "ephemeral"\nweb_search = "disabled"\n'
                 '[features]\nshell_tool = false\nenable_request_compression = false\nplugins = false\n'
                 'apps = false\nremote_plugin = false\nimage_generation = false\n[analytics]\nenabled = false\n')


def remove_tree(path):
    """Deletes a disposable home, read-only files included; retries briefly on Windows locks."""
    def clear_read_only(function, target, _):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    for _ in range(20):
        if not path.exists():
            return True
        try:
            shutil.rmtree(path, onexc=clear_read_only)
        except OSError:
            time.sleep(.5)
    return not path.exists()


def external_side(args, raw, x_home, fixture, window_override):
    """Renders X's config like the manager and returns (config, env, binding, upstream, provider_id).

    A window override goes through the manager's own profile settings, so the config's
    model_context_window and auto-compaction limit, the model catalog and the binding the
    request adapter applies on resume all carry it.
    """
    override = None if window_override is None else {'context_window': window_override, 'auto_compact_percent': 90}
    if fixture is not None:
        registry = ProviderRegistry(raw / 'fixture-registry')
        # Like the live external model, the fixture model has a 1M window unless overridden.
        saved = registry.save({'name': 'External fixture', 'base_url': 'https://fixture.invalid/v1',
                               'protocol': 'responses'},
                              {'wire_model_id': 'fixture-flash', 'reasoning_effort': 'low',
                               'capabilities': {'context_window': 1048576}})
        data = registry._read()
        data['models'][0]['capabilities']['verified'] = True
        registry.path.write_text(json.dumps(data), encoding='utf-8')
        model_id, upstream = saved['model']['id'], fixture.url
        rendered = registry.render_for_host(x_home, False, [], BASE_SETTINGS, primary_model_id=model_id,
                                            primary_settings=override)
        config = rendered['files']['config.toml'].replace('https://fixture.invalid/v1', upstream)
        environment = {registry._env_name(saved['provider']): 'fixture-X'}
    else:
        state = json.loads((ROOT / 'work/control-center/state.json').read_text(encoding='utf-8-sig'))
        profile = next((p for p in state['profiles'] if p['id'] == args.external_profile), None)
        if profile is None or profile.get('auth_mode') != 'external' or not profile.get('external_model_id'):
            raise RuntimeError('The external profile is not registered as an external API profile.')
        model_id, settings = profile['external_model_id'], profile.get('external_settings')
        if override:
            settings = {**(settings or {}), **override}
        registry = ProviderRegistry(ROOT)  # read-only use: _read, render_for_host, environment
        registry_state = registry._read()
        model = next(m for m in registry_state['models'] if m['id'] == model_id)
        provider = next(p for p in registry_state['providers'] if p['id'] == model['provider_id'])
        upstream = provider['base_url']
        rendered = registry.render_for_host(x_home, False, [], BASE_SETTINGS, primary_model_id=model_id,
                                            primary_settings=settings)
        config = rendered['files']['config.toml']
        environment = registry.environment([model_id])
    x_home.mkdir(parents=True, exist_ok=True)
    for relative, body in rendered['files'].items():
        if relative != 'config.toml':
            (x_home / relative).parent.mkdir(parents=True, exist_ok=True)
            (x_home / relative).write_text(body, encoding='utf-8')
    return config, environment, rendered['primary'], upstream, rendered['primary']['model_provider']


def chatgpt_auth(profile_id, secrets):
    """Reads a registered ChatGPT profile (read-only) and borrows its access token."""
    state = json.loads((ROOT / 'work/control-center/state.json').read_text(encoding='utf-8-sig'))
    profile = next((p for p in state['profiles'] if p['id'] == profile_id), None)
    if profile is None or profile.get('auth_mode') == 'external' or not profile.get('account_fingerprint'):
        raise RuntimeError('The GPT profile is not a registered ChatGPT profile with an account fingerprint.')
    auth = LiveAuth(Path(profile.get('source_home') or profile['home']), profile['account_fingerprint'], secrets)
    auth.tokens()  # fail early, before any runtime starts
    return auth, profile


def gpt_side(args, fixture, secrets):
    """Returns (config head, model, provider id, auth) for G."""
    if fixture is not None:
        head = ('model = "gpt-5.5"\nmodel_provider = "openai_fixture"\nmodel_reasoning_effort = "low"\n'
                + BASE_SETTINGS + '[model_providers.openai_fixture]\nname = "OpenAI"\n'
                f'base_url = "{fixture.url}"\nenv_key = "LOCAL_FIXTURE_TOKEN"\nwire_api = "responses"\n')
        return head, 'gpt-5.5', 'openai_fixture', None
    auth, profile = chatgpt_auth(args.gpt_profile, secrets)
    model = args.gpt_model
    if not model:
        configured = tomllib.loads((Path(profile['home']) / 'config.toml').read_text(encoding='utf-8-sig'))
        model = configured.get('model') or 'gpt-5.5'
    head = f'model = "{model}"\nmodel_reasoning_effort = "low"\n' + BASE_SETTINGS
    return head, model, 'openai', auth


def classify_cross_account(result):
    if result['status'] == 'completed' and not result.get('error'):
        return 'succeeded'
    text = ' '.join([result.get('error') or ''] + [note['detail'] for note in result.get('notes') or []])
    return 'rejected: encrypted content' if ENCRYPTED_ERROR.search(text) else 'failed: other'


def x_rebuild_checks(first, runtime_rebuild, *, summary_path, summary_gate):
    """X's rebuild checks from its first request summary and the runtime's rebuild log line.

    The log-based checks are always present: a missing log line (a runtime that logs the
    decision elsewhere, or a RUST_LOG that hides it) fails them instead of dropping them."""
    logged = bool(runtime_rebuild and 'source' in runtime_rebuild)
    source = runtime_rebuild.get('source') if logged else None
    checks = {'x_runtime_rebuild_logged': logged,
              # Steps 4 and 5 add a context note; the other steps do not.
              'x_context_note_consistent': logged and bool(first.get('context_note_present')) == (
                  source in ('PortableCheckpointWithRecentItems', 'RecentItemsOnly'))}
    if summary_path:
        checks['x_context_source_portable_summary'] = context_source(first) == 'portable summary'
        checks['x_portable_summary_sent'] = bool(first.get('portable_summary_present'))
        checks['x_facts_only_via_summary'] = bool(
            first.get('portable_summary_present') and not first.get('plant_prompt_present')
            and first.get('codeword_outside_summary') == 0 and first.get('colour_outside_summary') == 0)
        if summary_gate:
            checks['x_runtime_rebuilt_from_summary'] = logged and source in (
                'PortableCheckpoint', 'PortableCheckpointWithRecentItems')
    return checks


def context_source(request):
    if request.get('portable_summary_present'):
        return 'portable summary'
    if request.get('context_note_present'):
        return 'recent items only'
    if request.get('plant_prompt_present'):
        return 'full transcript'
    return 'unknown'


def finish_evidence(out, evidence, report, secrets):
    """Writes the scrubbed evidence and scans what this run wrote (report, evidence and kept
    fixture homes; nothing else in the output directory).

    The report goes to disk first as FAIL with secret_scan 'pending', so a scan that stops
    half-way never leaves a passing report behind. A file the scan flags is deleted (a
    failed delete is recorded, never raised) and the run fails; only a clean scan restores
    the concluded status. report['secret_scan'] ends as 'clean' or the findings."""
    concluded = report['status']
    try:
        (evidence / 'proxy-requests.json').write_text(secrets.dumps(report.get('proxy_requests', [])),
                                                      encoding='utf-8')
    except OSError as error:
        report.setdefault('evidence_errors', []).append('proxy-requests.json: ' + type(error).__name__)
        concluded = 'FAIL'
    path = out / 'report.json'
    report['status'], report['secret_scan'] = 'FAIL', 'pending'
    path.write_text(secrets.dumps(report), encoding='utf-8')
    findings = secrets.scan(out, [path, evidence, out / 'raw'])
    for finding in findings:
        flagged = out / finding['file']
        try:
            if flagged.exists():
                flagged.unlink()
                finding['action'] = 'deleted'
            else:
                finding['action'] = 'deleted (earlier finding)'
        except OSError as error:
            finding['action'] = 'delete failed: ' + type(error).__name__
    report['status'] = 'FAIL' if findings else concluded
    report['secret_scan'] = findings or 'clean'
    path.write_text(secrets.dumps(report), encoding='utf-8')
    if secrets.scan(out, [path]):
        withheld = findings + [{'file': 'report.json', 'match': 'final report', 'action': 'withheld'}]
        report['status'], report['secret_scan'] = 'FAIL', withheld
        path.write_text(json.dumps({'status': 'FAIL', 'error': 'The secret scan flagged the report; it was withheld.',
                                    'secret_scan': withheld}, indent=2), encoding='utf-8')


def negative_control_expected(summary_path):
    """Checks that fail by design when G's config leaves out the external provider."""
    expected = {'portable_summary_ready', 'portable_summary_non_empty'}
    if summary_path:
        expected |= {'x_context_source_portable_summary', 'x_portable_summary_sent',
                     'x_facts_only_via_summary', 'x_answer_codeword', 'x_answer_colour'}
    return expected


def conclude(report, *, negative_control, summary_path, raw_must_be_deleted):
    """Sets the final status. A run passes (or matches the negative control) only when it
    reached the end, every check passed (or failed exactly as expected), its evidence was
    written and, when required, its disposable homes are gone."""
    checks = report['checks']
    blockers = []
    if report.get('stage') != 'done':
        blockers.append('the run stopped at ' + str(report.get('stage')))
    if report.get('error'):
        blockers.append('the run raised an error')
    if report.get('evidence_errors'):
        blockers.append('the evidence could not be written')
    if raw_must_be_deleted and not report.get('raw_homes_deleted'):
        blockers.append('the disposable homes could not be deleted')
    if blockers:
        report['status_blockers'] = blockers
    if negative_control:
        expected = negative_control_expected(summary_path)
        failed = {name for name, passed in checks.items() if not passed}
        control = {'expected_failures': sorted(expected), 'failed': sorted(failed),
                   'unexpected_failures': sorted(failed - expected),
                   'unexpected_passes': sorted(expected - failed), 'x_context_source': report.get('x_context_source')}
        control['result'] = 'as expected' if not (
            blockers or control['unexpected_failures'] or control['unexpected_passes']) else 'unexpected'
        report['negative_control'] = control
        report['status'] = 'NEGATIVE_CONTROL_OK' if control['result'] == 'as expected' else 'NEGATIVE_CONTROL_UNEXPECTED'
    else:
        report['status'] = 'PASS' if not blockers and all(checks.values()) else 'FAIL'
    return report['status']


def write_evidence_and_dispose(report, roots, thread_ids, evidence, raw, secrets, delete_raw):
    """Writes the redacted rollouts, then always deletes the disposable homes (when asked),
    even if the rollouts could not be written. Errors are recorded, never raised."""
    try:
        thread_ids = [thread_id for thread_id in thread_ids if thread_id]
        if thread_ids:
            paths, _ = rollout_records(roots, thread_ids)
            report['redacted_rollouts'] = write_redacted_rollouts(paths, evidence / 'rollout', secrets)
    except Exception as error:
        report.setdefault('evidence_errors', []).append(
            'redacted rollouts: ' + secrets.scrub(f'{type(error).__name__}: {error}')[:300])
    finally:
        if delete_raw:
            try:
                remove_tree(raw)
            except Exception as error:
                report.setdefault('cleanup_errors', []).append('raw homes: ' + type(error).__name__)
        report['raw_homes_deleted'] = not raw.exists()


def run(args, plan):
    live = args.live
    summary_path = args.scenario == 'summary'
    second = bool(args.second_gpt_profile or args.fixture_second_account)
    binary = Path(args.runtime or resolve(ROOT)['runtime']).resolve(strict=True)
    out = (Path(args.output) if args.output else
           ROOT / 'artifacts/results' / f'cross-provider-{"live" if live else "fixture"}-{uuid4().hex[:8]}').resolve()
    if any(out.is_relative_to(path.resolve()) for path in FORBIDDEN_OUTPUT):
        raise SystemExit('Refusing to write into live manager state or deployed artifacts.')
    out.mkdir(parents=True, exist_ok=True)
    raw, evidence = out / 'raw', out / 'evidence'
    if not remove_tree(raw) or not remove_tree(evidence):
        raise SystemExit('The previous disposable homes or evidence could not be removed: ' + str(out))
    raw.mkdir()
    evidence.mkdir()
    shared, work = raw / 'shared-record', raw / 'work'
    g_home, x_home, g2_home = raw / 'home-g', raw / 'home-x', raw / 'home-g2'
    shared.mkdir()
    work.mkdir()
    secrets = Secrets()
    padding = filler(args.padding_words) if summary_path else None
    report = {'mode': 'live' if live else 'fixture', 'scenario': plan['scenario'], 'summary_gate': plan['summary_gate'],
              'started_at': now(), 'status': 'FAIL', 'stage': 'setup', 'plan': plan,
              'runtime': str(binary), 'prompts': {'plant': PLANT, 'ack': ACK, 'ask_x': ASK_X, 'ask_g': ASK_G},
              'steps': [], 'checks': {}}
    if padding:
        report['prompts']['padding'] = {'words': args.padding_words, 'chars': len(padding),
                                        'sha256_12': digest(padding), 'reply_asked': PAD_REPLY}
    if second:
        report['prompts']['ask_g2'] = ASK_G2
    with binary.open('rb') as stream:
        report['runtime_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    fixture = None if live else FixtureModel(args.fixture_second_account)
    clients, proxy, auth, auth2 = [], None, None, None

    def step(name, **values):
        report['stage'] = name
        report['steps'].append({'step': name, 'at': now(), **values})

    def start(home, alias, **kwargs):
        client = ManagedClient(binary, home, shared, alias, secrets=secrets, **kwargs)
        clients.append(client)
        return client

    def stop(client):
        client.close()
        clients.remove(client)
        report.setdefault('server_requests', []).extend(client.server_requests)

    checks = report['checks']
    try:
        x_config, x_env, binding, upstream, x_provider = external_side(
            args, raw, x_home, fixture, args.external_context_window if summary_path else None)
        for value in x_env.values():
            secrets.add(value)
        proxy = RecordingProxy(upstream)
        x_config = x_config.replace(f'base_url = "{upstream}"', f'base_url = "{proxy.url}"')
        if proxy.url not in x_config:
            raise RuntimeError('The external provider base URL could not be routed through the proxy.')
        x_settings = tomllib.loads(x_config)
        report['external'] = {'provider_id': x_provider, 'model': binding['model'],
                              'upstream_host': urlsplit(upstream).hostname,
                              'efforts': binding.get('supported_reasoning_efforts'),
                              'context_window': x_settings.get('model_context_window'),
                              'auto_compact_token_limit': x_settings.get('model_auto_compact_token_limit'),
                              'binding_context_window': binding.get('context_window')}
        if summary_path:
            checks['x_window_configured'] = (x_settings.get('model_context_window') == args.external_context_window
                                             == binding.get('context_window'))
        x_effort = args.x_effort or (binding.get('supported_reasoning_efforts') or [binding['reasoning_effort']])[0]
        g_head, g_model, g_provider, auth = gpt_side(args, fixture, secrets)
        if args.second_gpt_profile:
            auth2, _ = chatgpt_auth(args.second_gpt_profile, secrets)
            if auth2.expected == auth.expected:
                raise RuntimeError('The second GPT profile uses the same ChatGPT account as the first.')
        # The external provider in G's config turns the portable-summary gate on; G never gets its key.
        g_config = g_head if args.no_summary_gate else g_head + '\n' + provider_block(x_config, x_provider)
        report['gpt'] = {'model': g_model, 'provider_id': g_provider, 'effort': 'low',
                         'auth': 'chatgptAuthTokens (borrowed access token)' if auth else 'loopback fixture key'}
        if second:
            report['gpt2'] = {'auth': 'chatgptAuthTokens (borrowed access token, second account)' if auth2 else
                              f'loopback fixture key (simulated second account, {args.fixture_second_account})'}
        g_environment = {'CODEX_REQUEST_COMPRESSION': 'off'}

        step('G start')
        g = start(g_home, 'G', config_text=g_config, environment=g_environment, auth=auth)
        if auth:
            g.login()
        thread_id = g.rpc('thread/start', {'historyMode': 'paginated', 'cwd': str(work)})['thread']['id']
        report['thread_id'] = thread_id
        turns = [('G turn 1 (plant facts)', PLANT), ('G turn 2 (acknowledge)', ACK)]
        if padding:
            turns.append(('G turn 3 (padding filler)', padding))
        for name, text in turns:
            result = g.run_turn(thread_id, text, effort='low')
            step(name, **result)
            if result['status'] != 'completed':
                raise RuntimeError(name + ' did not complete')
        compaction = g.compact(thread_id)
        step('G compaction (thread/compact/start)', **compaction)
        if second and compaction['status'] == 'completed':
            # G2 later resumes this fork, so its request carries only what compaction left
            # (G's native encrypted compaction), not the reasoning of G's later turns, and
            # G's own thread never sees the second account.
            fork = g.rpc('thread/fork', {'threadId': thread_id, 'excludeTurns': True})
            report['g2_thread_id'] = fork['thread']['id']
            step('G fork for G2 (right after compaction)', fork_thread_id=report['g2_thread_id'])
        stop(g)
        if report.get('g2_thread_id'):
            # A paginated fork references its parent's history up to the fork point instead of
            # copying it, so what G2 replays is the parent's model-visible history right now.
            report['g2_fork_replays_encrypted_items'] = history_encrypted_items(
                rollout_records([shared, g_home], thread_id)[1])
        after_compaction = rollout_evidence([shared, g_home], thread_id)
        native = [c for c in after_compaction['checkpoints'] if (c['origin'] or {}).get('provider_family') == 'openai']
        checkpoint = native[-1] if native else None
        report['compaction_checkpoint'] = checkpoint
        portable = (checkpoint or {}).get('portable') or {}
        checks['compaction_completed'] = compaction['status'] == 'completed'
        checks['checkpoint_origin_openai'] = checkpoint is not None
        checks['checkpoint_native_encrypted_state'] = bool(checkpoint and checkpoint['native_encrypted_items'])
        checks['portable_summary_ready'] = (portable.get('status') or {}).get('state') == 'ready'
        checks['portable_summary_non_empty'] = portable.get('summary_chars', 0) > 0
        report['portable_summary_mentions'] = {'codeword': portable.get('summary_mentions_codeword'),
                                               'colour': portable.get('summary_mentions_colour')}
        if not checks['compaction_completed']:
            raise RuntimeError('compaction did not complete')

        step('X start')
        adapter = ExternalProfile({'CODEX_MANAGER_PRIMARY_MODEL': json.dumps(binding)})
        x = start(x_home, 'X', config_text=x_config, environment={**x_env, 'CODEX_REQUEST_COMPRESSION': 'off'},
                  adapter=adapter.request)
        resumed = x.rpc('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
        checks['x_same_thread'] = resumed['thread']['id'] == thread_id
        checks['x_uses_external_provider'] = resumed.get('modelProvider') == x_provider
        result = x.run_turn(thread_id, ASK_X, effort=x_effort)
        step('X turn (ask facts)', effort=x_effort, **result)
        stop(x)
        checks['x_turn_completed'] = result['status'] == 'completed'
        checks['x_answer_codeword'] = CODEWORD in result['answer']
        checks['x_answer_colour'] = COLOUR in result['answer']
        x_turns = [r for r in proxy.records if (r.get('request') or {}).get('kind') == 'turn']
        report['proxy_requests'] = proxy.records
        checks['x_requests_recorded'] = bool(x_turns)
        checks['x_no_encrypted_content'] = bool(x_turns) and all(
            not r['request'].get('encrypted_content_items') and not r['request'].get('has_native_compaction_item')
            for r in x_turns)
        after_x = rollout_evidence([shared, x_home], thread_id)
        rebuilt_x = [c for c in after_x['checkpoints'] if (c['kind'] or {}).get('type') == 'rebuilt'
                     and (c['origin'] or {}).get('provider_family') == x_provider]
        report['x_rebuilt_checkpoint'] = rebuilt_x[-1] if rebuilt_x else None
        checks['x_rebuilt_checkpoint_recorded'] = bool(rebuilt_x) and (rebuilt_x[-1]['kind'] or {}).get('from_family') == 'openai'
        checks['x_did_not_compact'] = all(
            (r.get('request') or {}).get('kind') == 'turn' for r in proxy.records if r.get('request')) and not any(
            (c['origin'] or {}).get('provider_family') == x_provider and (c['kind'] or {}).get('type') == 'compaction'
            for c in after_x['checkpoints'])
        first = x_turns[0]['request'] if x_turns else {}
        report['x_context_source'] = context_source(first) if x_turns else None
        runtime_rebuild = x.rebuild_logs[-1] if x.rebuild_logs else None
        report['x_rebuild'] = {
            'runtime_log': runtime_rebuild,
            'context_note_present': first.get('context_note_present'),
            'portable_summary_chars': first.get('summary_chars'),
            'plant_prompt_present': first.get('plant_prompt_present'),
            'codeword_outside_summary': first.get('codeword_outside_summary'),
            'colour_outside_summary': first.get('colour_outside_summary'),
            'padding_chars_kept': first.get('padding_chars'),
            'request_bytes': first.get('bytes'),
            'input_tokens': ((x_turns[0].get('response') or {}).get('usage') or {}).get('input_tokens') if x_turns else None}
        checks.update(x_rebuild_checks(first, runtime_rebuild, summary_path=summary_path,
                                       summary_gate=not args.no_summary_gate))

        step('G resume')
        g = start(g_home, 'G', config_text=g_config, environment=g_environment, auth=auth)
        if auth:
            g.login()
        resumed = g.rpc('thread/resume', {'threadId': thread_id, 'excludeTurns': True,
                                          'model': g_model, 'modelProvider': g_provider})
        checks['g_resumes_own_provider'] = resumed.get('modelProvider') == g_provider
        result = g.run_turn(thread_id, ASK_G, effort='low')
        step('G turn (ask what X was asked)', **result)
        report['g_rebuild'] = {'runtime_log': g.rebuild_logs[-1] if g.rebuild_logs else None}
        stop(g)
        checks['g_turn_completed'] = result['status'] == 'completed'
        checks['g_answer_codeword'] = CODEWORD in result['answer']
        after_g_evidence = rollout_evidence([shared, g_home, x_home], thread_id)
        after_g = after_g_evidence['checkpoints'][len(after_x['checkpoints']):]
        report['g_resume_checkpoints'] = after_g
        report['g_resume_path'] = (
            'rebuilt from its own native checkpoint (step 1)' if any(
                (c['kind'] or {}).get('type') == 'rebuilt' and (c['origin'] or {}).get('provider_family') == 'openai'
                and c['native_encrypted_items'] for c in after_g) else
            'rebuilt without a native checkpoint' if any((c['kind'] or {}).get('type') == 'rebuilt' for c in after_g) else
            'plain replay (no new checkpoint)')

        if second:
            # After G's last turn, on the fork taken right after compaction: steps 1-4 are
            # unaffected by the second account, and G2's request carries G's native encrypted
            # compaction as the only item from the first account.
            fork_id = report.get('g2_thread_id')
            checks['g2_fork_created'] = bool(fork_id) and fork_id != thread_id
            if not checks['g2_fork_created']:
                raise RuntimeError('the thread could not be forked for G2')
            replayed = report.get('g2_fork_replays_encrypted_items') or {}
            step('G2 start (second ChatGPT account)')
            g2 = start(g2_home, 'G2', config_text=g_config, environment=g_environment, auth=auth2)
            if auth2:
                g2.login()
            resumed = g2.rpc('thread/resume', {'threadId': fork_id, 'excludeTurns': True,
                                               'model': g_model, 'modelProvider': g_provider})
            checks['g2_resumes_fork'] = resumed['thread']['id'] == fork_id
            result = g2.run_turn(fork_id, ASK_G2, effort='low')
            step('G2 turn (second account replays native state)', **result)
            g2_rebuild = g2.rebuild_logs[-1] if g2.rebuild_logs else None
            stop(g2)
            outcome = classify_cross_account(result)
            report['cross_account'] = {
                'thread': 'fork of the thread right after G\'s compaction',
                'replayed_encrypted_items': replayed, 'runtime_rebuild': g2_rebuild,
                'outcome': outcome, 'turn_status': result['status'], 'error': result['error'],
                'answer_mentions_codeword': CODEWORD in result['answer']}
            # From the parent's recorded history at the fork point: what G2's first request
            # replays from account 1.
            checks['g2_replays_only_native_compaction'] = bool(replayed) and all(
                label in NATIVE_COMPACTION_TYPES for label in replayed)
            checks['g2_outcome_determined'] = outcome != 'failed: other'
            if fixture is not None:
                g2_requests = [r.get('request') or {} for r in fixture.requests if r.get('provider') == 'G2']
                report['cross_account']['simulated'] = args.fixture_second_account
                report['cross_account']['wire_encrypted_items'] = [r.get('encrypted_content_items') for r in g2_requests]
                checks['fixture_g2_replays_native_checkpoint'] = any(
                    r.get('has_native_compaction_item') for r in g2_requests)
                # Only the fixture sees G2's wire: nothing encrypted but the native compaction.
                checks['fixture_g2_wire_only_native_compaction'] = bool(g2_requests) and all(
                    label in NATIVE_COMPACTION_TYPES for r in g2_requests
                    for label in r.get('encrypted_content_items') or {})
                checks['fixture_g2_outcome_as_simulated'] = outcome == (
                    'succeeded' if args.fixture_second_account == 'accept' else 'rejected: encrypted content')

        final = rollout_evidence([shared, g_home, x_home, g2_home], thread_id)
        report['rollout'] = {key: value for key, value in final.items() if key not in ('checkpoints', 'rollout_files')}
        report['all_checkpoints'] = [{k: v for k, v in c.items() if k != 'portable'} | {
            'portable_status': (c['portable'] or {}).get('status')} for c in final['checkpoints']]
        if fixture is not None:
            report['fixture_requests'] = fixture.requests
            by_provider = {name: [r for r in fixture.requests if r.get('provider') == name] for name in ('G', 'G2', 'X')}
            report['request_count'] = {name: len(requests) for name, requests in by_provider.items()} | {
                'total': len(fixture.requests)}
            # Only the fixture sees G's wire: its last turn must replay its own native checkpoint
            # plus X's plaintext turn.
            last = (by_provider['G'][-1].get('request') or {}) if by_provider['G'] else {}
            checks['fixture_g_replays_native_checkpoint'] = bool(last.get('has_native_compaction_item')
                                                                 and last.get('x_question_present'))
        else:
            report['request_count'] = {'X': len(proxy.records),
                                       'GPT_estimated_from_rollout_usage': final['token_usage_records'] - sum(
                                           1 for r in proxy.records if (r.get('response') or {}).get('usage'))}
        report['server_request_methods'] = sorted(set(report.get('server_requests', [])))
        report['stage'] = 'done'  # the status is concluded after the evidence is written
    except Exception as error:
        report['error'] = secrets.scrub(f'{type(error).__name__}: {error}')[:1200]
    finally:
        for client in list(clients):
            try:
                client.close()
            except Exception:
                pass
        for label, closer in (('proxy', proxy), ('fixture', fixture)):
            if closer is not None:
                try:
                    closer.close()
                except Exception as error:
                    report.setdefault('cleanup_errors', []).append(label + ': ' + type(error).__name__)
        if proxy is not None:
            report.setdefault('proxy_requests', proxy.records)
        report['auth_refreshes_answered'] = sum(getattr(a, 'refreshes', 0) for a in (auth, auth2) if a)
        delete_raw = live or not args.keep_raw
        write_evidence_and_dispose(report, [shared, g_home, x_home, g2_home],
                                   [report.get('thread_id'), report.get('g2_thread_id')], evidence, raw, secrets,
                                   delete_raw)
        conclude(report, negative_control=args.no_summary_gate, summary_path=summary_path,
                 raw_must_be_deleted=delete_raw)
        report['finished_at'] = now()
        finish_evidence(out, evidence, report, secrets)
        print(json.dumps({'report': str(out / 'report.json'), 'status': report['status'], 'stage': report['stage'],
                          'scenario': report['scenario'], 'x_context_source': report.get('x_context_source'),
                          'x_rebuild': (report.get('x_rebuild') or {}).get('runtime_log'),
                          'checks': checks, 'negative_control': (report.get('negative_control') or {}).get('result'),
                          'cross_account': (report.get('cross_account') or {}).get('outcome'),
                          'error': secrets.scrub(report.get('error')) if report.get('error') else None,
                          'secret_scan': report['secret_scan']}, ensure_ascii=False))
    return report['status'] in ('PASS', 'NEGATIVE_CONTROL_OK') and report['secret_scan'] == 'clean'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runtime', help='runtime binary (default: the active manager runtime)')
    parser.add_argument('--output', help='output directory (default: artifacts/results/cross-provider-*)')
    parser.add_argument('--live', action='store_true', help='use real providers (billable)')
    parser.add_argument('--confirm-real-model-calls', action='store_true',
                        help='required with --live: acknowledges real, billable model requests')
    parser.add_argument('--gpt-profile', help='registered ChatGPT profile id (live)')
    parser.add_argument('--external-profile', help='registered external API profile id (live)')
    parser.add_argument('--gpt-model', help='GPT model (default: the GPT profile\'s configured model)')
    parser.add_argument('--x-effort', help='external reasoning effort (default: its lowest supported)')
    parser.add_argument('--summary-path', action='store_true',
                        help='summary-path scenario with default window and padding')
    parser.add_argument('--external-context-window', type=int, metavar='N',
                        help=f'summary path: X\'s context window in tokens (default {DEFAULT_SUMMARY_WINDOW})')
    parser.add_argument('--padding-tokens', type=int, metavar='N',
                        help='summary path: filler words (~tokens) G sends before compaction '
                             f'(default: a rebuild estimate of {PADDING_WINDOW_MARGIN}x the window)')
    parser.add_argument('--no-summary-gate', action='store_true',
                        help='fixture negative control: G\'s config omits the external provider')
    parser.add_argument('--second-gpt-profile', metavar='ID',
                        help='cross-account mode (live): a second ChatGPT profile resumes a fork taken '
                             'right after G\'s compaction; '
                             'needs the user\'s explicit approval for that account')
    parser.add_argument('--confirm-second-account', action='store_true',
                        help='required with --second-gpt-profile: the user approved using the second account')
    parser.add_argument('--fixture-second-account', choices=('accept', 'reject'),
                        help='fixture: run the G2 step against a simulated second account')
    parser.add_argument('--plan-only', action='store_true',
                        help='print the expected requests and input tokens, then exit without running')
    parser.add_argument('--keep-raw', action='store_true', help='fixture mode only: keep the disposable homes')
    args = parser.parse_args(argv)
    if args.live and not (args.confirm_real_model_calls and args.gpt_profile and args.external_profile):
        parser.error('--live needs --confirm-real-model-calls, --gpt-profile and --external-profile')
    if args.live and args.keep_raw:
        parser.error('--keep-raw is fixture-only; live homes are always deleted')
    if args.live and (args.no_summary_gate or args.fixture_second_account):
        parser.error('--no-summary-gate and --fixture-second-account are fixture-only')
    if args.second_gpt_profile and not (args.live and args.confirm_second_account):
        parser.error('--second-gpt-profile is live-only and needs --confirm-second-account')
    if args.second_gpt_profile and args.second_gpt_profile == args.gpt_profile:
        parser.error('--second-gpt-profile must be a different profile from --gpt-profile')
    summary = args.summary_path or args.external_context_window is not None or args.padding_tokens is not None
    args.scenario = 'summary' if summary else 'full'
    if summary:
        if args.external_context_window is None:
            args.external_context_window = DEFAULT_SUMMARY_WINDOW
        if not 4096 <= args.external_context_window <= 10_000_000:
            parser.error('--external-context-window must be between 4096 and 10000000')
        args.padding_words = (args.padding_tokens if args.padding_tokens is not None
                              else default_padding_words(args.external_context_window))
        if args.padding_words < 1:
            parser.error('--padding-tokens must be positive')
        text = filler(args.padding_words)
        _, upper = x_limits(args.external_context_window)
        if rebuild_tokens(text) <= upper:
            parser.error(f'the filler (rebuild estimate {rebuild_tokens(text)}) fits X\'s rebuild budget '
                         f'(at most {upper}); X would get the full transcript. Raise --padding-tokens or '
                         'lower --external-context-window.')
        if runtime_tokens(text) > MAX_PADDING_RUNTIME_TOKENS:
            parser.error(f'the filler (runtime estimate {runtime_tokens(text)}) exceeds '
                         f'{MAX_PADDING_RUNTIME_TOKENS} tokens; the resumed G context would no longer fit its '
                         'native checkpoint on ~100k-window GPT models. Lower --padding-tokens or '
                         '--external-context-window.')
    return args


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = parse_args()
    plan = make_plan(args)
    print(json.dumps({'plan': plan}, ensure_ascii=False), flush=True)
    if args.plan_only:
        raise SystemExit(0)
    raise SystemExit(0 if run(args, plan) else 1)


if __name__ == '__main__':
    main()
