"""Read-only health report over a temporary workspace; nothing live is read."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from activate_manager_runtime import activate
import health_report
from health_report import report, summary
from manager_core.store import atomic_json
from test_manager_runtime_activation import THREADS_CRLF, handoff_evidence, migrated_store, stage_release

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
SECRET = 'fixture-access-token-must-not-appear'


class HealthReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'sample-repo'
        self.root.mkdir()
        self.local = Path(self.temporary.name).resolve() / 'local-app-data'

    def test_empty_workspace_reports_every_section_without_failing(self):
        value = report(self.root, now=NOW, environ={'LOCALAPPDATA': str(self.local)})
        self.assertEqual(value['manager_release'], dict(state='missing'))
        self.assertEqual({name: pointer['state'] for name, pointer in value['runtime_pointers'].items()},
                         {'current': 'missing', 'last-known-good': 'missing', 'previous': 'missing'})
        self.assertEqual(value['candidates'], dict(pending=[], incomplete=[], older_count=0))
        self.assertEqual(value['migrations']['state'], 'unknown')
        self.assertEqual(value['remote_updates'], dict(state='missing'))
        self.assertEqual((value['claude_logins'], value['runtime_exits'], value['runtime_start_retries'],
                          value['known_bad']), ([], [], [], []))
        lines = summary(value)
        self.assertIn('런타임: current 없음 / last-known-good 없음 / previous 없음', lines)
        self.assertIn('활성화 대기 후보: 없음', lines)

    def test_populated_workspace(self):
        root = self.root
        # Runtime pointers: a valid current, previous naming a deleted release, no last-known-good.
        old, old_digest = stage_release(root, '20261001-000000-aaaaaa')
        current, digest = stage_release(root, '20261002-000000-bbbbbb')
        activate(old, handoff_evidence(root, old_digest, 'old'), root)
        activate(current, handoff_evidence(root, digest, 'current'), root)
        shutil.rmtree(old.parent)
        atomic_json(root / 'artifacts/manager-runtime/known-bad.json',
                    dict(version=1, releases=[dict(release=old.parent.name, sha256=old_digest, reason='fixture')]))
        # Staged releases: one older, one newer and activatable, one newer without migrations, one incomplete.
        stage_release(root, '20260930-000000-cccccc')
        stage_release(root, '20261003-000000-dddddd')
        stage_release(root, '20261004-000000-eeeeee', migrations=None)
        (root / 'artifacts/manager-runtime/releases/20261005-000000-ffffff').mkdir()
        # Stores: the canonical home matches but was also migrated by a newer runtime;
        # a profile store was migrated by an LF build.
        home = root / 'record-home'
        atomic_json(root / 'work/control-center/canonical-storage.json', dict(version=1, home=str(home)))
        canonical = migrated_store(home / 'state_5.sqlite', hashlib.sha384(THREADS_CRLF).digest())
        with closing(sqlite3.connect(canonical)) as connection:
            connection.execute("INSERT INTO _sqlx_migrations VALUES (2, 'thread titles', '2026-10-02', 1, ?, 1)",
                               (b'\x01' * 48,))
            connection.commit()
        profile_id, claude_id, expired_id, removed_id = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        migrated_store(root / 'work/control-center/profiles' / profile_id / 'codex/state_5.sqlite',
                       hashlib.sha384(THREADS_CRLF.replace(b'\r\n', b'\n')).digest())
        # Manager state: SSH update backlog and Claude logins.
        managed = dict(state='update_available', active_bundle='0.1-old', available_bundle='0.2-new',
                       prepared_bundle='0.1-old')
        atomic_json(root / 'work/control-center/state.json', dict(version=1, revision=1, profiles=[
            dict(id=profile_id, alias='work-profile'),
            dict(id=claude_id, alias='claude-profile', auth_mode='claude_code'),
            dict(id=expired_id, alias='claude-expired', auth_mode='claude_code'),
            dict(id=str(uuid4()), alias='claude-missing', auth_mode='claude_code'),
            dict(id=removed_id, alias='claude-removed', auth_mode='claude_code', removed_at='2026-10-01')],
            remote_updates={
                # Left behind by the removal of its profile: not a backlog.
                removed_id + ':remote-host': dict(profile_id=removed_id, alias='remote-host', managed=managed),
                profile_id + ':remote-host': dict(profile_id=profile_id, alias='remote-host', auto_apply=False,
                                                  managed=managed, job=None, checked_at='2026-10-07T11:00:00+00:00'),
                profile_id + ':other-host': dict(profile_id=profile_id, alias='other-host',
                                                 managed=dict(state='current'))}))
        for identifier, expires in ((claude_id, NOW + timedelta(hours=5, minutes=30)), (expired_id, NOW - timedelta(hours=1))):
            directory = self.local / 'codex-multi-provider/claude-profiles' / identifier
            directory.mkdir(parents=True)
            (directory / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
                'accessToken': SECRET, 'refreshToken': SECRET, 'expiresAt': int(expires.timestamp() * 1000)}}),
                encoding='utf-8')
        # The runtime proxy's exit record and the manager release pointer.
        atomic_json(root / 'work/control-center/instances' / profile_id / 'runtime-state.json', dict(
            profile_id=profile_id, last_exit=dict(exit_code=1, uptime_ms=420, initialize_completed=False,
                                                  exited_at='2026-10-07T11:59:00+00:00')))
        release = root / 'artifacts/manager/releases/20261006-000000-000'
        release.mkdir(parents=True)
        atomic_json(root / 'artifacts/manager/current.json', dict(version=1, directory=str(release),
                                                                 created_at='2026-10-06T00:00:00Z'))

        value = report(root, now=NOW, environ={'LOCALAPPDATA': str(self.local)})
        text = json.dumps(value, ensure_ascii=False)
        self.assertNotIn(SECRET, text)
        self.assertEqual(value['manager_release'], dict(id=release.name, created_at='2026-10-06T00:00:00Z',
                                                        state='valid'))
        pointers = value['runtime_pointers']
        self.assertEqual((pointers['current']['state'], pointers['current']['release']), ('valid', current.parent.name))
        self.assertNotIn('activation_problem', pointers['current'])
        self.assertEqual(pointers['previous']['state'], 'missing_on_disk')
        self.assertEqual(pointers['previous']['known_bad'], 'fixture')
        self.assertEqual(pointers['last-known-good'], dict(state='missing'))
        self.assertEqual(value['known_bad'], [old.parent.name])
        staged = value['candidates']
        self.assertEqual([item['release'] for item in staged['pending']],
                         ['20261003-000000-dddddd', '20261004-000000-eeeeee'])
        self.assertNotIn('activation_problem', staged['pending'][0])
        self.assertIn('no embedded migrations', staged['pending'][1]['activation_problem'])
        self.assertEqual((staged['incomplete'], staged['older_count']), (['20261005-000000-ffffff'], 1))
        stores = value['migrations']
        self.assertEqual((stores['state'], stores['stores']), ('incompatible', 2))
        self.assertEqual(stores['incompatible'],
                         ['work/control-center/profiles/%s/codex/state_5.sqlite: 1 threads' % profile_id])
        self.assertEqual(stores['ahead'], ['record-home/state_5.sqlite: 2 thread titles'])
        self.assertEqual(value['remote_updates']['pending'], [dict(
            profile_id=profile_id, profile='work-profile', host='remote-host', state='update_available',
            active_bundle='0.1-old', available_bundle='0.2-new', prepared_bundle='0.1-old', auto_apply=False,
            checked_at='2026-10-07T11:00:00+00:00')])
        self.assertEqual(value['remote_updates']['states'], {'update_available': 1, 'current': 1})
        logins = {item['profile']: item for item in value['claude_logins']}
        self.assertEqual(set(logins), {'claude-profile', 'claude-expired', 'claude-missing'})
        self.assertEqual((logins['claude-profile']['state'], logins['claude-profile']['hours_left']), ('valid', 5.5))
        self.assertEqual(logins['claude-expired']['state'], 'expired')
        self.assertEqual(logins['claude-missing']['state'], 'missing')
        self.assertEqual(value['runtime_exits'], [dict(profile_id=profile_id, profile='work-profile', exit_code=1,
                                                       uptime_ms=420, initialize_completed=False,
                                                       exited_at='2026-10-07T11:59:00+00:00')])
        lines = summary(value)
        self.assertEqual(lines[1], '관리 앱 릴리스(다음 실행용): ' + release.name)
        self.assertIn('previous %s 파일 없음, 불량 목록' % old.parent.name, lines[2])
        self.assertIn('활성화 대기 후보: 2개 (20261003-000000-dddddd, 20261004-000000-eeeeee)', lines)
        self.assertIn('저장소 마이그레이션: 불일치 1건 - 현재 런타임이 시작하지 못합니다, 런타임에 없는 적용 1건', lines)
        self.assertIn('SSH 업데이트 대기: 1건 (work-profile@remote-host)', lines)
        self.assertIn('Claude 로그인: claude-profile 5.5시간 남음, claude-expired 만료됨, claude-missing 자격 증명 없음',
                      lines)
        self.assertIn('초기화 전에 종료된 런타임: 1개 프로필 (work-profile 종료 코드 1)', lines)

    def test_unreadable_files_are_reported_not_raised(self):
        (self.root / 'work/control-center').mkdir(parents=True)
        (self.root / 'work/control-center/state.json').write_text('{broken', encoding='utf-8')
        (self.root / 'artifacts/manager-runtime').mkdir(parents=True)
        (self.root / 'artifacts/manager-runtime/current.json').write_text('[1]', encoding='utf-8')
        (self.root / 'artifacts/manager-runtime/known-bad.json').write_text('{broken', encoding='utf-8')
        (self.root / 'artifacts/manager').mkdir(parents=True)
        (self.root / 'artifacts/manager/current.json').write_text('{broken', encoding='utf-8')
        value = report(self.root, now=NOW, environ={'LOCALAPPDATA': str(self.local)})
        self.assertIn('state_error', value)
        self.assertEqual(value['runtime_pointers']['current']['state'], 'invalid')
        self.assertIn('error', value['manager_release'])
        self.assertIn('error', value['known_bad'])
        lines = summary(value)
        self.assertIn('관리 앱 릴리스(다음 실행용): 확인 실패', lines)
        self.assertIn('SSH 업데이트 대기: 확인 불가', lines)

    def test_unreadable_known_bad_list_is_not_reported_as_listed(self):
        root = self.root
        names = ('20261001-000000-aaaaaa', '20261002-000000-bbbbbb', '20261003-000000-cccccc')
        for name in names:
            candidate, digest = stage_release(root, name)
            activate(candidate, handoff_evidence(root, digest, name), root)
            if name == names[1]:
                shutil.rmtree(candidate.parent)  # Deleted while active.
        (root / 'artifacts/manager-runtime/known-bad.json').write_text('{broken', encoding='utf-8')
        value = report(root, now=NOW, environ={'LOCALAPPDATA': str(self.local)})
        pointers = value['runtime_pointers']
        self.assertIs(pointers['current']['known_bad_unreadable'], True)
        self.assertNotIn('known_bad', pointers['current'])
        # previous.json names the deleted runtime the last activation replaced.
        self.assertEqual((pointers['previous']['release'], pointers['previous']['state'],
                          pointers['previous']['verified']), (names[1], 'missing_on_disk', False))
        runtimes = next(line for line in summary(value) if line.startswith('런타임: '))
        self.assertIn('current %s 정상, 불량 목록 읽기 실패 /' % names[2], runtimes)
        self.assertNotIn('불량 목록 /', runtimes)

    def test_clean_exit_before_initialize_is_not_a_failed_start(self):
        exits = dict(clean=dict(exit_code=0), failed=dict(exit_code=101), unknown=dict(exit_code=None),
                     retried=dict(exit_code=0xC0000142, retries=2))
        for name, fields in exits.items():
            atomic_json(self.root / 'work/control-center/instances' / name / 'runtime-state.json',
                        dict(last_exit=dict(initialize_completed=False, uptime_ms=10, **fields)))
        value = report(self.root, now=NOW, environ={'LOCALAPPDATA': str(self.local)})
        self.assertEqual(sorted(item['profile_id'] for item in value['runtime_exits']),
                         ['clean', 'failed', 'retried', 'unknown'])
        self.assertEqual([item.get('retries') for item in value['runtime_exits']], [None, None, 2, None])
        self.assertIn('초기화 전에 종료된 런타임: 2개 프로필 (failed 종료 코드 101, '
                      'retried 종료 코드 3221225794 · 시작 재시도 2회)', summary(value))

    def test_runtimes_recovered_after_start_retries_are_summarized(self):
        profile_id = 'aaaaaaaa-0000-4000-8000-000000000001'
        atomic_json(self.root / 'work/control-center/state.json', dict(version=1, revision=1, profiles=[
            dict(id=profile_id, alias='work-profile')]))
        failed_start = dict(exit_code=0xC0000142, uptime_ms=900, exited_at='2026-10-07T11:58:00+00:00')
        states = {
            # Still running; the observer saw the replacement complete initialize.
            profile_id: dict(initialized=True, start_retries=[failed_start]),
            # Ended after a replacement; only the exit record's count remains.
            'counted': dict(initialized=True, last_exit=dict(exit_code=0, initialize_completed=True, retries=1)),
            'ended': dict(initialized=True, start_retries=[failed_start] * 2,
                          last_exit=dict(exit_code=0, uptime_ms=60000, initialize_completed=True, retries=2)),
            # Every start failed: an early exit with its retry count, not a recovery.
            'failed': dict(initialized=False, start_retries=[failed_start] * 2,
                           last_exit=dict(exit_code=0xC0000142, uptime_ms=300, initialize_completed=False, retries=2)),
            'starting': dict(initialized=False, start_retries=[failed_start]),
            'plain': dict(initialized=True),
        }
        for name, data in states.items():
            atomic_json(self.root / 'work/control-center/instances' / name / 'runtime-state.json', data)
        value = report(self.root, now=NOW, environ={'LOCALAPPDATA': str(self.local)})
        retried = {item['profile_id']: item for item in value['runtime_start_retries']}
        self.assertEqual({name: (item['retries'], item['recovered']) for name, item in retried.items()},
                         {profile_id: (1, True), 'counted': (1, True), 'ended': (2, True), 'failed': (2, False),
                          'starting': (1, False)})
        self.assertEqual(retried[profile_id], dict(profile_id=profile_id, profile='work-profile', retries=1,
                                                   recovered=True, exit_codes=[0xC0000142],
                                                   last_retry_at='2026-10-07T11:58:00+00:00'))
        lines = summary(value)
        self.assertIn('시작 재시도 후 복구된 런타임: 3개 프로필 (work-profile 시작 재시도 1회 · 실패 종료 코드 3221225794, '
                      'counted 시작 재시도 1회, ended 시작 재시도 2회 · 실패 종료 코드 3221225794)', lines)
        self.assertIn('초기화 전에 종료된 런타임: 1개 프로필 (failed 종료 코드 3221225794 · 시작 재시도 2회)', lines)

    def test_candidate_that_cannot_be_read_is_listed_as_incomplete(self):
        stage_release(self.root, '20261001-000000-aaaaaa')
        stage_release(self.root, '20261002-000000-bbbbbb')
        read = health_report._json

        def locked(path, *arguments):
            if Path(path).parent.name == '20261001-000000-aaaaaa':
                raise PermissionError('sharing violation')
            return read(path, *arguments)

        with patch.object(health_report, '_json', side_effect=locked):
            value = report(self.root, now=NOW, environ={'LOCALAPPDATA': str(self.local)})
        self.assertEqual(value['candidates']['incomplete'], ['20261001-000000-aaaaaa'])
        self.assertEqual([item['release'] for item in value['candidates']['pending']], ['20261002-000000-bbbbbb'])

    def test_runs_as_a_script_from_the_repository_root(self):
        environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        environment.update(LOCALAPPDATA=str(self.local), PYTHONUTF8='1')
        result = subprocess.run([sys.executable, 'scripts/health_report.py', '--root', str(self.root)], cwd=REPO,
                                env=environment, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        document, separator, rest = result.stdout.partition('\n\n[상태 점검] ')
        self.assertTrue(separator)
        self.assertEqual(json.loads(document)['runtime_pointers']['current'], dict(state='missing'))
        self.assertIn('런타임: current 없음', rest)


if __name__ == '__main__':
    unittest.main()
