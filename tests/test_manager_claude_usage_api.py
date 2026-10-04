import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from manager_core import claude_usage_api as api
from manager_core.claude_usage import read_quota


class ClaudeUsageApiTests(unittest.TestCase):
    def test_percent_is_not_fraction_and_missing_windows_stay_unknown(self):
        self.assertEqual(api.normalize({'five_hour': {'utilization': 25,
            'resets_at': '2030-01-01T00:00:00Z'}}),
            [{'key': 'five_hour', 'used_percent': 25, 'resets_at': 1893456000}])
        for value in (None, True, '25', -1, 101, float('nan')):
            self.assertEqual(api.normalize({'five_hour': {'utilization': value}}), [])
        self.assertEqual(api.normalize({'seven_day': {'utilization': 0}})[0]['used_percent'], 0)

    def test_only_fixed_endpoint_and_filtered_metadata_leave_reader(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({'five_hour': {'utilization': 8},
            'access_token': 'never-copy', 'email': 'private@example.invalid'}).encode())
        with patch.object(api, 'read_access_token', return_value={'accessToken': 'fixture-token'}) as token:
            windows, error = api.query('root', {'id': 'profile', 'claude_account_identity': 'identity'}, opener=opener)
        token.assert_called_once_with('root', 'profile', 'identity')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, api.URL)
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)
        self.assertIsNone(error)
        self.assertEqual(windows, [{'key': 'five_hour', 'used_percent': 8, 'resets_at': None}])
        self.assertIsNone(api.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid'))

    def test_auth_failure_is_safe_and_never_becomes_zero_usage(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(api.URL, 401, 'private-detail', {}, None)
        with patch.object(api, 'read_access_token', return_value={'accessToken': 'fixture-token'}):
            self.assertEqual(api.query('root', {'id': 'p', 'claude_account_identity': 'a'}, opener=opener),
                             (None, 'usage_access_unavailable'))

    def test_identity_rechecked_and_onboarding_is_not_needed(self):
        identity = 'a' * 64
        profile = {'id': 'p', 'claude_account_identity': identity}
        ready = {'logged_in': True, 'account_identity': identity}
        with patch('manager_core.claude_usage.auth_status', return_value=ready), \
             patch.object(api, 'query', return_value=([{'key': 'five_hour', 'used_percent': 25}], None)), \
             patch('manager_core.claude_usage_terminal.query') as terminal:
            value = read_quota(profile, root='root')['usage']
        terminal.assert_not_called()
        self.assertEqual(value['source'], 'claude_oauth_usage')
        self.assertEqual(value['windows'][0]['remaining_percent'], 75)
        with patch('manager_core.claude_usage.auth_status', side_effect=[ready, dict(ready, account_identity='b'*64)]), \
             patch.object(api, 'query', return_value=([{'key': 'five_hour', 'used_percent': 25}], None)):
            self.assertEqual(read_quota(profile, root='root')['usage']['windows'], [])
