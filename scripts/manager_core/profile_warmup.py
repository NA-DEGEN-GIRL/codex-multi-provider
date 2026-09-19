"""Prepare account windows once per manager start, without foreground navigation."""
from copy import deepcopy
import threading

from .login_health import inspect as login_health


class ProfileWarmup:
    """Run one serial worker; the injected launcher must preserve existing windows.

    Launch callbacks receive a profile ID and must use the same profile-scoped
    admission lock as foreground opens, with ``reopen_existing=False``. Shutdown
    cancels pending work; a callback already admitted may finish normally.
    """

    def __init__(self, store, instances, launch, *, spawn=None, health=login_health):
        self.store, self.instances, self.launch = store, instances, launch
        self.health = health
        self.spawn = spawn or self._spawn
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.started = False
        self.pending = []
        self.priority = None
        self.entries = {}
        self.result = dict(state='not_started', worker_active=False, profiles=[])

    @staticmethod
    def _spawn(target):
        threading.Thread(target=target, name='profile-warmup', daemon=True).start()

    def status(self):
        with self.lock:
            value = deepcopy(self.result)
        counts = dict(ready=0, started=0, pending=0, skipped=0, attention=0, cancelled=0)
        for entry in value['profiles']:
            state = entry['state']
            counts[state if state in counts else 'pending'] += 1
        value.update(total=len(value['profiles']), counts=counts)
        return value

    def start(self):
        with self.lock:
            if self.stopping.is_set():
                raise RuntimeError('관리 서비스가 종료 중입니다.')
            # A later refresh must not reopen a profile the user deliberately closed.
            if self.started:
                return self.status()
            self.started = True
            self.result.update(state='preparing', worker_active=True)
        try:
            self.spawn(self._run)
        except Exception:
            with self.lock:
                self.result.update(state='attention', worker_active=False,
                                   code='worker_start_failed')
            raise
        return self.status()

    def prioritize(self, profile_id):
        """Prefer the selected queued account after the current launch finishes."""
        with self.lock:
            if self.stopping.is_set() or not self.result['worker_active']:
                return False
            if self.entries and profile_id not in self.pending:
                return False
            self.priority = profile_id
            return True

    def shutdown(self):
        with self.lock:
            self.stopping.set()
            self.result['state'] = 'stopped'
            for entry in self.entries.values():
                if entry['state'] == 'queued':
                    entry.update(state='cancelled', code='service_stopped')

    def _update(self, profile_id, **changes):
        with self.lock:
            self.entries[profile_id].update(changes)

    def _run(self):
        failed = False
        try:
            if self.stopping.is_set():
                return
            profiles = self.store.read()['profiles']
            with self.lock:
                self.entries = {
                    p['id']: dict(profile_id=p['id'], alias=p.get('alias', p['id']), state='queued')
                    for p in profiles
                }
                self.pending = list(self.entries)
                self.result['profiles'] = list(self.entries.values())
            while True:
                with self.lock:
                    if self.stopping.is_set() or not self.pending:
                        break
                    profile_id = self.priority if self.priority in self.pending else self.pending[0]
                    self.priority = None
                    self.pending.remove(profile_id)
                    self.entries[profile_id]['state'] = 'checking'
                try:
                    self._prepare(profile_id)
                except Exception:
                    # Status is public UI state; never copy provider/credential diagnostics into it.
                    self._update(profile_id, state='attention', code='prepare_failed',
                                 message='프로필을 미리 열지 못했습니다. 선택해서 다시 열 수 있습니다.')
        except Exception:
            failed = True
            with self.lock:
                self.result['code'] = 'profile_scan_failed'
        finally:
            with self.lock:
                if self.stopping.is_set():
                    for entry in self.entries.values():
                        if entry['state'] in ('queued', 'checking'):
                            entry.update(state='cancelled', code='service_stopped')
                self.pending.clear()
                self.priority = None
                self.result.update(
                    worker_active=False,
                    state='stopped' if self.stopping.is_set() else 'attention'
                    if failed or any(e['state'] == 'attention' for e in self.entries.values())
                    else 'complete',
                )

    def _prepare(self, profile_id):
        # Re-read immediately before each attempt: account removal or login changes
        # while another profile opens must invalidate the startup snapshot.
        profile = self.store.profile(profile_id)
        code = ('removed' if profile.get('removed_at') else
                'view_only' if profile.get('view_only') else
                'login_pending' if profile.get('native_login_pending')
                or profile.get('runtime_channel') == 'packaged' else
                'account_missing' if profile.get('account_missing')
                and profile.get('auth_mode') != 'native' else None)
        if code:
            self._update(profile_id, state='skipped', code=code)
            return
        if self.health(profile).get('blocks_launch'):
            self._update(profile_id, state='skipped', code='login_unhealthy')
            return
        observed = self.instances.observe(profile)
        if observed.get('status') == 'running':
            self._update(profile_id, state='ready' if observed.get('window_handle') else 'started',
                         code='already_running')
            return
        with self.lock:
            if self.stopping.is_set():
                self.entries[profile_id].update(state='cancelled', code='service_stopped')
                return
            self.entries[profile_id]['state'] = 'opening'
        result = self.launch(profile_id)
        running = result.get('profile', {})
        if result.get('state') not in ('launched', 'existing') or running.get('status') != 'running':
            self._update(profile_id, state='attention', code='launch_not_ready')
        else:
            self._update(profile_id, state='ready' if running.get('window_handle') else 'started',
                         code='prepared' if running.get('window_handle') else 'window_pending')
