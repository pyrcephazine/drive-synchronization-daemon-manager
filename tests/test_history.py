import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src import history
from src.config import SyncError
from src.engine import Runner, run, run_lock
import test_integration as fixture


@unittest.skipUnless(fixture.supported_binary(), "Requires rclone 1.66+")
class HistoryTests(unittest.TestCase):
    setUp = fixture.EngineIntegrationTests.setUp
    tearDown = fixture.EngineIntegrationTests.tearDown

    def initialize(self):
        self.assertEqual(run(self.cfg), 0)
        self.work = self.cfg.state / "bisync"
        self.saved = json.loads((self.cfg.state / history.CHECKPOINT).read_text())

    def remove_history(self):
        for path in self.work.glob("*.lst*"):
            path.unlink()

    def test_checkpoint_recovers_deletions_and_new_files_without_resync(self):
        self.initialize()
        (self.local / "seed-1.txt").unlink()
        (self.remote / "seed-2.txt").unlink()
        (self.local / "new-local").write_text("new upload")
        (self.remote / "new-remote").write_text("new download")
        self.remove_history()
        with patch("src.engine.initialize", side_effect=AssertionError("must not resync")):
            self.assertEqual(run(self.cfg, check_changes=True), 0)
        self.assertFalse((self.remote / "seed-1.txt").exists())
        self.assertFalse((self.local / "seed-2.txt").exists())
        self.assertEqual((self.remote / "new-local").read_text(), "new upload")
        self.assertEqual((self.local / "new-remote").read_text(), "new download")
        self.assertTrue(list(self.cfg.state.glob("history-recovery-*/bisync")))
        self.assertNotEqual(self.saved, json.loads((self.cfg.state / history.CHECKPOINT).read_text()))

    def test_native_backups_recover_without_checkpoint(self):
        self.initialize()
        (self.cfg.state / history.CHECKPOINT).unlink()
        for path in history.pair(self.work):
            path.with_name(path.name + "-old").write_bytes(path.read_bytes())
            path.rename(path.with_name(path.name + "-err"))
        with run_lock(self.cfg):
            self.assertTrue(history.recover(self.cfg, Runner()))
        self.assertEqual(history.status(self.cfg), "ready")
        self.assertEqual(run(self.cfg), 0)

    def test_invalid_native_backup_is_not_installed(self):
        self.initialize()
        (self.cfg.state / history.CHECKPOINT).unlink()
        for path in history.pair(self.work):
            path.rename(path.with_name(path.name + "-old"))
        right = next(self.work.glob("*.path2.lst-old"))
        right.write_text(right.read_text().splitlines(keepends=True)[0])
        with run_lock(self.cfg), self.assertRaises(SyncError):
            history.recover(self.cfg, Runner())
        self.assertFalse(list(self.work.glob("*.lst")))

    def test_damaged_checkpoint_can_use_valid_native_backup(self):
        self.initialize()
        (self.cfg.state / history.CHECKPOINT).write_text("damaged checkpoint")
        for path in history.pair(self.work):
            path.rename(path.with_name(path.name + "-old"))
        with run_lock(self.cfg):
            self.assertTrue(history.recover(self.cfg, Runner()))
        self.assertEqual(history.status(self.cfg), "ready")
        archives = list(self.cfg.state.glob("history-recovery-*/" + history.CHECKPOINT))
        self.assertEqual(archives[0].read_text(), "damaged checkpoint")

    def test_missing_all_history_never_reinitializes(self):
        self.initialize()
        self.remove_history()
        (self.cfg.state / history.CHECKPOINT).unlink()
        self.assertEqual(history.status(self.cfg), "missing")
        with patch("src.engine.initialize", side_effect=AssertionError("must not resync")):
            self.assertEqual(run(self.cfg), 1)
        self.assertFalse(list(self.work.glob("*.lst")))

    def test_checkpoint_checksum_and_identity_are_verified(self):
        self.initialize()
        self.remove_history()
        checkpoint = self.cfg.state / history.CHECKPOINT
        for change in ("checksum", "identity"):
            value = json.loads(json.dumps(self.saved))
            if change == "checksum":
                next(iter(value["files"].values()))["text"] += "damage"
            else:
                value["fingerprint"] = "another connection"
            checkpoint.write_text(json.dumps(value))
            with run_lock(self.cfg), self.assertRaises(SyncError):
                history.recover(self.cfg, Runner())
            self.assertFalse(list(self.work.glob("*.lst")))

    def test_interrupted_restore_replays_both_sides(self):
        self.initialize()
        self.remove_history()
        original = history.atomic_write
        def interrupted(path, text, *args):
            if str(path).endswith(".path2.lst") and Path(path).parent == self.work:
                raise OSError("simulated power failure")
            return original(path, text, *args)
        with run_lock(self.cfg), patch("src.history.atomic_write", side_effect=interrupted), self.assertRaises(OSError):
            history.recover(self.cfg, Runner())
        self.assertTrue((self.cfg.state / history.JOURNAL).exists())
        with run_lock(self.cfg):
            history.recover(self.cfg, Runner())
        self.assertFalse((self.cfg.state / history.JOURNAL).exists())
        for name, item in self.saved["files"].items():
            self.assertEqual((self.work / name).read_text(), item["text"])

    def test_missing_setup_marker_and_truncated_listing_recover(self):
        self.initialize()
        (self.cfg.state / "baseline-established").unlink()
        with run_lock(self.cfg):
            self.assertTrue(history.recover(self.cfg, Runner()))
        next(self.work.glob("*.path1.lst")).write_text("")
        with run_lock(self.cfg):
            self.assertTrue(history.recover(self.cfg, Runner()))
        self.assertEqual(history.status(self.cfg), "ready")

    def test_idle_check_does_not_rewrite_checkpoint(self):
        self.initialize()
        self.assertEqual(run(self.cfg, check_changes=True), 0)
        checkpoint = self.cfg.state / history.CHECKPOINT
        before = checkpoint.stat().st_mtime_ns
        with patch("src.history.save", side_effect=AssertionError("idle checkpoint rewrite")):
            self.assertEqual(run(self.cfg, check_changes=True), 0)
        self.assertEqual(before, checkpoint.stat().st_mtime_ns)

    def test_upgrade_seeds_checkpoint_without_full_reconciliation(self):
        self.initialize()
        self.assertEqual(run(self.cfg, check_changes=True), 0)
        checkpoint = self.cfg.state / history.CHECKPOINT
        checkpoint.unlink()
        with patch("src.engine.preflight", side_effect=AssertionError("unnecessary full scan")):
            self.assertEqual(run(self.cfg, check_changes=True), 0)
        self.assertTrue(checkpoint.is_file())

    def test_unexpired_native_lock_defers_without_changing_history(self):
        self.initialize()
        left = history.pair(self.work)[0]
        lock = self.work / (left.name.removesuffix(".path1.lst") + ".lck")
        lock.write_text(json.dumps({"TimeExpires": "2999-01-01T00:00:00Z"}))
        before = {p.name: p.read_bytes() for p in self.work.iterdir()}
        self.assertEqual(run(self.cfg), 75)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.work.iterdir()})
        self.assertEqual(json.loads((self.cfg.state / "status.json").read_text())["phase"], "waiting")

    def interrupted_worker(self, forced):
        import os
        import signal
        import subprocess
        import sys
        import time

        self.cfg.max_size_bytes = 0
        from src.config import write_json
        write_json(self.cfg.state / "identity.json", {"fingerprint": self.cfg.fingerprint()})
        write_json(self.cfg.state / "setup-approved.json", {"fingerprint": self.cfg.fingerprint()})
        self.initialize()
        checkpoint = (self.cfg.state / history.CHECKPOINT).read_bytes()
        (self.local / "shutdown-payload.bin").write_bytes(b"payload\n" * (256 * 1024))
        (self.local / "seed-1.txt").unlink()
        self.cfg.upload_kib = 16
        self.cfg.download_kib = 16
        config = self.root / "worker.json"
        self.cfg.save(config)
        log = self.root / "worker.log"
        code = "from src.config import Settings; from src.engine import run; import sys; sys.exit(run(Settings.load(sys.argv[1])))"
        child_pid = None
        with log.open("w") as output:
            worker = subprocess.Popen([sys.executable, "-c", code, str(config)], stdout=output, stderr=output,
                                      env={**os.environ, "RCLONE_DISABLE": "Copy"})
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    self.assertIsNone(worker.poll(), log.read_text()[-3000:])
                    children = Path(f"/proc/{worker.pid}/task/{worker.pid}/children").read_text().split()
                    for pid in children:
                        try:
                            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                        except FileNotFoundError:
                            continue
                        if len(args) > 1 and args[1] == b"bisync":
                            child_pid = int(pid)
                    if child_pid and any(p.stat().st_size for p in self.remote.glob("shutdown-payload*")):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Worker did not start a transfer: " + log.read_text()[-3000:])
                started = time.monotonic()
                if forced:
                    # Match systemd's final cgroup kill after a stop deadline.
                    worker.kill()
                    os.kill(child_pid, signal.SIGKILL)
                else:
                    worker.send_signal(signal.SIGTERM)
                code = worker.wait(timeout=110)
                if not forced:
                    self.assertEqual(code, 130, log.read_text()[-3000:])
                    self.assertLess(time.monotonic() - started, 105)
                    status = json.loads((self.cfg.state / "status.json").read_text())
                    self.assertEqual(status["phase"], "stopped")
                self.assertEqual((self.cfg.state / history.CHECKPOINT).read_bytes(), checkpoint)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
                if child_pid:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if forced:
            # Simulate the next boot after the native two-minute lease expires.
            # Live code retains that lease to protect other rclone processes.
            for path in self.work.glob("*.lck"):
                lease = json.loads(path.read_text())
                lease["TimeExpires"] = "2000-01-01T00:00:00Z"
                path.write_text(json.dumps(lease))
        self.cfg.upload_kib = 0
        self.cfg.download_kib = 0
        with patch("src.engine.initialize", side_effect=AssertionError("shutdown must not trigger resync")):
            self.assertEqual(run(self.cfg), 0)
        self.assertEqual((self.local / "shutdown-payload.bin").read_bytes(),
                         (self.remote / "shutdown-payload.bin").read_bytes())
        self.assertFalse((self.remote / "seed-1.txt").exists())
        self.assertEqual(history.status(self.cfg), "ready")

    def test_graceful_shutdown_recovers_and_resumes_transfer(self):
        self.interrupted_worker(forced=False)

    def test_forced_shutdown_recovers_after_native_lease_expires(self):
        self.interrupted_worker(forced=True)
