import os
import json
from pathlib import Path
import time
import threading
import unittest
from unittest.mock import patch

from src.config import Settings, SyncError
from src.conflicts import allowed, inventory_conflicts, resolve, scan, reported_snapshot
from src.engine import Runner, run
import test_integration as integration


class ClassificationTests(unittest.TestCase):
    def test_duplicate_normalized_names_and_folder_collisions(self):
        local = [{"Path": "café", "Size": 2}, {"Path": "cafe\u0301", "Size": 3},
                 {"Path": "folder", "Size": 0, "IsDir": True}]
        remote = [{"Path": "folder", "Size": 2}]
        rows = inventory_conflicts(local, remote, 5000, "health")
        self.assertEqual(rows["café"]["kind"], "Duplicate name")
        self.assertEqual(rows["folder"]["kind"], "File / folder conflict")
        for row in rows.values():
            self.assertFalse(allowed(Settings(), row, "local"))
            self.assertFalse(allowed(Settings(), row, "remote"))

    def test_unsafe_paths_and_native_docs_cannot_be_replaced(self):
        for path in ("../escape", "/absolute", "x/../escape", "health", "file.url", "./file", "x//file"):
            row = {"kind": "Both versions changed", "local": [{"Path": path, "Size": 1}],
                   "remote": [{"Path": path, "Size": 2}]}
            self.assertFalse(allowed(Settings(health_file="health"), row, "local"), path)
        item = {"Path": "doc", "Size": 1, "MimeType": "application/vnd.google-apps.document"}
        self.assertFalse(allowed(Settings(), {"kind": "Both versions changed", "local": [item], "remote": [item]}, "remote"))


@unittest.skipUnless(integration.supported_binary(), "Requires rclone 1.66+")
class ResolutionTests(unittest.TestCase):
    setUp = integration.EngineIntegrationTests.setUp
    tearDown = integration.EngineIntegrationTests.tearDown

    def prepare(self):
        self.assertEqual(run(self.cfg), 0)
        local, remote = self.local / "seed-5.txt", self.remote / "seed-5.txt"
        local.write_text("local revised version")
        remote.write_text("remote revised version is different")
        now = time.time()
        os.utime(local, (now + 2, now + 2))
        os.utime(remote, (now + 5, now + 5))
        return local, remote

    def test_both_directions_back_up_loser_and_allow_normal_sync(self):
        for side in ("local", "remote"):
            with self.subTest(side=side):
                local, remote = self.prepare()
                snapshot = scan(self.cfg)
                self.assertEqual(len(snapshot["rows"]), 1)
                row = snapshot["rows"][0]
                self.assertEqual(row["kind"], "Both versions changed")
                winner, loser = (local, remote) if side == "local" else (remote, local)
                kept, replaced = winner.read_bytes(), loser.read_bytes()
                history = {p.name: p.read_bytes() for p in (self.cfg.state / "bisync").iterdir()}
                backup = resolve(self.cfg, snapshot, row, side)
                self.assertEqual((backup / "original").read_bytes(), replaced)
                self.assertEqual(local.read_bytes(), kept)
                self.assertEqual(remote.read_bytes(), kept)
                self.assertEqual(history, {p.name: p.read_bytes() for p in (self.cfg.state / "bisync").iterdir()})
                self.assertEqual(run(self.cfg), 0)
                self.assertFalse(scan(self.cfg)["rows"])

    def test_size_limit_allows_only_smaller_source(self):
        local, remote = self.prepare()
        local.write_bytes(b"x" * 5001)
        snapshot = scan(self.cfg)
        row = snapshot["rows"][0]
        self.assertEqual(row["kind"], "Size limit conflict")
        self.assertFalse(allowed(self.cfg, row, "local"))
        self.assertTrue(allowed(self.cfg, row, "remote"))
        with self.assertRaises(SyncError):
            resolve(self.cfg, snapshot, row, "local")
        resolve(self.cfg, snapshot, row, "remote")
        self.assertEqual(local.read_bytes(), remote.read_bytes())
        self.assertEqual(run(self.cfg), 0)

    def test_stale_file_and_changed_history_are_rejected(self):
        local, remote = self.prepare()
        snapshot = scan(self.cfg)
        local.write_text("changed after review")
        original = remote.read_bytes()
        with self.assertRaisesRegex(SyncError, "file changed"):
            resolve(self.cfg, snapshot, snapshot["rows"][0], "local")
        self.assertEqual(remote.read_bytes(), original)
        (self.cfg.state / "status.json").write_text("{}")
        with self.assertRaisesRegex(SyncError, "Sync state changed"):
            resolve(self.cfg, snapshot, snapshot["rows"][0], "remote")

    def test_backup_failure_prevents_replacement(self):
        local, remote = self.prepare()
        snapshot = scan(self.cfg)
        runner = Runner()
        original_call = runner.call
        before = local.read_bytes(), remote.read_bytes()
        def call(args, **kwargs):
            if args[1] == "copyto":
                raise SyncError("Backup failed")
            return original_call(args, **kwargs)
        with patch.object(runner, "call", side_effect=call):
            with self.assertRaisesRegex(SyncError, "Backup failed"):
                resolve(self.cfg, snapshot, snapshot["rows"][0], "local", runner)
        self.assertEqual(before, (local.read_bytes(), remote.read_bytes()))

    def test_missing_health_marker_blocks_resolution(self):
        local, remote = self.prepare()
        snapshot = scan(self.cfg)
        (self.remote / self.cfg.health_file).unlink()
        with self.assertRaisesRegex(SyncError, "health marker"):
            resolve(self.cfg, snapshot, snapshot["rows"][0], "local")
        self.assertNotEqual(local.read_bytes(), remote.read_bytes())

    def test_edit_during_backup_prevents_replacement(self):
        local, remote = self.prepare()
        snapshot = scan(self.cfg)
        runner = Runner()
        original_call = runner.call
        before = remote.read_bytes()
        def call(args, **kwargs):
            result = original_call(args, **kwargs)
            if args[1] == "copyto" and "--immutable" in args:
                local.write_text("edited during the backup")
            return result
        with patch.object(runner, "call", side_effect=call):
            with self.assertRaisesRegex(SyncError, "changed while making its backup"):
                resolve(self.cfg, snapshot, snapshot["rows"][0], "local", runner)
        self.assertEqual(remote.read_bytes(), before)


    def test_only_conflicting_files_are_hashed(self):
        self.prepare()
        runner = Runner()
        original = runner.call
        calls = []
        def call(args, **kwargs):
            calls.append(args)
            return original(args, **kwargs)
        with patch.object(runner, "call", side_effect=call):
            scan(self.cfg, runner)
        hashes = [args for args in calls if "--hash" in args]
        self.assertEqual(len(hashes), 2)
        self.assertTrue(all("--stat" in args and "--recursive" not in args and args[2].endswith("seed-5.txt") for args in hashes))

    def test_worker_reports_all_conflicts_without_a_second_scan(self):
        self.prepare()
        (self.local / "seed-5.txt").write_bytes(b"x" * 5001)
        self.assertEqual(run(self.cfg), 1)
        detail = json.loads((self.cfg.state / "status.json").read_text())
        with patch.object(Runner, "call", side_effect=AssertionError("must not query remote")):
            snapshot = reported_snapshot(self.cfg, detail)
        self.assertEqual(snapshot["rows"][0]["path"], "seed-5.txt")
        self.assertTrue(snapshot["reported"])
        self.assertFalse(allowed(self.cfg, snapshot["rows"][0], "remote"))

    def test_old_worker_duplicate_diagnostic_is_immediately_visible(self):
        path = "folder/duplicate 'name'"
        detail = {"phase": "error", "message": f"Duplicate file or folder name: {path!r}. Rename one of the matching items before syncing."}
        snapshot = reported_snapshot(self.cfg, detail)
        self.assertEqual(snapshot["rows"][0]["path"], path)
        self.assertFalse(allowed(self.cfg, snapshot["rows"][0], "local"))

    def test_unchanged_cache_avoids_scan_and_hashes(self):
        self.prepare()
        observation = {"local": {"kind": "test"}, "remote": {"kind": "test"}}
        with patch("src.changes.observe", return_value=(observation, False, "test")):
            first = scan(self.cfg)
            with patch("src.conflicts._scan", side_effect=AssertionError("unexpected full scan")):
                second = scan(self.cfg, previous=first)
            self.assertEqual(first["rows"], second["rows"])
            with patch("src.conflicts._scan", wraps=__import__("src.conflicts", fromlist=["_scan"])._scan) as full:
                scan(self.cfg, previous=first, force=True)
                full.assert_called_once()

    def test_remote_queries_release_worker_lock_and_flag_changed_history(self):
        self.prepare()
        runner = Runner()
        original = runner.call
        touched = False
        guard = threading.Lock()
        def call(args, **kwargs):
            nonlocal touched
            from src.engine import run_lock
            with guard, run_lock(self.cfg):
                if not touched:
                    (self.cfg.state / "status.json").write_text("{}")
                    touched = True
            return original(args, **kwargs)
        with patch.object(runner, "call", side_effect=call):
            result = scan(self.cfg, runner)
        self.assertTrue(result["stale"])
