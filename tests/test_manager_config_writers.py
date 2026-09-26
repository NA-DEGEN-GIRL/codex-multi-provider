"""Concurrent config.toml writers on fixture profile homes; no app, process or network."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import common, personal_skills, providers


class ConfigWriterTests(unittest.TestCase):
    """Another launch's shared skill pass edits a home that is being prepared."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-config-writers-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.registry = providers.ProviderRegistry(self.root)
        self.home = self.root / 'work/control-center/profiles' / str(uuid.uuid4()) / 'codex'
        self.home.mkdir(parents=True)
        self.config = self.home / 'config.toml'
        self.config.write_text('model = "gpt-6-astra"\n', encoding='utf-8')
        self.source = self.root / 'user/.codex'
        self.source.mkdir(parents=True)
        # A personal skill outside source/skills, so no profile junction is made.
        skill = self.root / 'user/.agents/skills/demo'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: demo\n---\n', encoding='utf-8')
        self.enterContext(patch.object(providers, 'build_opener',
                                       side_effect=AssertionError('tests must not open a connection')))

    def pass_during(self, results):
        """Start the skill pass on this home and report whether it had to wait."""
        rows = personal_skills.inventory(self.source)
        rows[0]['enabled'] = False
        def run():
            try:
                results['applied'] = personal_skills.sync_home(self.home, self.source, rows)
            except Exception as error:
                results['error'] = error
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(.3)
        results['waited'], results['worker'] = worker.is_alive(), worker

    def finish_pass(self, results):
        results['worker'].join(5)
        self.assertFalse(results['worker'].is_alive())
        self.assertNotIn('error', results)
        self.assertTrue(results['waited'], 'the pass edited the config while it was being prepared')
        self.assertTrue(results['applied'])

    def skill_rules(self, config):
        return [rule['enabled'] for rule in config['skills']['config']]

    def test_skill_pass_waits_for_provider_generation_and_both_edits_survive(self):
        render, results = self.registry.render_for_host, {}
        def render_during_pass(*args, **kwargs):
            self.pass_during(results)
            return render(*args, **kwargs)
        with patch.object(self.registry, 'render_for_host', side_effect=render_during_pass):
            self.registry.generate(self.home, False, [])
        self.finish_pass(results)
        config = tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(config['subagent_model_selection'], 'automatic')
        self.assertEqual(self.skill_rules(config), [False])

    def test_generation_never_overwrites_an_edit_made_outside_the_manager(self):
        render = self.registry.render_for_host
        def render_after_app_edit(*args, **kwargs):
            # The desktop app or another process does not take config_lock.
            self.config.write_text('model = "chosen-in-the-app"\n', encoding='utf-8')
            return render(*args, **kwargs)
        with patch.object(self.registry, 'render_for_host', side_effect=render_after_app_edit), \
                self.assertRaisesRegex(providers.ProviderError, 'changed while they were prepared'):
            self.registry.generate(self.home, False, [])
        self.assertEqual(self.config.read_text(encoding='utf-8'), 'model = "chosen-in-the-app"\n')
        self.assertFalse((self.home / 'manager-provider-binding.json').exists())
        self.registry.generate(self.home, False, [])
        config = tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual((config['model'], config['subagent_model_selection']), ('chosen-in-the-app', 'automatic'))

    def test_common_preparation_and_skill_pass_no_longer_fail_each_other(self):
        (self.source / 'config.toml').write_text('[mcp_servers.shared]\ncommand = "tool"\n', encoding='utf-8')
        refresh, results = common._refresh_mcp, {}
        def refresh_during_pass(*args):
            self.pass_during(results)
            return refresh(*args)
        with patch.object(common, '_refresh_mcp', side_effect=refresh_during_pass):
            common.prepare_common(self.home, self.source)
        self.finish_pass(results)
        config = tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(config['mcp_servers']['shared']['command'], 'tool')
        self.assertEqual(self.skill_rules(config), [False])

    def test_config_lock_is_per_home(self):
        other = self.home.parent.parent / str(uuid.uuid4()) / 'codex'
        entered = threading.Event()
        with common.config_lock(self.home):
            def hold_other():
                with common.config_lock(other):
                    entered.set()
            worker = threading.Thread(target=hold_other)
            worker.start()
            self.assertTrue(entered.wait(2), 'another home waited for this one')
            same = threading.Event()
            def hold_same():
                # Another spelling of the same home shares its lock.
                with common.config_lock(self.home.parent.parent / '..' / 'profiles' / self.home.parent.name / 'codex'):
                    same.set()
            waiter = threading.Thread(target=hold_same)
            waiter.start()
            self.assertFalse(same.wait(.2), 'the same home was edited twice at once')
        self.assertTrue(same.wait(2))
        worker.join(2)
        waiter.join(2)


@unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
class WindowsReaderTests(unittest.TestCase):
    """The desktop app or a scanner briefly holding config.toml open for reading."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-config-reader-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'config.toml'
        self.path.write_bytes(b'old = true\n')
        self.writers = dict(common=lambda text: common._atomic_write(self.path, text),
                            providers=lambda text: providers._atomic_bytes(self.path, text.encode('utf-8')))

    def test_config_writers_wait_for_a_brief_reader(self):
        replace = os.replace
        for name, write in self.writers.items():
            with self.subTest(writer=name):
                denied = threading.Event()
                def observed_replace(source, target):
                    try:
                        return replace(source, target)
                    except PermissionError:
                        denied.set()
                        raise
                reader = self.path.open('rb')
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool, patch('os.replace', observed_replace):
                        future = pool.submit(write, f'{name} = true\n')
                        self.assertTrue(denied.wait(3), 'the real reader must deny replacement first')
                        reader.close()
                        future.result(timeout=3)
                finally:
                    reader.close()
                self.assertEqual(self.path.read_text(encoding='utf-8'), f'{name} = true\n')
                self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_persistent_reader_fails_bounded_and_keeps_the_old_config(self):
        for name, write in self.writers.items():
            with self.subTest(writer=name):
                with self.path.open('rb') as reader:
                    with self.assertRaises(PermissionError):
                        write('replacement = true\n')
                    self.assertEqual(reader.read(), b'old = true\n')
                self.assertEqual(self.path.read_text(encoding='utf-8'), 'old = true\n')
                self.assertFalse(list(self.path.parent.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
