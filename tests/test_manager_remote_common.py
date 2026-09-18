"""Remote common settings with synthetic homes only; no SSH or real accounts."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("remote_common_test", ROOT / "scripts/remote_helpers/common.py")
COMMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMON)
BASE = 'model = "gpt-test"\n[features]\nmulti_agent = true\n'


class RemoteCommonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "remote-user/.codex"
        self.profile = self.root / "managed/profile/codex"
        self.source.mkdir(parents=True)
        self.profile.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def source_config(self, text):
        (self.source / "config.toml").write_text(text, encoding="utf-8")

    def profile_config(self, text):
        (self.profile / "config.toml").write_text(text, encoding="utf-8")

    def apply(self, generated=BASE):
        return tomllib.loads(COMMON.apply(self.profile, generated, source_home=self.source))

    def state(self):
        return json.loads((self.profile / COMMON.STATE_FILE).read_text(encoding="utf-8"))

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest("This test requires local symlink support.")

    def test_imports_only_remote_mcp_not_source_account_or_runtime_settings(self):
        self.source_config('model = "never-inherit-model"\n'
                           'cli_auth_credentials_store = "never-inherit-auth"\n'
                           '[mcp_servers.docs]\ncommand = "/usr/bin/python3"\n'
                           'args = ["/home/remote/mcp.py"]\n'
                           '[mcp_servers.docs.env]\nTOKEN = "synthetic-mcp-secret"\n')
        (self.source / "auth.json").write_text("never-read-account-auth")
        (self.source / "credentials.json").write_text("never-read-credentials")
        with patch.object(COMMON, "_read_text", wraps=COMMON._read_text) as reads:
            output = self.apply()
        self.assertEqual(output["model"], "gpt-test")
        self.assertNotIn("cli_auth_credentials_store", output)
        self.assertEqual(output["mcp_servers"]["docs"]["command"], "/usr/bin/python3")
        self.assertEqual(output["mcp_servers"]["docs"]["env"]["TOKEN"], "synthetic-mcp-secret")
        read_names = {call.args[0].name for call in reads.call_args_list}
        self.assertFalse({"auth.json", "credentials.json"} & read_names)
        self.assertNotIn("synthetic-mcp-secret", json.dumps(self.state()))
        self.assertFalse((self.profile / "auth.json").exists())
        self.assertFalse((self.profile / "credentials.json").exists())

    def test_updates_and_removes_unchanged_owned_servers_preserving_manual_servers(self):
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v1"\n')
        self.apply()
        text = (self.profile / "config.toml").read_text(encoding="utf-8")
        self.profile_config(text + '\n[mcp_servers.manual]\ncommand = "user-command"\n')
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v2"\n')
        output = self.apply()
        self.assertEqual(output["mcp_servers"]["shared"]["command"], "remote-v2")
        self.source_config("")
        output = self.apply()
        self.assertEqual(output["mcp_servers"], {"manual": {"command": "user-command"}})

    def test_manual_modified_server_detaches_from_future_updates(self):
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v1"\n')
        self.apply()
        text = (self.profile / "config.toml").read_text(encoding="utf-8")
        self.profile_config(text.replace("remote-v1", "manual-override"))
        for version in ("remote-v2", "remote-v3"):
            self.source_config('[mcp_servers.shared]\ncommand = "' + version + '"\n')
            self.assertEqual(self.apply()["mcp_servers"]["shared"]["command"], "manual-override")
        self.assertEqual(self.state()["mcp_preserved"], ["shared"])
        self.assertNotIn("shared", self.state()["mcp_owned"])

    def test_manual_deleted_server_is_not_reimported_on_later_activation(self):
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v1"\n')
        self.apply()
        current = tomllib.loads((self.profile / "config.toml").read_text(encoding="utf-8"))
        current.pop("mcp_servers")
        self.profile_config(COMMON._dump(current))
        for _ in range(2):
            self.assertNotIn("mcp_servers", self.apply())
        self.assertEqual(self.state()["mcp_preserved"], ["shared"])

    def test_existing_name_collision_stays_manual_even_when_initial_values_match(self):
        server = '[mcp_servers.same]\ncommand = "same-value"\n'
        self.source_config(server)
        self.profile_config(server)
        self.apply()
        self.source_config('[mcp_servers.same]\ncommand = "new-source-value"\n')
        self.assertEqual(self.apply()["mcp_servers"]["same"]["command"], "same-value")

    def test_generated_updates_preserve_unrelated_manual_nested_values(self):
        first = BASE + '[agents.old]\nconfig_file = "old.toml"\n'
        self.apply(first)
        text = (self.profile / "config.toml").read_text(encoding="utf-8")
        self.profile_config(text + '\n[agents.manual]\ndescription = "keep me"\n')
        second = BASE + '[agents.new]\nconfig_file = "new.toml"\n'
        output = self.apply(second)
        self.assertEqual(output["agents"], {"manual": {"description": "keep me"}, "new": {"config_file": "new.toml"}})

    def test_manual_required_runtime_conflict_fails_without_writes(self):
        self.apply()
        edited = (self.profile / "config.toml").read_text(encoding="utf-8").replace("gpt-test", "user-model")
        self.profile_config(edited)
        state_before = (self.profile / COMMON.STATE_FILE).read_bytes()
        with self.assertRaisesRegex(COMMON.CommonSettingsError, "manual setting conflicts"):
            self.apply(BASE.replace("gpt-test", "new-model"))
        self.assertEqual((self.profile / "config.toml").read_text(encoding="utf-8"), edited)
        self.assertEqual((self.profile / COMMON.STATE_FILE).read_bytes(), state_before)

    def test_obsolete_owned_field_keeps_manual_override_after_generated_removal(self):
        self.apply(BASE + 'unused = true\n')
        text = (self.profile / "config.toml").read_text(encoding="utf-8").replace('"unused" = true', '"unused" = false')
        self.profile_config(text)
        self.assertFalse(self.apply()["features"]["unused"])

    def test_legacy_whole_file_hash_bootstraps_generated_ownership(self):
        self.profile_config(BASE)
        digest = hashlib.sha256((self.profile / "config.toml").read_bytes()).hexdigest()
        output = COMMON.apply(self.profile, BASE.replace("gpt-test", "gpt-next"), source_home=self.source,
                              previous_generated_sha256=digest)
        self.assertEqual(tomllib.loads(output)["model"], "gpt-next")

    def test_nonmatching_legacy_hash_does_not_claim_manual_values(self):
        self.profile_config(BASE)
        with self.assertRaises(COMMON.CommonSettingsError):
            COMMON.prepare(self.profile, BASE.replace("gpt-test", "gpt-next"), source_home=self.source,
                           previous_generated_sha256="f" * 64)
        self.assertFalse((self.profile / COMMON.STATE_FILE).exists())

    def test_prepare_is_side_effect_free_and_commit_requires_exact_installed_plan(self):
        self.source_config('[mcp_servers.remote]\nurl = "https://example.invalid"\n')
        plan = COMMON.prepare(self.profile, BASE, source_home=self.source)
        self.assertEqual(list(self.profile.iterdir()), [])
        with self.assertRaises(COMMON.CommonSettingsError):
            COMMON.commit(self.profile, plan)
        COMMON.stage(self.profile, plan)
        (self.profile / "config.toml").write_bytes(plan.config.encode("utf-8"))
        summary = COMMON.commit(self.profile, plan)
        self.assertEqual(summary["mcp_added"], ["remote"])
        self.assertEqual(summary["mcp_shared_count"], 1)

    def fail_atomic_for(self, filename):
        original = COMMON._atomic

        def failing(path, content):
            if path.name == filename:
                raise OSError("synthetic write failure")
            return original(path, content)

        return patch.object(COMMON, "_atomic", side_effect=failing)

    def test_first_ownership_commit_failure_never_claims_unrelated_manual_leaves(self):
        self.profile_config('manual_theme = "keep-me"\n[features]\nmanual_flag = true\n')
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v1"\n')
        with self.fail_atomic_for(COMMON.STATE_FILE), self.assertRaises(OSError):
            self.apply()
        self.assertFalse((self.profile / COMMON.STATE_FILE).exists())
        self.assertTrue((self.profile / COMMON.PENDING_FILE).exists())
        # Reproduce the former launcher ordering, where the generated whole-file
        # digest had already advanced even though common ownership commit failed.
        advanced_digest = hashlib.sha256((self.profile / "config.toml").read_bytes()).hexdigest()
        output = tomllib.loads(COMMON.apply(self.profile, BASE.replace("gpt-test", "next-model"),
            source_home=self.source, previous_generated_sha256=advanced_digest))
        self.assertEqual(output["manual_theme"], "keep-me")
        self.assertTrue(output["features"]["manual_flag"])
        self.assertEqual(output["model"], "next-model")
        self.assertNotIn('["manual_theme"]', self.state()["base_owned"])
        self.assertNotIn('["features","manual_flag"]', self.state()["base_owned"])
        self.assertNotIn("shared", self.state()["mcp_preserved"])
        self.assertFalse((self.profile / COMMON.PENDING_FILE).exists())

    def test_existing_ownership_commit_failure_recovers_mcp_ownership_after_config_write(self):
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v1"\n')
        self.apply()
        before_state = (self.profile / COMMON.STATE_FILE).read_bytes()
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v2"\n')
        with self.fail_atomic_for(COMMON.STATE_FILE), self.assertRaises(OSError):
            self.apply(BASE.replace("gpt-test", "second-model"))
        self.assertEqual((self.profile / COMMON.STATE_FILE).read_bytes(), before_state)
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v3"\n')
        output = self.apply(BASE.replace("gpt-test", "third-model"))
        self.assertEqual(output["model"], "third-model")
        self.assertEqual(output["mcp_servers"]["shared"]["command"], "remote-v3")
        self.assertEqual(self.state()["mcp_preserved"], [])

    def test_failure_before_config_write_recovers_previous_ownership(self):
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v1"\n')
        self.apply()
        before_config = (self.profile / "config.toml").read_bytes()
        self.source_config('[mcp_servers.shared]\ncommand = "never-installed-v2"\n')
        with self.fail_atomic_for("config.toml"), self.assertRaises(OSError):
            self.apply(BASE.replace("gpt-test", "never-installed-model"))
        self.assertEqual((self.profile / "config.toml").read_bytes(), before_config)
        self.assertTrue((self.profile / COMMON.PENDING_FILE).exists())
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v3"\n')
        output = self.apply(BASE.replace("gpt-test", "third-model"))
        self.assertEqual(output["model"], "third-model")
        self.assertEqual(output["mcp_servers"]["shared"]["command"], "remote-v3")
        self.assertEqual(self.state()["mcp_preserved"], [])

    def test_pending_journal_config_mismatch_fails_without_adopting_or_overwriting(self):
        self.profile_config('manual_setting = "keep"\n')
        plan = COMMON.prepare(self.profile, BASE, source_home=self.source)
        COMMON.stage(self.profile, plan)
        manually_changed = plan.config + 'unexpected_manual = "keep-too"\n'
        self.profile_config(manually_changed)
        before = {item.name: item.read_bytes() for item in self.profile.iterdir()}
        with self.assertRaisesRegex(COMMON.CommonSettingsError, "unfinished common settings update"):
            COMMON.apply(self.profile, BASE, source_home=self.source,
                previous_generated_sha256=hashlib.sha256((self.profile / "config.toml").read_bytes()).hexdigest())
        self.assertEqual({item.name: item.read_bytes() for item in self.profile.iterdir()}, before)

    def test_failure_after_ownership_write_before_journal_cleanup_is_recoverable(self):
        original_unlink = Path.unlink

        def failing(path, *args, **kwargs):
            if path.name == COMMON.PENDING_FILE:
                raise OSError("synthetic cleanup failure")
            return original_unlink(path, *args, **kwargs)

        self.source_config('[mcp_servers.shared]\ncommand = "remote-v1"\n')
        with patch.object(Path, "unlink", failing), self.assertRaises(OSError):
            self.apply()
        self.assertTrue((self.profile / COMMON.STATE_FILE).exists())
        self.assertTrue((self.profile / COMMON.PENDING_FILE).exists())
        self.source_config('[mcp_servers.shared]\ncommand = "remote-v2"\n')
        self.assertEqual(self.apply()["mcp_servers"]["shared"]["command"], "remote-v2")
        self.assertFalse((self.profile / COMMON.PENDING_FILE).exists())

    def test_stage_failure_prevents_any_config_write(self):
        self.profile_config('manual = "preserve"\n')
        before = (self.profile / "config.toml").read_bytes()
        with self.fail_atomic_for(COMMON.PENDING_FILE), self.assertRaises(OSError):
            self.apply()
        self.assertEqual((self.profile / "config.toml").read_bytes(), before)
        self.assertFalse((self.profile / COMMON.STATE_FILE).exists())
        self.assertFalse((self.profile / COMMON.PENDING_FILE).exists())

    def test_stage_refuses_config_change_since_prepare(self):
        plan = COMMON.prepare(self.profile, BASE, source_home=self.source)
        self.profile_config('manual = "new change"\n')
        with self.assertRaisesRegex(COMMON.CommonSettingsError, "changed after preparing"):
            COMMON.stage(self.profile, plan)
        self.assertFalse((self.profile / COMMON.PENDING_FILE).exists())
        self.assertEqual(tomllib.loads((self.profile / "config.toml").read_text()), {"manual": "new change"})

    def test_journal_never_contains_remote_mcp_values(self):
        self.source_config('[mcp_servers.shared]\ncommand = "remote-command"\n'
                           '[mcp_servers.shared.env]\nTOKEN = "synthetic-secret"\n')
        plan = COMMON.prepare(self.profile, BASE, source_home=self.source)
        COMMON.stage(self.profile, plan)
        journal = (self.profile / COMMON.PENDING_FILE).read_text(encoding="utf-8")
        self.assertNotIn("synthetic-secret", journal)
        self.assertNotIn("remote-command", journal)
        self.assertNotIn("gpt-test", journal)

    def test_corrupt_pending_journal_never_falls_back_to_legacy_adoption(self):
        self.profile_config('manual = "keep"\n')
        (self.profile / COMMON.PENDING_FILE).write_text('{"schema": 1, "bad": true}')
        before = {item.name: item.read_bytes() for item in self.profile.iterdir()}
        with self.assertRaisesRegex(COMMON.CommonSettingsError, "transaction journal is invalid"):
            COMMON.apply(self.profile, BASE, source_home=self.source,
                previous_generated_sha256=hashlib.sha256((self.profile / "config.toml").read_bytes()).hexdigest())
        self.assertEqual({item.name: item.read_bytes() for item in self.profile.iterdir()}, before)

    def test_commit_requires_staging_even_when_config_was_written(self):
        plan = COMMON.prepare(self.profile, BASE, source_home=self.source)
        (self.profile / "config.toml").write_bytes(plan.config.encode("utf-8"))
        with self.assertRaisesRegex(COMMON.CommonSettingsError, "not staged"):
            COMMON.commit(self.profile, plan)
        self.assertFalse((self.profile / COMMON.STATE_FILE).exists())

    def test_generated_mcp_and_invalid_source_toml_are_rejected_without_secret_errors(self):
        with self.assertRaises(COMMON.CommonSettingsError):
            self.apply(BASE + '[mcp_servers.windows]\ncommand = "C:\\\\bad.exe"\n')
        self.source_config('[mcp_servers.broken]\nTOKEN = "synthetic-secret"\nbad = [')
        with self.assertRaises(COMMON.CommonSettingsError) as failure:
            self.apply()
        self.assertNotIn("synthetic-secret", str(failure.exception))
        self.assertEqual(list(self.profile.iterdir()), [])

    def test_toml_round_trip_keeps_complex_remote_values_and_quoted_server_names(self):
        source = ('[mcp_servers."with.dot and 공백"]\n'
                  'command = "remote-command"\n'
                  'args = ["line\\nnext", "quote\\\"", "back\\\\slash"]\n'
                  'metadata = [{name = "one", enabled = true}, {name = "two", ratio = 1.5}]\n'
                  'created = 2026-09-13T12:34:56+09:00\n'
                  '[mcp_servers."with.dot and 공백".env]\n'
                  'TOKEN = "synthetic-secret"\n')
        self.source_config(source)
        output = self.apply()
        self.assertEqual(output["mcp_servers"], tomllib.loads(source)["mcp_servers"])
        plan = COMMON.prepare(self.profile, BASE, source_home=self.source)
        self.assertNotIn("synthetic-secret", repr(plan))
        self.assertNotIn("synthetic-secret", json.dumps(plan.summary))

    def test_private_atomic_files_request_owner_only_permissions(self):
        with patch.object(COMMON.os, "chmod", wraps=os.chmod) as chmod:
            self.apply()
        modes = [call.args[1] for call in chmod.call_args_list]
        self.assertGreaterEqual(modes.count(0o600), 2)
        self.assertIn(0o700, modes)
        self.assertFalse(list(self.profile.glob(".private-common-*")))

    def test_config_symlink_is_never_overwritten(self):
        protected = self.root / "protected.toml"
        protected.write_text('keep = "manual"\n')
        self.symlink(self.profile / "config.toml", protected)
        with self.assertRaises(COMMON.CommonSettingsError):
            self.apply()
        self.assertEqual(protected.read_text(), 'keep = "manual"\n')

    def test_source_config_symlink_is_not_followed(self):
        protected = self.root / "private-auth-file"
        protected.write_text('mcp_servers = "should not be read"')
        self.symlink(self.source / "config.toml", protected)
        with self.assertRaises(COMMON.CommonSettingsError):
            self.apply()
        self.assertEqual(list(self.profile.iterdir()), [])

    def test_skills_share_remote_directory_and_live_changes_without_copy(self):
        skills = self.source / "skills"
        skills.mkdir()
        try:
            result = COMMON.reconcile_skills(self.profile, source_home=self.source)
        except OSError:
            self.skipTest("This test requires local symlink support.")
        self.assertEqual(result, {"skills": "shared"})
        self.assertTrue((self.profile / "skills").is_symlink())
        (skills / "new-skill.md").write_text("remote shared skill")
        self.assertEqual((self.profile / "skills/new-skill.md").read_text(), "remote shared skill")
        self.assertEqual(COMMON.reconcile_skills(self.profile, source_home=self.source), {"skills": "shared"})

    def test_skills_conflicting_directory_is_preserved(self):
        (self.source / "skills").mkdir()
        (self.profile / "skills").mkdir()
        (self.profile / "skills/manual.txt").write_text("keep")
        self.assertEqual(COMMON.reconcile_skills(self.profile, source_home=self.source), {"skills": "preserved"})
        self.assertEqual((self.profile / "skills/manual.txt").read_text(), "keep")

    def test_skills_foreign_symlink_is_preserved(self):
        foreign = self.root / "manual-skills"
        foreign.mkdir()
        (self.source / "skills").mkdir()
        self.symlink(self.profile / "skills", foreign, directory=True)
        self.assertEqual(COMMON.reconcile_skills(self.profile, source_home=self.source), {"skills": "preserved"})
        self.assertEqual((self.profile / "skills").resolve(), foreign.resolve())

    def test_missing_common_skills_does_not_create_a_dangling_link(self):
        self.assertEqual(COMMON.reconcile_skills(self.profile, source_home=self.source), {"skills": "unavailable"})
        self.assertFalse((self.profile / "skills").exists())


if __name__ == "__main__":
    unittest.main()
