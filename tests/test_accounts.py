import os
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.accounts import AccountSetup, cleanup_staging, read_config
from src.config import SyncError, find_rclone
from src.locking import BusyError, credential_lock, operation_lock
from src.rclone import require_rclone

BINARY = find_rclone()
try:
    require_rclone(BINARY)
    SUPPORTED = True
except SyncError:
    SUPPORTED = False


class VersionTests(unittest.TestCase):
    def test_install_rejects_old_rclone_before_copying_files(self):
        path = Path(__file__).resolve().parents[1] / "scripts/package.py"
        spec = importlib.util.spec_from_file_location("localdrive_package", path)
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        with patch.object(package, "require_rclone", side_effect=SyncError("version too old")), \
             patch.object(package, "copy_app") as copy:
            with self.assertRaises(SyncError):
                package.install_user()
        copy.assert_not_called()

    def test_minimum_version_and_invalid_executable(self):
        for version, accepted in (("1.65.9", False), ("1.66.0", True), ("1.75.1", True), ("2.0.0", True)):
            with self.subTest(version=version), patch("src.rclone.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, f"rclone v{version}\n", "")
                if accepted:
                    self.assertEqual(require_rclone("rclone"), f"rclone v{version}")
                else:
                    with self.assertRaises(SyncError):
                        require_rclone("rclone")
        for result in (subprocess.CompletedProcess([], 1, "rclone v1.75.1", ""),
                       subprocess.CompletedProcess([], 0, "unrecognized", "")):
            with patch("src.rclone.subprocess.run", return_value=result), self.assertRaises(SyncError):
                require_rclone("rclone")
        with self.assertRaises(SyncError):
            require_rclone("/no/such/rclone")


@unittest.skipUnless(SUPPORTED, "Account integration tests require rclone 1.66+")
class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="localdrive-accounts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                          "XDG_CONFIG_HOME": str(self.root / "config")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.target = self.root / "rclone.conf"
        self.original = "[existing]\ntype = local\n"
        self.target.write_text(self.original)
        self.setup = AccountSetup(BINARY, self.target)
        self.addCleanup(self.setup.close)

    def ready(self):
        providers = self.setup.open()
        self.assertIn("local", {provider["Name"] for provider in providers})
        result = self.setup.start("new account", "local")
        self.assertEqual(result["Option"]["Name"], "config_fs_advanced")
        self.assertEqual(self.setup.answer("false")["State"], "")

    def test_complete_flow_preserves_existing_accounts_and_private_mode(self):
        self.ready()
        self.assertEqual(self.target.read_text(), self.original)
        self.assertEqual(self.setup.work.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.setup.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.setup.save(), "new account:")
        self.assertIn("[existing]", self.target.read_text())
        self.assertIn("[new account]", self.target.read_text())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.target.parent.glob(".rclone.conf.*")))
        self.setup.close()
        self.assertFalse(self.setup.work.exists())
        self.assertFalse(self.setup.runner.children)

    def test_cancel_discards_partial_account_and_missing_target(self):
        self.target.unlink()
        self.setup.open()
        self.setup.start("cancelled", "local")
        self.setup.close()
        self.assertFalse(self.target.exists())
        self.assertFalse(self.setup.work.exists())

    def test_existing_account_cannot_be_overwritten(self):
        self.setup.open()
        for name in ("existing", "EXISTING"):
            with self.assertRaises(SyncError):
                self.setup.start(name, "local")
        self.assertEqual(self.target.read_text(), self.original)

    def test_external_changes_are_not_overwritten(self):
        self.ready()
        changed = self.original + "\n[external]\ntype = local\n"
        self.target.write_text(changed)
        with self.assertRaisesRegex(SyncError, "changed during setup"):
            self.setup.save()
        self.assertEqual(self.target.read_text(), changed)

    def test_save_cannot_race_worker_or_credential_refresh(self):
        self.ready()
        for lock in (operation_lock(), credential_lock(self.target, shared=True)):
            with lock, self.assertRaises(BusyError):
                self.setup.save()
        self.assertEqual(self.target.read_text(), self.original)
        self.setup.save()

    def test_incomplete_setup_cannot_be_saved(self):
        self.setup.open()
        with self.assertRaises(SyncError):
            self.setup.save()
        self.setup.start("unfinished", "local")
        with self.assertRaises(SyncError):
            self.setup.save()
        self.assertEqual(self.target.read_text(), self.original)

    def test_atomic_write_failure_leaves_original_and_no_temporary_file(self):
        self.ready()
        with patch("src.config.os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.setup.save()
        self.assertEqual(self.target.read_text(), self.original)
        self.assertFalse(list(self.target.parent.glob(".rclone.conf.*")))

    def test_single_wizard_and_cleanup_after_interruption(self):
        self.setup.work.mkdir(parents=True)
        (self.setup.work / "abandoned").write_text("unfinished")
        self.setup.open()
        self.assertFalse((self.setup.work / "abandoned").exists())
        other = AccountSetup(BINARY, self.target)
        with self.assertRaises(BusyError):
            other.open()
        other.close()
        cleanup_staging()
        self.assertTrue(self.setup.path.exists())
        self.setup.close()
        self.setup.work.mkdir()
        (self.setup.work / "abandoned").touch()
        cleanup_staging()
        self.assertFalse(self.setup.work.exists())

    def test_symlinks_and_special_files_are_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.target)
        with self.assertRaises(SyncError):
            read_config(link)
        self.target.unlink()
        with self.assertRaises(SyncError):
            read_config(link)
        pipe = self.root / "fifo"
        os.mkfifo(pipe)
        with self.assertRaises(SyncError):
            read_config(pipe)

    def test_staging_cannot_be_selected_as_the_real_config(self):
        self.setup.work.mkdir(parents=True)
        self.setup.path.write_text(self.original)
        with self.assertRaises(SyncError):
            AccountSetup(BINARY, self.setup.path)
        self.assertEqual(self.setup.path.read_text(), self.original)
        cleanup_staging()

    def test_cancel_reaps_a_waiting_child_before_cleaning_staging(self):
        self.setup.open()
        spawned = threading.Event()
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            spawned.set()
            return child
        errors = []
        def work():
            try:
                with self.setup.guard:
                    self.setup.runner.call(["/usr/bin/sleep", "60"], private=True)
            except SyncError as error:
                errors.append(error)
        with patch("src.engine.subprocess.Popen", side_effect=launch):
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(spawned.wait(3))
            self.setup.close()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.setup.runner.children)
        self.assertFalse(self.setup.work.exists())
        self.assertEqual(len(errors), 1)
