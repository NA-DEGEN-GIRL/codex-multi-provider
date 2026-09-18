"""Fixture tests only: no network, real package updates, or real app closures."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager_core import updates


def package(version="26.904.1.0", **extra):
    result = {
        "name": updates.PACKAGE_NAME, "family": updates.PACKAGE_FAMILY,
        "publisher": updates.PACKAGE_PUBLISHER, "architecture": "x64",
        "version": version, "signature_kind": "Store", "package_status": "Ok",
        "install_location": r"C:\Program Files\WindowsApps\fixture",
    }
    result.update(extra)
    return result


def zip_bytes(identity=None):
    identity = identity or package()
    stream = io.BytesIO()
    attrs = {key: identity[name] for key, name in (
        ("Name", "name"), ("Publisher", "publisher"), ("Version", "version"),
        ("ProcessorArchitecture", "architecture"))}
    xml = '<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"><Identity '
    xml += " ".join(f'{k}="{v}"' for k, v in attrs.items()) + "/></Package>"
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("AppxManifest.xml", xml)
    return stream.getvalue()


class FakeRange(io.BytesIO):
    data = zip_bytes()

    def __init__(self, _url):
        super().__init__(self.data)
        self.size = len(self.data)


class UpdateFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installed = package("26.903.9818.0")
        self.latest = package("26.904.1.0")
        self.latest.update(url=updates.OFFICIAL_BASE + "ChatGPT-x64.msix")
        self.instances = [{
            "profile_id": "alpha", "process_id": 100, "job_state": "idle",
            "idle_verified": True, "thread_id": "thread-alpha", "host_id": "local",
            "source_store_id": "store-alpha",
            "created_at": "2026-09-13T01:00:00Z", "executable": r"C:\Program Files\WindowsApps\fixture\app\ChatGPT.exe",
        }]
        self.live = [{"process_id": 100, "parent_process_id": 0,
                      "created_at": self.instances[0]["created_at"], "executable": self.instances[0]["executable"]}]
        self.closed, self.restored, self.installs = [], [], []
        self.manager = updates.UpdateManager(
            self.root,
            snapshot_instances=lambda: copy.deepcopy(self.instances),
            close_instance=self.close_instance,
            restore_instance=self.restore_instance,
            verify_compatibility=lambda _p: {"compatible": True},
            inventory=lambda: copy.deepcopy(self.installed),
            processes=lambda _p: copy.deepcopy(self.live),
            acquire_maintenance=lambda _, **kwargs: {"verified": True, "token": "test-only"},
            release_maintenance=lambda _: None,
        )
        self.set_check()

    def set_check(self):
        self.manager._check_cache = {
            "status": "available", "message": "Available",
            "installed": copy.deepcopy(self.installed), "latest": copy.deepcopy(self.latest),
        }
        self.manager._cache_time = time.time()

    def close_instance(self, instance):
        self.closed.append(instance["profile_id"])
        self.live = [p for p in self.live if p["process_id"] != instance["process_id"]]
        self.instances = [i for i in self.instances if i["profile_id"] != instance["profile_id"]]
        return True

    def restore_instance(self, entry):
        self.restored.append(entry)
        return {"verified": True}

    def fake_download(self, latest):
        path = self.root / "fixture.msix"
        path.write_bytes(zip_bytes(latest))
        return path, updates._sha256(path)

    def install(self, _path, _digest):
        self.installs.append(True)
        self.installed = copy.deepcopy(self.latest)

    def patched_execution(self):
        self.download_patch = patch.object(self.manager, "_download", self.fake_download)
        self.signature_patch = patch.object(self.manager, "_validate_download",
                                             lambda *args: {"signature": {"status": "Valid"}})
        self.install_patch = patch.object(self.manager, "_install", self.install)
        for mock in (self.download_patch, self.signature_patch, self.install_patch):
            mock.start()
            self.addCleanup(mock.stop)

    def test_numeric_version_and_malformed_values(self):
        self.assertGreater(updates.version_tuple("26.10.0.0"), updates.version_tuple("26.9.999.0"))
        for value in ["1.2", "1.2.3.4.5", "1.2.3.-4", "1.2.3.65536", None]:
            with self.assertRaises(updates.UpdateError):
                updates.version_tuple(value)

    def test_no_available_update_is_successful_noop_even_without_explicit_check_button(self):
        for status in ('installed_newer', 'up_to_date'):
            self.manager._check_cache.update(status=status, message='Already current')
            plan = self.manager.plan(self.instances)
            self.assertEqual(plan['status'], status)
            self.assertEqual(plan['blockers'], [])
            self.assertEqual(self.manager.apply(plan)['status'], status)
        self.assertFalse(self.closed or self.restored or self.installs)

    def test_private_desktop_process_is_included_in_update_identity_inventory(self):
        private = self.root / 'artifacts/managed-desktop/version'
        private.mkdir(parents=True)
        (private/'manager-desktop.json').write_text('{}')
        executable = private/'ChatGPT.exe';executable.touch()
        own = dict(process_id=20,executable=str(executable))
        foreign = dict(process_id=30,executable=str(self.root/'elsewhere/ChatGPT.exe'))
        with patch.object(updates,'_powershell',return_value=json.dumps([own,foreign])):
            self.assertEqual(updates.package_processes(self.installed,private.parent), [own])

    def test_package_identity_architecture_signature_and_status(self):
        updates.validate_identity(self.installed)
        for changes in [
            {"name": "Other.Codex"}, {"family": "OpenAI.Codex_evil"},
            {"publisher": "CN=Other"}, {"architecture": "x86"},
            {"signature_kind": "Developer"}, {"package_status": "Modified"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(updates.UpdateError):
                updates.validate_identity(package(**changes))
        with self.assertRaises(updates.UpdateError):
            updates.validate_identity(package(architecture="arm64"), self.installed)

    def test_manifest_reads_exact_identity(self):
        self.assertEqual(updates.package_manifest(io.BytesIO(zip_bytes()))["name"], updates.PACKAGE_NAME)
        with self.assertRaises(updates.UpdateError):
            updates.package_manifest(io.BytesIO(b"not a zip"))

    def test_source_allows_only_exact_official_https_packages(self):
        self.assertEqual(updates._official_url(self.latest["url"]), self.latest["url"])
        for url in [
            "http://persistent.oaistatic.com/codex-app-prod/ChatGPT-x64.msix",
            "https://evil.test/codex-app-prod/ChatGPT-x64.msix",
            self.latest["url"] + "?token=secret", self.latest["url"] + "#fragment",
            "https://user:password@persistent.oaistatic.com/codex-app-prod/ChatGPT-x64.msix",
            "https://persistent.oaistatic.com/other/ChatGPT-x64.msix",
        ]:
            with self.subTest(url=url), self.assertRaises(updates.UpdateError):
                updates._official_url(url)

    def test_check_latest_and_installed_newer_do_not_download_or_install(self):
        with patch.object(updates, "HttpRangeReader", FakeRange):
            result = self.manager.check()
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["latest"]["signature"], "not_yet_checked")
            self.installed["version"] = "26.999.1.0"
            self.assertEqual(self.manager.check()["status"], "installed_newer")
        self.assertFalse(self.closed or self.installs)
        self.assertFalse(self.manager.directory.exists())

    def test_check_rejects_different_official_manifest_identity(self):
        class WrongRange(FakeRange):
            data = zip_bytes(package(name="Other.App"))
        with patch.object(updates, "HttpRangeReader", WrongRange):
            result = self.manager.check()
            self.assertEqual(result["status"], "check_failed")
            self.assertEqual(result["code"], "identity_mismatch")

    def test_active_and_unknown_jobs_are_blocked(self):
        for state, verified in [("running", True), ("unknown", False), ("idle", False), (None, True)]:
            self.instances[0]["job_state"] = state
            self.instances[0]["idle_verified"] = verified
            result = self.manager.plan(self.instances)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("jobs_not_quiescent", [b["code"] for b in result["blockers"]])
        self.assertFalse(self.closed or self.installs)

    def test_unmanaged_original_app_is_blocked_even_if_managed_idle(self):
        self.live.append({"process_id": 200, "parent_process_id": 0})
        result = self.manager.plan(self.instances)
        self.assertIn("unmanaged_instance", [b["code"] for b in result["blockers"]])

    def test_closed_windows_do_not_skip_active_ssh_or_its_restore_manifest(self):
        self.live = []
        self.instances = [dict(profile_id='remote-fixture', process_id=None,
                               remote_only=True, remote_maintenance_required=True,
                               idle_verified=False, job_state='active')]
        self.assertEqual(self.manager.plan(self.instances)['status'], 'blocked')
        self.instances[0].update(idle_verified=True, job_state='idle')
        plan = self.manager.plan(self.instances)
        self.assertEqual(plan['status'], 'ready')
        self.assertTrue(plan['restore_manifest'][0]['remote_only'])
        self.patched_execution()
        result = self.manager.apply(plan['plan_id'])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.closed, ['remote-fixture'])
        self.assertTrue(self.restored[0]['remote_only'])

    def test_managed_electron_descendants_are_not_unmanaged(self):
        self.live.extend([
            {"process_id": 102, "parent_process_id": 101},
            {"process_id": 101, "parent_process_id": 100},
        ])
        self.assertEqual(self.manager.plan(self.instances)["status"], "ready")

    def test_unknown_process_inventory_fails_closed(self):
        self.manager.processes = lambda _: (_ for _ in ()).throw(OSError())
        self.assertEqual(self.manager.plan(self.instances)["blockers"][0]["code"], "process_inventory_unknown")

    def test_runtime_compatibility_must_be_explicit(self):
        self.manager.verify_compatibility = None
        result = self.manager.plan(self.instances)
        self.assertIn("runtime_compatibility_unverified", [b["code"] for b in result["blockers"]])

    def test_missing_restart_hooks_block_before_any_close(self):
        self.manager.restore_instance = None
        result = self.manager.plan(self.instances)
        self.assertIn("restart_hooks_unavailable", [b["code"] for b in result["blockers"]])

    def test_plan_strips_secrets_and_retains_offline_remote_pending(self):
        self.instances[0]["environment"] = {"KEY": "never-persist-this"}
        self.instances[0]["api_key"] = "never-persist-this"
        self.manager.remote_snapshot = lambda: [{"host_id": "ssh-alpha", "online": False}]
        plan = self.manager.plan(self.instances)
        contents = (self.manager.directory / "plans" / (plan["plan_id"] + ".json")).read_text("utf-8")
        self.assertNotIn("never-persist-this", contents)
        self.assertEqual(plan["remote_pending"][0]["host_id"], "ssh-alpha")
        self.assertEqual(plan["restore_manifest"][0]["thread_id"], "thread-alpha")

    def test_client_tampered_plan_fields_are_ignored(self):
        self.patched_execution()
        plan = self.manager.plan(self.instances)
        plan["latest"] = {"url": "https://evil.test/malicious.exe"}
        plan["restore_manifest"] = []
        result = self.manager.apply(plan)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(self.restored[0]["thread_id"], "thread-alpha")

    def test_apply_installs_shared_package_once_and_restores_only_open_profiles(self):
        self.instances.append({"profile_id": "closed", "process_id": None})
        self.patched_execution()
        plan = self.manager.plan(self.instances)
        result = self.manager.apply(plan)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(self.closed, ["alpha"])
        self.assertEqual([p["profile_id"] for p in self.restored], ["alpha"])
        self.assertEqual(self.manager.apply(plan)["status"], "blocked")

    def test_new_active_job_after_plan_prevents_download_and_close(self):
        self.patched_execution()
        plan = self.manager.plan(self.instances)
        self.instances[0]["job_state"] = "running"
        self.assertEqual(self.manager.apply(plan)["status"], "blocked")
        self.assertFalse(self.closed or self.installs)
        self.assertFalse((self.root / "fixture.msix").exists())

    def test_new_job_during_download_prevents_closure(self):
        self.patched_execution()
        original = self.fake_download
        def start_job(latest):
            result = original(latest)
            self.instances[0]["job_state"] = "running"
            return result
        with patch.object(self.manager, "_download", start_job):
            result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.closed or self.installs)

    def test_new_idle_profile_during_download_prevents_closure(self):
        self.patched_execution()
        original = self.fake_download
        def open_profile(latest):
            result = original(latest)
            self.instances.append({"profile_id": "beta", "process_id": 200,
                                   "job_state": "idle", "idle_verified": True,
                                   "created_at": "new", "executable": "fixture"})
            self.live.append({"process_id": 200, "parent_process_id": 0,
                              "created_at": "new", "executable": "fixture"})
            return result
        with patch.object(self.manager, "_download", open_profile):
            result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.closed or self.installs)

    def test_signature_failure_never_closes_or_installs(self):
        self.patched_execution()
        with patch.object(self.manager, "_validate_download",
                          side_effect=updates.UpdateError("untrusted_signature", "Invalid signature")):
            result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["status"], "failed_before_install")
        self.assertFalse(self.closed or self.installs)

    def test_graceful_close_failure_never_forces_install(self):
        self.patched_execution()
        self.manager.close_instance = lambda _: False
        result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["code"], "graceful_close_failed")
        self.assertFalse(self.installs)

    def test_install_failure_observes_reality_and_restores_old_profiles(self):
        self.patched_execution()
        with patch.object(self.manager, "_install", side_effect=OSError("failed fixture install")):
            result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["status"], "failed_install")
        self.assertEqual(result["installed_after"]["version"], "26.903.9818.0")
        self.assertEqual(len(self.restored), 0)
        self.assertTrue(result["recovery_required"])
        self.manager.verify_recovery = lambda *_: {"installer_settled": True}
        self.assertFalse(self.manager.recover()["recovery_required"])
        self.assertEqual(len(self.restored), 1)

    def test_install_error_after_commit_observes_new_version_without_reinstall(self):
        self.patched_execution()
        def committed_then_failed(path, digest):
            self.install(path, digest)
            raise OSError("lost response")
        with patch.object(self.manager, "_install", committed_then_failed):
            result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["status"], "failed_install")
        self.assertEqual(result["installed_after"]["version"], self.latest["version"])
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(len(self.restored), 0)
        self.assertTrue(result["recovery_required"])
        self.manager.verify_recovery = lambda *_: {"installer_settled": True}
        self.assertFalse(self.manager.recover()["recovery_required"])
        self.assertEqual(len(self.restored), 1)

    def test_success_exit_without_actual_upgrade_is_not_success(self):
        self.patched_execution()
        with patch.object(self.manager, "_install", lambda *_: None):
            result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["status"], "failed_install")
        self.assertEqual(result["code"], "install_not_verified")

    def test_restore_failure_is_reported_separately_from_install_success(self):
        self.patched_execution()
        self.manager.restore_instance = lambda _: {"verified": False}
        result = self.manager.apply(self.manager.plan(self.instances))
        self.assertEqual(result["status"], "failed_restore")
        self.assertEqual(result["installed_after"]["version"], self.latest["version"])
        self.assertEqual(result["restore_failures"][0]["profile_id"], "alpha")

    def test_expired_and_unknown_plan_cannot_apply(self):
        plan = self.manager.plan(self.instances)
        plan_file = self.manager.directory / "plans" / (plan["plan_id"] + ".json")
        plan["expires_at"] = 0
        updates._atomic_json(plan_file, plan)
        self.assertEqual(self.manager.apply(plan)["status"], "blocked")
        with self.assertRaises(updates.UpdateError):
            self.manager.apply({"plan_id": "../../elsewhere"})

    def test_stale_apply_lock_requires_recovery_and_never_retries(self):
        plan = self.manager.plan(self.instances)
        (self.manager.directory / "apply.lock").write_text("0", encoding="ascii")
        updates._atomic_json(self.manager.directory / "transaction.json", {"status": "installing"})
        self.assertEqual(self.manager.apply(plan)["status"], "recovery_required")
        status = self.manager.recovery_status()
        self.assertTrue(status["lock_present"])
        self.assertFalse(status["install_retried"])
        self.assertFalse(self.closed or self.installs)

    def test_reused_pid_or_missing_process_birth_is_never_managed(self):
        for birth in [None, "different-process-birth"]:
            self.instances[0]["created_at"] = birth
            result = self.manager.plan(self.instances)
            codes = {b["code"] for b in result["blockers"]}
            self.assertIn("process_identity_unverified", codes)
            self.assertIn("unmanaged_instance", codes)

    def test_maintenance_barrier_is_required_and_held_while_closing(self):
        self.patched_execution()
        self.manager.acquire_maintenance = None
        self.assertIn("maintenance_barrier_unavailable", {
            b["code"] for b in self.manager.plan(self.instances)["blockers"]})
        lease_active = []
        self.manager.acquire_maintenance = lambda _, **kwargs: lease_active.append(True) or {"verified": True}
        self.manager.release_maintenance = lambda _: lease_active.pop()
        def close_with_barrier(instance):
            self.assertTrue(lease_active)
            return self.close_instance(instance)
        self.manager.close_instance = close_with_barrier
        self.assertEqual(self.manager.apply(self.manager.plan(self.instances))["status"], "complete")
        self.assertFalse(lease_active)

    def test_final_view_state_is_saved_after_download(self):
        self.patched_execution()
        original = self.fake_download
        def switch_view(latest):
            result = original(latest)
            self.instances[0]["thread_id"] = "newly-selected-thread"
            return result
        with patch.object(self.manager, "_download", switch_view):
            self.assertEqual(self.manager.apply(self.manager.plan(self.instances))["status"], "complete")
        self.assertEqual(self.restored[0]["thread_id"], "newly-selected-thread")

    def test_unresolved_restore_blocks_new_transaction(self):
        self.patched_execution()
        plan = self.manager.plan(self.instances)
        updates._atomic_json(self.manager.directory / "transaction.json", {
            "status": "failed_restore", "closed_profiles": ["alpha"], "restored_profiles": [],
            "transaction_id": "old-transaction"})
        self.assertEqual(self.manager.apply(plan)["status"], "recovery_required")
        self.assertEqual(self.manager.status()["transaction_id"], "old-transaction")
        self.assertFalse(self.closed or self.installs)

    def test_crash_after_close_intent_is_reconciled_without_install_retry(self):
        intent = {k: self.instances[0][k] for k in ("profile_id", "process_id", "created_at", "executable")}
        intent["state"] = "requested"
        transaction = {
            "status": "closing_instances", "installed_before": self.installed, "target": self.latest,
            "close_intents": [intent], "closed_profiles": [],
            "restore_manifest": [{"profile_id": "alpha", "thread_id": "thread-alpha"}],
        }
        updates._atomic_json(self.manager.directory / "transaction.json", transaction)
        self.live = []
        result = self.manager.recover()
        self.assertFalse(result["install_retried"])
        self.assertEqual(len(self.restored), 1)
        self.assertFalse(result["recovery_required"])
        self.assertFalse(self.installs)

    def test_crash_before_close_does_not_duplicate_still_running_profile(self):
        intent = {k: self.instances[0][k] for k in ("profile_id", "process_id", "created_at", "executable")}
        intent["state"] = "requested"
        transaction = {
            "status": "closing_instances", "installed_before": self.installed, "target": self.latest,
            "close_intents": [intent], "closed_profiles": [],
            "restore_manifest": [{"profile_id": "alpha", "thread_id": "thread-alpha"}],
        }
        updates._atomic_json(self.manager.directory / "transaction.json", transaction)
        self.assertEqual(self.manager.recover()["status"], "recovery_required")
        self.manager.verify_recovery = lambda *_: {"cancelled_close_profiles": ["alpha"]}
        self.assertFalse(self.manager.recover()["recovery_required"])
        self.assertFalse(self.restored or self.installs)

    def test_uncertain_installer_does_not_restore_until_settled_proof(self):
        self.patched_execution()
        with patch.object(self.manager, "_install", side_effect=TimeoutError()):
            self.manager.apply(self.manager.plan(self.instances))
        result = self.manager.recover()
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["install_outcome"], "unknown")
        self.assertFalse(self.restored)
        self.manager.verify_recovery = lambda *_: {"installer_settled": True}
        self.assertFalse(self.manager.recover()["recovery_required"])
        self.assertEqual(len(self.restored), 1)

    def test_crash_lease_requires_supervisor_reconciliation(self):
        transaction = {
            "status": "preparing", "installed_before": self.installed, "target": self.latest,
            "close_intents": [], "closed_profiles": [], "restore_manifest": [],
            "maintenance_state": "held",
        }
        updates._atomic_json(self.manager.directory / "transaction.json", transaction)
        self.assertEqual(self.manager.recover()["status"], "recovery_required")
        self.manager.verify_recovery = lambda *_: {"maintenance_released": True}
        self.assertFalse(self.manager.recover()["recovery_required"])

    def test_recovery_status_is_read_only_even_with_recovery_hook(self):
        updates._atomic_json(self.manager.directory / "transaction.json", {
            "status": "installing", "install_outcome": "unknown"})
        before = (self.manager.directory / "transaction.json").read_bytes()
        self.manager.verify_recovery = lambda *_: (_ for _ in ()).throw(AssertionError("read called hook"))
        self.assertTrue(self.manager.recovery_status()["recovery_required"])
        self.assertEqual(before, (self.manager.directory / "transaction.json").read_bytes())

    def test_prepare_can_download_without_closure_and_blocks_bad_signature(self):
        with patch.object(self.manager, "_download", self.fake_download), patch.object(
                updates, "_powershell", return_value=json.dumps({"status": "NotSigned", "signer": ""})):
            result = self.manager.prepare()
        self.assertEqual(result["status"], "prepare_failed")
        self.assertFalse(self.closed or self.installs)

    def test_download_hash_and_manifest_are_validated_before_signature(self):
        path, digest = self.fake_download(self.latest)
        with self.assertRaises(updates.UpdateError) as caught:
            self.manager._validate_download(path, "0" * 64, self.installed, self.latest)
        self.assertEqual(caught.exception.code, "package_changed")
        path.write_bytes(zip_bytes(package(name="Evil.App")))
        with self.assertRaises(updates.UpdateError) as caught:
            self.manager._validate_download(path, updates._sha256(path), self.installed, self.latest)
        self.assertEqual(caught.exception.code, "identity_mismatch")


class RangeTests(unittest.TestCase):
    class Response(io.BytesIO):
        def __init__(self, content, status, headers):
            super().__init__(content)
            self.status, self.headers = status, headers

    def test_ignored_range_is_rejected_without_reading_package(self):
        responses = [
            self.Response(b"", 200, {"Content-Length": "100", "ETag": "one"}),
            self.Response(b"x" * 100, 200, {"Content-Length": "100"}),
        ]
        with patch.object(updates, "_request", side_effect=responses):
            reader = updates.HttpRangeReader(updates.OFFICIAL_BASE + "ChatGPT-x64.msix")
            with self.assertRaises(updates.UpdateError) as caught:
                reader.read(10)
        self.assertEqual(caught.exception.code, "range_unavailable")

    def test_range_checks_etag_bounds_and_exact_content_range(self):
        head = self.Response(b"", 200, {"Content-Length": "100", "ETag": "one"})
        body = self.Response(b"abcdefghij", 206, {"Content-Range": "bytes 90-99/100"})
        with patch.object(updates, "_request", side_effect=[head, body]) as request:
            reader = updates.HttpRangeReader(updates.OFFICIAL_BASE + "ChatGPT-x64.msix")
            reader.seek(-10, io.SEEK_END)
            self.assertEqual(reader.read(), b"abcdefghij")
            headers = request.call_args.args[0].headers
            self.assertEqual(headers["If-match"], "one")
            self.assertEqual(headers["Range"], "bytes=90-99")
            self.assertEqual(reader.read(), b"")

    def test_metadata_budget_prevents_whole_archive_reads(self):
        head = self.Response(b"", 200, {"Content-Length": str(updates.MAX_METADATA_BYTES + 1)})
        with patch.object(updates, "_request", return_value=head):
            reader = updates.HttpRangeReader(updates.OFFICIAL_BASE + "ChatGPT-x64.msix")
            with self.assertRaises(updates.UpdateError) as caught:
                reader.read()
        self.assertEqual(caught.exception.code, "metadata_limit")


if __name__ == "__main__":
    unittest.main()
