import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest

from src.config import Settings, find_rclone, write_json
from src.engine import run


BINARY = os.environ.get("RCLONE_TEST_BINARY", find_rclone())


def supported_binary():
    try:
        result = subprocess.run([BINARY, "version"], capture_output=True, text=True, timeout=10)
        match = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", result.stdout)
        return match and tuple(map(int, match.groups())) >= (1, 66, 0)
    except OSError:
        return False


@unittest.skipUnless(supported_binary(), "Set RCLONE_TEST_BINARY to rclone 1.66+ for local integration tests")
class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="localdrive-integration-")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.local = self.root / "local"
        self.remote.mkdir()
        config = self.root / "rclone.conf"
        config.write_text("[fixture]\ntype = local\n")
        self.cfg = Settings(local_dir=str(self.local), remote="fixture:" + str(self.remote),
                            rclone_binary=BINARY, rclone_config=str(config), state_dir=str(self.root / "state"),
                            backup_dir=str(self.root / "backups"), min_free_bytes=0,
                            max_size_bytes=5000, excludes=["*.tmp"], transfers=1, checkers=2,
                            retries=1, retry_delay_seconds=0)
        self.cfg.state.mkdir()
        write_json(self.cfg.state / "identity.json", {"fingerprint": self.cfg.fingerprint()})
        write_json(self.cfg.state / "setup-approved.json", {"fingerprint": self.cfg.fingerprint()})
        for index in range(8):
            (self.remote / f"seed-{index}.txt").write_text(f"original remote file {index}\n")
        (self.remote / "excluded.tmp").write_text("not synced")
        (self.remote / "oversized.bin").write_bytes(b"x" * 5001)
        (self.remote / "exact-limit.bin").write_bytes(b"b" * 5000)

    def tearDown(self):
        self.temp.cleanup()

    def test_full_local_first_workflow(self):
        self.assertEqual(run(self.cfg), 0)
        self.assertTrue(self.cfg.initialized)
        self.assertTrue((self.local / "seed-0.txt").is_file())
        self.assertFalse((self.local / "oversized.bin").exists())
        self.assertFalse((self.local / "excluded.tmp").exists())
        self.assertEqual((self.local / "exact-limit.bin").stat().st_size, 5000)
        self.assertTrue((self.remote / "oversized.bin").exists())

        (self.local / "from-local.txt").write_text("local upload")
        (self.remote / "from-remote.txt").write_text("remote download")
        (self.local / "large-local.bin").write_bytes(b"L" * 5001)
        (self.local / "seed-1.txt").unlink()
        (self.remote / "seed-2.txt").unlink()
        self.assertEqual(run(self.cfg), 0)
        self.assertEqual((self.remote / "from-local.txt").read_text(), "local upload")
        self.assertEqual((self.local / "from-remote.txt").read_text(), "remote download")
        self.assertFalse((self.remote / "large-local.bin").exists())
        self.assertFalse((self.remote / "seed-1.txt").exists())
        self.assertFalse((self.local / "seed-2.txt").exists())
        self.assertTrue(list(Path(self.cfg.backup_dir).rglob("seed-2.txt")))

        # Dry-run does not touch either root or the live bisync listings.
        (self.local / "preview-only.txt").write_text("do not upload yet")
        def listing_hashes():
            return {str(p.relative_to(self.cfg.state / "bisync")): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (self.cfg.state / "bisync").rglob("*") if p.is_file()}
        before = listing_hashes()
        self.assertEqual(run(self.cfg, preview=True), 0)
        self.assertFalse((self.remote / "preview-only.txt").exists())
        self.assertEqual(before, listing_hashes())

        # Growing a formerly synced file past the cap must not delete its peer.
        original = (self.remote / "seed-3.txt").read_bytes()
        (self.local / "seed-3.txt").write_bytes(b"g" * 5001)
        self.assertEqual(run(self.cfg), 1)
        self.assertEqual((self.remote / "seed-3.txt").read_bytes(), original)
        self.assertEqual((self.local / "seed-3.txt").stat().st_size, 5001)
        self.assertIn("Files exceed the size limit", json.loads((self.cfg.state / "status.json").read_text())["message"])

    def test_missing_health_stops_before_propagating_deletion(self):
        self.assertEqual(run(self.cfg), 0)
        (self.local / self.cfg.health_file).unlink()
        (self.local / "seed-0.txt").unlink()
        self.assertEqual(run(self.cfg), 1)
        self.assertTrue((self.remote / "seed-0.txt").exists())

    def test_changed_identity_stops_before_sync(self):
        self.assertEqual(run(self.cfg), 0)
        self.cfg.max_size_bytes = 4500
        self.assertEqual(run(self.cfg), 1)
        self.assertTrue((self.remote / "seed-0.txt").exists())

    def test_first_run_refuses_preexisting_files(self):
        self.local.mkdir()
        (self.local / "keep.txt").write_text("keep this")
        self.assertEqual(run(self.cfg), 1)
        self.assertFalse(self.cfg.initialized)
        self.assertEqual((self.local / "keep.txt").read_text(), "keep this")
        self.assertFalse((self.remote / self.cfg.health_file).exists())

    def test_rename_propagates_without_losing_content(self):
        self.assertEqual(run(self.cfg), 0)
        before = (self.remote / "seed-4.txt").read_bytes()
        (self.local / "seed-4.txt").rename(self.local / "renamed.txt")
        self.assertEqual(run(self.cfg), 0)
        self.assertFalse((self.remote / "seed-4.txt").exists())
        self.assertEqual((self.remote / "renamed.txt").read_bytes(), before)

    def test_simultaneous_edits_preserve_losing_version(self):
        self.assertEqual(run(self.cfg), 0)
        local, remote = self.local / "seed-5.txt", self.remote / "seed-5.txt"
        local.write_text("new LOCAL contents")
        remote.write_text("newer REMOTE contents with another size")
        base = time.time()
        os.utime(local, (base + 2, base + 2))
        os.utime(remote, (base + 5, base + 5))
        self.assertEqual(run(self.cfg), 0)
        self.assertEqual(local.read_text(), "newer REMOTE contents with another size")
        self.assertEqual(remote.read_text(), local.read_text())
        preserved = [p for p in self.local.glob("seed-5*") if p != local]
        self.assertTrue(any(p.read_text() == "new LOCAL contents" for p in preserved))

    def test_remote_health_content_mismatch_is_safe(self):
        self.assertEqual(run(self.cfg), 0)
        (self.remote / self.cfg.health_file).write_text("wrong connection")
        (self.local / "seed-0.txt").unlink()
        self.assertEqual(run(self.cfg), 1)
        self.assertTrue((self.remote / "seed-0.txt").exists())

    def test_missing_remote_health_reports_recovery_without_changing_files_or_history(self):
        self.assertEqual(run(self.cfg), 0)
        (self.remote / self.cfg.health_file).unlink()
        (self.local / "seed-0.txt").unlink()
        work = self.cfg.state / "bisync"
        before = {p.name: p.read_bytes() for p in work.iterdir() if p.is_file()}

        self.assertEqual(run(self.cfg), 1)
        message = json.loads((self.cfg.state / "status.json").read_text())["message"]
        self.assertIn("remote sync health marker", message)
        self.assertIn(self.cfg.health_file, message)
        self.assertIn("restore the original", message)
        self.assertTrue((self.remote / "seed-0.txt").exists())
        self.assertFalse((self.remote / self.cfg.health_file).exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in work.iterdir() if p.is_file()})

    def test_missing_initialized_marker_never_triggers_resync(self):
        self.assertEqual(run(self.cfg), 0)
        (self.cfg.state / "initialized").unlink()
        (self.local / "seed-0.txt").unlink()
        self.assertEqual(run(self.cfg), 1)
        self.assertFalse((self.local / "seed-0.txt").exists())
        self.assertTrue((self.remote / "seed-0.txt").exists())
        self.assertIn("cannot safely restart setup automatically", json.loads((self.cfg.state / "status.json").read_text())["message"])


if __name__ == "__main__":
    unittest.main()
