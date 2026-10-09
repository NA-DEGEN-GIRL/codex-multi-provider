"""Keep a cold-start navigation intent, never repeat launches or writer handoffs.

Only opaque, expiring IDs cross the continuation API. Pending entries contain no
launch environment or credentials. Readiness polls release the request channel
and launch-admission lock so other profiles stay usable.
"""
from copy import deepcopy
import threading
import time
from uuid import uuid4
from .app_transport import AppTransport
from .store import identifier


class PendingNavigations:
    # An SSH task also waits for this launch's connection to its host, which
    # follows the local runtime (and may update a remote runtime first).
    def __init__(self, center, *, clock=time.monotonic, timeout=45, remote_timeout=90):
        self.center, self.clock, self.timeout, self.remote_timeout = center, clock, timeout, remote_timeout
        self.pending = {}
        # Opens of different profiles run concurrently (per-profile gates).
        self.lock = threading.Lock()

    def profile_of(self, token):
        """The profile a pending navigation waits for (request gating), or None."""
        try:
            key = identifier(token)
        except ValueError:
            return None
        with self.lock:
            plan = self.pending.get(key)
        return plan['profile']['id'] if plan else None

    def _keep(self, token, plan):
        with self.lock:
            self.pending[token] = plan

    def _take(self, token):
        with self.lock:
            return self.pending.pop(identifier(token), None)

    @property
    def transport(self):
        return AppTransport(self.center.root)

    @staticmethod
    def _binding(link):
        return tuple(link.get(k) for k in ('id', 'profile_id', 'host_id', 'source_store_id', 'thread_id'))

    @staticmethod
    def _lifetime(profile):
        return tuple(profile.get(k) for k in ('id', 'generation', 'process_id', 'process_created',
                     'executable_path', 'account_fingerprint', 'runtime_channel'))

    @staticmethod
    def _blocked(reason, message):
        return dict(state='blocked', access_mode='unavailable', reason=reason, message=message)

    def begin(self, original_link, profile, target_link, read_only, *, annotations=None):
        with self.lock:
            # Latest open wins: an earlier wait for this profile never resumes.
            self.pending = {k: v for k, v in self.pending.items()
                            if v['expires'] > self.clock() and v['profile']['id'] != profile['id']}
            full = len(self.pending) >= 64
        if full:
            return self._blocked('navigation_limit', '대기 중인 대화 열기가 많습니다. 잠시 후 다시 시도하세요.')
        remote = target_link.get('host_id', 'local') != 'local'
        plan = dict(original=deepcopy(original_link), profile=deepcopy(profile),
                    link=deepcopy(target_link), read_only=read_only,
                    annotations=deepcopy(annotations or {}),
                    expires=self.clock() + (self.remote_timeout if remote else self.timeout))
        result = self._attempt(plan)
        if result.get('state') == 'waiting_for_reader':
            token = str(uuid4())
            plan['waiting_reason'] = result.get('reason')
            self._keep(token, plan)
            result['navigation_id'] = token
        return result

    def resume(self, token):
        # Consume before attempting a send. Exceptions and uncertain launches
        # cannot be replayed with this ID; only a no-send readiness wait retains it.
        plan = self._take(token)
        token = identifier(token)
        if plan is not None and plan['expires'] <= self.clock() and plan.get('waiting_reason') == 'remote_connection_starting':
            return self._blocked('remote_connection_timeout', 'SSH 연결이 준비되지 않아 대화 이동 요청은 보내지 않았습니다. '
                                 '로컬 창은 그대로 쓸 수 있습니다. SSH 연결이 끝난 뒤 다시 여세요.')
        if plan is None or plan['expires'] <= self.clock():
            return self._blocked('navigation_expired', 'Codex 준비 대기 시간이 지났습니다. 대화 이동 요청은 보내지 않았습니다.')
        with self.center.update_hooks.launch_admission(plan['profile']['id']):
            current = self.center.store.profile(plan['profile']['id'])
            link = next((x for x in self.center.store.read()['shortcuts'] if x['id'] == plan['original']['id']), None)
            if (current.get('removed_at') or self._lifetime(current) != self._lifetime(plan['profile'])
                    or link is None or self._binding(link) != self._binding(plan['original'])):
                return self._blocked('navigation_changed', '프로필 실행이나 바로가기가 변경되어 이전 대화 이동을 취소했습니다.')
            observed = self.center.instances.observe(current)
            if observed.get('status') != 'running':
                return self._blocked('profile_not_running', 'Codex가 종료되어 대화 이동을 취소했습니다.')
            # A cheap file read while warming; do not rebuild the environment or
            # start another Electron process on each poll.
            readiness = (self.transport.navigation_readiness(current)
                         or self.transport.remote_navigation_readiness(current, plan['link']))
            if readiness is not None:
                result = self._describe({**readiness, 'profile_id': current['id']}, plan)
            else:
                plan['profile'] = {**current, **observed}
                result = self._attempt(plan)
        if result.get('state') == 'waiting_for_reader':
            plan['waiting_reason'] = result.get('reason')
            self._keep(token, plan)
            result['navigation_id'] = token
        return result

    def _attempt(self, plan):
        profile = plan['profile']
        environment = self.center.instances.environment(profile)
        try:
            result = self.transport.open_conversation(profile, plan['link'], profile['executable_path'],
                                                      environment, read_only=plan['read_only'])
        finally:
            environment.clear()
        return self._describe(result, plan)

    @staticmethod
    def _describe(result, plan):
        result.update(profile=plan['profile'], **plan['annotations'])
        if result.get('state') == 'waiting_for_reader':
            result['access_mode'] = 'preparing'
        elif result.get('state') != 'request_sent':
            result['access_mode'] = 'unavailable'
        elif result.get('readonly_projection'):
            result.update(access_mode='view_only',
                          message='지정 계정에서 대화의 조회 화면 열기를 요청했습니다. 현재는 이 기록에 메시지를 보낼 수 없습니다.')
            connection = result.get('account_connection') or {}
            result['view_reason'] = connection.get('code', 'shared_record')
            if result['view_reason'] == 'source_busy':
                result['message'] = '다른 계정에서 진행 중인 대화입니다. 지정 계정에서 최신 기록을 조회합니다.'
        else:
            result.update(access_mode='ready', message=plan['profile']['alias'] + ' 계정에 대화 열기를 요청했습니다.')
        return result
