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
    # relaunch: an app that exits during the wait is started once more and the
    # same task continues (_relaunch).
    def __init__(self, center, *, clock=time.monotonic, timeout=45, remote_timeout=90, relaunch=True):
        self.center, self.clock, self.timeout, self.remote_timeout = center, clock, timeout, remote_timeout
        self.relaunch = relaunch
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

    def forget(self, profile_id):
        """Drop every wait for a profile the user stops, restarts or signs into
        again: such a wait never resumes, so it can never relaunch that app."""
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if v['profile']['id'] != profile_id}

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
                if plan.get('relaunched') or not self.relaunch:
                    return self._blocked('profile_not_running', 'Codex가 종료되어 대화 이동을 취소했습니다.')
                result = self._relaunch(plan)
            else:
                # A cheap file read while warming; do not rebuild the environment or
                # start another Electron process on each poll.
                readiness = (self.transport.navigation_readiness(current)
                             or self.transport.remote_navigation_readiness(current, plan['link']))
                if readiness is not None:
                    # The same launch (lifetime checked above): its window, once
                    # found, lets the shell attach before the next state poll.
                    if observed.get('window_handle'):
                        plan['profile'] = {**plan['profile'], 'window_handle': observed['window_handle']}
                    result = self._describe({**readiness, 'profile_id': current['id']}, plan)
                else:
                    plan['profile'] = {**current, **observed}
                    result = self._attempt(plan)
        if result.get('state') == 'waiting_for_reader':
            plan['waiting_reason'] = result.get('reason')
            self._keep(token, plan)
            result['navigation_id'] = token
        return result

    def _relaunch(self, plan):
        """The app this wait followed exited before its task link: start it once
        more and keep waiting for the same task (inside the profile's launch
        admission). Only a launch this wait started is followed; another
        launch of the profile (a restart, an update) still cancels the wait.
        """
        profile_id = plan['profile']['id']
        try:
            shown = self.center.instances.show(profile_id, reopen_existing=False, wait_for_window=False)
        except (RuntimeError, ValueError, OSError, KeyError, TypeError):
            # Full exit in progress, maintenance, login or account problems:
            # their own messages belong to an explicit open, not this wait.
            return self._blocked('profile_relaunch_failed', 'Codex가 종료되어 한 번 다시 시작하려 했지만 시작하지 못했습니다. '
                                 '대화 이동 요청은 보내지 않았습니다. 프로필을 다시 선택해 주세요.')
        self.center.instances.finish_show_later(shown)
        relaunched = shown.get('profile') or {}
        if relaunched.get('id') != profile_id or not relaunched.get('generation'):
            return self._blocked('profile_relaunch_failed', 'Codex를 다시 시작했지만 실행을 확인하지 못했습니다. 대화 이동 요청은 보내지 않았습니다.')
        plan['profile'] = relaunched
        plan['relaunched'] = True
        remote = plan['link'].get('host_id', 'local') != 'local'
        plan['expires'] = self.clock() + (self.remote_timeout if remote else self.timeout)
        return self._describe(dict(state='waiting_for_reader', reason='app_relaunching', profile_id=profile_id,
                                   message='Codex가 종료되어 한 번 다시 시작했습니다. 준비되면 선택한 작업으로 이동합니다.'), plan)

    def _attempt(self, plan):
        """Authorize from the record keys only; build the full launch environment
        only if a second ChatGPT.exe must carry the link (never for a running
        app that takes a local task over its own navigation pipe)."""
        profile = plan['profile']
        instances = self.center.instances
        environment = instances.navigation_environment(profile)
        spawned = []
        def spawn_environment():
            spawned.append(instances.environment(profile))
            return spawned[-1]
        try:
            result = self.transport.open_conversation(profile, plan['link'], profile['executable_path'],
                                                      environment, read_only=plan['read_only'],
                                                      spawn_environment=spawn_environment,
                                                      direct=self._direct(plan))
        finally:
            environment.clear()
            for env in spawned:
                env.clear()
        return self._describe(result, plan)

    def _direct(self, plan):
        """navigate_to_codex_page over this exact launch's app-tools pipe, for a
        local task; None for an SSH task (the tool takes no host id)."""
        if plan['link'].get('host_id', 'local') != 'local':
            return None
        profile, root = plan['profile'], self.center.root

        def navigate(thread_id):
            from .native_app_bridge import AppBridge, NativePipe
            # The descriptor must name this profile, generation and app
            # process; the pipe server's own PID and birth time are checked too.
            bridge = AppBridge(root, profile, pipe_factory=lambda path, expected_identity: NativePipe(
                path, expected_identity=expected_identity, timeout=1))
            bridge.call('navigate_to_codex_page', {'threadId': thread_id}, thread_id, timeout=4)
            return True
        return navigate

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
