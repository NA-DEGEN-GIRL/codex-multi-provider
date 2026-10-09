"""State poll cost: one store read per poll, no no-op rewrites, and shared concurrent polls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import control_center
from control_center import ControlCenter, INTERNAL_ERROR
from manager_core.store import Store, Unchanged, atomic_json, file_stamp, json_copy


def streamed(data):
    """The bytes the former json.dump-based writer produced."""
    buffer = io.StringIO()
    json.dump(data, buffer, ensure_ascii=False, indent=2, allow_nan=False)
    return (buffer.getvalue() + '\n').encode('utf-8')


def wait_for(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError('condition not reached')
        time.sleep(.005)


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'nested' / 'state.json'

    def test_single_write_keeps_the_streamed_bytes(self):
        data = dict(version=1, revision=7, alias='프로필 04 · 기본', empty_list=[], empty_dict={},
                    nested=[dict(a=[1, 2.5, -0.0, 1e-07, 12345678901234567890], b=None, c=True, d=False)],
                    text='quote " backslash \\ tab \t newline \n bell \x07 \u2028 🙂',
                    **{'숫자키': {'1': 'one', '': 'empty'}})
        atomic_json(self.path, data)
        written = self.path.read_bytes()
        self.assertEqual(written, streamed(data))
        self.assertTrue(written.endswith(b'}\n'))
        self.assertNotIn(b'\r\n', written)
        self.assertEqual(json.loads(written.decode('utf-8')), data)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_invalid_document_keeps_previous_file_and_no_temporary(self):
        atomic_json(self.path, dict(value=1))
        before = self.path.read_bytes()
        for invalid, error in ((dict(value=float('nan')), ValueError), (dict(value={1, 2}), TypeError)):
            with self.subTest(error=error.__name__), self.assertRaises(error):
                atomic_json(self.path, invalid)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


class StoreNoChangeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.profile = self.store.add_profile('01')

    def test_unchanged_operation_keeps_file_and_revision(self):
        before, revision = self.store.path.read_bytes(), self.store.read()['revision']
        value = dict(items=[1, 2])
        result = self.store.mutate(lambda data: Unchanged(value))
        self.assertEqual(result, value)
        self.assertIsNot(result, value)
        self.assertIsNone(self.store.mutate(lambda data: Unchanged()))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.read()['revision'], revision)
        self.store.mutate(lambda data: data.update(marker=True))
        self.assertEqual(self.store.read()['revision'], revision + 1)

    def test_existing_usage_account_is_returned_without_a_rewrite(self):
        account = str(uuid4())
        added = self.store.add_profile('usage', account, str(self.store.root / 'source'))
        before, revision = self.store.path.read_bytes(), self.store.read()['revision']
        again = self.store.add_profile('renamed elsewhere', account, str(self.store.root / 'source'))
        self.assertEqual(again, added)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.read()['revision'], revision)


class StoreReadCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.profile = self.store.add_profile('01')

    def test_unchanged_file_is_served_without_the_lock_as_independent_copies(self):
        first = self.store.read()
        first['profiles'].clear()  # Callers modify what they read.
        with patch.object(Store, 'locked', side_effect=AssertionError('read took the store lock')):
            second, third = self.store.read(), Store(self.store.root).read()
        self.assertEqual([p['id'] for p in second['profiles']], [self.profile['id']])
        self.assertEqual(second, third)
        self.assertIsNot(second['profiles'][0], third['profiles'][0])

    def test_every_write_is_seen_by_the_next_read(self):
        self.store.read()
        self.store.mutate(lambda data: data.update(marker=1))
        self.assertEqual(self.store.read()['marker'], 1)
        Store(self.store.root).mutate(lambda data: data.update(marker=2))  # Another writer.
        self.assertEqual(self.store.read()['marker'], 2)
        self.store.path.write_text('{broken', encoding='utf-8')  # An in-place rewrite.
        with self.assertRaises(ValueError):
            self.store.read()

    def test_reader_does_not_wait_for_a_writer_holding_the_lock(self):
        self.store.read()
        held, release = threading.Event(), threading.Event()
        def writer():
            with self.store.locked():
                held.set()
                release.wait(5)
        thread = threading.Thread(target=writer)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(3))
        started = time.monotonic()
        self.assertEqual(self.store.read()['profiles'][0]['id'], self.profile['id'])
        self.assertLess(time.monotonic() - started, 1)

    def test_file_stamp_trusts_a_new_file_identity_or_a_settled_file(self):
        path = self.store.path
        current = time.time_ns()
        os.utime(path, ns=(current, current))
        self.assertTrue(file_stamp(path, replaced=True))  # atomic_json gave it a new identity.
        self.assertIsNone(file_stamp(path))  # An in-place rewrite within one tick looks the same.
        old = current - 10 ** 10
        os.utime(path, ns=(old, old))
        self.assertEqual(file_stamp(path)[1:], (path.stat().st_size, old))
        self.assertEqual(file_stamp(path.with_name('missing.json')), ())
        anonymous = SimpleNamespace(st_ino=0, st_size=5, st_mtime_ns=current)
        with patch('manager_core.store.os.stat', return_value=anonymous):
            self.assertIsNone(file_stamp(path, replaced=True))

    def test_json_copy_is_independent_and_falls_back_for_other_values(self):
        document = dict(items=[dict(a=1)], text='프로필', flag=True, none=None, number=2.5)
        copied = json_copy(document)
        self.assertEqual(copied, document)
        self.assertIsNot(copied['items'][0], document['items'][0])
        self.assertEqual(json_copy([Path('sample-repo')]), [Path('sample-repo')])  # Not marshallable.

    def write_marker(self, value):
        """Another process replaces the file."""
        data = json.loads(self.store.path.read_text(encoding='utf-8'))
        data['marker'] = value
        atomic_json(self.store.path, data)

    def test_lock_holders_and_mutate_never_build_on_a_repeated_stamp(self):
        # Off NTFS a stamp can repeat: a reused inode, the same size, one mtime tick.
        self.store.read()
        stamp = file_stamp(self.store.path, replaced=True)
        self.write_marker('second')
        with patch('manager_core.store.file_stamp', return_value=stamp):
            self.assertNotIn('marker', self.store.read())  # The repeat fools an unlocked read only.
            self.assertEqual(self.store.mutate(lambda data: data.setdefault('seen', data.get('marker'))), 'second')
            self.write_marker('third')
            with self.store.locked():
                self.assertEqual(self.store.read()['marker'], 'third')
        saved = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertEqual((saved['marker'], saved['seen']), ('third', 'second'))

    def test_a_failed_stat_falls_back_to_the_locked_read(self):
        self.store.read()
        real, failed = os.stat, []
        def stat(path, *args, **kwargs):
            if not failed:
                failed.append(path)
                raise PermissionError(13, 'fixture sharing violation')
            return real(path, *args, **kwargs)
        with patch('manager_core.store.os.stat', side_effect=stat), \
                patch.object(Store, 'locked', wraps=self.store.locked) as locked:
            self.assertEqual(self.store.read()['profiles'][0]['id'], self.profile['id'])
        self.assertEqual((len(failed), locked.call_count), (1, 1))


class StatePollTests(unittest.TestCase):
    def setUp(self):
        self.quota_guard = patch('manager_core.usage_refresh.read_quota',
                                 side_effect=RuntimeError('fixture: no network'))
        self.quota_guard.start()
        self.addCleanup(self.quota_guard.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        home = self.root / 'user-home'
        home_guard = patch('pathlib.Path.home', return_value=home)
        home_guard.start()
        self.addCleanup(home_guard.stop)
        self.store = Store(self.root)
        self.one = self.store.add_profile('01')
        self.two = self.store.add_profile('02')
        def seed(data):
            Store._source(data, home / '.codex', 'original:local', '기존 Codex')
            data['ssh_inventory'] = {self.one['id']: dict(generation=str(uuid4()), adapter='tracked-ssh-v1',
                hosts=['fixture-a'], unclassified=False, operations={str(uuid4()): dict(
                    operation='native-proxy', pid=4000 + index, process_created=index) for index in range(50)})}
            data['remote_updates'] = {self.one['id'] + ':fixture-a': dict(profile_id=self.one['id'],
                alias='fixture-a', checking=False, job=None, stock={}, managed={}, _target=None)}
        self.store.mutate(seed)
        self.center = ControlCenter(self.root)
        self.center._remote_reconcile_started = True
        self.center.remote.list_hosts = Mock(return_value=[])
        self.center.usage_refresh.schedule = Mock()
        forks = patch('manager_core.note_forks.refresh')
        self.forks = forks.start()
        self.addCleanup(forks.stop)

    def poll(self):
        with patch.object(self.center.store, 'read', wraps=self.center.store.read) as read:
            return self.center.state(), read.call_count

    def test_poll_reads_the_store_once_and_omits_only_ssh_inventory(self):
        saved = self.store.read()
        result, reads = self.poll()
        self.assertEqual(reads, 1)
        self.assertNotIn('ssh_inventory', result)
        self.assertIn('ssh_inventory', self.store.read())
        for key in saved:
            if key != 'ssh_inventory':
                self.assertIn(key, result)
        for key in ('profile_restarts', 'startup_updates', 'remote_updates', 'profile_warmup', 'local_launches',
                    'providers', 'models', 'removed_profiles', 'updates', 'hosts', 'view_instances',
                    'catalog_refresh', 'capabilities', 'notices'):
            self.assertIn(key, result)
        self.assertEqual([item['alias'] for item in result['remote_updates']['items']], ['fixture-a'])
        self.assertFalse(result['remote_updates']['worker_active'])
        self.assertEqual({p['id'] for p in result['profiles']}, {self.one['id'], self.two['id']})
        self.assertEqual(self.forks.call_args.args[1]['sources'], saved['sources'])
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_quota_refresh_gets_saved_profiles_not_the_decorated_view(self):
        saved = self.store.read()['profiles']
        result, _ = self.poll()
        self.center.usage_refresh.schedule.assert_called_once()
        profiles = self.center.usage_refresh.schedule.call_args.kwargs['profiles']
        self.assertEqual(profiles, saved)
        self.assertTrue(all('runtime_selection' in p for p in result['profiles']))
        self.assertFalse(any('runtime_selection' in p or 'status_message' in p for p in profiles))
        shown = {id(p) for p in result['profiles']}
        self.assertFalse(any(id(p) in shown for p in profiles))

    def test_first_poll_registers_the_original_home_then_uses_that_snapshot(self):
        def forget(data):
            data['sources'] = [s for s in data['sources'] if s['id'] != 'original:local']
        self.store.mutate(forget)
        result, reads = self.poll()
        self.assertEqual(reads, 2)  # read, then one fresh read after the registering mutate.
        self.assertIn('original:local', {s['id'] for s in result['sources']})
        self.assertIn('original:local', {s['id'] for s in self.store.read()['sources']})
        self.assertEqual(self.center.usage_refresh.schedule.call_args.kwargs['profiles'], self.store.read()['profiles'])
        _, reads = self.poll()
        self.assertEqual(reads, 1)

    def test_polls_rescan_task_forks_at_most_every_30_seconds(self):
        self.center.state()
        self.center.state()
        self.assertEqual(self.forks.call_count, 1)
        self.center._due_at['note_forks'] -= self.center.NOTE_FORKS_SECONDS
        self.center.state()
        self.assertEqual(self.forks.call_count, 2)
        # Opening a task's notes still refreshes that task immediately.
        thread = str(uuid4())
        self.assertEqual(self.center.dispatch('notes.refresh_forks', dict(task=dict(thread_id=thread))),
                         dict(refreshed=True))
        self.assertEqual((self.forks.call_count, self.forks.call_args.kwargs), (3, dict(thread_id=thread)))

    def test_a_failed_fork_rescan_is_retried_on_the_next_poll(self):
        self.forks.side_effect = RuntimeError('fixture: database is locked')
        self.center.state()
        self.forks.side_effect = None
        self.center.state()
        self.center.state()
        self.assertEqual(self.forks.call_count, 2)  # Retried once, then throttled again.

    def test_poll_reads_the_runtime_release_once_and_resolves_no_saved_home(self):
        homes = {os.path.normcase(s['home']) for s in self.store.read()['sources'] if s['id'] != 'original:local'}
        resolved, real = [], Path.resolve
        def resolve(path, *args, **kwargs):
            resolved.append(os.path.normcase(str(path)))
            return real(path, *args, **kwargs)
        with patch('control_center.runtime_build', wraps=control_center.runtime_build) as build, \
                patch.object(Path, 'resolve', resolve):
            self.center.state()
        self.assertEqual(build.call_count, 1)
        self.assertTrue(resolved)
        self.assertFalse(homes & set(resolved))

    def test_repeated_polls_never_rewrite_the_store_or_raise_notices(self):
        before = self.store.path.read_bytes()
        with patch('manager_core.store.atomic_json', side_effect=AssertionError('rewrote unchanged store')):
            for _ in range(3):
                result = self.center.state()
                self.assertEqual(result['notices'], [])
        self.assertEqual(self.store.path.read_bytes(), before)


class SharedStateTests(unittest.TestCase):
    def setUp(self):
        service = patch('manager_core.rust_service.enabled', return_value=True)
        service.start()
        self.addCleanup(service.stop)
        self.calls = []
        self.release = threading.Event()
        self.entered = threading.Event()
        center = self.center = ControlCenter.__new__(ControlCenter)
        center._mutex = threading.RLock()
        center._request_gates = {}
        center._request_gate_lock = threading.Lock()
        center._state_ready = threading.Condition()
        center._state_flight = None
        center.state = self.compute
        self.failure = None
        self.gates = {1: self.release}  # Computation number -> event it waits for.
        self.executor = ThreadPoolExecutor(4)
        self.addCleanup(self.executor.shutdown, wait=True)
        self.addCleanup(self.release.set)

    def compute(self):
        self.calls.append(threading.current_thread().name)
        number = len(self.calls)
        if number == 1:
            self.entered.set()
        if number in self.gates:
            self.assertTrue(self.gates[number].wait(3))
        if self.failure and number == 1:
            raise self.failure
        return dict(poll=number, profiles=[dict(id='one')])

    def poll(self, rid):
        return self.executor.submit(self.center.request, dict(id=rid, command='state', args={}))

    def waiting(self, count):
        # Condition keeps its sleeping threads in _waiters (CPython).
        wait_for(lambda: len(self.center._state_ready._waiters) == count)

    def finish_command(self, **outcome):
        self.center.dispatch = Mock(**outcome)
        return self.center.request(dict(id='w', command='profile.rename', args=dict(profile_id='one', alias='02')))

    def test_concurrent_polls_share_one_computation(self):
        first = self.poll('a')
        self.assertTrue(self.entered.wait(3))
        second, third = self.poll('b'), self.poll('c')
        self.waiting(2)
        self.release.set()
        results = [future.result(3) for future in (first, second, third)]
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([r['id'] for r in results], ['a', 'b', 'c'])
        self.assertTrue(all(r['ok'] and r['result'] == dict(poll=1, profiles=[dict(id='one')]) for r in results))
        self.assertIsNone(self.center._state_flight)

    def test_poll_after_a_stall_gets_a_fresh_computation(self):
        self.center.STATE_SHARE_SECONDS = .05
        first = self.poll('a')
        self.assertTrue(self.entered.wait(3))
        time.sleep(.2)  # The running computation is now older than the share window.
        later = self.poll('b')
        self.waiting(1)
        self.assertEqual(len(self.calls), 1)
        self.release.set()
        self.assertEqual(first.result(3)['result']['poll'], 1)
        self.assertEqual(later.result(3)['result']['poll'], 2)
        self.assertEqual(len(self.calls), 2)

    def test_poll_after_a_finished_command_never_joins_an_older_computation(self):
        # Read-your-writes: the running computation may predate the command.
        first = self.poll('a')
        self.assertTrue(self.entered.wait(3))
        self.assertTrue(self.finish_command(return_value=dict(saved=True))['ok'])
        second = self.gates[2] = threading.Event()
        self.addCleanup(second.set)
        later = [self.poll('b'), self.poll('c')]
        self.waiting(2)
        self.assertEqual(len(self.calls), 1)
        self.release.set()
        self.assertEqual(first.result(3)['result']['poll'], 1)
        wait_for(lambda: len(self.calls) == 2)
        self.waiting(1)  # The poll that did not lead joins the fresh computation.
        second.set()
        self.assertEqual([future.result(3)['result']['poll'] for future in later], [2, 2])
        self.assertEqual(len(self.calls), 2)

    def test_failed_command_also_ends_sharing(self):
        first = self.poll('a')
        self.assertTrue(self.entered.wait(3))
        self.assertFalse(self.finish_command(side_effect=RuntimeError('이름을 저장하지 못했습니다.'))['ok'])
        later = self.poll('b')
        self.waiting(1)
        self.release.set()
        self.assertEqual([first.result(3)['result']['poll'], later.result(3)['result']['poll']], [1, 2])

    def test_command_finished_before_the_computation_keeps_sharing(self):
        self.assertTrue(self.finish_command(return_value=dict(saved=True))['ok'])
        first = self.poll('a')
        self.assertTrue(self.entered.wait(3))
        joined = self.poll('b')
        self.waiting(1)
        self.release.set()
        self.assertEqual([future.result(3)['result']['poll'] for future in (first, joined)], [1, 1])
        self.assertEqual(len(self.calls), 1)

    def test_completed_poll_is_never_served_again(self):
        self.release.set()
        results = [self.center.request(dict(id=str(n), command='state', args={})) for n in range(3)]
        self.assertEqual([r['result']['poll'] for r in results], [1, 2, 3])

    def test_failure_reaches_every_waiter_and_the_next_poll_retries(self):
        self.failure = RuntimeError('다른 관리 작업이 상태를 저장 중입니다. 다시 시도하세요.')
        first = self.poll('a')
        self.assertTrue(self.entered.wait(3))
        joined = self.poll('b')
        self.waiting(1)
        self.release.set()
        for rid, future in (('a', first), ('b', joined)):
            response = future.result(3)
            self.assertEqual(response['id'], rid)
            self.assertFalse(response['ok'])
            self.assertEqual(response['error']['message'], str(self.failure))
        retry = self.center.request(dict(id='c', command='state', args={}))
        self.assertTrue(retry['ok'])
        self.assertEqual(retry['result']['poll'], 2)

    def test_without_the_service_state_keeps_the_global_gate(self):
        self.release.set()
        with patch('manager_core.rust_service.enabled', return_value=False):
            response = self.center.request(dict(id='x', command='state', args={}))
        self.assertTrue(response['ok'])
        self.assertIsNone(self.center._state_flight)

    def test_unexpected_error_keeps_the_id_and_hides_its_text(self):
        center = self.center
        center.dispatch = Mock(side_effect=IndexError('C:/Users/secret/auth.json token'))
        response = center.request(dict(id='x', command='profile.show', args=dict(profile_id='one')))
        self.assertEqual(response, dict(id='x', ok=False, error=dict(code='internal_error', message=INTERNAL_ERROR)))
        self.assertNotIn('secret', json.dumps(response))


class MainLoopTests(unittest.TestCase):
    def run_main(self, lines, *extra, center=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        output = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')  # Answers go to sys.stdout.buffer.
        argv = ['control_center', '--root', temp.name, *extra]
        patches = [patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(''.join(lines))),
                   patch('manager_core.rust_service.enabled', return_value=False),
                   patch.dict(os.environ, {'CODEX_MANAGER_STARTUP_PROFILE': '0'})]
        if center is not None:
            patches.append(patch('control_center.ControlCenter', return_value=center))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with redirect_stdout(output):
            control_center.main()
        self.written = output.buffer.getvalue()
        return [json.loads(line) for line in self.written.decode('utf-8').splitlines()]

    def test_every_request_gets_an_answer_with_its_id(self):
        def dispatch(center, command, args):
            if command == 'explode':
                raise StopIteration('C:/Users/secret/auth.json')
            if command == 'nan':
                return dict(value=float('nan'))
            return dict(fine=True)
        lines = [json.dumps(dict(id=rid, command=command, args={})) + '\n'
                 for rid, command in (('explode', 'explode'), ('nan', 'nan'), ('ok', 'fine'))]
        with patch.object(ControlCenter, 'dispatch', dispatch):
            responses = self.run_main(lines + ['not json\n'])
        internal = dict(code='internal_error', message=INTERNAL_ERROR)
        self.assertEqual(responses, [dict(id='explode', ok=False, error=internal),
                                     dict(id='nan', ok=False, error=internal),
                                     dict(id='ok', ok=True, result=dict(fine=True)),
                                     dict(id=None, ok=False, error=dict(code='invalid_json',
                                                                        message='JSON 요청을 해석하지 못했습니다.'))])
        self.assertNotIn('secret', json.dumps(responses))

    def test_unencodable_result_still_answers_with_the_id(self):
        # Electron writes a truncated emoji as "\ud83d"; json.dumps accepts it, UTF-8 does not.
        lone = json.loads('"\\ud83d"')
        def dispatch(center, command, args):
            return dict(title=lone if command == 'surrogate' else '정상 🙂')
        lines = [json.dumps(dict(id=rid, command=command, args={})) + '\n'
                 for rid, command in (('surrogate', 'surrogate'), ('ok', 'fine'), (lone, 'fine'))]
        with patch.object(ControlCenter, 'dispatch', dispatch):
            responses = self.run_main(lines)
        internal = dict(code='internal_error', message=INTERNAL_ERROR)
        self.assertEqual(responses, [dict(id='surrogate', ok=False, error=internal),
                                     dict(id='ok', ok=True, result=dict(title='정상 🙂')),
                                     dict(id=None, ok=False, error=internal)])  # No UTF-8 client sent that id.
        self.assertEqual(self.written.count(b'\n'), 3)
        self.assertNotIn(b'\r\n', self.written)
        self.assertIn('"정상 🙂"'.encode('utf-8'), self.written)

    def test_request_handler_failure_still_answers_with_the_id(self):
        center = MagicMock()
        center.request.side_effect = StopIteration('private text')
        responses = self.run_main([json.dumps(dict(id='lost', command='state', args={})) + '\n'], center=center)
        self.assertEqual(responses, [dict(id='lost', ok=False, error=dict(code='internal_error', message=INTERNAL_ERROR))])

    def test_serve_mode_runs_the_ssh_inventory_sweeper_for_the_backend_lifetime(self):
        center = MagicMock()
        self.run_main([], '--serve', center=center)
        calls = center.mock_calls
        self.assertIn(call.ssh_inventory.start_sweeper(), calls)
        self.assertLess(calls.index(call.remote_catalog.start()), calls.index(call.ssh_inventory.start_sweeper()))
        self.assertLess(calls.index(call.ssh_inventory.start_sweeper()), calls.index(call.ssh_inventory.stop_sweeper()))
        self.assertLess(calls.index(call.remote_updates.start()), calls.index(call.ssh_inventory.start_sweeper()))

    def test_one_shot_mode_never_starts_the_sweeper(self):
        center = MagicMock()
        center.request.return_value = dict(id='x', ok=True, result={})
        self.run_main([json.dumps(dict(id='x', command='state')) + '\n'], '--once', center=center)
        self.assertNotIn(call.ssh_inventory.start_sweeper(), center.mock_calls)
        self.assertNotIn(call.ssh_inventory.stop_sweeper(), center.mock_calls)


if __name__ == '__main__':
    unittest.main()
