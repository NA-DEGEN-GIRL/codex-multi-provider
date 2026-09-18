"""Read selected-account proof from existing, process-bound SSH transports."""
import re

from .runtime_admin import AdminClient, AdminError
from .ssh_runtime_control import endpoint_id


class RemoteReadiness:
    def __init__(self, root, *, client_factory=None):
        self.root = root
        self.client_factory = client_factory or (
            lambda profile, alias: AdminClient(root, endpoint_id(profile['id'], alias), profile['generation']))

    def inspect(self, profile, entries):
        connections = []
        for entry in entries:
            if not entry.get('reconnect_required'):
                continue
            binding = entry.get('next_binding', entry['binding'])
            alias = binding['alias']
            state = 'connecting'
            try:
                expected = profile.get('account_fingerprint')
                if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                    state = 'account_unknown'
                else:
                    health = self.client_factory(profile, alias).request('manager/maintenance/status', {}, timeout=1)
                    observed = health.get('sshBinding', {})
                    if (health.get('generation') != profile['generation']
                            or any(observed.get(key) != value for key, value in
                                   (('profileId', profile['id']), ('hostAlias', alias), ('revision', binding['revision'])))):
                        state = 'connection_changed'
                    elif observed.get('accountFingerprint') not in (None, expected):
                        state = 'account_mismatch'
                    elif observed.get('authState') == 'login_needed':
                        state = 'login_required'
                    elif (observed.get('accountFingerprint') == expected and observed.get('authState') == 'ready'
                          and all(health.get(key) is True for key in ('connected', 'initialized', 'streamComplete', 'accountReady'))
                          and health.get('held') is False and health.get('frontendMutationBlocked') is False):
                        state = 'ready'
            except (AdminError, OSError, ValueError, KeyError, TypeError):
                pass
            connections.append({'host_alias': alias, 'state': state})
        return {'ready': all(item['state'] == 'ready' for item in connections), 'connections': connections}


def pending_message(result):
    labels = {'connecting': '연결 대기', 'account_unknown': 'Windows 계정 확인 필요',
              'account_mismatch': '계정 불일치', 'login_required': '로그인 확인 필요',
              'connection_changed': '연결 설정 확인 필요'}
    pending = [item['host_alias'] + ': ' + labels.get(item['state'], '확인 대기')
               for item in result.get('connections', []) if item['state'] != 'ready']
    return '설정은 적용됐으며 SSH 계정 연결을 확인하고 있습니다. ' + ', '.join(pending)
