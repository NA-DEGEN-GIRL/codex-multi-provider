"""Long-lived Claude tokens: storage, lending conditions, races and presentation.

Dummy tokens only (built at run time); DPAPI is replaced by a reversible fake
except in the one real round trip, which runs only on Windows.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import claude_long_lived_auth as long_lived
from manager_core import providers
from manager_core.claude_profiles import ClaudeProfiles, remote_helper_files
from manager_core.store import Store

TOKEN = 'sk-ant-' + 'oat01-' + 'Fixture' * 12
OTHER = 'sk-ant-' + 'oat01-' + 'Replaced' * 11
IDENTITY = 'a' * 64
SECOND = 'b' * 64
DAY = 86400
NOW = 1_800_000_000


def fake_crypt(calls=None):
    """Reversible, entropy-bound stand-in for DPAPI; never stores plaintext."""
    def crypt(value, protect, *, entropy=None):
        if calls is not None:
            calls.append(('protect' if protect else 'unprotect', entropy))
        key = hashlib.sha256(b'fixture-dpapi' + (entropy or b'')).digest()
        if protect:
            data = bytes(value)
            return b'FAKEDPAPI1' + key[:4] + bytes(b ^ key[i % 32] for i, b in enumerate(data))
        value = bytes(value)
        if not value.startswith(b'FAKEDPAPI1') or value[10:14] != key[:4]:
            raise providers.ProviderError('The API key could not be encrypted or unlocked for this Windows user.')
        return bytes(b ^ key[i % 32] for i, b in enumerate(value[14:]))
    return crypt


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root)
        self.profile = self.store.add_profile('Claude fixture', claude_settings={})
        self.pid = self.profile['id']
        self.facade = ClaudeProfiles(self.store)
        self.login(IDENTITY)
        self.calls = []
        crypt = patch.object(providers, '_crypt_secret', side_effect=fake_crypt(self.calls))
        crypt.start()
        self.addCleanup(crypt.stop)

    def login(self, identity, logged_in=True, pid=None):
        return self.facade.record_status(pid or self.pid, dict(logged_in=logged_in, account_identity=identity,
                                                               email='person@example.test'))

    def refresh(self, identity=IDENTITY, logged_in=True):
        return lambda pid: self.login(identity, logged_in, pid)

    def save(self, token=TOKEN, *, now=NOW, pid=None, **options):
        options.setdefault('attested', True)
        options.setdefault('refresh', self.refresh())
        return long_lived.save(self.root, pid or self.pid, token, now=now, **options)

    def meta(self, pid=None):
        return self.store.profile(pid or self.pid).get(long_lived.METADATA_KEY)

    def blobs(self, pid=None):
        directory = self.root / 'work/control-center/credentials/claude' / (pid or self.pid)
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    def lend(self, *, now=NOW + DAY, identity=IDENTITY, rejected=None):
        return long_lived.lend(self.root, self.pid, identity, rejected, now=now)


class FormatTests(unittest.TestCase):
    def test_wrapped_console_copy_is_joined_and_bad_input_is_refused_without_echo(self):
        wrapped = '  ' + TOKEN[:30] + '\r\n' + TOKEN[30:60] + '\n\t' + TOKEN[60:] + ' ​\n'
        self.assertEqual(long_lived.normalize(wrapped), TOKEN)
        cases = {
            'sk-ant-' + 'ort01-' + 'Fixture' * 12: long_lived.REFRESH_TOKEN_MESSAGE,
            'sk-ant-' + 'api03-' + 'Fixture' * 12: long_lived.API_KEY_MESSAGE,
            'Bearer ' + TOKEN: long_lived.FORMAT_MESSAGE,
            TOKEN + '!': long_lived.FORMAT_MESSAGE,
            TOKEN + 'é': long_lived.FORMAT_MESSAGE,
            TOKEN[:39]: long_lived.LENGTH_MESSAGE,
            'sk-ant-oat01-' + 'x' * 5000: long_lived.LENGTH_MESSAGE,
            'x' * 20000: long_lived.FORMAT_MESSAGE,
            '': long_lived.FORMAT_MESSAGE,
        }
        for value, message in cases.items():
            with self.subTest(value=value[:12]):
                with self.assertRaises(long_lived.LongLivedTokenError) as caught:
                    long_lived.normalize(value)
                self.assertEqual(str(caught.exception), message)
                self.assertNotIn('Fixture', str(caught.exception))
        for value in (None, 7, b'sk-ant-oat01-' + b'x' * 50):
            with self.assertRaises(long_lived.LongLivedTokenError):
                long_lived.normalize(value)
        # Two digits after the prefix are not required; only prefix and charset.
        self.assertTrue(long_lived.normalize('sk-ant-oat' + 'Z' * 40))


class SaveTests(Fixture):
    def test_save_encrypts_with_profile_entropy_and_keeps_only_metadata(self):
        result = self.save()
        meta = self.meta()
        self.assertTrue(result['saved'])
        self.assertFalse(result['replaced'])
        self.assertEqual(long_lived._canonical(meta['credential_id']), meta['credential_id'])
        self.assertEqual(self.blobs(), [meta['credential_id'] + '.dpapi'])
        self.assertEqual((meta['saved_at'], meta['minted_at'], meta['minted_source']), (NOW, NOW, 'paste_time'))
        self.assertEqual((meta['validity_days'], meta['expires_at']), (365, NOW + 365 * DAY))
        self.assertEqual((meta['account_identity'], meta['account_label']), (IDENTITY, 'p***@example.test'))
        self.assertIsNone(meta['rejected_credential_id'])
        self.assertEqual(self.calls, [('protect', b'codex-manager/claude-long-lived/v1\0' + self.pid.encode())])
        blob = (self.root / 'work/control-center/credentials/claude' / self.pid / self.blobs()[0]).read_bytes()
        state = self.store.path.read_bytes()
        digest = hashlib.sha256(TOKEN.encode()).hexdigest()
        for stored in (blob, state, json.dumps(result).encode()):
            self.assertNotIn(TOKEN.encode(), stored)
            self.assertNotIn(b'Fixture', stored)
            self.assertNotIn(digest.encode(), stored)
        self.assertEqual(result['long_lived']['state'], 'active')

    def test_plaintext_is_encrypted_before_the_slow_identity_check(self):
        order = []
        def refresh(pid):
            order.append(('refresh', list(self.calls)))
            return self.login(IDENTITY)
        self.save(refresh=refresh)
        self.assertEqual(order[0][1][0][0], 'protect')

    def test_save_requires_attestation_login_identity_and_a_claude_profile(self):
        with self.assertRaises(long_lived.LongLivedTokenError):
            self.save(attested=False)
        with self.assertRaises(long_lived.LongLivedTokenError):
            self.save(attested='true')
        with self.assertRaisesRegex(long_lived.LongLivedTokenError, '로그인'):
            self.save(refresh=self.refresh(logged_in=False))
        with self.assertRaisesRegex(long_lived.LongLivedTokenError, '로그인'):
            self.save(refresh=self.refresh(identity='not-an-identity'))
        codex = self.store.add_profile('Codex fixture')
        with self.assertRaisesRegex(long_lived.LongLivedTokenError, 'Claude'):
            self.save(pid=codex['id'])
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(removed_at='2026-01-01T00:00:00Z'))
        with self.assertRaisesRegex(long_lived.LongLivedTokenError, '복원'):
            self.save()
        self.assertIsNone(self.meta())
        self.assertEqual(self.blobs(), [])

    def test_identity_change_during_save_is_refused_and_leaves_no_file(self):
        real, armed = Store.mutate, []
        def refresh(pid):
            status = self.login(IDENTITY)
            armed.append(True)
            return status
        def racing(store, operation):
            if armed:
                armed.clear()  # Another login lands between the check and the metadata write.
                real(store, lambda data: store.profile(self.pid, data).update(claude_account_identity=SECOND))
            return real(store, operation)
        with patch.object(Store, 'mutate', autospec=True, side_effect=racing), \
                self.assertRaisesRegex(long_lived.LongLivedTokenError, '바뀌었습니다'):
            self.save(refresh=refresh)
        self.assertIsNone(self.meta())
        self.assertEqual(self.blobs(), [])

    def test_issue_date_and_validity_shown_by_the_cli(self):
        yesterday = time.strftime('%Y-%m-%d', time.localtime(NOW - DAY))
        self.save(minted_on=yesterday, validity_days=30)
        meta = self.meta()
        self.assertEqual(meta['minted_source'], 'user_date')
        self.assertLessEqual(meta['minted_at'], NOW - DAY)
        self.assertEqual(meta['expires_at'], meta['minted_at'] + 30 * DAY)
        today = time.strftime('%Y-%m-%d', time.localtime(NOW))
        self.save(minted_on=today)
        self.assertEqual((self.meta()['minted_at'], self.meta()['minted_source']), (NOW, 'paste_time'))
        tomorrow = time.strftime('%Y-%m-%d', time.localtime(NOW + DAY))
        for options in (dict(minted_on=tomorrow), dict(minted_on='2026/01/01'), dict(minted_on='2026-02-30'),
                        dict(validity_days=0), dict(validity_days=367), dict(validity_days='365'),
                        dict(validity_days=True)):
            with self.subTest(options=options), self.assertRaises(long_lived.LongLivedTokenError):
                self.save(OTHER, **options)
        old = time.strftime('%Y-%m-%d', time.localtime(NOW - 365 * DAY))
        with self.assertRaisesRegex(long_lived.LongLivedTokenError, '만료'):
            self.save(OTHER, minted_on=old)
        self.assertEqual(self.lend()['accessToken'], TOKEN)

    def test_crash_between_blob_and_metadata_keeps_the_previous_token(self):
        self.save()
        first = self.meta()['credential_id']
        with patch.object(Store, 'mutate', side_effect=RuntimeError('fixture crash')):
            with self.assertRaises(RuntimeError):
                self.save(OTHER, refresh=lambda pid: dict(logged_in=True))
        self.assertEqual(self.meta()['credential_id'], first)
        self.assertEqual(self.blobs(), [first + '.dpapi'])
        self.assertEqual(self.lend()['accessToken'], TOKEN)
        # A generation written by a process that died before its metadata
        # update is never used, and the next save removes it.
        orphan = self.root / 'work/control-center/credentials/claude' / self.pid / (str(uuid4()) + '.dpapi')
        orphan.write_bytes(b'FAKEDPAPI1 orphan')
        self.assertEqual(self.lend()['credentialId'], first)
        replaced = self.save(OTHER)
        self.assertTrue(replaced['replaced'])
        self.assertEqual(self.blobs(), [self.meta()['credential_id'] + '.dpapi'])
        self.assertEqual(self.lend()['accessToken'], OTHER)

    def test_delete_is_local_and_idempotent(self):
        self.save()
        result = long_lived.delete(self.root, self.pid)
        self.assertTrue(result['existed'])
        self.assertIn('해지되지는 않으며', result['message'])
        self.assertIsNone(self.meta())
        self.assertFalse((self.root / 'work/control-center/credentials/claude' / self.pid).exists())
        self.assertIsNone(self.lend())
        again = long_lived.delete(self.root, self.pid)
        self.assertFalse(again['existed'])
        self.assertEqual(again['long_lived']['state'], 'none')

    def test_replace_requires_an_existing_token(self):
        with self.assertRaises(long_lived.LongLivedTokenError):
            long_lived.replace(self.root, self.pid, TOKEN, attested=True, refresh=self.refresh(), now=NOW)
        self.save()
        long_lived.replace(self.root, self.pid, OTHER, attested=True, refresh=self.refresh(), now=NOW)
        self.assertEqual(self.lend()['accessToken'], OTHER)


class LendTests(Fixture):
    def setUp(self):
        super().setUp()
        self.save()
        self.cid = self.meta()['credential_id']

    def test_lend_returns_the_long_lived_shape(self):
        self.assertEqual(self.lend(), {'accessToken': TOKEN, 'expiresAt': NOW + 365 * DAY,
                                       'accountIdentity': IDENTITY, 'credentialSource': 'longLivedToken',
                                       'credentialId': self.cid})
        self.assertEqual(self.calls[-1], ('unprotect', b'codex-manager/claude-long-lived/v1\0' + self.pid.encode()))

    def test_expiry_margin_identity_and_profile_conditions(self):
        expires = NOW + 365 * DAY
        self.assertIsNotNone(self.lend(now=expires - DAY - 1))
        self.assertIsNone(self.lend(now=expires - DAY))
        self.assertIsNone(self.lend(identity=SECOND))
        self.assertIsNone(self.lend(identity='A' * 64))
        self.assertIsNone(long_lived.lend(self.root, 'not-a-profile', IDENTITY))
        self.login(SECOND)
        self.assertIsNone(self.lend())
        self.assertIsNone(self.lend(identity=SECOND))
        self.login(None, logged_in=False)
        self.assertIsNone(self.lend())
        self.login(IDENTITY)
        self.assertIsNotNone(self.lend())
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(removed_at='2026-01-01T00:00:00Z'))
        self.assertIsNone(self.lend())

    def test_unreadable_blob_is_informational_and_cleared_by_a_good_read(self):
        path = self.root / 'work/control-center/credentials/claude' / self.pid / (self.cid + '.dpapi')
        good = path.read_bytes()
        path.write_bytes(b'not a protected blob')
        self.assertIsNone(self.lend())
        self.assertEqual(self.meta()['unreadable_at'], NOW + DAY)
        self.assertEqual(long_lived.state(self.store.profile(self.pid), now=NOW + DAY)['state'], 'unreadable')
        path.write_bytes(good)
        self.assertEqual(self.lend()['accessToken'], TOKEN)
        self.assertIsNone(self.meta()['unreadable_at'])

    def test_blob_of_another_profile_or_generation_is_never_used(self):
        directory = self.root / 'work/control-center/credentials/claude' / self.pid
        path = directory / (self.cid + '.dpapi')
        other = self.store.add_profile('Claude second', claude_settings={})
        self.login(IDENTITY, pid=other['id'])
        long_lived.save(self.root, other['id'], OTHER, attested=True, now=NOW,
                        refresh=lambda pid: self.login(IDENTITY, pid=pid))
        other_meta = self.meta(other['id'])
        foreign = (self.root / 'work/control-center/credentials/claude' / other['id']
                   / (other_meta['credential_id'] + '.dpapi')).read_bytes()
        path.write_bytes(foreign)  # Wrong entropy and wrong IDs.
        self.assertIsNone(self.lend())
        # Same profile entropy, but the blob names another generation.
        swapped = long_lived._encrypt(self.pid, str(uuid4()), OTHER)
        path.write_bytes(swapped)
        self.assertIsNone(self.lend())
        self.assertIsNotNone(self.meta()['unreadable_at'])

    @unittest.skipUnless(os.name == 'nt', 'NTFS junctions')
    def test_linked_credential_folder_is_refused(self):
        import _winapi
        directory = self.root / 'work/control-center/credentials/claude' / self.pid
        moved = self.root / 'elsewhere'
        directory.rename(moved)
        _winapi.CreateJunction(str(moved), str(directory))
        self.assertIsNone(self.lend())
        with self.assertRaisesRegex(long_lived.LongLivedTokenError, '저장하지 못했습니다'):
            self.save(OTHER)

    def test_rejection_is_recorded_only_for_the_current_credential(self):
        self.assertFalse(long_lived.record_rejection(self.root, self.pid, str(uuid4()), now=NOW + DAY))
        self.assertFalse(long_lived.record_rejection(self.root, self.pid, self.cid.upper(), now=NOW + DAY))
        self.assertFalse(long_lived.record_rejection(self.root, self.pid, '../' + self.cid, now=NOW + DAY))
        self.assertIsNone(self.meta()['rejected_credential_id'])
        self.assertTrue(long_lived.record_rejection(self.root, self.pid, self.cid, now=NOW + DAY))
        meta = self.meta()
        self.assertEqual((meta['rejected_credential_id'], meta['rejection_kind'], meta['last_rejected_at']),
                         (self.cid, 'rejected', NOW + DAY))
        self.assertIsNone(self.lend())
        view = long_lived.state(self.store.profile(self.pid), now=NOW + DAY)
        self.assertEqual((view['state'], view['retry_available']), ('rejected', True))
        # 다시 시도 clears the mark (metadata only).
        result = long_lived.retry(self.root, self.pid, now=NOW + DAY)
        self.assertEqual(result['long_lived']['state'], 'active')
        self.assertEqual(self.lend()['credentialId'], self.cid)
        # A new save gets a new ID, which carries no rejection.
        long_lived.record_rejection(self.root, self.pid, self.cid, now=NOW + DAY)
        self.save(OTHER)
        self.assertIsNone(self.meta()['rejected_credential_id'])
        self.assertEqual(self.lend()['accessToken'], OTHER)

    def test_a_401_near_the_computed_expiry_is_classified_as_expiry(self):
        near = NOW + 365 * DAY - DAY - 3600
        self.assertTrue(long_lived.record_rejection(self.root, self.pid, self.cid, now=near))
        self.assertEqual(self.meta()['rejection_kind'], 'expired')
        self.assertEqual(long_lived.state(self.store.profile(self.pid), now=near)['state'], 'expired')

    def test_rejecting_read_records_and_never_returns_that_credential(self):
        self.assertIsNone(self.lend(rejected=self.cid))
        self.assertEqual(self.meta()['rejected_credential_id'], self.cid)
        self.assertIsNone(self.lend(rejected='not-a-uuid'))
        long_lived.retry(self.root, self.pid)
        # The rejection names an older generation: the newer token is lent.
        self.save(OTHER)
        self.assertEqual(self.lend(rejected=self.cid)['accessToken'], OTHER)
        self.assertIsNone(self.meta()['rejected_credential_id'])
        # Even if the mark cannot be written, that credential is not returned.
        current = self.meta()['credential_id']
        with patch.object(long_lived, 'record_rejection', side_effect=RuntimeError('store busy')):
            self.assertIsNone(self.lend(rejected=current))

    def test_replace_while_lending_rereads_the_metadata_once(self):
        real = long_lived._read_blob
        seen = []
        def racing(root, pid, cid):
            if not seen:
                seen.append(cid)
                self.save(OTHER)  # Deletes the generation this read is about to open.
            return real(root, pid, cid)
        with patch.object(long_lived, '_read_blob', side_effect=racing):
            lent = self.lend()
        self.assertEqual(seen, [self.cid])
        self.assertEqual(lent['accessToken'], OTHER)
        self.assertNotEqual(lent['credentialId'], self.cid)
        self.assertIsNone(self.meta()['unreadable_at'])

    def test_a_missing_blob_without_a_replace_is_marked_unreadable(self):
        (self.root / 'work/control-center/credentials/claude' / self.pid / (self.cid + '.dpapi')).unlink()
        self.assertIsNone(self.lend())
        self.assertIsNotNone(self.meta()['unreadable_at'])

    def test_delete_while_lending_falls_back(self):
        real = long_lived._read_blob
        def racing(root, pid, cid):
            long_lived.delete(self.root, self.pid)
            return real(root, pid, cid)
        with patch.object(long_lived, '_read_blob', side_effect=racing):
            self.assertIsNone(self.lend())
        self.assertIsNone(self.meta())


class LockTests(Fixture):
    def test_concurrent_saves_leave_one_consistent_generation(self):
        errors = []
        def worker(token):
            try:
                self.save(token)
            except Exception as error:  # Surfaced below.
                errors.append(error)
        threads = [threading.Thread(target=worker, args=(token,)) for token in (TOKEN, OTHER) * 3]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        meta = self.meta()
        self.assertEqual(self.blobs(), [meta['credential_id'] + '.dpapi'])
        self.assertIn(self.lend()['accessToken'], (TOKEN, OTHER))

    def test_rejection_and_unreadable_marks_take_only_the_store_lock(self):
        self.save()
        cid = self.meta()['credential_id']
        done = threading.Event()
        with long_lived._credential_lock(self.root, self.pid):
            def mark():
                long_lived.record_rejection(self.root, self.pid, cid, now=NOW + DAY)
                long_lived._mark_unreadable(self.store, self.pid, cid, NOW + DAY)
                self.assertIsNone(self.lend())
                done.set()
            thread = threading.Thread(target=mark)
            thread.start()
            self.assertTrue(done.wait(10), 'a mark or lend waited for the credential lock')
            thread.join(10)
        self.assertEqual(self.meta()['rejected_credential_id'], cid)


class PresentationTests(Fixture):
    def view(self, now=NOW, **ssh):
        usage = {**dict(bindings=0, supported=0), **ssh} if ssh else None
        return long_lived.state(self.store.profile(self.pid), now=now, ssh=usage)

    def test_every_state_and_its_warning(self):
        view = self.view()
        self.assertEqual((view['state'], view['label'], view['attention'], view['expanded']),
                         ('none', '장기 토큰 · 없음', False, False))
        self.assertEqual(self.view(bindings=1)['expanded'], True)
        self.save()
        expires = NOW + 365 * DAY
        day = time.strftime('%Y-%m-%d', time.localtime(expires))
        view = self.view()
        self.assertEqual((view['state'], view['label'], view['attention'], view['lendable']),
                         ('active', f'설정됨 · p***@example.test · 만료 {day}', False, True))
        view = self.view(expires - 10 * DAY)
        self.assertEqual((view['state'], view['days_left'], view['card_text']), ('expiring', 10, '토큰 확인'))
        self.assertIn('p***@example.test', view['label'])
        self.assertIn('(10일 남음)', view['label'])
        view = self.view(expires - 3600)
        self.assertEqual((view['state'], view['lendable']), ('stopped', False))
        self.assertTrue(view['label'].startswith('만료(사용 중지)'))
        self.assertEqual(self.view(expires)['state'], 'expired')
        cid = self.meta()['credential_id']
        long_lived.record_rejection(self.root, self.pid, cid, now=NOW + DAY)
        view = self.view(NOW + DAY)
        self.assertEqual(view['state'], 'rejected')
        self.assertIn('지금은 Windows 로그인 토큰 사용', view['label'])
        self.login(None, logged_in=False)
        self.assertIn('Windows 로그인도 필요', self.view(NOW + DAY)['label'])
        long_lived.retry(self.root, self.pid)
        self.assertEqual(self.view()['state'], 'login_needed')
        self.login(SECOND)
        self.assertEqual(self.view()['state'], 'other_account')
        self.login(IDENTITY)
        self.store.mutate(lambda data: self.store.profile(self.pid, data)[long_lived.METADATA_KEY].update(version=9))
        self.assertEqual(self.view()['state'], 'unreadable')
        for now in (NOW, expires - 10 * DAY, expires - 3600, expires):
            for text in (self.view(now)['attention_text'], self.view(now)['card_text']):
                self.assertNotIn('@', text)

    def test_ssh_usage_counts_owned_and_preset_bindings(self):
        other = self.store.add_profile('Codex owner')
        def bind(data):
            self.store.profile(self.pid, data)['remote_bindings'] = [dict(alias='h1', prepared=True),
                                                                     dict(alias='h2', prepared=False)]
            self.store.profile(other['id'], data)['remote_bindings'] = [
                dict(alias='h3', prepared=True, execution_presets_version=1, claude_credential_sources=1)]
        self.store.mutate(bind)
        profiles = self.store.read()['profiles']
        self.assertEqual(long_lived.ssh_usage(profiles, self.pid, {}), dict(bindings=1, supported=0))
        presets = self.root / 'work/control-center/execution-presets.json'
        presets.write_text(json.dumps(dict(version=1, revision=1, defaults={}, bindings={}, presets={
            str(uuid4()): dict(profile_id=other['id'], current_revision=1, deleted=False,
                               revisions={'1': dict(roles=[dict(name='Claude', profile_id=self.pid)])}),
            str(uuid4()): dict(profile_id=self.pid, current_revision=1, deleted=True,
                               revisions={'1': dict(roles=[dict(name='x', profile_id='ignored')])})})))
        users = long_lived.preset_users(self.root)
        self.assertEqual(users, {self.pid: {other['id']}})
        usage = long_lived.ssh_usage(profiles, self.pid, users)
        self.assertEqual(usage, dict(bindings=2, supported=1))
        text = long_lived.state(self.store.profile(self.pid), ssh=usage)['ssh']['text']
        self.assertIn('1곳에서 사용', text)
        self.assertIn('재준비', long_lived.state({}, ssh=dict(bindings=1, supported=0))['ssh']['text'])


class SecrecyTests(Fixture):
    def test_token_never_reaches_logs_errors_state_or_results(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root_logger = logging.getLogger()
        previous = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        self.addCleanup(root_logger.removeHandler, handler)
        self.addCleanup(root_logger.setLevel, previous)
        texts = []
        texts.append(json.dumps(self.save()))
        cid = self.meta()['credential_id']
        lent = self.lend()
        self.assertEqual(lent['accessToken'], TOKEN)
        for attempt in (lambda: self.save(TOKEN + '!'), lambda: self.save(TOKEN, attested=False),
                        lambda: self.save(TOKEN, refresh=self.refresh(logged_in=False)),
                        lambda: self.save(TOKEN, validity_days=0)):
            with self.assertRaises(long_lived.LongLivedTokenError) as caught:
                attempt()
            error = caught.exception
            while error is not None:  # The whole chain a traceback could print.
                texts.append(repr(error))
                error = error.__cause__ or error.__context__
        long_lived.record_rejection(self.root, self.pid, cid, now=NOW + DAY)
        texts.append(json.dumps(long_lived.retry(self.root, self.pid)))
        texts.append(json.dumps(long_lived.state(self.store.profile(self.pid))))
        texts.append(json.dumps(long_lived.delete(self.root, self.pid)))
        texts.append(self.store.path.read_text(encoding='utf-8'))
        texts.extend(handler.format(record) for record in records)
        for text in texts:
            self.assertNotIn(TOKEN, text)
            self.assertNotIn('Fixture', text)

    def test_module_never_ships_to_ssh_hosts(self):
        files = remote_helper_files()
        self.assertNotIn('manager_core/claude_long_lived_auth.py', files)
        for name, content in files.items():
            self.assertNotIn('claude_long_lived_auth', content, name)


class ProfileRemovalTests(Fixture):
    def test_removal_keeps_the_token_and_says_so(self):
        from manager_core.profile_lifecycle import ProfileLifecycle
        instances = Mock()
        instances.observe.return_value = {'status': 'not_started'}
        lifecycle = ProfileLifecycle(self.store, instances)
        plain = self.store.add_profile('Claude plain', claude_settings={})
        self.assertNotIn('장기 토큰', lifecycle.remove(plain['id'])['message'])
        self.save()
        result = lifecycle.remove(self.pid)
        self.assertTrue(result['long_lived_token_kept'])
        self.assertIn('장기 토큰도 이 PC에 남아', result['message'])
        self.assertIsNotNone(self.meta())
        self.assertEqual(len(self.blobs()), 1)
        self.assertIsNone(self.lend())


class ControlCenterTests(Fixture):
    def center(self):
        from control_center import ControlCenter
        control = ControlCenter.__new__(ControlCenter)
        control.store, control.root = self.store, self.root
        control._mutex, control._request_gate_lock = threading.RLock(), threading.Lock()
        return control

    def test_commands_drop_the_token_and_return_presentation_only(self):
        control = self.center()
        status = dict(logged_in=True, account_identity=IDENTITY, email='person@example.test')
        args = dict(profile_id=self.pid, token=TOKEN, attested=True)
        with patch('manager_core.claude_auth.auth_status', return_value=status):
            result = control.dispatch('claude.token.save', args)
        self.assertNotIn('token', args)
        self.assertEqual(result['long_lived']['state'], 'active')
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(self.lend(now=int(time.time()) + DAY)['accessToken'], TOKEN)
        bad = control.request(dict(id='bad', command='claude.token.save',
                                   args=dict(profile_id=self.pid, token=TOKEN + 'é', attested=True)))
        self.assertEqual(bad['error']['code'], 'claude_token_format')
        self.assertNotIn('Fixture', json.dumps(bad, ensure_ascii=False))
        cid = self.meta()['credential_id']
        long_lived.record_rejection(self.root, self.pid, cid)
        retried = control.dispatch('claude.token.retry', dict(profile_id=self.pid))
        self.assertEqual(retried['long_lived']['state'], 'active')
        removed = control.dispatch('claude.token.remove', dict(profile_id=self.pid))
        self.assertTrue(removed['removed'])
        self.assertIsNone(self.meta())
        with patch.object(long_lived, 'launch_issue_console', return_value=dict(status='issue_started')) as issue:
            self.assertEqual(control.dispatch('claude.token.issue', dict(profile_id=self.pid))['status'], 'issue_started')
        issue.assert_called_once_with(self.pid)
        codex = self.store.add_profile('Codex fixture')
        with self.assertRaises(ValueError):
            control.dispatch('claude.token.issue', dict(profile_id=codex['id']))

    def test_state_poll_projects_presentation_and_hides_metadata(self):
        from control_center import ControlCenter
        self.save(now=int(time.time()))
        with patch('manager_core.usage_refresh.read_quota', side_effect=RuntimeError('fixture: no network')), \
                patch('pathlib.Path.home', return_value=self.root / 'user-home'), \
                patch('manager_core.accounts.Accounts.list', return_value=[]):
            center = ControlCenter(self.root)
            center._remote_reconcile_started = True
            center._sync_at = time.monotonic()
            center.remote.list_hosts = Mock(return_value=[])
            center.usage_refresh.schedule = Mock()
            center.claude_usage.schedule = Mock()
            center.claude_usage.value = Mock(return_value={})
            with patch('manager_core.note_forks.refresh'):
                result = center.state()
        profile = next(p for p in result['profiles'] if p['id'] == self.pid)
        self.assertNotIn(long_lived.METADATA_KEY, profile)
        self.assertEqual(profile[long_lived.PRESENTATION_KEY]['state'], 'active')
        self.assertIn(long_lived.METADATA_KEY, self.store.profile(self.pid))
        text = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn(TOKEN, text)


@unittest.skipUnless(os.name == 'nt', 'A new console is a Windows launch')
class IssueConsoleTests(unittest.TestCase):
    def test_setup_token_runs_scrubbed_in_a_throwaway_config_directory(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        pid = str(uuid4())
        environ = dict(LOCALAPPDATA=str(base), PATH='', CLAUDE_CODE_OAUTH_TOKEN='sk-ant-' + 'oat01-' + 'Env' * 20,
                       ANTHROPIC_API_KEY='fixture-key', CLAUDE_CONFIG_DIR=str(base / 'elsewhere'), KEEP='1')
        stale = base / 'codex-multi-provider' / 'claude-setup-token' / 'issue-stale'
        stale.mkdir(parents=True)
        os.utime(stale, (time.time() - 3 * DAY, time.time() - 3 * DAY))
        cli = base / 'claude.exe'
        with patch('manager_core.claude_auth.discover_cli', return_value=cli), \
                patch('manager_core.claude_auth.cli_version', return_value='2.1.282'), \
                patch.object(long_lived.subprocess, 'Popen') as popen:
            popen.return_value.pid = 4242
            result = long_lived.launch_issue_console(pid, environ=environ)
        self.assertEqual(result['status'], 'issue_started')
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(cli), 'setup-token'])
        self.assertEqual(kwargs['creationflags'], long_lived.subprocess.CREATE_NEW_CONSOLE)
        self.assertTrue(kwargs['close_fds'])
        for stream in ('stdin', 'stdout', 'stderr'):
            self.assertIsNone(kwargs.get(stream))
        env = kwargs['env']
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertEqual(env['KEEP'], '1')
        config = Path(env['CLAUDE_CONFIG_DIR'])
        self.assertEqual(config, Path(kwargs['cwd']))
        self.assertEqual(config.parent, base / 'codex-multi-provider' / 'claude-setup-token')
        self.assertNotEqual(config, base / 'codex-multi-provider' / 'claude-profiles' / pid)
        self.assertEqual(list(config.iterdir()), [])
        self.assertFalse(stale.exists())


@unittest.skipUnless(os.name == 'nt', 'Windows user DPAPI is required')
class RealDpapiTests(unittest.TestCase):
    def test_entropy_bound_round_trip(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        store = Store(root)
        profile = store.add_profile('Claude DPAPI', claude_settings={})
        other = store.add_profile('Claude other', claude_settings={})
        facade = ClaudeProfiles(store)
        refresh = lambda pid: facade.record_status(pid, dict(logged_in=True, account_identity=IDENTITY))
        long_lived.save(root, profile['id'], TOKEN, attested=True, refresh=refresh, now=NOW)
        cid = store.profile(profile['id'])[long_lived.METADATA_KEY]['credential_id']
        blob = (root / 'work/control-center/credentials/claude' / profile['id'] / (cid + '.dpapi')).read_bytes()
        self.assertNotIn(TOKEN.encode(), blob)
        self.assertNotIn(b'Fixture', blob)
        self.assertEqual(long_lived.lend(root, profile['id'], IDENTITY, now=NOW + DAY)['accessToken'], TOKEN)
        with self.assertRaises(providers.ProviderError):
            providers._crypt_secret(blob, False)  # The provider-key path passes no entropy.
        with self.assertRaises(providers.ProviderError):
            long_lived._unprotect(blob, other['id'])
        self.assertEqual(json.loads(long_lived._unprotect(blob, profile['id']))['credential_id'], cid)


if __name__ == '__main__':
    unittest.main()
