"""Exercise runtime-validated permission persistence for the local proxy.

The expectations follow the deployed app-server schema captured in
``tests/fixtures/app-server-permission-schema-89.json``: resume accepts
``sandbox`` / ``approvalPolicy`` / ``approvalsReviewer`` (no experimental
``permissions`` or ``runtimeWorkspaceRoots``), ``ThreadSettings`` exposes
``activePermissionProfile``, and ``ConfigRequirements`` types
``allowedPermissionProfiles`` as ``dict[str, bool]`` plus
``allowedSandboxModes`` as a mode list.
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import permission_selection as permissions
from manager_core.runtime_proxy import managed_client_message


PROFILE = '5f2c1b8e-3a44-4f0e-9b17-2c6d5a7e1f30'
THREAD = '7c9d2e41-0b53-4a6f-8e21-9d3f5b7a0c62'
OTHER = '1a4b6c8d-2e30-4f52-8a74-9b1c3d5e7f90'
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'app-server-permission-schema-89.json'
RESPONSE = {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
            'sandbox': {'type': 'dangerFullAccess'},
            'activePermissionProfile': {'id': ':danger-full-access', 'extends': None}}
BARE = {'sandbox': 'danger-full-access', 'approval_policy': 'never',
        'approvals_reviewer': 'user'}
CUSTOM = {'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
          'sandbox': {'type': 'workspaceWrite', 'networkAccess': False,
                      'writableRoots': ['C:\\work']}}


class StubExternal:
    def request(self, message):
        return message


class PermissionSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'work/control-center'

    def remember(self, value=RESPONSE, thread=THREAD):
        self.assertTrue(permissions.remember(self.directory, PROFILE, thread, value))
        return permissions.recall(self.directory, PROFILE, thread)

    def test_selection_keeps_only_the_reproducible_bare_policy(self):
        # The built-in full profile is reported as a bare policy; the profile
        # id itself is never replayed.
        self.assertEqual(permissions.selection(RESPONSE), BARE)
        self.assertEqual(permissions.selection({'sandbox': {'type': 'dangerFullAccess'}}),
                         {'sandbox': 'danger-full-access'})
        self.assertEqual(permissions.selection({'sandboxPolicy': {'type': 'dangerFullAccess'}}),
                         {'sandbox': 'danger-full-access'})
        # Named profiles and restricted workspace policies are not replayable
        # against the deployed resume schema.
        self.assertIsNone(permissions.selection({'permissions': ':workspace'}))
        self.assertIsNone(permissions.selection({'activePermissionProfile': {'id': ':workspace'},
                                                 'sandboxPolicy': {'type': 'workspaceWrite'}}))
        # A custom profile is not the built-in full profile even when its
        # sandbox policy looks unrestricted.
        self.assertIsNone(permissions.selection(
            {'activePermissionProfile': {'id': ':custom-full', 'extends': None},
             'sandbox': {'type': 'dangerFullAccess'}}))
        self.assertEqual(permissions.selection(
            {'activePermissionProfile': {'id': ':danger-full-access', 'extends': None},
             'sandbox': {'type': 'dangerFullAccess'}}), {'sandbox': 'danger-full-access'})
        self.assertEqual(permissions.selection(
            {'activePermissionProfile': None, 'sandbox': {'type': 'dangerFullAccess'}}),
            {'sandbox': 'danger-full-access'})
        self.assertIsNone(permissions.selection(
            {'sandboxPolicy': {'type': 'workspaceWrite', 'networkAccess': False,
                               'writableRoots': ['C:\\work']}}))
        self.assertIsNone(permissions.selection({'sandboxPolicy': {'type': 'readOnly'}}))
        self.assertIsNone(permissions.selection({'sandbox': 'read-only'}))
        self.assertIsNone(permissions.selection({'sandbox': 'external-sandbox'}))
        # AskForApproval object forms cannot be replayed as a mode string, so
        # the whole selection is unrepresentable rather than partially kept.
        self.assertIsNone(permissions.selection(
            {'sandbox': {'type': 'dangerFullAccess'},
             'approvalPolicy': {'reject': {'reason': 'policy'}}}))
        self.assertIsNone(permissions.selection(
            {'sandbox': {'type': 'dangerFullAccess'},
             'approvalsReviewer': {'custom': 'reviewer'}}))
        self.assertIsNone(permissions.selection({}))
        self.assertIsNone(permissions.selection(None))

    def test_carries_selection_matches_the_request_contract(self):
        for key, value in (('permissions', ':workspace'), ('sandbox', 'workspace-write'),
                           ('sandboxPolicy', {'type': 'workspaceWrite'}),
                           ('approvalPolicy', 'never'), ('approvalsReviewer', 'user'),
                           ('runtimeWorkspaceRoots', ['C:\\work']),
                           ('runtimeWorkspaceRoots', [])):
            self.assertTrue(permissions.carries_selection({key: value}), key)
        self.assertFalse(permissions.carries_selection({'threadId': THREAD, 'cwd': 'C:\\work'}))
        self.assertFalse(permissions.carries_selection(None))
        for key, value in (('sandbox_mode', 'read-only'), ('approval_policy', 'on-request'),
                           ('permissions', ':workspace'),
                           ('sandbox_workspace_write', {'network_access': False})):
            self.assertTrue(permissions.carries_selection({'config': {key: value}}), key)
        for key in ('sandbox_workspace_write.network_access',
                    'sandbox_workspace_write.writable_roots',
                    'permissions.custom', 'default_permissions', 'approvals_reviewer',
                    'profile', 'profiles.work.sandbox_mode'):
            self.assertTrue(permissions.carries_selection({'config': {key: 'x'}}), key)
        self.assertFalse(permissions.carries_selection({'config': {'model': 'gpt-other'}}))
        self.assertFalse(permissions.carries_selection(
            {'permissions': None, 'sandbox': None, 'approvalPolicy': None}))

    def test_stored_validation_rejects_malformed_canonical_values(self):
        self.assertEqual(permissions.stored({'sandbox': 'danger-full-access',
                                             'approval_policy': 'never'}),
                         {'sandbox': 'danger-full-access', 'approval_policy': 'never'})
        self.assertIsNone(permissions.stored({'sandbox': 'danger-full-access', 'extra': None}))
        self.assertIsNone(permissions.stored({}))
        self.assertIsNone(permissions.stored({'permissions': ':workspace'}))
        self.assertIsNone(permissions.stored({'sandbox': 'workspace-write'}))
        self.assertIsNone(permissions.stored({'sandbox': {'type': 'dangerFullAccess'}}))
        self.assertIsNone(permissions.stored({'sandbox': 'danger-full-access', 'extra': 1}))
        self.assertIsNone(permissions.stored({'approval_policy': 'never'}))
        self.assertIsNone(permissions.stored({'sandbox': 7}))

    def test_attach_writes_only_deployed_keys_and_fills_nulls(self):
        self.assertEqual(permissions.attach({'threadId': THREAD}, BARE),
                         {'threadId': THREAD, 'sandbox': 'danger-full-access',
                          'approvalPolicy': 'never', 'approvalsReviewer': 'user'})
        nulls = {'threadId': THREAD, 'permissions': None, 'sandbox': None,
                 'approvalPolicy': None, 'approvalsReviewer': None}
        filled = permissions.attach(nulls, BARE)
        self.assertEqual(filled['sandbox'], 'danger-full-access')
        self.assertEqual(filled['approvalPolicy'], 'never')
        # The client's own null key is left exactly as it arrived.
        self.assertIsNone(filled['permissions'])
        # A request that states anything is never rewritten, and the caller's
        # object is never mutated.
        existing = {'sandbox': 'read-only', 'config': {'sandbox_mode': 'read-only'}}
        original = json.loads(json.dumps(existing))
        self.assertEqual(permissions.attach(existing, BARE), existing)
        self.assertEqual(existing, original)

    def test_requirements_handle_dict_profiles_and_mode_lists(self):
        payload = {'requirements': {
            'allowedPermissionProfiles': {':danger-full-access': True, ':workspace': False},
            'allowedSandboxModes': ['danger-full-access'],
            'allowedApprovalPolicies': ['never']}}
        limits = permissions.requirements(payload)
        self.assertEqual(limits['allowedPermissionProfiles'],
                         {':danger-full-access': True, ':workspace': False})
        self.assertTrue(permissions.within_requirements(limits, BARE))
        self.assertFalse(permissions.within_requirements(
            {'allowedPermissionProfiles': {':danger-full-access': False}}, BARE))
        self.assertFalse(permissions.within_requirements(
            {'allowedSandboxModes': []}, BARE))
        self.assertFalse(permissions.within_requirements(
            {'allowedSandboxModes': ['read-only', 'workspace-write']}, BARE))
        self.assertFalse(permissions.within_requirements(
            {'allowedApprovalPolicies': ['on-request']}, BARE))
        self.assertTrue(permissions.within_requirements(
            {'allowedPermissionProfiles': [':danger-full-access']}, BARE))
        self.assertTrue(permissions.within_requirements(None, BARE))
        self.assertEqual(permissions.requirements(
            {'requirements': {'allowedPermissionProfiles': {}}}),
            {'allowedPermissionProfiles': {}})

    def test_store_is_scoped_bounded_and_survives_corruption(self):
        self.assertEqual(self.remember(), BARE)
        self.assertIsNone(permissions.recall(self.directory, PROFILE, OTHER))
        self.assertIsNone(permissions.recall(self.directory, OTHER, THREAD))
        path = self.directory / 'profiles' / PROFILE / 'permission-selection.json'
        self.assertTrue(path.is_file())
        path.write_text('{not json', encoding='utf-8')
        self.assertIsNone(permissions.recall(self.directory, PROFILE, THREAD))
        self.remember()
        for index in range(permissions.MAX_THREADS + 4):
            thread = '7c9d2e41-0b53-4a6f-8e21-%012d' % index
            self.assertTrue(permissions.remember(self.directory, PROFILE, thread, RESPONSE))
        document = json.loads(path.read_text(encoding='utf-8'))
        self.assertLessEqual(len(document['threads']), permissions.MAX_THREADS)
        self.assertEqual(document['schema'], permissions.SCHEMA)
        self.assertTrue(permissions.remember_requirements(
            self.directory, PROFILE, {'requirements': {
                'allowedPermissionProfiles': {':danger-full-access': False}}}))
        self.assertIsNone(permissions.recall(self.directory, PROFILE, THREAD))

    def test_documents_from_another_profile_are_never_reused(self):
        self.remember()
        path = self.directory / 'profiles' / PROFILE / 'permission-selection.json'
        document = json.loads(path.read_text(encoding='utf-8'))
        document['profile_id'] = OTHER
        path.write_text(json.dumps(document), encoding='utf-8')
        self.assertIsNone(permissions.recall(self.directory, PROFILE, THREAD))

    def test_concurrent_writers_keep_both_threads(self):
        threads = [THREAD, OTHER]
        workers = [threading.Thread(target=permissions.remember,
                                    args=(self.directory, PROFILE, thread, RESPONSE))
                   for thread in threads]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        entry = json.loads((self.directory / 'profiles' / PROFILE /
                            'permission-selection.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(entry['threads']), sorted(threads))

    def test_proxy_fills_only_a_resume_that_states_nothing(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        # Client requests are never the source of a remembered selection.
        request = {'id': 1, 'method': 'thread/resume',
                   'params': {'threadId': THREAD, 'sandbox': 'workspace-write'}}
        self.assertEqual(managed_client_message(request, external, proxy), request)
        self.assertIsNone(permissions.recall(self.directory, PROFILE, THREAD))
        # A successful runtime response is.
        proxy.to_runtime({'id': 2, 'method': 'thread/resume', 'params': {'threadId': THREAD}})
        proxy.from_runtime({'id': 2, 'result': {'thread': {'id': THREAD}, **RESPONSE}})
        filled = managed_client_message(
            {'id': 3, 'method': 'thread/resume', 'params': {'threadId': THREAD, 'cwd': 'C:\\w'}},
            external, proxy)['params']
        self.assertEqual(filled['sandbox'], 'danger-full-access')
        self.assertEqual(filled['approvalPolicy'], 'never')
        self.assertEqual(filled['approvalsReviewer'], 'user')
        self.assertNotIn('permissions', filled)
        self.assertNotIn('runtimeWorkspaceRoots', filled)
        # A fresh, narrower selection in the request wins and is never widened.
        narrower = {'threadId': THREAD, 'sandbox': 'workspace-write'}
        self.assertEqual(managed_client_message(
            {'id': 4, 'method': 'thread/resume', 'params': narrower}, external, proxy)['params'],
            narrower)
        # A config override is the same kind of fresh authority.
        config = {'threadId': THREAD, 'config': {'sandbox_mode': 'read-only'}}
        self.assertEqual(managed_client_message(
            {'id': 6, 'method': 'thread/resume', 'params': config}, external, proxy)['params'],
            config)
        # Explicit nulls are an omission, not a choice.
        nulls = {'threadId': THREAD, 'permissions': None, 'sandbox': None, 'approvalPolicy': None}
        filled = managed_client_message(
            {'id': 7, 'method': 'thread/resume', 'params': nulls}, external, proxy)['params']
        self.assertEqual(filled['sandbox'], 'danger-full-access')
        self.assertEqual(filled['approvalPolicy'], 'never')
        # Errors never become evidence.
        proxy.to_runtime({'id': 5, 'method': 'thread/resume', 'params': {'threadId': OTHER}})
        proxy.from_runtime({'id': 5, 'error': {'code': -32600, 'message': 'rejected'}})
        self.assertIsNone(permissions.recall(self.directory, PROFILE, OTHER))

    def test_proxy_observes_settings_notifications_and_requirements(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        proxy.from_runtime({'method': 'thread/settings/updated', 'params': {
            'threadId': OTHER,
            'threadSettings': {'cwd': 'C:\\w', 'approvalPolicy': 'never',
                               'approvalsReviewer': 'user',
                               'sandboxPolicy': {'type': 'dangerFullAccess'},
                               'activePermissionProfile': {'id': ':danger-full-access'}}}})
        self.assertEqual(permissions.recall(self.directory, PROFILE, OTHER), BARE)
        proxy.to_runtime({'id': 'r', 'method': 'configRequirements/read'})
        proxy.from_runtime({'id': 'r', 'result': {'requirements': {
            'allowedPermissionProfiles': {':danger-full-access': False}}}})
        self.assertIsNone(permissions.recall(self.directory, PROFILE, OTHER))
        resume = proxy.to_runtime({'id': 'x', 'method': 'thread/resume', 'params': {'threadId': OTHER}})
        self.assertNotIn('sandbox', resume['params'])

    def test_confirmed_custom_policy_retires_stale_full_access(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, RESPONSE))
        # The runtime now confirms a restricted workspace policy: the older
        # full-access selection must be retired, not kept for a later resume.
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, CUSTOM))
        self.assertIsNone(permissions.recall(self.directory, PROFILE, THREAD))
        omitted = {'threadId': THREAD, 'cwd': 'C:\\work'}
        self.assertEqual(managed_client_message(
            {'id': 11, 'method': 'thread/resume', 'params': omitted},
            external, proxy)['params'], omitted)
        # A policy-free message and a rejected update keep the current state.
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, RESPONSE))
        proxy.to_runtime({'id': 12, 'method': 'thread/resume', 'params': {'threadId': THREAD}})
        proxy.from_runtime({'id': 12, 'error': {'code': -32600, 'message': 'rejected'}})
        self.assertIsNotNone(permissions.recall(self.directory, PROFILE, THREAD))
        self.assertFalse(permissions.remember(self.directory, PROFILE, THREAD, {'cwd': 'C:\\work'}))
        self.assertIsNotNone(permissions.recall(self.directory, PROFILE, THREAD))

    def test_custom_full_policy_retires_stale_builtin_full_access(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, RESPONSE))
        custom = {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                  'sandbox': {'type': 'dangerFullAccess'},
                  'activePermissionProfile': {'id': ':custom-full', 'extends': None}}
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, custom))
        self.assertIsNone(permissions.recall(self.directory, PROFILE, THREAD))
        omitted = {'threadId': THREAD, 'cwd': 'C:\\work'}
        self.assertEqual(managed_client_message(
            {'id': 17, 'method': 'thread/resume', 'params': omitted},
            external, proxy)['params'], omitted)

    def test_object_form_approval_retires_stale_builtin_full_access(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, RESPONSE))
        # A confirmed full sandbox with an object-form approval policy keeps
        # its restriction; the older, less restricted selection is retired.
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, {
            'sandbox': {'type': 'dangerFullAccess'},
            'approvalPolicy': {'reject': {'reason': 'policy'}}}))
        self.assertIsNone(permissions.recall(self.directory, PROFILE, THREAD))
        omitted = {'threadId': THREAD, 'cwd': 'C:\\work'}
        self.assertEqual(managed_client_message(
            {'id': 18, 'method': 'thread/resume', 'params': omitted},
            external, proxy)['params'], omitted)

    def test_custom_workspace_policy_without_profile_is_not_remembered(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        proxy.to_runtime({'id': 8, 'method': 'thread/resume', 'params': {'threadId': OTHER}})
        proxy.from_runtime({'id': 8, 'result': {'thread': {'id': OTHER}, **CUSTOM}})
        self.assertIsNone(permissions.recall(self.directory, PROFILE, OTHER))
        resume = {'threadId': OTHER}
        self.assertEqual(managed_client_message(
            {'id': 9, 'method': 'thread/resume', 'params': resume}, external, proxy)['params'], resume)

    def test_dotted_config_narrowing_blocks_restore(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, RESPONSE))
        for config in ({'sandbox_workspace_write.network_access': False},
                       {'permissions.custom': ':workspace'},
                       {'default_permissions': ':workspace'},
                       {'profile': 'work'},
                       {'profiles.work.sandbox_mode': 'read-only'},
                       {'sandbox_workspace_write': {'writable_roots': ['C:\\work']}}):
            params = {'threadId': THREAD, 'config': config}
            filled = managed_client_message(
                {'id': 13, 'method': 'thread/resume', 'params': params}, external, proxy)['params']
            self.assertEqual(filled, params, config)
        value = managed_client_message(
            {'id': 14, 'method': 'thread/resume', 'params': {'threadId': THREAD}},
            external, proxy)['params']
        self.assertEqual(value['sandbox'], 'danger-full-access')

    def test_bare_danger_full_access_round_trips_and_restores(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        bare = {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                'sandbox': {'type': 'dangerFullAccess'}}
        self.assertTrue(permissions.remember(self.directory, PROFILE, THREAD, bare))
        self.assertEqual(permissions.recall(self.directory, PROFILE, THREAD), BARE)
        filled = managed_client_message(
            {'id': 15, 'method': 'thread/resume', 'params': {'threadId': THREAD}},
            external, proxy)['params']
        self.assertEqual(filled['sandbox'], 'danger-full-access')
        self.assertNotIn('permissions', filled)
        self.assertEqual(filled['approvalPolicy'], 'never')
        self.assertEqual(filled['approvalsReviewer'], 'user')
        # A supplied workspace scope keeps the request untouched.
        scoped = {'threadId': THREAD, 'runtimeWorkspaceRoots': ['C:\\work']}
        self.assertEqual(managed_client_message(
            {'id': 16, 'method': 'thread/resume', 'params': scoped}, external, proxy)['params'],
            scoped)

    def test_deployed_schema_matches_what_we_replay(self):
        schema = json.loads(FIXTURE.read_text(encoding='utf-8'))
        resume = set(schema['ThreadResumeParams']['properties'])
        self.assertNotIn('permissions', resume)
        self.assertNotIn('runtimeWorkspaceRoots', resume)
        self.assertTrue({'sandbox', 'approvalPolicy', 'approvalsReviewer'} <= resume)
        self.assertIn('activePermissionProfile', schema['ThreadSettings']['properties'])
        self.assertEqual(schema['ConfigRequirements']['properties']
                         ['allowedPermissionProfiles']['type'], ['object', 'null'])
        self.assertEqual(schema['ConfigRequirements']['properties']
                         ['allowedSandboxModes']['items']['enum'][-1], 'danger-full-access')
        # Everything this module replays must exist in that schema.
        remembered = permissions.attach({'threadId': THREAD}, BARE)
        self.assertTrue(set(remembered) - {'threadId'} <= resume)
        # And the deployed requirement shapes drive the ceiling exactly.
        limits = permissions.requirements({'requirements': {
            'allowedPermissionProfiles': {':danger-full-access': True},
            'allowedSandboxModes': ['danger-full-access']}})
        self.assertTrue(permissions.within_requirements(limits, BARE))
        denied = permissions.requirements({'requirements': {
            'allowedPermissionProfiles': {':danger-full-access': False}}})
        self.assertFalse(permissions.within_requirements(denied, BARE))
        self.assertFalse(permissions.within_requirements(
            permissions.requirements({'requirements': {'allowedSandboxModes': []}}), BARE))

    def test_proxy_ignores_malformed_traffic(self):
        proxy = permissions.PermissionSelectionProxy(self.root, PROFILE)
        external = StubExternal()
        for message in ({}, {'method': 5}, {'method': 'thread/resume', 'params': 'nope'},
                        {'id': None, 'method': 'thread/resume', 'params': {'threadId': 'not-a-uuid'}},
                        {'id': 'x', 'method': 'thread/resume', 'params': {'threadId': PROFILE}}):
            self.assertEqual(managed_client_message(message, external, proxy), message)
        proxy.from_runtime(None)
        proxy.from_runtime({'method': 'thread/settings/updated', 'params': 'nope'})
        proxy.from_runtime({'id': 'missing', 'result': {}})


if __name__ == '__main__':
    unittest.main()
