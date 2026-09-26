"""Prepare account windows once per manager start, without foreground navigation."""
from copy import deepcopy
import os
import threading
import time

from .login_health import inspect as login_health

# Leader gate timing. Measured: a solo first launch took 17.7 s to prepare and
# spawn and 4.8 s more to its window (revision 93); the launch returns after
# at most 8 s of window wait (window pending). Eight launches together took
# 26 s to prepare and 40-48 s more to show windows (revision 94).
#
# Each leader's own head start, from the moment a worker takes its launch: a
# solo cold launch returns within it even at the 8 s window cap (25.7 s).
LEADER_HEAD_START_SECONDS = 30.0
# The others never wait longer than this from the profile scan, whichever
# leader still launches: a profile clicked within 15 s of the scan keeps its
# whole head start, and the wait stays near two solo launches (2 x 22.5 s).
LEADER_GATE_CAP_SECONDS = 45.0

_READY = ('ready', 'started')
_LAUNCHING = ('queued', 'checking', 'opening')


def default_workers(cpus=None):
    """Two launches per three logical processors, 2..8 at once.

    Different profiles prepare in parallel; most of that is file I/O, while
    each launch also starts a multi-process desktop. Twelve processors open
    eight profiles together; four keep two slots for a responsive machine.
    """
    if cpus is None:
        cpus = getattr(os, 'process_cpu_count', os.cpu_count)() or 4
    return max(2, min(8, int(cpus) * 2 // 3))


class ProfileWarmup:
    """Overlap a bounded number of launches without foreground navigation.

    Launch callbacks receive a profile ID and must use the same profile-scoped
    admission lock as foreground opens, with ``reopen_existing=False``. Shutdown
    cancels pending work; a callback already admitted may finish normally.

    Leader gate, once per pass: the profile the shell restores (``start``) and
    any profile opened while the gate waits start at once. The newest leader
    governs; a leader selected again while it still launches is the newest
    again. The others are not taken until its launch returns (window ready,
    or window pending after the window wait), its head start
    (LEADER_HEAD_START_SECONDS from the moment a worker took it) passes, or
    LEADER_GATE_CAP_SECONDS pass from the gate start; then all run together.
    If the newest leader fails or is skipped, any other leader still
    launching governs instead (its return, or the newest one's head start,
    opening at once if that is already spent); with none left the gate
    opens. A held worker has taken no profile and
    holds no admission or fence, only this object's condition.
    """

    def __init__(self, store, instances, launch, *, spawn=None, health=login_health,
                 max_workers=None, metrics=None, head_start_seconds=LEADER_HEAD_START_SECONDS,
                 cap_seconds=LEADER_GATE_CAP_SECONDS):
        self.store, self.instances, self.launch = store, instances, launch
        self.health = health
        self.spawn = spawn or self._spawn
        self.max_workers = max(1, min(8, int(default_workers() if max_workers is None else max_workers)))
        self.metrics = metrics
        self.head_start_seconds, self.cap_seconds = head_start_seconds, cap_seconds
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.stopping = threading.Event()
        self.started = False
        self.resume_after_drain = False
        self.pending = []
        self.priority = None
        self.entries = {}
        # Leader -> monotonic time it was promoted; None for a selection made
        # before the profile scan. Insertion order: the last one is the newest.
        self.leaders = {}
        # Leader -> monotonic time a worker took its launch (its head start).
        self.admitted = {}
        self.gate_started = self.gate_released = None
        # (monotonic time, released_by code) at which a waiting gate opens.
        self.deadline = None
        self.result = dict(state='not_started', worker_active=False, profiles=[],
                           gate=dict(state='none', leaders=[]))

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

    def start(self, leader=None):
        with self.lock:
            if self.stopping.is_set():
                raise RuntimeError('관리 서비스가 종료 중입니다.')
            # A later refresh must not reopen a profile the user deliberately closed.
            if self.started:
                return self.status()
            self.started = True
            if leader is not None:
                self.leaders.setdefault(leader, None)
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
        """Prefer the selected queued account after the current launch finishes.

        While the leader gate waits, the account also becomes a leader and
        starts at once. A leader selected again while it still launches
        becomes the newest leader again.
        """
        with self.lock:
            if self.stopping.is_set() or not self.result['worker_active']:
                return False
            queued = not self.entries or profile_id in self.pending
            if queued:
                self.priority = profile_id
            if profile_id in self.leaders:
                return self._reselect(profile_id) or queued
            if not queued:
                return False
            self._promote(profile_id)
            return True

    def promote(self, profile_id):
        """Let an account opened outside the warmup pass a waiting gate."""
        with self.lock:
            if self.stopping.is_set() or profile_id not in self.pending:
                return False
            return self._promote(profile_id)

    def _promote(self, profile_id):
        if profile_id in self.leaders:
            return False
        if not self.entries:
            # Before the profile scan, like the selection passed to start().
            self.leaders[profile_id] = None
            return True
        if not self._gate_waiting():
            return False
        self.leaders[profile_id] = time.monotonic()
        self.result['gate']['leaders'].append(profile_id)
        # The newest leader now governs; its head start begins when taken.
        self._schedule()
        return True

    def _reselect(self, leader):
        """A leader selected again while it still launches governs again (lock held).

        It becomes the newest leader (user clicks A, then B, then A). Its head
        start still runs from the moment a worker first took it; a spent one
        opens the gate now.
        """
        if self.entries and (self.entries[leader]['state'] not in _LAUNCHING
                             or not self._gate_waiting()):
            return False
        self.leaders[leader] = self.leaders.pop(leader)
        if self.entries:
            order = self.result['gate']['leaders']
            order.remove(leader)
            order.append(leader)
            self._schedule()
        return True

    def _gate_waiting(self):
        """True while non-leaders are held (lock held); applies the deadline."""
        if self.result['gate']['state'] != 'waiting':
            return False
        at, code = self.deadline
        if time.monotonic() >= at:
            self._release(code)
            return False
        return True

    def _schedule(self, spent=None):
        """Re-arm a waiting gate's deadline and wake held workers (lock held).

        The newest leader still launching governs with its own head start from
        the moment a worker took it, never past the cap from the gate start.
        A leader no worker has taken yet has only the cap.

        A re-armed deadline already past (an earlier leader's head start after
        the newest failed, or a re-selected leader's) never held anyone: the
        gate opens now with ``spent`` (default: that deadline's code), not
        backdated to that time. Only the deadline in force before the call is
        backdated (_gate_waiting, _release).
        """
        cap = self.gate_started + self.cap_seconds
        launching = [p for p in self.leaders if self.entries[p]['state'] in _LAUNCHING]
        taken = self.admitted.get(launching[-1]) if launching else None
        own = None if taken is None else taken + self.head_start_seconds
        self.deadline = (own, 'timeout') if own is not None and own < cap else (cap, 'cap')
        now = time.monotonic()
        if self.deadline[0] <= now:
            self.deadline = (now, spent or self.deadline[1])
            self._release(self.deadline[1])
        self.changed.notify_all()

    def _settle(self):
        """A leader's launch returned while the gate waits (lock held)."""
        states = [self.entries[p]['state'] for p in self.leaders]
        if states[-1] in _READY:
            self._release('leader_ready')
        elif states[-1] in _LAUNCHING:
            # An earlier leader returned; the newest still governs.
            self._schedule()
        elif any(state in _READY for state in states):
            # The newest failed or was skipped; an earlier leader is ready.
            self._release('leader_ready')
        elif any(state in _LAUNCHING for state in states):
            # The newest failed or was skipped; wait for the others still
            # launching, or open now if their head start is already spent.
            self._schedule(spent='leader_failed')
        else:
            self._release('leader_failed')

    def _release(self, reason):
        """Open a waiting gate once (lock held). Later calls change nothing."""
        if self.result['gate']['state'] != 'waiting':
            return
        released = time.monotonic()
        at, code = self.deadline
        if reason != 'stopped' and released >= at:
            # Observed late (a worker woke after the deadline): it opened then.
            reason, released = code, at
        self.gate_released = released
        self.result['gate'].update(state='released', released_by=reason)
        self.changed.notify_all()

    def shutdown(self):
        with self.lock:
            self.resume_after_drain = False
            self.stopping.set()
            self.result['state'] = 'stopped'
            self._release('stopped')
            self.changed.notify_all()
            for entry in self.entries.values():
                if entry['state'] == 'queued':
                    entry.update(state='cancelled', code='service_stopped')

    def resume(self):
        """Undo a failed full exit without relaunching closed profiles."""
        with self.lock:
            if self.result['worker_active']:
                # The admitted pass must still finish cancelling its queue.
                self.resume_after_drain = True
                return
            self.stopping.clear()
            if self.result['state'] == 'stopped':
                self.result['state'] = 'complete' if self.started else 'not_started'

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
                # An unknown or removed selection never holds the others.
                self.leaders = {p: at for p, at in self.leaders.items() if p in self.entries}
                if self.leaders and not self.stopping.is_set():
                    self.gate_started = time.monotonic()
                    self.result['gate'] = dict(state='waiting', leaders=list(self.leaders))
                    self._schedule()
            workers = []
            try:
                for index in range(min(self.max_workers, len(profiles)) - 1):
                    worker = threading.Thread(target=self._drain, name=f'profile-warmup-{index}',
                                              daemon=True)
                    worker.start()
                    workers.append(worker)
                self._drain()
            finally:
                # Keep worker_active true until every admitted callback finishes.
                for worker in workers:
                    worker.join()
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
                # Every leader already released it; this only keeps status exact.
                self._release('stopped' if self.stopping.is_set() else 'leader_failed')
                if self.resume_after_drain:
                    self.stopping.clear()
                    self.resume_after_drain = False
                self.result.update(
                    worker_active=False,
                    state='stopped' if self.stopping.is_set() else 'attention'
                    if failed or any(e['state'] == 'attention' for e in self.entries.values())
                    else 'complete',
                )

    def _drain(self):
        while True:
            with self.lock:
                profile_id, gate = self._next()
                if profile_id is None:
                    return
                self.pending.remove(profile_id)
                self.entries[profile_id]['state'] = 'checking'
            if gate and self.metrics is not None:
                released_by, waited = gate
                # A fixed code and the time this profile was held, nothing else.
                self.metrics.record(profile_id, 'warmup_gate', time.perf_counter() - waited,
                                    released_by=released_by)
            try:
                self._prepare(profile_id)
            except Exception:
                # Status is public UI state; never copy provider/credential diagnostics into it.
                self._update(profile_id, state='attention', code='prepare_failed',
                             message='프로필을 미리 열지 못했습니다. 선택해서 다시 열 수 있습니다.')
            finally:
                with self.lock:
                    # The deadline in force is applied before this return can
                    # re-arm it, so a late return never moves an expired one.
                    if profile_id in self.leaders and self._gate_waiting():
                        self._settle()

    def _next(self):
        """The next profile and its gate record (lock held); (None, None) when done.

        While the gate waits only leaders are taken. The other workers wait
        here, holding nothing but this condition, until a release, a promotion,
        shutdown or the deadline.
        """
        while True:
            if self.stopping.is_set() or not self.pending:
                return None, None
            leaders = [p for p in self.pending if p in self.leaders]
            waiting = self._gate_waiting()
            if leaders or not waiting:
                break
            self.changed.wait(max(0.0, self.deadline[0] - time.monotonic()))
        choices = leaders if waiting else self.pending
        profile_id = self.priority if self.priority in choices else (leaders or self.pending)[0]
        self.priority = None
        if self.gate_started is None:
            return profile_id, None
        if profile_id in self.leaders:
            self.admitted[profile_id] = time.monotonic()
            if waiting:
                # This leader's head start begins now.
                self._schedule()
            promoted = self.leaders[profile_id]
            return profile_id, (('leader', 0.0) if promoted is None
                                else ('promoted', promoted - self.gate_started))
        return profile_id, (self.result['gate']['released_by'], self.gate_released - self.gate_started)

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
