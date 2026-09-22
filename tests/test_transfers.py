import hashlib
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_integration as fixture
from src.config import Settings, SyncError
from src.engine import Runner, run, run_lock
from src.transfers import (eligible, format_bytes, format_totals, indexed,
                                 parse_plan, remote_capacity, scan, totals, unquote)


def contents(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(root).rglob("*") if path.is_file()}


class TransferUnitTests(unittest.TestCase):
    def test_unknown_sizes_do_not_turn_into_zero(self):
        result = totals([{"size": 123}, {"size": -1}, {"size": None}, {"size": 0}])
        self.assertEqual(result, {"files": 4, "bytes": 123, "unknown": 2})
        self.assertIn("unknown", format_totals(result))
        self.assertEqual(format_bytes(None), "Unknown")
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(2_000_000_000), "2.00 GB")

    def test_inclusive_file_limit_and_health_exclusion(self):
        cfg = Settings(max_size_bytes=5000)
        entries = [{"Path": name, "Size": size, "IsDir": is_dir} for name, size, is_dir in
                   (("exact", 5000, False), ("large", 5001, False), ("doc.url", -1, False),
                    (cfg.health_file, 10, False), ("folder", -1, True))]
        self.assertEqual(set(eligible(entries, cfg)), {"exact", "doc.url"})

    def test_remote_quota_omitted_values_remain_unknown(self):
        runner = unittest.mock.Mock()
        runner.call.return_value = '{"used":123,"free":0,"total":-1}'
        self.assertEqual(remote_capacity(Settings(), runner), {"used": 123, "free": 0})
        runner.call.side_effect = SyncError("Unsupported backend")
        self.assertIn("quota_error", remote_capacity(Settings(), runner))

    def test_go_quoted_paths(self):
        self.assertEqual(unquote(r'"space \"quote\"\n\t\\\x01\a\v\U0001f600"'),
                         'space "quote"\n\t\\\x01\a\v😀')

    def test_incompatible_or_incomplete_preview_is_not_empty_success(self):
        header = json.dumps({"msg": 'Synching Path1 "/local/" with Path2 "remote:/"'})
        finish = json.dumps({"msg": "Bisync successful"})
        for text in ("", "not json", header, finish,
                     header + '\n' + json.dumps({"msg": "- Path1 Queue teleport - remote:/file"}) + '\n' + finish):
            with self.subTest(text=text), self.assertRaises(SyncError):
                parse_plan(text, {}, {}, ".health")
        result = parse_plan(header + "\n" + finish, {}, {}, ".health")
        self.assertEqual(result, {"uploads": [], "downloads": [], "other": []})
        with self.assertRaises(SyncError):
            parse_plan(header + '\n' + json.dumps({"msg": "Skipped copy", "skipped": "copy"}) + '\n' + finish, {}, {}, '.health')

    def test_directories_not_counted_as_file_transfers(self):
        events = ['Synching Path1 "/local/" with Path2 "remote:/"',
                  '- Path1 Queue copy to Path2 - remote:/folder', 'Bisync successful']
        files = indexed([{"Path": "folder", "Size": -1, "IsDir": True}])
        result = parse_plan("\n".join(json.dumps({"msg": m}) for m in events), files, {}, ".health")
        self.assertFalse(result["uploads"])
        self.assertEqual(result["other"][0]["action"], "Create folder")


@unittest.skipUnless(fixture.supported_binary(), "rclone 1.66+ required")
class TransferIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture.EngineIntegrationTests.setUp(self)
        self.assertEqual(run(self.cfg), 0)

    def tearDown(self):
        fixture.EngineIntegrationTests.tearDown(self)

    def test_both_directions_deletions_filters_and_no_writes(self):
        (self.local / "upload.txt").write_text("local upload")
        (self.remote / "download.txt").write_text("remote download")
        (self.local / "large-local.bin").write_bytes(b"L" * 5001)
        (self.local / "excluded-local.tmp").write_text("ignored")
        (self.local / "seed-1.txt").unlink()
        (self.remote / "seed-2.txt").unlink()
        before = contents(self.root)
        result = scan(self.cfg)
        self.assertIsNone(result["error"], result["error"])
        self.assertFalse(result["stale"])
        self.assertEqual([p["path"] for p in result["uploads"]], ["upload.txt"])
        self.assertEqual([p["path"] for p in result["downloads"]], ["download.txt"])
        self.assertEqual({(p["path"], p["side"]) for p in result["other"]},
                         {("seed-1.txt", "Remote"), ("seed-2.txt", "Local")})
        self.assertEqual(result["remote"]["eligible"]["files"], 9)
        self.assertEqual(result["local"]["eligible"]["files"], 9)
        self.assertGreater(result["local"]["free"], 0)
        self.assertEqual(before, contents(self.root), "Preview changed files or live history")

    def test_no_changes_is_an_empty_queue(self):
        result = scan(self.cfg)
        self.assertIsNone(result["error"], result["error"])
        self.assertFalse(any(result[key] for key in ("uploads", "downloads", "other")))

    def test_unchanged_cached_preview_avoids_full_listing_and_dry_run(self):
        observation = {"local": {"kind": "inotify", "generation": 1}, "remote": {"kind": "drive", "cursor": "1"}}
        with patch("src.changes.observe", return_value=(observation, False, "test")):
            before = contents(self.root)
            first = scan(self.cfg, cache=True)
            self.assertIsNone(first["error"], first["error"])
            with patch("src.transfers.preflight", side_effect=AssertionError("unnecessary listing")), \
                 patch("src.transfers.bisync_command", side_effect=AssertionError("unnecessary dry run")):
                cached = scan(self.cfg, previous=first, cache=True)
            self.assertEqual(cached["uploads"], first["uploads"])
            self.assertFalse(cached["stale"])
            self.assertEqual(contents(self.root), before)

    def test_manual_preview_refresh_bypasses_cache(self):
        observation = {"local": {"kind": "inotify"}, "remote": {"kind": "drive"}}
        with patch("src.changes.observe", return_value=(observation, False, "test")):
            first = scan(self.cfg, cache=True)
            from src.transfers import preflight
            with patch("src.transfers.preflight", wraps=preflight) as checked:
                refreshed = scan(self.cfg, previous=first, force=True, cache=True)
        self.assertIsNone(refreshed["error"], refreshed["error"])
        checked.assert_called_once()

    def test_changed_observation_invalidates_cached_preview(self):
        observation = {"local": {"kind": "inotify"}, "remote": {"kind": "drive"}}
        with patch("src.changes.observe", return_value=(observation, False, "test")):
            first = scan(self.cfg, cache=True)
        (self.local / "new-after-preview").write_text("new contents")
        with patch("src.changes.observe", return_value=(observation, True, "test")):
            result = scan(self.cfg, previous=first, cache=True)
        self.assertIsNone(result["error"], result["error"])
        self.assertIn("new-after-preview", [entry["path"] for entry in result["uploads"]])

    def test_conflict_policy_change_invalidates_cached_preview(self):
        observation = {"local": {"kind": "inotify"}, "remote": {"kind": "drive"}}
        with patch("src.changes.observe", return_value=(observation, False, "test")):
            first = scan(self.cfg, cache=True)
            self.cfg.conflict_resolve = "none"
            from src.transfers import preflight
            with patch("src.transfers.preflight", wraps=preflight) as checked:
                result = scan(self.cfg, previous=first, cache=True)
        self.assertIsNone(result["error"], result["error"])
        checked.assert_called_once()

    def test_conflicts_include_preserved_versions(self):
        local, remote = self.local / "seed-5.txt", self.remote / "seed-5.txt"
        local.write_text("new LOCAL contents")
        remote.write_text("newer REMOTE contents with another size")
        base = time.time()
        os.utime(local, (base + 2, base + 2))
        os.utime(remote, (base + 5, base + 5))
        result = scan(self.cfg)
        self.assertIsNone(result["error"], result["error"])
        self.assertEqual(len(result["uploads"]), 1)
        self.assertTrue(result["uploads"][0]["path"].startswith("seed-5.txt.conflict"))
        self.assertEqual(result["uploads"][0]["source_path"], "seed-5.txt")
        self.assertEqual(result["uploads"][0]["size"], local.stat().st_size)
        self.assertEqual(result["downloads"][0]["path"], "seed-5.txt")
        self.assertEqual(result["downloads"][0]["size"], remote.stat().st_size)

    def test_unicode_and_quoted_filenames(self):
        names = ['café "quote".txt', ' spaced name .txt', 'line\nbreak.txt', 'back\\slash.txt']
        for name in names:
            (self.local / name).write_text("filename test")
        result = scan(self.cfg)
        self.assertIsNone(result["error"], result["error"])
        # rclone presents control characters using its standard path encoding.
        self.assertEqual({p["path"] for p in result["uploads"]}, {name.replace('\n', '␊') for name in names})
        self.assertTrue(all(p["size"] == 13 for p in result["uploads"]))

    def test_existing_conflict_named_file_keeps_its_own_size(self):
        (self.local / "seed-5.txt").write_text("local edit")
        (self.remote / "seed-5.txt").write_text("different remote edit")
        (self.local / "seed-5.txt.conflict99").write_text("a manually created file, not the current conflict")
        result = scan(self.cfg)
        self.assertIsNone(result["error"], result["error"])
        item = next(p for p in result["uploads"] if p["path"] == "seed-5.txt.conflict99")
        self.assertEqual(item["size"], (self.local / item["path"]).stat().st_size)
        self.assertEqual(item["action"], "Copy")

    def test_safety_failure_does_not_claim_no_pending_changes(self):
        (self.local / "seed-3.txt").write_bytes(b"L" * 5001)
        before = contents(self.root)
        result = scan(self.cfg)
        self.assertIn("Files exceed the size limit", result["error"])
        self.assertEqual(before, contents(self.root))
        self.assertIn("free", result["local"])

    def test_busy_worker_deferred(self):
        with run_lock(self.cfg):
            result = scan(self.cfg)
        self.assertIn("running", result["error"])

    def test_unknown_quota_does_not_hide_the_queue(self):
        (self.local / "upload.txt").write_text("local upload")
        with patch("src.transfers.remote_capacity", return_value={"quota_error": "Not supported"}):
            result = scan(self.cfg)
        self.assertIsNone(result["error"], result["error"])
        self.assertEqual(result["uploads"][0]["path"], "upload.txt")
        self.assertNotIn("free", result["remote"])
        self.assertIn("eligible", result["remote"])

    def test_live_lock_released_and_concurrent_cycle_marks_snapshot_stale(self):
        runner = Runner()
        original = runner.call
        def call(args, **kwargs):
            if args[1] == "bisync":
                with run_lock(self.cfg):
                    (self.cfg.state / "status.json").write_text('{"phase":"syncing"}')
            return original(args, **kwargs)
        runner.call = call
        result = scan(self.cfg, runner)
        self.assertIsNone(result["error"], result["error"])
        self.assertTrue(result["stale"])

    def test_missing_identity_or_baseline_cannot_trigger_resync(self):
        (self.cfg.state / "identity.json").unlink()
        result = scan(self.cfg)
        self.assertIn("Saved connection details are missing", result["error"])
        (self.cfg.state / "initialized").unlink()
        result = scan(self.cfg)
        self.assertIn("first sync", result["error"])
