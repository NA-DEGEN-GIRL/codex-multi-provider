"""Synthetic protocol and credential fixtures; never use a real login or network."""

import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.proxy_auth import AuthProxy, AuthTokens, LoginNeededError, account_fingerprint, read_existing_tokens


NOW = 1_800_000_000


def jwt(account='fixture-account', expiry=NOW + 3600, **extra):
    claims = {'exp': expiry, 'https://api.openai.com/auth': {'chatgpt_account_id': account}, **extra}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'fixture-header.' + payload + '.fixture-signature'


def tokens(account='fixture-account', expiry=NOW + 3600):
    return AuthTokens(jwt(account, expiry), account)


class ProxyAuthTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.loaded = []
        self.credential = tokens()
        self.proxy = AuthProxy(token_loader=self.load, clock=lambda: self.now)

    def load(self):
        self.loaded.append(True)
        return self.credential

    def initialize(self, proxy=None, early_notification=False):
        proxy = proxy or self.proxy
        request = {'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'fixture', 'version': '1'},
            'capabilities': {'optOutNotificationMethods': ['fixture/notice']}}}
        output = proxy.process('frontend', request)
        self.assertTrue(output.runtime[0]['params']['capabilities']['experimentalApi'])
        if early_notification:
            self.assertEqual(proxy.process('frontend', {'method': 'initialized'}).runtime, [])
        output = proxy.process('runtime', {'id': 1, 'result': {'userAgent': 'fixture'}})
        self.assertEqual(output.frontend, [{'id': 1, 'result': {'userAgent': 'fixture'}}])
        if early_notification:
            self.assertEqual(output.runtime[0], {'method': 'initialized'})
        login = output.runtime[-1]
        self.assertEqual(login['method'], 'account/login/start')
        self.assertEqual(login['params'], self.credential.login_params())
        self.assertEqual(proxy.state, 'authenticating')
        return login['id']

    def ready(self):
        login_id = self.initialize()
        result = self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'chatgptAuthTokens'}})
        self.assertEqual(result.frontend, [])
        self.assertEqual(self.proxy.state, 'ready')
        return login_id

    def test_missing_environment_is_transparent_and_does_not_read_credentials(self):
        with patch.dict('os.environ', {}, clear=True):
            proxy = AuthProxy()
        request = {'id': 1, 'method': 'account/login/start', 'params': {'type': 'chatgpt'}}
        self.assertEqual(proxy.state, 'disabled')
        self.assertEqual(proxy.process('frontend', request).runtime, [request])
        response = {'id': 1, 'result': {'type': 'chatgpt', 'loginId': 'fixture'}}
        self.assertEqual(proxy.process('runtime', response).frontend, [response])

    def test_explicit_none_overrides_source_environment(self):
        with patch.dict('os.environ', {'CODEX_MANAGER_AUTH_SOURCE': 'never-read-this-fixture'}):
            self.assertFalse(AuthProxy(source_home=None).bound)
            self.assertTrue(AuthProxy().bound)

    def test_enables_experimental_without_mutating_input(self):
        request = {'id': 2, 'method': 'initialize', 'params': {'capabilities': {'other': True}}}
        before = copy.deepcopy(request)
        result = self.proxy.process('frontend', request)
        self.assertEqual(request, before)
        self.assertEqual(result.runtime[0]['params']['capabilities'], {'other': True, 'experimentalApi': True})
        self.assertEqual(self.loaded, [])

    def test_successful_initialize_starts_auth_without_optional_notification(self):
        self.initialize()
        self.assertEqual(len(self.loaded), 1)

    def test_native_desktop_config_read_without_initialized_is_released_after_auth(self):
        login_id = self.initialize()
        config = {'id': 'configRequirements/read:fixture', 'method': 'configRequirements/read'}
        turn = {'id': 3, 'method': 'turn/start', 'params': {'threadId': 'fixture'}}
        for message in (config, turn):
            self.assertEqual(self.proxy.process('frontend', message).runtime, [])
        result = self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'chatgptAuthTokens'}})
        self.assertEqual(result.runtime, [config, turn])
        response = {'id': config['id'], 'result': {'requirements': None}}
        self.assertEqual(self.proxy.process('runtime', response).frontend, [response])
        self.assertEqual(len(self.loaded), 1)

    def test_optional_initialized_after_response_does_not_start_a_second_login(self):
        login_id = self.initialize()
        notification = {'method': 'initialized'}
        self.assertEqual(self.proxy.process('frontend', notification).runtime, [notification])
        self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'chatgptAuthTokens'}})
        self.assertEqual(self.proxy.process('frontend', notification).runtime, [notification])
        self.assertEqual(len(self.loaded), 1)

    def test_initialized_before_response_is_reordered_safely(self):
        self.initialize(early_notification=True)

    def test_normal_requests_and_notifications_wait_then_flush_in_order(self):
        pending = [{'id': 2, 'method': 'thread/list'}, {'method': 'fixture/event'},
                   {'id': 3, 'method': 'turn/start', 'params': {'threadId': 'fixture'}}]
        for message in pending:
            self.assertEqual(self.proxy.process('frontend', message).runtime, [])
        login_id = self.initialize()
        result = self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'chatgptAuthTokens'}})
        self.assertEqual(result.runtime, pending)
        self.assertEqual(result.frontend, [])
        next_request = {'id': 4, 'method': 'thread/read'}
        self.assertEqual(self.proxy.process('frontend', next_request).runtime, [next_request])

    def test_internal_auth_error_is_suppressed_and_pending_calls_fail_closed(self):
        login_id = self.initialize()
        self.proxy.process('frontend', {'id': 2, 'method': 'turn/start'})
        result = self.proxy.process('runtime', {'id': login_id, 'error': {
            'code': -1, 'message': 'SENSITIVE-FIXTURE-ERROR', 'data': self.credential.login_params()}})
        self.assertEqual(self.proxy.state, 'login_needed')
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[0]['id'], 2)
        self.assertEqual(result.frontend[0]['error']['data']['status'], 'login_needed')
        self.assertNotIn('SENSITIVE-FIXTURE-ERROR', json.dumps(result.frontend + result.events))
        result = self.proxy.process('frontend', {'id': 3, 'method': 'turn/start'})
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[0]['error']['data']['reason'], 'auth_rejected')

    def test_unexpected_auth_success_shape_fails_closed(self):
        login_id = self.initialize()
        self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'apiKey'}})
        self.assertEqual(self.proxy.reason, 'invalid_auth_response')

    def test_auth_timeout_fails_pending_and_late_response_cannot_recover(self):
        login_id = self.initialize()
        self.proxy.process('frontend', {'id': 2, 'method': 'turn/start'})
        self.now += 31
        result = self.proxy.poll()
        self.assertEqual(self.proxy.reason, 'auth_timeout')
        self.assertEqual(result.frontend[0]['id'], 2)
        result = self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'chatgptAuthTokens'}})
        self.assertEqual(self.proxy.state, 'login_needed')
        self.assertEqual(result.frontend, [])

    def test_source_exception_does_not_expose_details(self):
        def broken():
            raise RuntimeError('fixture-access-token-DO-NOT-EXPOSE')
        proxy = AuthProxy(token_loader=broken, clock=lambda: NOW)
        proxy.process('frontend', {'id': 1, 'method': 'initialize'})
        proxy.process('frontend', {'id': 2, 'method': 'turn/start'})
        result = proxy.process('runtime', {'id': 1, 'result': {}})
        self.assertEqual(proxy.reason, 'unreadable_auth_source')
        self.assertEqual(result.runtime, [])
        self.assertEqual(result.frontend[1]['error']['data']['reason'], 'unreadable_auth_source')
        self.assertNotIn('DO-NOT-EXPOSE', json.dumps(result.frontend + result.events))

    def test_refresh_reads_new_access_token_without_forwarding_request(self):
        self.ready()
        self.credential = tokens(expiry=NOW + 7200)
        request = {'id': 30, 'method': 'account/chatgptAuthTokens/refresh',
                   'params': {'reason': 'unauthorized', 'previousAccountId': 'fixture-account'}}
        result = self.proxy.process('runtime', request)
        self.assertEqual(result.frontend, [])
        self.assertEqual(result.runtime, [{'id': 30, 'result': self.credential.refresh_result()}])
        self.assertEqual(len(self.loaded), 2)
        self.assertEqual(self.proxy.state, 'ready')
        self.assertEqual(self.proxy.process('frontend', {'id': 30, 'result': {'accessToken': 'stale'}}).runtime, [])

    def test_unrelated_frontend_server_response_passes(self):
        self.ready()
        response = {'id': 'approval-1', 'result': {'decision': 'decline'}}
        self.assertEqual(self.proxy.process('frontend', response).runtime, [response])

    def test_source_account_change_on_refresh_is_rejected(self):
        self.ready()
        self.credential = tokens(account='another-fixture-account')
        result = self.proxy.process('runtime', {'id': 8, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}})
        self.assertEqual(self.proxy.reason, 'account_mismatch')
        self.assertEqual(result.frontend, [])
        self.assertEqual(result.runtime[0]['error']['data']['status'], 'login_needed')
        self.assertNotIn('accessToken', json.dumps(result.runtime))

    def test_refresh_for_different_previous_account_is_rejected(self):
        self.ready()
        self.proxy.process('runtime', {'id': 8, 'method': 'account/chatgptAuthTokens/refresh',
                                      'params': {'previousAccountId': 'another-account'}})
        self.assertEqual(self.proxy.reason, 'account_mismatch')

    def test_expired_source_refresh_fails_without_using_refresh_token(self):
        self.ready()
        self.credential = tokens(expiry=NOW - 1)
        result = self.proxy.process('runtime', {'id': 8, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}})
        self.assertEqual(self.proxy.reason, 'expired_access_token')
        self.assertEqual(result.frontend, [])
        self.assertIn('error', result.runtime[0])

    def test_initial_expected_account_is_enforced(self):
        proxy = AuthProxy(token_loader=self.load, expected_account_id='other', clock=lambda: NOW)
        proxy.process('frontend', {'id': 1, 'method': 'initialize'})
        proxy.process('runtime', {'id': 1, 'result': {}})
        proxy.process('frontend', {'method': 'initialized'})
        self.assertEqual(proxy.reason, 'account_mismatch')

    def test_ready_exposes_only_noncredential_identity_fingerprint(self):
        login_id = self.initialize()
        result = self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'chatgptAuthTokens'}})
        expected = account_fingerprint('fixture-account')
        self.assertEqual(result.events, [{'state': 'ready', 'account_fingerprint': expected}])
        self.assertEqual(len(expected), 64)
        self.assertEqual(self.proxy.account_fingerprint, expected)
        self.assertNotIn('fixture-account', json.dumps(result.events))

    def test_initial_expected_fingerprint_is_enforced(self):
        for account, succeeds in [('fixture-account', True), ('another-account', False)]:
            with self.subTest(account=account):
                proxy = AuthProxy(token_loader=self.load,
                                  expected_account_fingerprint=account_fingerprint(account), clock=lambda: NOW)
                proxy.process('frontend', {'id': 1, 'method': 'initialize'})
                result = proxy.process('runtime', {'id': 1, 'result': {}})
                if succeeds:
                    self.assertEqual(proxy.state, 'authenticating')
                    self.assertEqual(result.runtime[0]['method'], 'account/login/start')
                else:
                    self.assertEqual(proxy.reason, 'account_mismatch')
                    self.assertEqual(result.runtime, [])

    def test_auth_mode_change_after_ready_blocks_new_turns(self):
        for mode in (None, 'apikey', 'chatgpt', 'headers'):
            with self.subTest(mode=mode):
                self.proxy = AuthProxy(token_loader=self.load, clock=lambda: NOW)
                self.ready()
                notice = {'method': 'account/updated', 'params': {'authMode': mode, 'planType': None}}
                result = self.proxy.process('runtime', notice)
                self.assertEqual(self.proxy.reason, 'bound_auth_changed')
                self.assertEqual(result.frontend, [notice])
                self.assertEqual(self.proxy.process('frontend', {'id': 50, 'method': 'turn/start'}).runtime, [])

    def test_correct_external_auth_notification_keeps_ready(self):
        self.ready()
        notice = {'method': 'account/updated', 'params': {'authMode': 'chatgptAuthTokens', 'planType': 'pro'}}
        self.assertEqual(self.proxy.process('runtime', notice).frontend, [notice])
        self.assertEqual(self.proxy.state, 'ready')

    def test_refresh_before_authenticated_handshake_does_not_read_source(self):
        result = self.proxy.process('runtime', {'id': 30, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}})
        self.assertEqual(self.proxy.reason, 'refresh_before_auth')
        self.assertEqual(self.loaded, [])
        self.assertEqual(result.frontend, [])
        self.assertIn('error', result.runtime[0])

    def test_account_controls_are_rejected_but_read_usage_is_allowed(self):
        self.ready()
        for method in ('account/login/start', 'account/logout', 'account/login/cancel',
                       'account/sessions/add', 'account/sessions/switch', 'account/sessions/logout'):
            with self.subTest(method=method):
                result = self.proxy.process('frontend', {'id': 20, 'method': method})
                self.assertEqual(result.runtime, [])
                self.assertEqual(result.frontend[0]['error']['data']['status'], 'account_bound')
                self.assertEqual(self.proxy.state, 'ready')
        request = {'id': 21, 'method': 'account/rateLimits/read'}
        self.assertEqual(self.proxy.process('frontend', request).runtime, [request])

    def test_initialize_failure_rejects_queued_requests(self):
        self.proxy.process('frontend', {'id': 1, 'method': 'initialize'})
        self.proxy.process('frontend', {'id': 2, 'method': 'turn/start'})
        result = self.proxy.process('runtime', {'id': 1, 'error': {'code': -1, 'message': 'fixture-init-error'}})
        self.assertEqual(self.proxy.reason, 'initialize_failed')
        self.assertEqual([message['id'] for message in result.frontend], [1, 2])
        self.assertEqual(self.loaded, [])

    def test_bounded_queue_fails_closed(self):
        proxy = AuthProxy(token_loader=self.load, clock=lambda: NOW, max_queued=1)
        proxy.process('frontend', {'id': 2, 'method': 'turn/start'})
        result = proxy.process('frontend', {'id': 3, 'method': 'turn/start'})
        self.assertEqual(proxy.reason, 'auth_queue_full')
        self.assertEqual([message['id'] for message in result.frontend], [2, 3])
        self.assertEqual(result.runtime, [])

    def test_pending_payload_byte_budget_is_cumulative_and_failure_releases_it(self):
        proxy = AuthProxy(token_loader=self.load, clock=lambda: NOW, max_queued_bytes=2500)
        messages = [{'id': index, 'method': 'turn/start', 'params': {'input': 'x' * 1000}}
                    for index in (2, 3, 4)]
        for message in messages[:2]:
            self.assertEqual(proxy.process('frontend', message).runtime, [])
        self.assertGreater(proxy._pending_bytes, 2000)
        result = proxy.process('frontend', messages[2])
        self.assertEqual(proxy.reason, 'auth_queue_full')
        self.assertEqual([message['id'] for message in result.frontend], [2, 3, 4])
        self.assertEqual(result.runtime, [])
        self.assertEqual(proxy._pending_bytes, 0)
        self.assertEqual(len(proxy._pending), 0)
        self.assertNotIn('x' * 1000, json.dumps(result.frontend + result.events))

    def test_queue_byte_budget_counts_utf8_and_allows_exact_limit(self):
        message = {'id': 2, 'method': 'turn/start', 'params': {'input': '한글' * 100}}
        byte_count = len(json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        proxy = AuthProxy(token_loader=self.load, clock=lambda: NOW, max_queued_bytes=byte_count)
        self.assertEqual(proxy.process('frontend', message).frontend, [])
        self.assertEqual(proxy._pending_bytes, byte_count)
        proxy = AuthProxy(token_loader=self.load, clock=lambda: NOW, max_queued_bytes=byte_count - 1)
        result = proxy.process('frontend', message)
        self.assertEqual(proxy.reason, 'auth_queue_full')
        self.assertEqual(result.frontend[0]['id'], 2)
        self.assertEqual(proxy._pending_bytes, 0)

    def test_successful_auth_flushes_bytes_and_retains_validated_payload_copy(self):
        message = {'id': 2, 'method': 'turn/start', 'params': {'input': 'fixture-original'}}
        before = copy.deepcopy(message)
        self.proxy.process('frontend', message)
        self.assertGreater(self.proxy._pending_bytes, 0)
        message['params']['input'] = 'changed-after-queue'
        login_id = self.initialize()
        result = self.proxy.process('runtime', {'id': login_id, 'result': {'type': 'chatgptAuthTokens'}})
        self.assertEqual(result.runtime, [before])
        self.assertEqual(self.proxy._pending_bytes, 0)
        self.assertEqual(len(self.proxy._pending), 0)

    def test_late_initialize_success_does_not_revive_terminal_failure(self):
        proxy = AuthProxy(token_loader=self.load, clock=lambda: NOW, max_queued=0)
        proxy.process('frontend', {'id': 1, 'method': 'initialize'})
        proxy.process('frontend', {'method': 'initialized'})
        proxy.process('frontend', {'id': 2, 'method': 'turn/start'})
        self.assertEqual(proxy.reason, 'auth_queue_full')
        result = proxy.process('runtime', {'id': 1, 'result': {}})
        self.assertEqual(proxy.state, 'login_needed')
        self.assertEqual(result.runtime, [])
        self.assertEqual(self.loaded, [])

    def test_duplicate_initialize_success_after_error_cannot_start_login(self):
        self.proxy.process('frontend', {'id': 1, 'method': 'initialize'})
        self.proxy.process('frontend', {'method': 'initialized'})
        self.proxy.process('runtime', {'id': 1, 'error': {'code': -1, 'message': 'fixture'}})
        result = self.proxy.process('runtime', {'id': 1, 'result': {}})
        self.assertEqual(self.proxy.reason, 'initialize_failed')
        self.assertEqual(result.runtime, [])
        self.assertEqual(self.loaded, [])

    def test_private_response_and_repr_do_not_leak_credentials(self):
        login_id = self.initialize()
        self.assertNotIn(self.credential.access_token, repr(self.credential))
        refresh = self.proxy.process('runtime', {'id': 30, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}})
        self.assertNotIn(self.credential.access_token, repr(refresh))
        self.assertNotIn(self.credential.access_token, json.dumps(refresh.events))
        spurious = self.proxy.process('frontend', {'id': login_id, 'result': {'accessToken': 'injected'}})
        self.assertEqual(spurious.runtime, [])


class TokenSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.file = self.home / 'auth.json'

    def write(self, token=None, account='fixture-account'):
        self.file.write_text(json.dumps({'tokens': {
            'access_token': jwt() if token is None else token, 'account_id': account,
            'refresh_token': 'fixture-refresh-NEVER-SEND', 'id_token': 'fixture-id-NEVER-SEND'}}), encoding='utf-8')

    def test_reads_only_required_fields_and_leaves_source_unchanged(self):
        self.write()
        before = self.file.read_bytes()
        result = read_existing_tokens(self.home, now=NOW)
        self.assertEqual(result, tokens())
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(list(self.home.iterdir()), [self.file])
        self.assertNotIn('NEVER-SEND', json.dumps(result.login_params()))
        self.assertNotIn('NEVER-SEND', json.dumps(result.refresh_result()))

    def test_bad_files_and_tokens_return_sanitized_reasons(self):
        invalid = [('', 'missing_access_token'), ('fixture-malformed-secret', 'invalid_access_token'),
                   (jwt(expiry=NOW + 120), 'expired_access_token'),
                   (jwt(account='other-account'), 'account_mismatch'),
                   (jwt(expiry=True), 'invalid_token_expiry'),
                   (jwt(expiry=None), 'invalid_token_expiry')]
        for token, reason in invalid:
            with self.subTest(reason=reason):
                self.write(token)
                with self.assertRaises(LoginNeededError) as raised:
                    read_existing_tokens(self.home, now=NOW)
                self.assertEqual(raised.exception.reason, reason)
                self.assertNotIn(token, str(raised.exception)) if token else None

    def test_absent_auth_and_malformed_json_do_not_expose_path_or_content(self):
        for content in (None, 'fixture-secret-is-not-json', '[]', '{}'):
            with self.subTest(content=content):
                if content is not None:
                    self.file.write_text(content, encoding='utf-8')
                with self.assertRaises(LoginNeededError) as raised:
                    read_existing_tokens(self.home, now=NOW)
                self.assertNotIn(str(self.home), str(raised.exception))
                self.assertNotIn('fixture-secret', str(raised.exception))

    def test_token_without_account_claim_is_allowed_when_source_identifies_account(self):
        self.write(jwt(**{'https://api.openai.com/auth': {}}))
        result = read_existing_tokens(self.home, now=NOW)
        self.assertEqual(result.account_id, 'fixture-account')


if __name__ == '__main__':
    unittest.main()
