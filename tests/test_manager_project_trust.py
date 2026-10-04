"""Explicit trust sharing, independent of credentials and machine paths."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import tomllib
from unittest.mock import Mock, patch
import unittest

from manager_core.project_trust import merge_projects, path_key, sync
from test_manager_remote_common import COMMON


class TrustTests(unittest.TestCase):
    def test_sync_preserves_unrelated_config_and_does_not_read_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = Mock(directory=root / 'work/control-center')
            home = store.directory / 'profiles/selected/codex'
            source = root / 'user/.codex'
            home.mkdir(parents=True)
            source.mkdir(parents=True)
            profile = {'id': 'selected', 'home': str(home)}
            store.profile.return_value = profile
            store.read.return_value = {'profiles': [profile]}
            project = str(root / 'approved')
            (source / 'config.toml').write_text('[projects.' + json.dumps(project) + ']\ntrust_level="trusted"\n', encoding='utf-8')
            before = '# Keep comment\nmodel="keep"\n'
            (home / 'config.toml').write_text(before, encoding='utf-8')
            (home / 'auth.json').write_text('private-sentinel')
            with patch('manager_core.store.Store', return_value=store), patch.object(Path, 'home', return_value=root / 'user'):
                self.assertEqual(sync(root, 'selected'), 1)
                self.assertEqual(sync(root, 'selected'), 0)
            text = (home / 'config.toml').read_text(encoding='utf-8')
            self.assertTrue(text.startswith(before))
            self.assertEqual(tomllib.loads(text)['model'], 'keep')
            self.assertEqual((home / 'auth.json').read_text(), 'private-sentinel')

    def test_windows_profile_switch_reuses_explicit_approval_only(self):
        source = {'projects': {r'D:\code\app': {'trust_level': 'trusted', 'extra': 'never-copy'},
                               '/srv/remote': {'trust_level': 'trusted'}}, 'token': 'never-copy'}
        target = {'model': 'preserved'}
        self.assertEqual(merge_projects(target, [source], windows=True), 1)
        self.assertEqual(target, {'model': 'preserved', 'projects': {
            r'D:\code\app': {'trust_level': 'trusted'}}})
        self.assertEqual(merge_projects(target, [source], windows=True), 0)

    def test_normalized_target_deny_wins_and_donor_conflict_is_not_granted(self):
        target = {'projects': {r'd:\CODE\app': {'trust_level': 'untrusted'}}}
        approved = {'projects': {r'D:\code\app': {'trust_level': 'trusted'},
                                  r'D:\other': {'trust_level': 'trusted'}}}
        denied = {'projects': {r'd:\OTHER': {'trust_level': 'untrusted'}}}
        before = deepcopy(target)
        self.assertEqual(merge_projects(target, [approved, denied], windows=True), 0)
        self.assertEqual(target, before)

    def test_unsafe_or_cross_machine_paths_are_not_imported(self):
        for path in ('relative', '/srv/a', 'D:relative', 'D:\\bad\nvalue'):
            self.assertIsNone(path_key(path, windows=True), path)
        for path in ('relative', r'D:\code', '/bad\nvalue'):
            self.assertIsNone(path_key(path, windows=False), path)

    def test_remote_trust_uses_only_posix_approvals_and_keeps_local_deny(self):
        target = {'projects': {'/srv/denied': {'trust_level': 'untrusted'}}, 'model': 'preserved'}
        source = {'projects': {'/srv/app': {'trust_level': 'trusted'},
                               '/srv/denied': {'trust_level': 'trusted'},
                               r'D:\code': {'trust_level': 'trusted'}}, 'token': 'never-copy'}
        COMMON.merge_project_trust(target, [source])
        self.assertEqual(target, {'model': 'preserved', 'projects': {
            '/srv/app': {'trust_level': 'trusted'}, '/srv/denied': {'trust_level': 'untrusted'}}})


if __name__ == '__main__':
    unittest.main()
