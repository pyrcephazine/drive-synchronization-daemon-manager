import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from src.config import Settings, SyncError, atomic_write
from src.engine import BusyError, RcloneError, Runner, bisync_command, filter_text, native_folder, preflight, run_lock, size_conflicts
from src.service import Manager, desktop_quote, render_service, render_timer, unit_quote


def fixture(root):
    return Settings(local_dir=str(root / "local"), remote="fixture:remote", rclone_binary="/usr/bin/true",
                    rclone_config=str(root / "rclone.conf"), state_dir=str(root / "state"),
                    backup_dir=str(root / "backups"), min_free_bytes=0)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="localdrive-unit-")
        self.root = Path(self.temp.name)
        self.cfg = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_and_private_permissions(self):
        path = self.root / "config.json"
        self.cfg.save(path)
        self.assertEqual(Settings.load(path), self.cfg)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_defaults_match_local_first_policy(self):
        self.assertEqual(self.cfg.max_size_bytes, 2_000_000_000)
        self.assertEqual(self.cfg.interval_seconds, 30)
        self.assertTrue(self.cfg.start_at_login)
        self.assertTrue(self.cfg.schedule_enabled)
        self.assertTrue(self.cfg.tray_at_login)
        self.assertTrue(self.cfg.close_to_tray)

    def test_bad_remote_and_paths(self):
        for remote in ("", "/tmp/remote", "--delete-excluded", "google:../outside", "g:\nunsafe"):
            with self.subTest(remote=remote):
                self.cfg.remote = remote
                with self.assertRaises(SyncError):
                    self.cfg.validate(check_paths=False)
        self.cfg.remote = "fixture:ok"
        for folder in (str(Path.home()), "/", "/home", "relative"):
            self.cfg.local_dir = folder
            with self.assertRaises(SyncError):
                self.cfg.validate(check_paths=False)

    def test_paths_with_spaces_and_remote_subfolder(self):
        self.cfg.local_dir = str(self.root / "My files")
        self.cfg.remote = "My cloud:Documents/Work space"
        self.cfg.validate(check_paths=False)

    def test_no_state_or_backup_overlap(self):
        self.cfg.backup_dir = self.cfg.local_dir + "/backups"
        with self.assertRaises(SyncError):
            self.cfg.validate(check_paths=False)
        self.cfg.backup_dir = str(self.root)
        with self.assertRaises(SyncError):
            self.cfg.validate(check_paths=False)

    def test_invalid_typed_options(self):
        for key, value in (("interval_seconds", 0), ("retries", True), ("schedule_enabled", "false"),
                           ("drive_chunk_mib", 3), ("conflict_resolve", "delete"),
                           ("excludes", ["*.tmp\n+ **"]), ("google_docs", "docx"), ("max_size_bytes", 100)):
            with self.subTest(key=key):
                cfg = fixture(self.root)
                setattr(cfg, key, value)
                with self.assertRaises(SyncError):
                    cfg.validate(check_paths=False)

    def test_identity_is_stable_for_performance_not_filters(self):
        first = self.cfg.fingerprint()
        self.cfg.transfers = 2
        self.cfg.schedule_enabled = False
        self.assertEqual(first, self.cfg.fingerprint())
        self.cfg.max_size_bytes += 1
        self.assertNotEqual(first, self.cfg.fingerprint())

    def test_unsupported_config_is_not_overwritten(self):
        path = self.root / "config.json"
        atomic_write(path, '{"schema": 99}')
        with self.assertRaises(SyncError):
            Settings.load(path)
        self.assertEqual(path.read_text(), '{"schema": 99}')

    def test_lock_excludes_manual_and_scheduled_workers(self):
        with run_lock(self.cfg):
            with self.assertRaises(BusyError):
                with run_lock(self.cfg):
                    pass
        with run_lock(self.cfg):
            pass

    def test_different_connections_share_one_worker_lock(self):
        second = fixture(self.root / "second")
        with run_lock(self.cfg), self.assertRaises(BusyError):
            with run_lock(second):
                pass
        with run_lock(second):
            pass

    def test_symlink_root_rejected(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(SyncError):
            native_folder(link)

    def test_fuse_subfolder_rejected(self):
        folder = self.root / "mounted/child"
        folder.mkdir(parents=True)
        listing = f"1 0 0:1 / / rw - ext4 /dev/test rw\n2 1 0:2 / {folder.parent} rw - fuse.rclone rclone rw\n"
        with patch.object(Path, "read_text", return_value=listing):
            with self.assertRaises(SyncError):
                native_folder(folder)


class SizeGuardTests(unittest.TestCase):
    @staticmethod
    def file(path, size):
        return {"Path": path, "Size": size}

    def test_inclusive_boundary(self):
        entry = self.file("same", 2_000_000_000)
        self.assertEqual(size_conflicts([entry], [entry], 2_000_000_000), [])

    def test_transition_on_either_side_stops(self):
        small, big = self.file("same", 100), self.file("same", 2_000_000_001)
        self.assertEqual(size_conflicts([small], [big], 2_000_000_000), ["same"])
        self.assertEqual(size_conflicts([big], [small], 2_000_000_000), ["same"])

    def test_unpaired_large_files_are_allowed(self):
        self.assertEqual(size_conflicts([self.file("local", 3000)], [self.file("remote", 4000)], 2000), [])

    def test_duplicates_and_unicode_aliases_are_rejected(self):
        with self.assertRaises(SyncError):
            size_conflicts([self.file("é", 10), self.file("e\u0301", 10)], [], 2000)
        with self.assertRaises(SyncError):
            size_conflicts([], [self.file("dup", 10), self.file("dup", 20)], 0)

    def test_unknown_google_doc_size(self):
        self.assertEqual(size_conflicts([self.file("doc.url", 100)], [self.file("doc.url", -1)], 2000), [])

    def test_unlimited(self):
        self.assertEqual(size_conflicts([self.file("same", 9999999)], [self.file("same", 1)], 0), [])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.cfg = fixture(Path("/tmp/localdrive-command-tests"))

    def test_no_automatic_resync_and_no_destructive_overrides(self):
        args = bisync_command(self.cfg)
        self.assertFalse(any("resync" in arg for arg in args))
        self.assertNotIn("--delete-excluded", args)
        self.assertNotIn("--force", args)
        self.assertNotIn("--ignore-errors", args)
        self.assertIn("--drive-use-trash=true", args)
        self.assertIn("--conflict-loser=num", args)
        self.assertIn("--max-size=2000000000B", args)
        self.assertIn("--ask-password=false", args)

    def test_safe_initialization_and_preview(self):
        initial = bisync_command(self.cfg, initialize=True)
        self.assertIn("--resync-mode=path2", initial)
        self.assertIn("--track-renames=false", initial)
        self.assertNotIn("--track-renames", initial)
        self.assertIn("--dry-run", bisync_command(self.cfg, preview=True))

    def test_bandwidth_and_filters_are_wired(self):
        self.cfg.upload_kib, self.cfg.download_kib = 500, 2000
        self.cfg.excludes = ["*.tmp", "/cache/**"]
        args = bisync_command(self.cfg)
        self.assertIn("--bwlimit=500K:2000K", args)
        self.assertIn(f"--filters-file={self.cfg.state / 'filters.txt'}", args)
        self.assertEqual(filter_text(self.cfg).splitlines()[0], "+ /" + self.cfg.health_file)

    def test_systemd_quoting_and_no_shell(self):
        text = unit_quote('/tmp/A space/%name/$literal/"quoted"')
        self.assertIn("%%name", text)
        self.assertIn("$$literal", text)
        self.assertIn('\\"quoted\\"', text)
        unit = render_service(self.cfg, "/tmp/config with space.json", "/tmp/a.py")
        self.assertIn("ExecStart=/usr/bin/python3", unit)
        self.assertNotIn("/bin/sh", unit)
        self.assertIn("KillSignal=SIGINT", unit)
        self.assertIn("TimeoutStartSec=infinity", unit)

    def test_interval_is_after_previous_completion(self):
        self.cfg.interval_seconds = 120
        self.assertIn("OnUnitInactiveSec=120s", render_timer(self.cfg))
        self.assertIn("WantedBy=timers.target", render_timer(self.cfg))

    def test_runner_preserves_exit_code_and_diagnostic(self):
        with self.assertRaises(RcloneError) as caught:
            Runner().call([sys.executable, "-c", "import sys; sys.stderr.write('directory not found'); sys.exit(3)"])
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("directory not found", str(caught.exception))

    def test_health_errors_distinguish_not_found_from_other_failures(self):
        with tempfile.TemporaryDirectory(prefix="localdrive-health-") as directory:
            cfg = fixture(Path(directory))
            Path(cfg.local_dir).mkdir()
            (Path(cfg.local_dir) / cfg.health_file).write_text(cfg.health_content)
            for code, detail in ((3, "directory not found"), (4, "object not found"), (5, "connection failed")):
                with self.subTest(code=code):
                    runner = Mock()
                    runner.call.side_effect = RcloneError("cat", code, detail)
                    with self.assertRaises(SyncError) as caught:
                        preflight(cfg, runner)
                    message = str(caught.exception)
                    self.assertIn("remote sync health marker", message)
                    self.assertIn(cfg.health_file, message)
                    self.assertIn(detail, message)
                    self.assertEqual("restore the original" in message, code in (3, 4))
                    # No listing, repair, or bisync should follow a failed marker check.
                    self.assertEqual(runner.call.call_count, 1)

    def test_stop_during_process_creation_does_not_orphan_child(self):
        runner = Runner()
        original = subprocess.Popen
        def interrupted_spawn(*args, **kwargs):
            runner.interrupt()
            return original(*args, **kwargs)
        with patch("src.engine.subprocess.Popen", side_effect=interrupted_spawn):
            with self.assertRaises(SyncError) as caught:
                runner.call([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertNotIn("took too long to respond", str(caught.exception))
        self.assertFalse(runner.children)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="localdrive-manager-")
        self.root = Path(self.temp.name)
        self.cfg = fixture(self.root)
        Path(self.cfg.rclone_config).write_text("[fixture]\ntype = local\n")
        self.path = self.root / "config.json"
        self.manager = Manager(self.path, "/usr/bin/true")
        self.manager.units = self.root / "units"
        self.manager.autostart = self.root / "autostart/indicator.desktop"
        version_check = patch("src.installation.require_rclone")
        version_check.start()
        self.addCleanup(version_check.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_setup_writes_service_and_enables_background(self):
        props = {"ActiveState": "inactive", "LoadState": "not-found"}
        with patch.object(self.manager, "properties", return_value=props), \
             patch("src.service.check_rclone"), patch("src.service.systemctl") as ctl:
            self.manager.apply(self.cfg)
        self.assertEqual(Settings.load(self.path), self.cfg)
        self.assertTrue((self.cfg.state / "setup-approved.json").exists())
        self.assertTrue((self.cfg.state / "identity.json").exists())
        self.assertTrue(self.manager.autostart.exists())
        self.assertEqual((self.manager.units / "timers.target.wants/rclone-local-sync.timer").resolve(),
                         self.manager.units / "rclone-local-sync.timer")
        self.assertIn(("start", "--no-block", "rclone-local-sync.service"), [c.args for c in ctl.call_args_list])

    def test_nonempty_new_folder_is_never_adopted(self):
        folder = Path(self.cfg.local_dir)
        folder.mkdir()
        (folder / "keep.txt").write_text("precious")
        with patch("src.service.check_rclone"):
            with self.assertRaises(SyncError):
                self.manager.apply(self.cfg)
        self.assertEqual((folder / "keep.txt").read_text(), "precious")
        self.assertFalse(self.path.exists())

    def test_cannot_change_view_and_reuse_history(self):
        self.cfg.save(self.path)
        changed = Settings.load(self.path)
        changed.max_size_bytes = 500
        with patch("src.service.check_rclone"):
            with self.assertRaises(SyncError):
                self.manager.apply(changed)
        self.assertEqual(Settings.load(self.path).max_size_bytes, 2_000_000_000)

    def test_active_worker_blocks_settings(self):
        with patch.object(self.manager, "properties", return_value={"ActiveState": "activating"}), \
             patch("src.service.check_rclone"):
            with self.assertRaises(BusyError):
                self.manager.apply(self.cfg)
        self.assertFalse(self.path.exists())

    def test_unmanaged_unit_is_not_overwritten(self):
        self.manager.units.mkdir()
        unit = self.manager.units / "rclone-local-sync.service"
        unit.write_text("[Service]\nExecStart=/usr/bin/true\n")
        with patch.object(self.manager, "properties", return_value={"ActiveState": "inactive", "LoadState": "not-found"}), \
             patch("src.service.check_rclone"), patch("src.service.systemctl"):
            with self.assertRaises(SyncError):
                self.manager.apply(self.cfg)
        self.assertEqual(unit.read_text(), "[Service]\nExecStart=/usr/bin/true\n")
        self.assertFalse(self.path.exists())

    def test_brand_comment_alone_does_not_prove_ownership(self):
        self.manager.units.mkdir()
        unit = self.manager.units / "rclone-local-sync.service"
        old = "# Managed by Local Drive Sync. Change settings in the application.\n[Service]\nExecStart=/usr/bin/true\n"
        unit.write_text(old)
        with patch.object(self.manager, "properties", return_value={"ActiveState": "inactive", "LoadState": "loaded"}), \
             patch("src.service.check_rclone"), patch("src.service.systemctl"):
            with self.assertRaises(SyncError):
                self.manager.apply(self.cfg)
        self.assertEqual(unit.read_text(), old)

    def test_daemon_reload_failure_restores_configuration(self):
        self.cfg.save(self.path)
        before = self.path.read_text()
        changed = Settings.load(self.path)
        changed.interval_seconds = 60
        def fail_reload(*args, **kwargs):
            if args == ("daemon-reload",) and kwargs.get("check", True):
                raise SyncError("simulated reload failure")
        with patch.object(self.manager, "properties", return_value={"ActiveState": "inactive", "LoadState": "not-found"}), \
             patch("src.service.check_rclone"), patch("src.service.systemctl", side_effect=fail_reload):
            with self.assertRaises(SyncError):
                self.manager.apply(changed)
        self.assertEqual(self.path.read_text(), before)
        self.assertFalse((self.manager.units / "rclone-local-sync.service").exists())
        self.assertFalse((self.cfg.state / "identity.json").exists())
        self.assertFalse((self.cfg.state / "setup-approved.json").exists())
        self.assertFalse(self.manager.owner_path.exists())
        self.assertFalse(self.manager.autostart.exists())

    def test_stale_settings_window_cannot_overwrite_changes(self):
        self.cfg.save(self.path)
        expected = self.path.read_text()
        changed = Settings.load(self.path)
        changed.interval_seconds = 60
        changed.save(self.path)
        with self.assertRaisesRegex(SyncError, "Settings changed"):
            self.manager.apply(self.cfg, expected=expected)
        self.assertEqual(Settings.load(self.path).interval_seconds, 60)

    def test_second_settings_file_cannot_replace_active_services(self):
        props = {"ActiveState": "inactive", "LoadState": "not-found"}
        with patch.object(self.manager, "properties", return_value=props), \
             patch("src.service.check_rclone"), patch("src.service.systemctl"):
            self.manager.apply(self.cfg)
        other = Manager(self.root / "other.json", "/usr/bin/true")
        other.units = self.manager.units
        with self.assertRaisesRegex(SyncError, "Another settings file"):
            other.apply(self.cfg)
        self.assertFalse(other.config_path.exists())

    def test_replacement_retires_old_worker_and_preserves_history(self):
        props = {"ActiveState": "inactive", "LoadState": "not-found"}
        with patch.object(self.manager, "properties", return_value=props), \
             patch("src.service.check_rclone"), patch("src.service.systemctl"):
            self.manager.apply(self.cfg)
            next_cfg = fixture(self.root / "next")
            Path(next_cfg.rclone_config).parent.mkdir()
            Path(next_cfg.rclone_config).write_text("[fixture]\ntype = local\n")
            self.manager.apply(next_cfg)
        self.assertEqual(Settings.load(self.path), next_cfg)
        self.assertTrue((self.cfg.state / "identity.json").exists())
        with self.assertRaisesRegex(SyncError, "replaced"):
            with run_lock(self.cfg):
                pass
        with run_lock(next_cfg):
            pass

    def test_activation_failure_keeps_committed_replacement(self):
        props = {"ActiveState": "inactive", "LoadState": "not-found"}
        with patch.object(self.manager, "properties", return_value=props), \
             patch("src.service.check_rclone"), patch("src.service.systemctl"):
            self.manager.apply(self.cfg)
            before = self.path.read_text()
            startup = self.manager.autostart.read_text()
            next_cfg = fixture(self.root / "next")
            Path(next_cfg.rclone_config).parent.mkdir()
            Path(next_cfg.rclone_config).write_text("[fixture]\ntype = local\n")
            with patch.object(self.manager.installation, "activate", side_effect=SyncError("schedule failed")):
                with self.assertRaises(SyncError):
                    self.manager.apply(next_cfg)
        self.assertEqual(Settings.load(self.path), next_cfg)
        self.assertTrue((self.cfg.state / "retired").exists())
        self.assertTrue((next_cfg.state / "setup-approved.json").exists())
        self.assertEqual(json.loads(self.manager.installation.journal_path.read_text())["phase"], "committed")

    def test_pause_does_not_stop_current_job(self):
        self.cfg.save(self.path)
        self.manager.units.mkdir()
        (self.manager.units / "rclone-local-sync-watch.service").touch()
        with patch("src.service.systemctl") as ctl:
            self.manager.pause()
        self.assertIn(("stop", "rclone-local-sync.timer"), [c.args for c in ctl.call_args_list])
        self.assertNotIn(("stop", "rclone-local-sync.service"), [c.args for c in ctl.call_args_list])
        self.assertFalse(Settings.load(self.path).schedule_enabled)

    def test_old_installation_can_pause_without_a_watcher_unit(self):
        self.cfg.save(self.path)
        with patch("src.service.systemctl") as ctl:
            self.manager.pause()
        self.assertIn(("stop", "rclone-local-sync.timer"), [c.args for c in ctl.call_args_list])
        self.assertFalse(Settings.load(self.path).schedule_enabled)

    def test_failure_is_red_not_false_green(self):
        self.cfg.state.mkdir()
        (self.cfg.state / "initialized").touch()
        def props(unit):
            return {"ActiveState": "active"} if unit.endswith("timer") else {"ActiveState": "failed", "Result": "exit-code"}
        with patch.object(self.manager, "properties", side_effect=props):
            self.assertEqual(self.manager.status(self.cfg, health={"state": "healthy"})["phase"], "error")

    def test_active_and_waiting_are_distinct(self):
        self.cfg.state.mkdir()
        (self.cfg.state / "initialized").touch()
        def props(unit):
            return {"ActiveState": "active"} if unit.endswith("timer") else {"ActiveState": "inactive", "Result": "success"}
        with patch.object(self.manager, "properties", side_effect=props):
            self.assertEqual(self.manager.status(self.cfg, health={"state": "healthy"})["phase"], "ready")


if __name__ == "__main__":
    unittest.main()
