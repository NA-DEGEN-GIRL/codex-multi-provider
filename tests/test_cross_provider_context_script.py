"""Unit tests for scripts/test_cross_provider_context_live.py; no runtime, network or credential."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import test_cross_provider_context_live as script  # noqa: E402

THREAD = '01a0dcd0-0000-7000-8000-000000000001'
FORK = '01a0dcd0-0000-7000-8000-000000000002'
# Built at runtime so this file itself never looks like a credential.
FAKE_BEARER = 'Bear' + 'er ' + 'z' * 30


def response_item(kind, encrypted=None, role=None):
    item = {'type': kind}
    if role:
        item['role'] = role
    if encrypted:
        item['encrypted_content'] = encrypted
    return {'type': 'response_item', 'payload': item}


class RunFiles:
    """An output directory laid out like run(): raw homes with a rollout, and evidence."""

    def __init__(self, base):
        self.out = Path(base)
        self.raw, self.evidence = self.out / 'raw', self.out / 'evidence'
        self.shared = self.raw / 'shared-record'
        (self.shared / 'sessions').mkdir(parents=True)
        self.evidence.mkdir()
        self.rollout = self.shared / 'sessions' / f'rollout-2026-09-26T00-00-00-{THREAD}.jsonl'
        self.rollout.write_text(json.dumps(response_item('message', role='user')) + '\n', encoding='utf-8')

    def report(self, **values):
        return {'status': 'FAIL', 'stage': 'done', 'checks': {'a': True, 'b': True}, 'proxy_requests': [], **values}


class RolloutReadingTests(unittest.TestCase):
    def test_partial_utf8_line_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            # A runtime stopped mid-write: the last line ends inside a Korean character.
            with files.rollout.open('ab') as stream:
                stream.write('{"type": "event_msg", "payload": {"message": "확'.encode('utf-8')[:-1])
            paths, records = script.rollout_records([files.shared], THREAD)
            self.assertEqual(len(paths), 1)
            self.assertEqual(len(records), 1)
            written = script.write_redacted_rollouts(paths, files.evidence / 'rollout', script.Secrets())
            rows = (files.evidence / 'rollout' / written[0]).read_text(encoding='utf-8').splitlines()
            self.assertEqual(rows[-1], '"[unparsed line]"')

    def test_records_of_several_threads(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            fork = files.shared / 'sessions' / f'rollout-2026-09-26T00-00-01-{FORK}.jsonl'
            fork.write_text(json.dumps(response_item('message', role='user')) + '\n', encoding='utf-8')
            paths, _ = script.rollout_records([files.shared], [THREAD, FORK, None])
            self.assertEqual({path.name for path in paths}, {files.rollout.name, fork.name})
            paths, _ = script.rollout_records([files.shared], THREAD)
            self.assertEqual([path.name for path in paths], [files.rollout.name])


class EvidenceAndDisposalTests(unittest.TestCase):
    def test_rollout_failure_still_deletes_raw_homes_and_fails(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            report = files.report()
            with patch.object(script, 'write_redacted_rollouts', side_effect=OSError('simulated read failure')):
                script.write_evidence_and_dispose(report, [files.shared], [THREAD], files.evidence, files.raw,
                                                  script.Secrets(), True)
            self.assertFalse(files.raw.exists())
            self.assertTrue(report['raw_homes_deleted'])
            self.assertIn('simulated read failure', report['evidence_errors'][0])
            self.assertEqual(script.conclude(report, negative_control=False, summary_path=False,
                                             raw_must_be_deleted=True), 'FAIL')
            self.assertIn('the evidence could not be written', report['status_blockers'])
            script.finish_evidence(files.out, files.evidence, report, script.Secrets())
            on_disk = json.loads((files.out / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual((on_disk['status'], on_disk['secret_scan']), ('FAIL', 'clean'))

    def test_surviving_raw_homes_fail_the_run(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            report = files.report()
            with patch.object(script, 'remove_tree', return_value=False):
                script.write_evidence_and_dispose(report, [files.shared], [THREAD], files.evidence, files.raw,
                                                  script.Secrets(), True)
            self.assertEqual(report['redacted_rollouts'], [files.rollout.stem + '.redacted.jsonl'])
            self.assertFalse(report['raw_homes_deleted'])
            self.assertEqual(script.conclude(report, negative_control=False, summary_path=False,
                                             raw_must_be_deleted=True), 'FAIL')
            self.assertIn('the disposable homes could not be deleted', report['status_blockers'])
            # --keep-raw in fixture mode: the homes are meant to stay.
            self.assertEqual(script.conclude(report, negative_control=False, summary_path=False,
                                             raw_must_be_deleted=False), 'PASS')

    def test_remove_tree_exception_is_recorded(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            report = files.report()
            with patch.object(script, 'remove_tree', side_effect=PermissionError('locked')):
                script.write_evidence_and_dispose(report, [files.shared], [THREAD], files.evidence, files.raw,
                                                  script.Secrets(), True)
            self.assertEqual(report['cleanup_errors'], ['raw homes: PermissionError'])
            self.assertFalse(report['raw_homes_deleted'])

    def test_passing_checks_pass_only_after_a_finished_run(self):
        report = {'stage': 'done', 'checks': {'a': True}, 'raw_homes_deleted': True}
        self.assertEqual(script.conclude(report, negative_control=False, summary_path=False,
                                         raw_must_be_deleted=True), 'PASS')
        report.update(stage='X turn (ask facts)')
        self.assertEqual(script.conclude(report, negative_control=False, summary_path=False,
                                         raw_must_be_deleted=True), 'FAIL')
        report.update(stage='done', error='RuntimeError: boom')
        self.assertEqual(script.conclude(report, negative_control=False, summary_path=False,
                                         raw_must_be_deleted=True), 'FAIL')

    def test_negative_control_needs_expected_failures_and_a_clean_finish(self):
        expected = script.negative_control_expected(True)
        checks = {name: name not in expected for name in expected | {'x_turn_completed', 'x_did_not_compact'}}
        report = {'stage': 'done', 'checks': checks, 'raw_homes_deleted': True}
        self.assertEqual(script.conclude(report, negative_control=True, summary_path=True,
                                         raw_must_be_deleted=True), 'NEGATIVE_CONTROL_OK')
        report['raw_homes_deleted'] = False
        self.assertEqual(script.conclude(report, negative_control=True, summary_path=True,
                                         raw_must_be_deleted=True), 'NEGATIVE_CONTROL_UNEXPECTED')
        report['raw_homes_deleted'] = True
        report['checks'] = {**checks, 'x_answer_codeword': True}
        self.assertEqual(script.conclude(report, negative_control=True, summary_path=True,
                                         raw_must_be_deleted=True), 'NEGATIVE_CONTROL_UNEXPECTED')
        self.assertEqual(report['negative_control']['unexpected_passes'], ['x_answer_codeword'])


class SecretScanTests(unittest.TestCase):
    def test_clean_scan_keeps_the_concluded_status(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            secrets = script.Secrets()
            secrets.add('fixture-secret-value')
            report = files.report(status='PASS', note='carried fixture-secret-value')
            script.finish_evidence(files.out, files.evidence, report, secrets)
            text = (files.out / 'report.json').read_text(encoding='utf-8')
            self.assertNotIn('fixture-secret-value', text)
            on_disk = json.loads(text)
            self.assertEqual((on_disk['status'], on_disk['secret_scan']), ('PASS', 'clean'))
            self.assertTrue((files.evidence / 'proxy-requests.json').exists())

    def test_report_stays_failed_and_pending_until_the_scan_finishes(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            report = files.report(status='PASS')
            with patch.object(script.Secrets, 'scan', side_effect=RuntimeError('scan interrupted')):
                with self.assertRaises(RuntimeError):
                    script.finish_evidence(files.out, files.evidence, report, script.Secrets())
            on_disk = json.loads((files.out / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual((on_disk['status'], on_disk['secret_scan']), ('FAIL', 'pending'))

    def test_flagged_file_is_deleted_and_fails_the_run(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            leak = files.evidence / 'rollout' / 'leak.jsonl'
            leak.parent.mkdir()
            leak.write_text(FAKE_BEARER, encoding='utf-8')
            unrelated = files.out / 'notes.txt'
            unrelated.write_text(FAKE_BEARER, encoding='utf-8')
            report = files.report(status='PASS')
            script.finish_evidence(files.out, files.evidence, report, script.Secrets())
            self.assertFalse(leak.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(report['status'], 'FAIL')
            self.assertEqual([(f['match'], f['action']) for f in report['secret_scan']], [('bearer', 'deleted')])

    def test_failed_delete_is_recorded_and_fails_the_run(self):
        with tempfile.TemporaryDirectory() as base:
            files = RunFiles(base)
            leak = files.evidence / 'rollout' / 'leak.jsonl'
            leak.parent.mkdir()
            leak.write_text(FAKE_BEARER, encoding='utf-8')
            report = files.report(status='PASS')
            real_unlink = Path.unlink

            def locked(path, *args, **kwargs):
                if path.name == 'leak.jsonl':
                    raise PermissionError(32, 'locked')
                return real_unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', autospec=True, side_effect=locked):
                script.finish_evidence(files.out, files.evidence, report, script.Secrets())
            on_disk = json.loads((files.out / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(on_disk['status'], 'FAIL')
            self.assertEqual([f['action'] for f in on_disk['secret_scan']], ['delete failed: PermissionError'])
            self.assertEqual(report['status'], 'FAIL')
            self.assertNotEqual(report['secret_scan'], 'clean')


class RebuildCheckTests(unittest.TestCase):
    SUMMARY_REQUEST = {'portable_summary_present': True, 'context_note_present': False,
                       'plant_prompt_present': False, 'codeword_outside_summary': 0, 'colour_outside_summary': 0}

    def test_missing_runtime_log_fails_instead_of_dropping_checks(self):
        for log in (None, {}, {'family': 'x'}):
            checks = script.x_rebuild_checks(self.SUMMARY_REQUEST, log, summary_path=True, summary_gate=True)
            self.assertFalse(checks['x_runtime_rebuild_logged'])
            self.assertFalse(checks['x_context_note_consistent'])
            self.assertFalse(checks['x_runtime_rebuilt_from_summary'])
            checks = script.x_rebuild_checks({'plant_prompt_present': True}, log, summary_path=False, summary_gate=True)
            self.assertEqual(set(checks), {'x_runtime_rebuild_logged', 'x_context_note_consistent'})
            self.assertFalse(any(checks.values()))

    def test_summary_rebuild_passes_with_the_log(self):
        checks = script.x_rebuild_checks(self.SUMMARY_REQUEST, {'source': 'PortableCheckpoint', 'budget_tokens': 13416},
                                         summary_path=True, summary_gate=True)
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual(len(checks), 6)

    def test_context_note_must_match_the_rebuild_step(self):
        note = {**self.SUMMARY_REQUEST, 'portable_summary_present': False, 'context_note_present': True}
        checks = script.x_rebuild_checks(note, {'source': 'RecentItemsOnly'}, summary_path=True, summary_gate=False)
        self.assertTrue(checks['x_context_note_consistent'])
        self.assertNotIn('x_runtime_rebuilt_from_summary', checks)
        checks = script.x_rebuild_checks(note, {'source': 'PortableCheckpoint'}, summary_path=True, summary_gate=True)
        self.assertFalse(checks['x_context_note_consistent'])


class ArgumentTests(unittest.TestCase):
    def parse_error(self, argv):
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as raised:
            script.parse_args(argv)
        self.assertEqual(raised.exception.code, 2)
        return stderr.getvalue()

    def test_zero_or_negative_padding_is_rejected(self):
        for value in ('0', '-5'):
            self.assertIn('--padding-tokens must be positive', self.parse_error(['--padding-tokens', value]))

    def test_summary_defaults(self):
        args = script.parse_args(['--summary-path'])
        self.assertEqual((args.scenario, args.external_context_window), ('summary', script.DEFAULT_SUMMARY_WINDOW))
        self.assertEqual(args.padding_words, script.default_padding_words(script.DEFAULT_SUMMARY_WINDOW))
        self.assertEqual(script.parse_args(['--padding-tokens', '20000']).padding_words, 20000)
        self.assertEqual(script.parse_args([]).scenario, 'full')

    def test_second_account_needs_live_and_confirmation(self):
        self.assertIn('--confirm-second-account', self.parse_error(['--second-gpt-profile', 'p2']))


class ForkReplayTests(unittest.TestCase):
    def test_only_history_after_the_newest_checkpoint_counts(self):
        compacted = {'type': 'compacted', 'payload': {'replacement_history': [
            {'type': 'message', 'role': 'user', 'content': []},
            {'type': 'compaction', 'encrypted_content': 'opaque-compaction'}]}}
        records = [response_item('reasoning', 'opaque-before'), compacted]
        self.assertEqual(script.history_encrypted_items([(None, r) for r in records]), {'compaction': 1})
        records.append(response_item('reasoning', 'opaque-after'))
        records.append(response_item('message', role='assistant'))
        self.assertEqual(script.history_encrypted_items([(None, r) for r in records]),
                         {'compaction': 1, 'reasoning': 1})
        self.assertEqual(script.history_encrypted_items([(None, response_item('reasoning', 'x'))]), {'reasoning': 1})


if __name__ == '__main__':
    unittest.main()
