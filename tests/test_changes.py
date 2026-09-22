import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

import test_integration as fixture
from src import changes
from src.config import Settings, SyncError, write_json
from src.engine import run
from src.service import Manager, render_service, render_timer, render_watcher
from src.watcher import EVENT, Q_OVERFLOW, InotifyTree, Observer


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="localdrive-changes-")
        self.root = Path(self.temp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        self.cfg = Settings(local_dir=str(self.local), remote="fixture:",
                            state_dir=str(self.root / "state"), rclone_binary="/usr/bin/true",
                            rclone_config=str(self.root / "rclone.conf"), backup_dir=str(self.root / "backups"))
        self.cfg.state.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def observer(self):
        observer = Observer(self.cfg, self.root / "config.json")
        observer.rebuild()
        observer.not_before = 0
        self.assertIsNotNone(observer.tree)
        self.addCleanup(lambda: observer.tree.close() if observer.tree else None)
        return observer

    def test_inotify_idle_never_enumerates_files(self):
        (self.local / "nested").mkdir()
        (self.local / "nested/file").write_text("first")
        observer = self.observer()
        initial = changes.local_observation(self.cfg)
        self.assertEqual(initial["kind"], "inotify")
        with patch("src.changes.os.scandir", side_effect=AssertionError("idle scan")):
            self.assertEqual(changes.local_observation(self.cfg), initial)
        (self.local / "nested/file").write_text("second")
        observer.tick()
        self.assertNotEqual(changes.local_observation(self.cfg), initial)

    def test_create_move_delete_and_new_nested_watches(self):
        observer = self.observer()
        initial = changes.local_observation(self.cfg)
        folder = self.local / "new/deep"
        folder.mkdir(parents=True)
        observer.tick()
        self.assertNotEqual(changes.local_observation(self.cfg), initial)
        initial = changes.local_observation(self.cfg)
        (folder / "file").write_text("new data")
        observer.tick()
        self.assertNotEqual(changes.local_observation(self.cfg), initial)
        (self.local / "new").rename(self.local / "renamed")
        observer.tick()
        initial = changes.local_observation(self.cfg)
        (self.local / "renamed/deep/file").unlink()
        observer.tick()
        self.assertNotEqual(changes.local_observation(self.cfg), initial)

    def test_watch_restarts_and_overflow_cannot_reuse_clean_observation(self):
        observer = self.observer()
        before = changes.local_observation(self.cfg)
        event = EVENT.pack(-1, Q_OVERFLOW, 0, 0)
        with patch("src.watcher.os.read", side_effect=[event, BlockingIOError()]):
            self.assertEqual(observer.tree.drain(), (True, True))
        observer.rebuild()
        self.assertNotEqual(changes.local_observation(self.cfg), before)
        observer.tree.close()
        replacement = self.observer()
        self.assertNotEqual(changes.local_observation(self.cfg)["session"], before["session"])
        self.assertTrue(replacement.tree)

    def test_stale_unhealthy_or_dead_observer_uses_stat_fallback(self):
        observer = self.observer()
        path = self.cfg.state / "watch-status.json"
        data = changes.read_json(path)
        for replacement in ({"heartbeat": time.monotonic() - 40}, {"healthy": False},
                            {"pid": 99999999}, {"process": "previous process"}):
            with self.subTest(replacement=replacement):
                write_json(path, {**data, **replacement})
                self.assertEqual(changes.local_observation(self.cfg)["kind"], "stat")
        observer.publish(True)

    def test_root_replacement_rebuilds_and_symlink_cannot_be_watched(self):
        observer = self.observer()
        before = changes.local_observation(self.cfg)
        self.local.rename(self.root / "old")
        self.local.mkdir()
        self.assertEqual(changes.local_observation(self.cfg)["kind"], "stat")
        observer.tick()
        self.assertEqual(changes.local_observation(self.cfg)["kind"], "inotify")
        self.assertNotEqual(changes.local_observation(self.cfg), before)
        link = self.root / "link"
        link.symlink_to(self.local)
        with self.assertRaises(OSError):
            InotifyTree(link)

    def test_stat_fallback_detects_preserved_mtime_and_ignores_symlink_targets(self):
        file = self.local / "file"
        file.write_text("first")
        info = file.stat()
        before = changes.local_observation(self.cfg)
        time.sleep(0.02)  # Let the filesystem's ctime clock advance.
        file.write_text("other")
        os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(changes.local_observation(self.cfg), before)
        outside = self.root / "outside"
        outside.mkdir()
        (self.local / "link").symlink_to(outside)
        before = changes.local_observation(self.cfg)
        (outside / "ignored").write_text("outside the connection")
        self.assertEqual(changes.local_observation(self.cfg), before)

    def test_debounce_and_busy_worker_retain_pending_events(self):
        observer = self.observer()
        (self.cfg.state / "initialized").touch()
        now = time.monotonic()
        observer.pending_since, observer.last_change = now, now
        with patch.object(observer, "request_sync", return_value=False) as request:
            observer.tick()
            request.assert_not_called()
            observer.last_change = now - 3
            observer.tick()
            request.assert_called_once()
            self.assertIsNotNone(observer.pending_since)
        observer.last_request = 0
        with patch.object(observer, "request_sync", return_value=True):
            observer.tick()
        self.assertIsNone(observer.pending_since)

    def test_observer_respects_startup_delay(self):
        observer = self.observer()
        (self.cfg.state / "initialized").touch()
        now = time.monotonic()
        observer.pending_since = observer.last_change = now - 5
        observer.not_before = now + 60
        with patch.object(observer, "request_sync", return_value=True) as request:
            observer.tick()
            request.assert_not_called()
            observer.not_before = 0
            observer.tick()
            request.assert_called_once()

    def test_checkpoint_is_before_sync_and_failure_invalidates_it(self):
        remote = {"kind": "listing", "digest": "remote"}
        with patch("src.changes.remote_observation", return_value=(remote, False, "test")):
            checkpoint = changes.check(self.cfg, Mock())
            self.assertTrue(checkpoint["needed"])
            changes.commit(self.cfg, checkpoint, reconciled=True)
            self.assertFalse(changes.check(self.cfg, Mock())["needed"])
            before = changes.check(self.cfg, Mock())
            (self.local / "during-sync").write_text("must not be acknowledged")
            changes.commit(self.cfg, before, reconciled=True)
            self.assertTrue(changes.check(self.cfg, Mock())["needed"])
            changes.invalidate(self.cfg)
            self.assertTrue(changes.check(self.cfg, Mock())["needed"])

    def test_local_edit_does_not_wait_for_remote_probe(self):
        remote = {"kind": "drive", "cursor": "before"}
        with patch("src.changes.remote_observation", return_value=(remote, False, "test")):
            changes.commit(self.cfg, changes.check(self.cfg, Mock()), reconciled=True)
        (self.local / "changed.txt").write_text("changed")
        with patch("src.changes.remote_observation", side_effect=AssertionError("remote probe delays local change")):
            checkpoint = changes.check(self.cfg, Mock())
        self.assertTrue(checkpoint["needed"])
        self.assertEqual(checkpoint["observation"]["remote"], remote)
        self.assertEqual(checkpoint["mode"], "Local changes")

    def test_manual_force_periodic_audit_and_history_edits_bypass_gate(self):
        remote = {"kind": "listing", "digest": "remote"}
        with patch("src.changes.remote_observation", return_value=(remote, False, "test")):
            changes.commit(self.cfg, changes.check(self.cfg, Mock()), reconciled=True)
            path = self.cfg.state / "change-baseline.json"
            baseline = changes.read_json(path)
            write_json(path, {**baseline, "reconciled_at": time.time() - self.cfg.full_scan_interval_seconds - 1})
            self.assertTrue(changes.check(self.cfg, Mock())["needed"])
            write_json(path, baseline)
            (self.cfg.state / "force-sync").write_text("manual request")
            self.assertTrue(changes.check(self.cfg, Mock())["needed"])
            changes.commit(self.cfg, changes.check(self.cfg, Mock()), reconciled=True)
            (self.cfg.state / "initialized").touch()
            self.assertTrue(changes.check(self.cfg, Mock())["needed"])

    def test_malformed_optimization_checkpoint_forces_reconciliation(self):
        with patch("src.changes.remote_observation", return_value=({"kind": "listing"}, False, "test")):
            for value in ("{broken", "[]", json.dumps({"schema": 1, "fingerprint": self.cfg.fingerprint(), "observation": []})):
                (self.cfg.state / "change-baseline.json").write_text(value)
                self.assertTrue(changes.check(self.cfg, Mock())["needed"])

    def test_services_and_manual_force_are_wired(self):
        self.cfg.save(self.root / "config.json")
        manager = Manager(self.root / "config.json", "/usr/bin/true")
        manager.units = self.root / "units"
        with patch("src.service.systemctl") as ctl, patch.object(manager, "require_healthy"):
            manager.sync_now()
        self.assertTrue((self.cfg.state / "force-sync").read_text())
        self.assertIn(("start", "--no-block", "rclone-local-sync.service"), [call.args for call in ctl.call_args_list])
        self.assertIn("--worker --check-changes", render_service(self.cfg, manager.config_path, manager.launcher))
        self.assertNotIn("--check-changes", render_service(self.cfg, manager.config_path, manager.launcher, preview=True))
        self.assertIn("Restart=on-failure", render_watcher(self.cfg, manager.config_path, manager.launcher))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze required")
    def test_generated_units_validate_with_systemd(self):
        launcher = Path(__file__).resolve().parents[1] / "bin/drive-synchronization-daemon-manager"
        config = self.root / "config.json"
        units = {"rclone-local-sync.service": render_service(self.cfg, config, launcher),
                 "rclone-local-sync-preview.service": render_service(self.cfg, config, launcher, preview=True),
                 "rclone-local-sync.timer": render_timer(self.cfg),
                 "rclone-local-sync-watch.service": render_watcher(self.cfg, config, launcher)}
        for name, content in units.items():
            (self.root / name).write_text(content)
        result = subprocess.run(["systemd-analyze", "--user", "verify", *(str(self.root / name) for name in units)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class DriveChangesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings(remote="drive:")
        self.settings = {"type": "drive", "token": json.dumps({"access_token": "test-only", "refresh_token": "test-refresh"})}
        self.runner = Mock()
        self.feed = changes.DriveChanges(self.cfg, self.runner, self.settings)
        self.previous = {"kind": "drive", "scope": self.feed.scope, "cursor": "old"}

    def test_empty_feed_advances_cursor_without_listing(self):
        with patch.object(self.feed, "_request", return_value={"changes": [], "newStartPageToken": "new"}) as request:
            observation, changed = self.feed.observe(self.previous)
        self.assertFalse(changed)
        self.assertEqual(observation["cursor"], "new")
        self.assertEqual(request.call_count, 1)
        self.runner.call.assert_not_called()

    def test_all_pages_and_removals_are_considered(self):
        pages = [{"changes": [], "nextPageToken": "page2"},
                 {"changes": [{"fileId": "deleted", "removed": True}], "newStartPageToken": "new"}]
        with patch.object(self.feed, "_request", side_effect=pages) as request:
            observation, changed = self.feed.observe(self.previous)
        self.assertTrue(changed)
        self.assertEqual(observation["cursor"], "new")
        self.assertEqual(request.call_args_list[1].args[1]["pageToken"], "page2")
        self.assertEqual(request.call_args.args[1]["includeRemoved"], "true")

    def test_missing_or_looping_pages_are_never_clean(self):
        for result in ({"changes": []}, {"newStartPageToken": "new"},
                       {"changes": [], "nextPageToken": "old"}):
            with patch.object(self.feed, "_request", return_value=result), self.assertRaises(changes.ProbeUnavailable):
                self.feed.observe(self.previous)

    def test_new_account_or_scope_needs_a_full_baseline(self):
        self.previous["scope"] = {"drive": "another-account"}
        with patch.object(self.feed, "_request", return_value={"startPageToken": "start"}):
            observation, changed = self.feed.observe(self.previous)
        self.assertTrue(changed)
        self.assertEqual(observation["cursor"], "start")
        self.assertNotIn("test-refresh", json.dumps(observation))
        self.assertNotIn("test-only", json.dumps(observation))

    def test_refreshes_oauth_via_rclone_without_logging_credentials(self):
        error = HTTPError("https://www.googleapis.com/drive/v3/changes", 401, "unauthorized", {}, None)
        with patch("src.changes.urlopen", side_effect=[error, io.BytesIO(b'{"startPageToken":"start"}')]) as fetch, \
             patch("src.changes.remote_settings", return_value=self.settings):
            observation, _ = self.feed.observe(None)
        self.assertEqual(observation["cursor"], "start")
        self.assertEqual(self.runner.call.call_args.args[0][1], "about")
        request = fetch.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).hostname, "www.googleapis.com")
        self.assertNotIn("test-only", request.full_url)

    def test_api_error_falls_back_to_authoritative_listing(self):
        self.runner.call.return_value = '[{"Path":"file","Size":1,"ModTime":"2026-01-01T00:00:00Z"}]'
        with patch("src.changes.remote_settings", return_value=self.settings), \
             patch.object(changes.DriveChanges, "_request", side_effect=changes.ProbeUnavailable("expired cursor")):
            observation, changed, _ = changes.remote_observation(self.cfg, self.runner, self.previous)
        self.assertTrue(changed)
        self.assertEqual(observation["kind"], "listing")
        self.assertIn("--recursive", self.runner.call.call_args.args[0])


@unittest.skipUnless(fixture.supported_binary(), "rclone 1.66+ required")
class ScheduledIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture.EngineIntegrationTests.setUp(self)
        self.assertEqual(run(self.cfg), 0)
        self.assertEqual(run(self.cfg), 0)  # Establish optimization baseline.

    def tearDown(self):
        fixture.EngineIntegrationTests.tearDown(self)

    def test_idle_generic_remote_skips_preflight_and_bisync(self):
        last = (self.cfg.state / "last-success").read_bytes()
        with patch("src.engine.preflight", side_effect=AssertionError("unnecessary preflight")), \
             patch("src.engine.bisync_command", side_effect=AssertionError("unnecessary bisync")):
            self.assertEqual(run(self.cfg, check_changes=True), 0)
        status = changes.read_json(self.cfg.state / "status.json")
        self.assertEqual(status["phase"], "unchanged")
        self.assertEqual((self.cfg.state / "last-success").read_bytes(), last)

    def test_remote_and_local_deletions_still_sync(self):
        (self.remote / "seed-1.txt").unlink()
        (self.local / "seed-2.txt").unlink()
        self.assertEqual(run(self.cfg, check_changes=True), 0)
        self.assertFalse((self.local / "seed-1.txt").exists())
        self.assertFalse((self.remote / "seed-2.txt").exists())

    def test_failed_reconciliation_cannot_be_skipped(self):
        (self.local / "new").write_text("local update")
        with patch("src.engine.preflight", side_effect=SyncError("temporary failure")):
            self.assertEqual(run(self.cfg, check_changes=True), 1)
        self.assertEqual(changes.read_json(self.cfg.state / "change-baseline.json")["schema"], 0)
        self.assertEqual(run(self.cfg, check_changes=True), 0)
        self.assertEqual((self.remote / "new").read_text(), "local update")

    def test_manual_worker_always_reconciles(self):
        from src.engine import preflight
        with patch("src.engine.preflight", wraps=preflight) as checked:
            self.assertEqual(run(self.cfg), 0)
        checked.assert_called_once()

    def test_idle_drive_with_watcher_does_no_recursive_work(self):
        observer = Observer(self.cfg, self.root / "config.json")
        observer.rebuild()
        try:
            settings = {"type": "drive", "token": json.dumps({"access_token": "test-only"})}
            with patch("src.changes.remote_settings", return_value=settings), \
                 patch.object(changes.DriveChanges, "_request", return_value={"startPageToken": "baseline"}):
                self.assertEqual(run(self.cfg), 0)
            observer.tick()
            with patch("src.changes.remote_settings", return_value=settings), \
                 patch.object(changes.DriveChanges, "_request", return_value={"changes": [], "newStartPageToken": "baseline"}), \
                 patch("src.changes.os.scandir", side_effect=AssertionError("unexpected tree scan")), \
                 patch("src.engine.preflight", side_effect=AssertionError("unexpected preflight")), \
                 patch("src.engine.bisync_command", side_effect=AssertionError("unexpected bisync")):
                self.assertEqual(run(self.cfg, check_changes=True), 0)
        finally:
            observer.tree.close()
