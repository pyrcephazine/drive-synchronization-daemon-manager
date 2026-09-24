from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.config import SERVICE, TIMER, WATCH_SERVICE, Settings, SyncError, write_json
from src.installation import digest, json_file, read_record, restore, snapshot
from src.locking import BusyError, operation_lock
from src.service import Manager
from test_core import fixture


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="installation-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = fixture(self.root)
        Path(self.cfg.rclone_config).write_text("[fixture]\ntype = local\n")
        self.manager = Manager(self.root / "config.json", "/usr/bin/true")
        self.manager.units = self.root / "systemd/user"
        self.manager.autostart = self.root / "autostart/app.desktop"
        self.install = self.manager.installation
        self.active = set()
        self.overrides = {}
        self.calls = []
        for target, kwargs in (("src.service.check_rclone", {}),
                               ("src.installation.require_rclone", {}),
                               ("src.service.systemctl", {"side_effect": self.ctl})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.manager, "properties", side_effect=self.properties)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager.apply(self.cfg)
        self.assertEqual(self.install.inspect()["state"], "healthy")

    def properties(self, unit):
        path = self.manager.units / unit
        result = {"ActiveState": "active" if unit in self.active else "inactive",
                  "LoadState": "loaded" if path.exists() else "not-found",
                  "FragmentPath": str(path) if path.exists() else "",
                  "Result": "success", "NeedDaemonReload": "no"}
        result.update(self.overrides.get(unit, {}))
        return result

    def ctl(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] in ("start", "stop"):
            # Oneshot workers complete immediately in this fake manager.
            for unit in args[1:]:
                if args[0] == "start" and unit in (TIMER, WATCH_SERVICE):
                    self.active.add(unit)
                elif args[0] == "stop":
                    self.active.discard(unit)
        if args[0] == "daemon-reload":
            for value in self.overrides.values():
                value.pop("NeedDaemonReload", None)
        return subprocess.CompletedProcess(args, 0, "", "")

    def test_missing_owned_files_repair_and_keep_schedule(self):
        for path in self.manager.generated_files(self.cfg):
            with self.subTest(path=path.name):
                expected = snapshot(path)
                path.unlink()
                self.assertEqual(self.install.inspect()["state"], "repairable")
                self.assertEqual(self.manager.maintain()["state"], "healthy")
                self.assertEqual(snapshot(path), expected)
                self.assertIn(TIMER, self.active)
        self.assertFalse(self.install.journal_path.exists())

    def test_pause_survives_repair(self):
        self.manager.pause()
        (self.manager.units / SERVICE).unlink()
        self.assertEqual(self.manager.maintain()["state"], "healthy")
        self.assertFalse(Settings.load(self.manager.config_path).schedule_enabled)
        self.assertFalse(self.active)
        self.assertFalse(any(path.is_symlink() for path in self.install.links().values()))

    def test_external_disable_requires_review(self):
        link = self.install.links()[TIMER]
        link.unlink()
        self.active.discard(TIMER)
        report = self.manager.maintain()
        self.assertEqual(report["state"], "review")
        self.assertFalse(link.exists())
        self.manager.maintain(retry=True, reviewed_token=report["token"])
        self.assertTrue(link.exists())
        self.assertNotIn(TIMER, self.active)  # Explicit repair does not imply Resume.

    def test_modified_masked_and_override_are_not_automatically_replaced(self):
        path = self.manager.units / SERVICE
        for value in ({"kind": "link", "target": "/dev/null"},
                      {"kind": "file", "text": "external edit", "mode": 0o644}):
            restore(path, value)
            self.assertEqual(self.manager.maintain()["state"], "review")
            self.assertEqual(snapshot(path), value)
        self.overrides[SERVICE] = {"DropInPaths": "/tmp/external.conf"}
        report = self.install.inspect()
        with self.assertRaises(SyncError):
            self.manager.maintain(retry=True, reviewed_token=report["token"])

    def test_review_token_detects_later_edits(self):
        path = self.manager.units / SERVICE
        path.write_text("one")
        report = self.install.inspect()
        path.write_text("two")
        with self.assertRaisesRegex(SyncError, "changed"):
            self.manager.maintain(retry=True, reviewed_token=report["token"])
        self.assertEqual(path.read_text(), "two")

    def test_missing_settings_or_history_are_never_recreated(self):
        self.manager.config_path.unlink()
        self.assertEqual(self.manager.maintain()["state"], "review")
        self.assertFalse(self.manager.config_path.exists())
        self.cfg.save(self.manager.config_path)
        (self.cfg.state / "identity.json").unlink()
        self.assertEqual(self.manager.maintain()["state"], "review")
        self.assertFalse((self.cfg.state / "identity.json").exists())

    def test_established_history_requires_both_listings_and_health_marker(self):
        (self.cfg.state / "initialized").touch()
        (self.cfg.state / "baseline-established").touch()
        (self.cfg.state / "bisync").mkdir()
        Path(self.cfg.local_dir).mkdir()
        marker = Path(self.cfg.local_dir) / self.cfg.health_file
        marker.write_text(self.cfg.health_content)
        for side in ("path1", "path2"):
            self.assertEqual(self.install.inspect()["state"], "review")
            (self.cfg.state / "bisync" / f"test.{side}.lst").write_text("# bisync listing v1 from test\n")
        self.assertEqual(self.install.inspect()["state"], "healthy")
        marker.unlink()
        self.assertEqual(self.manager.maintain()["state"], "review")
        self.assertFalse(marker.exists())

    def test_exact_old_installation_can_register_ownership(self):
        self.install.manifest_path.unlink()
        self.manager.owner_path.write_text(json.dumps({"config": str(self.manager.config_path)}))
        self.assertEqual(self.manager.maintain()["state"], "healthy")
        self.assertTrue(self.install.manifest_path.exists())

    def test_automatic_history_repair_keeps_external_pause(self):
        from src import history
        (self.cfg.state / "initialized").touch()
        (self.cfg.state / "baseline-established").touch()
        work = self.cfg.state / "bisync"
        work.mkdir()
        Path(self.cfg.local_dir).mkdir()
        (Path(self.cfg.local_dir) / self.cfg.health_file).write_text(self.cfg.health_content)
        for side in ("path1", "path2"):
            (work / f"test.{side}.lst-old").write_text("# bisync listing v1 from test\n")
        report = self.install.inspect()
        self.assertEqual(report["state"], "repairable")
        self.assertEqual(report["issues"][0]["code"], "history")
        self.active.clear()
        with patch("src.engine.Runner") as runner:
            runner.return_value.call.side_effect = SyncError("Remote temporarily unavailable")
            self.assertEqual(self.manager.maintain()["state"], "repairable")
        self.assertFalse(list(work.glob("*.lst")))
        with patch("src.engine.Runner") as runner:
            result = self.manager.maintain()
            self.assertIn("--check-sync=only", runner.return_value.call.call_args.args[0])
        self.assertEqual(result["state"], "healthy")
        self.assertFalse(self.active)
        self.assertEqual(history.status(self.cfg), "ready")
        self.assertEqual(json.loads((self.cfg.state / "change-baseline.json").read_text()), {"schema": 0})

    def test_reload_stale_systemd_metadata(self):
        self.overrides[SERVICE] = {"NeedDaemonReload": "yes"}
        self.assertEqual(self.manager.maintain()["state"], "healthy")
        self.assertIn(("daemon-reload",), self.calls)

    def test_one_attempt_then_explicit_retry(self):
        (self.manager.units / SERVICE).unlink()
        with patch.object(self.install, "repair", side_effect=SyncError("disk full")) as repair:
            self.manager.maintain()
            self.manager.maintain()
            self.assertEqual(repair.call_count, 1)
        self.assertEqual(self.manager.maintain(retry=True)["state"], "healthy")

    def test_busy_repair_is_deferred_without_attempt(self):
        (self.manager.units / SERVICE).unlink()
        self.active.add(SERVICE)
        with self.assertRaises(BusyError):
            self.manager.maintain()
        self.assertFalse((self.install.root / "repair-attempt.json").exists())
        self.active.remove(SERVICE)
        self.assertEqual(self.manager.maintain()["state"], "healthy")

    def test_real_process_death_at_each_file_write_rolls_back(self):
        # os._exit bypasses Python exception handling, like killing the process.
        old = self.manager.config_path.read_text()
        changed = Settings.load(self.manager.config_path)
        changed.interval_seconds += 60
        changes = self.install.artifacts(changed)
        changes[self.manager.config_path] = json_file(asdict(changed))
        changes[self.install.manifest_path] = json_file(self.install.manifest(changed, self.install.artifacts(changed)))
        for cut in range(1, len(changes) + 1):
            pid = os.fork()
            if pid == 0:
                import src.installation as module
                original = module.restore
                def kill_after_write(path, value):
                    original(path, value)
                    kill_after_write.count += 1
                    if kill_after_write.count == cut:
                        os._exit(91)
                kill_after_write.count = 0
                module.restore = kill_after_write
                self.install.transact(changes, changed, self.cfg)
                os._exit(92)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 91)
            self.assertTrue(self.install.recover())
            self.assertFalse(self.install.recover())
            self.assertEqual(self.manager.config_path.read_text(), old)
            self.assertEqual(self.install.inspect()["state"], "healthy")

    def test_commit_failure_rolls_forward(self):
        changed = Settings.load(self.manager.config_path)
        changed.interval_seconds += 60
        with patch.object(self.install, "activate", side_effect=SyncError("activation failed")):
            with self.assertRaises(SyncError):
                self.manager.apply(changed)
        self.assertEqual(read_record(self.install.journal_path)["phase"], "committed")
        self.assertTrue(self.install.recover())
        self.assertEqual(Settings.load(self.manager.config_path), changed)
        self.assertEqual(self.install.inspect()["state"], "healthy")

    def test_recovery_refuses_external_edit_after_crash(self):
        changed = Settings.load(self.manager.config_path)
        changed.interval_seconds += 60
        with patch.object(self.install, "activate", side_effect=SyncError("activation failed")):
            with self.assertRaises(SyncError):
                self.manager.apply(changed)
        self.manager.config_path.write_text("external edit")
        with self.assertRaisesRegex(SyncError, "changed after"):
            self.install.recover()
        self.assertEqual(self.manager.config_path.read_text(), "external edit")
        self.assertTrue(self.install.journal_path.exists())

    def test_cleanup_archives_only_selected_files_and_links(self):
        legacy = self.manager.units / "rclone-old.service"
        legacy.write_text("legacy")
        unknown = self.manager.units / "unrelated.service"
        unknown.write_text("keep")
        link = self.manager.units / "default.target.wants" / legacy.name
        link.symlink_to(legacy)
        candidates = self.install.cleanup_candidates()
        self.assertEqual(len(candidates), 1)
        self.manager.cleanup(candidates)
        self.assertFalse(legacy.exists())
        self.assertFalse(link.is_symlink())
        self.assertEqual(unknown.read_text(), "keep")
        archives = list((self.install.root / "archive").glob("*/transaction.json"))
        self.assertTrue(any('legacy' in p.read_text() for p in archives))

    def test_cleanup_rechecks_selection_and_active_workers(self):
        path = self.manager.units / "rclone-old.service"
        path.write_text("legacy")
        selected = self.install.cleanup_candidates()
        path.write_text("changed")
        with self.assertRaises(SyncError):
            self.manager.cleanup(selected)
        selected = self.install.cleanup_candidates()
        self.active.add(path.name)
        with self.assertRaises(BusyError):
            self.manager.cleanup(selected)
        self.assertTrue(path.exists())

    def test_removal_preserves_data_and_cannot_be_auto_repaired(self):
        account = Path(self.cfg.rclone_config).read_text()
        self.manager.remove_connection()
        self.assertFalse(self.manager.config_path.exists())
        self.assertEqual(self.manager.maintain()["state"], "removed")
        self.assertEqual(Path(self.cfg.rclone_config).read_text(), account)
        self.assertTrue((self.cfg.state / "identity.json").exists())
        self.assertTrue((self.cfg.state / "retired").exists())
        self.assertFalse(self.active)
        self.assertFalse(any((self.manager.units / u).exists() for u in (SERVICE, TIMER, WATCH_SERVICE)))

    def test_removal_interruption_completes_instead_of_resurrecting(self):
        import src.installation as module
        original = module.restore
        class Crash(BaseException):
            pass
        def crash(path, value):
            original(path, value)
            if Path(path) == self.install.removed_path:
                raise Crash()
        with patch.object(module, "restore", side_effect=crash):
            with self.assertRaises(Crash):
                self.manager.remove_connection()
        self.install.recover()
        self.assertEqual(self.install.inspect()["state"], "removed")
        self.assertFalse(self.manager.config_path.exists())

    def test_cleanup_stops_selected_timer_and_preserves_other_schedules(self):
        path = self.manager.units / "rclone-old.timer"
        path.write_text("old timer")
        self.active.add(path.name)
        self.manager.cleanup(self.install.cleanup_candidates())
        self.assertNotIn(path.name, self.active)
        self.assertIn(TIMER, self.active)
        self.assertFalse(path.exists())

    def test_removal_with_missing_settings_fails_without_deleting_services(self):
        self.manager.config_path.unlink()
        with self.assertRaisesRegex(SyncError, "settings are missing"):
            self.manager.remove_connection()
        self.assertTrue((self.manager.units / SERVICE).exists())
        self.assertFalse(self.install.removed_path.exists())

    def test_remove_keeps_modified_owned_file_for_review(self):
        path = self.manager.units / SERVICE
        path.write_text("external edit")
        self.assertIn(str(path), self.manager.remove_connection())
        self.assertEqual(path.read_text(), "external edit")
        self.assertEqual(self.manager.maintain()["state"], "removed")
        self.assertIn(str(path), self.manager.remove_connection())
        self.manager.cleanup(self.install.cleanup_candidates())
        self.assertEqual(self.manager.remove_connection(), [])

    def test_uninstall_preserves_unlisted_and_modified_files(self):
        from src.uninstall import uninstall_user
        data = self.root / "data"
        app = data / "rclone-local-sync/app"
        app.mkdir(parents=True)
        owned = app / "owned.py"
        modified = app / "modified.py"
        unlisted = app / "user.txt"
        for path in (owned, modified, unlisted):
            path.write_text("original")
        write_json(app / "installed-files.json", {
            "version": 1, "files": {str(p): digest(snapshot(p)) for p in (owned, modified)}})
        modified.write_text("user edit")
        with patch("src.uninstall.data_root", return_value=data), patch("src.service.command"):
            leftovers = uninstall_user(self.manager)
        self.assertEqual(leftovers, [str(modified)])
        self.assertFalse(owned.exists())
        self.assertEqual(modified.read_text(), "user edit")
        self.assertEqual(unlisted.read_text(), "original")
        self.assertTrue(Path(self.cfg.rclone_config).exists())

    def test_uninstall_does_not_strand_modified_service(self):
        from src.uninstall import uninstall_user
        data = self.root / "data"
        app = data / "rclone-local-sync/app"
        app.mkdir(parents=True)
        owned = app / "owned.py"
        owned.write_text("original")
        write_json(app / "installed-files.json", {"version": 1, "files": {str(owned): digest(snapshot(owned))}})
        service = self.manager.units / SERVICE
        service.write_text("modified service")
        with patch("src.uninstall.data_root", return_value=data), patch("src.service.command") as command:
            self.assertIn(str(service), uninstall_user(self.manager))
            command.assert_not_called()
        self.assertTrue(owned.exists())

    def test_malformed_journal_fails_closed(self):
        write_json(self.install.journal_path, {"version": 1, "config": str(self.manager.config_path)})
        with self.assertRaises(SyncError):
            self.install.recover()
        self.assertTrue(self.manager.config_path.exists())

    def test_deleting_lock_file_does_not_allow_second_worker(self):
        from src.config import config_root
        with operation_lock():
            (config_root() / "rclone-local-sync/operation.lock").unlink()
            result = subprocess.run([sys.executable, "-c", "from src.locking import operation_lock, BusyError\ntry:\n with operation_lock(): pass\nexcept BusyError: raise SystemExit(75)"],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 75, result.stderr)
