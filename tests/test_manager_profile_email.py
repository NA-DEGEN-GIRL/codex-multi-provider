"""Synthetic account metadata only: no real credentials, login or model calls."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.profile_email import read, email_label
from manager_core.proxy_auth import account_fingerprint
from manager_core.claude_auth import sanitize_status, ClaudeError
from manager_core.claude_profiles import ClaudeProfiles
from manager_core.store import Store


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'fixture.' + payload + '.fixture'


class ProfileEmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.profile = self.store.add_profile('01')
        self.pid = self.profile['id']
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(
            auth_mode='native', account_fingerprint=account_fingerprint('account-one')))
        self.auth = Path(self.profile['home']) / 'auth.json'
        self.auth.parent.mkdir(parents=True)

    def save(self, **changes):
        tokens = dict(account_id='account-one', refresh_token='fixture-refresh-never-return',
            access_token='fixture-access-never-return', id_token=jwt(dict(email='one@example.test', exp=1,
                **{'https://api.openai.com/auth': {'chatgpt_account_id': 'account-one'}})))
        tokens.update(changes)
        self.auth.write_text(json.dumps(dict(tokens=tokens)), encoding='utf-8')

    def test_explicit_reveal_only_and_expired_label_does_not_change_auth_or_store(self):
        self.save()
        before = self.store.path.read_bytes(), self.auth.read_bytes()
        with self.assertRaises(ValueError):
            read(self.store, self.pid)
        with self.assertRaises(ValueError):
            read(self.store, self.pid, reveal='true')
        self.assertEqual(read(self.store, self.pid, reveal=True),
            dict(profile_id=self.pid, state='available', email='one@example.test'))
        self.assertEqual(before, (self.store.path.read_bytes(), self.auth.read_bytes()))
        self.assertNotIn('one@example.test', json.dumps(self.store.read()))

    def test_no_identity_mix_or_fallback_to_another_profile(self):
        self.save(account_id='account-two')
        self.assertEqual('unavailable', read(self.store, self.pid, reveal=True)['state'])
        self.save(id_token=jwt({'email': 'two@example.test',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'account-two'}}))
        self.assertEqual('unavailable', read(self.store, self.pid, reveal=True)['state'])
        self.save()
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(
            auth_mode='source', source_home=str(self.auth.parent / 'missing-source')))
        self.assertEqual('unavailable', read(self.store, self.pid, reveal=True)['state'])

    def test_source_account_and_access_token_profile_claim(self):
        self.save(id_token=None, access_token=jwt({'https://api.openai.com/profile': {'email': 'source@example.test'}}))
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(
            auth_mode='source', source_home=str(self.auth.parent)))
        self.assertEqual('source@example.test', read(self.store, self.pid, reveal=True)['email'])

    def test_manager_dispatch_requires_opt_in_and_returns_only_display_metadata(self):
        from control_center import ControlCenter
        self.save()
        center = ControlCenter.__new__(ControlCenter)
        center.store = self.store
        with self.assertRaises(ValueError):
            center.dispatch('profile.email', {'profile_id': self.pid})
        self.assertEqual(dict(profile_id=self.pid, state='available', email='one@example.test'),
            center.dispatch('profile.email', {'profile_id': self.pid, 'reveal': True}))

    def test_missing_malformed_and_oversized_metadata_never_leaks_parser_input(self):
        for content in ('not-json-secret', '[]', '{"tokens":[]}', 'x' * (1024 * 1024 + 1)):
            with self.subTest(size=len(content)):
                self.auth.write_text(content, encoding='utf-8')
                self.assertEqual(dict(profile_id=self.pid, state='unavailable'), read(self.store, self.pid, reveal=True))
        self.save(id_token=jwt({'email': 'line\nbreak@example.test'}))
        self.assertNotIn('email', read(self.store, self.pid, reveal=True))

    def test_external_and_removed_profiles_do_not_read_credentials(self):
        for changes in ({'auth_mode': 'external'}, {'auth_mode': 'native', 'removed_at': 'fixture'}):
            self.store.mutate(lambda data: self.store.profile(self.pid, data).update(changes))
            with patch('manager_core.profile_email._codex', side_effect=AssertionError('must not read')):
                self.assertEqual('not_applicable', read(self.store, self.pid, reveal=True)['state'])

    def test_claude_explicit_allowlist_is_transient_and_identity_bound(self):
        raw = dict(loggedIn=True, email='claude@example.test', orgId='fixture-org', accessToken='fixture-secret')
        regular = sanitize_status(raw)
        revealed = sanitize_status(raw, reveal_email=True)
        self.assertNotIn('claude@example.test', json.dumps(regular))
        self.assertNotIn('fixture-secret', json.dumps(revealed))
        profile = self.store.add_profile('Claude', claude_settings={})
        claude = ClaudeProfiles(self.store)
        claude.record_status(profile['id'], revealed)
        self.assertNotIn('claude@example.test', json.dumps(self.store.read()))
        with patch('manager_core.claude_auth.auth_status', return_value=revealed) as status:
            self.assertEqual('claude@example.test', read(self.store, profile['id'], reveal=True)['email'])
            status.assert_called_once_with(profile['id'], reveal_email=True)
        with patch('manager_core.claude_auth.auth_status', return_value={**revealed, 'account_identity': 'f' * 64}):
            self.assertNotIn('email', read(self.store, profile['id'], reveal=True))
        with patch('manager_core.claude_auth.auth_status', side_effect=ClaudeError('fixture', 'secret-error')):
            self.assertEqual(dict(profile_id=profile['id'], state='unavailable'), read(self.store, profile['id'], reveal=True))
        self.assertNotIn('account_email', sanitize_status({**raw, 'loggedIn': False}, reveal_email=True))

    def test_reassignment_while_reading_discards_result(self):
        self.save()
        def change(_):
            self.store.mutate(lambda data: self.store.profile(self.pid, data).update(account_fingerprint='changed'))
            return 'one@example.test'
        with patch('manager_core.profile_email._codex', side_effect=change):
            self.assertNotIn('email', read(self.store, self.pid, reveal=True))

    def test_email_label_rejects_masks_controls_and_arbitrary_text(self):
        for value in (None, 123, '', 'x', 'x@@example.test', 'x***@example.test',
                      'x\x7f@example.test', 'x @example.test', '<x@example.test>', 'x' * 321 + '@test'):
            self.assertIsNone(email_label(value))


if __name__ == '__main__':
    unittest.main()
