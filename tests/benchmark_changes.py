import argparse
from contextlib import redirect_stderr
import io
import json
import logging
from pathlib import Path
import statistics
import tempfile
import time
from unittest.mock import patch

from src.changes import DriveChanges
from src.config import Settings, write_json
from src.engine import Runner, bisync_command, check_identity, check_rclone, preflight, run, run_lock
from src.watcher import Observer


def main():
    parser = argparse.ArgumentParser(description='Reproducible idle-check benchmark using temporary local-only rclone remotes.\n\nThe Drive change-feed response is simulated, with zero network latency. Its\ntiming measures local overhead only, not real Google Drive performance.')
    parser.add_argument("--files", type=int, default=10000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="localdrive-benchmark-") as temporary:
        root = Path(temporary)
        remote = root / "remote"
        remote.mkdir()
        config = root / "rclone.conf"
        config.write_text("[fixture]\ntype = local\n")
        cfg = Settings(local_dir=str(root / "local"), remote="fixture:" + str(remote),
                       rclone_config=str(config), state_dir=str(root / "state"),
                       backup_dir=str(root / "backups"), min_free_bytes=0, log_level="NOTICE")
        cfg.save(root / "config.json")
        write_json(cfg.state / "identity.json", {"fingerprint": cfg.fingerprint()})
        write_json(cfg.state / "setup-approved.json", {"fingerprint": cfg.fingerprint()})
        for number in range(args.files):
            folder = remote / f"folder-{number // 100}"
            folder.mkdir(exist_ok=True)
            (folder / f"file-{number}.txt").write_text(f"file {number}\n")
        quiet = logging.Logger("benchmark")
        quiet.addHandler(logging.NullHandler())
        runner = Runner(quiet)
        timings, counts = {}, {}

        def original_cycle():
            # Exactly the previous steady-state worker's substantive work.
            with run_lock(cfg):
                check_identity(cfg)
                check_rclone(cfg, runner)
                preflight(cfg, runner)
                runner.call(bisync_command(cfg), stream=True)

        def measure(name, action, observer):
            durations, listing_counts = [], []
            original_call = Runner.call
            for _ in range(args.repeats):
                observer.tick()
                recursive = []
                def counted(self, command, **kwargs):
                    if command[1] == "bisync" or command[1] == "lsjson" and "--recursive" in command:
                        recursive.append(command[1])
                    return original_call(self, command, **kwargs)
                with patch.object(Runner, "call", counted):
                    started = time.perf_counter()
                    result = action()
                    durations.append((time.perf_counter() - started) * 1000)
                    if result not in (None, 0):
                        raise RuntimeError("Benchmark sync failed")
                listing_counts.append(len(recursive))
            timings[name] = round(statistics.median(durations), 2)
            counts[name] = listing_counts

        with redirect_stderr(io.StringIO()):
            if run(cfg):
                raise RuntimeError("Benchmark setup failed")
            observer = Observer(cfg, root / "config.json")
            observer.rebuild()
            if observer.tree is None:
                raise RuntimeError("inotify is unavailable")
            try:
                with patch.object(observer, "request_sync", return_value=False):
                    measure("previous_full_cycle", original_cycle, observer)
                    run(cfg)  # Warm the ordinary remote-listing checkpoint.
                    observer.tick()
                    measure("generic_remote_idle_check", lambda: run(cfg, check_changes=True), observer)
                    settings = {"type": "drive", "token": json.dumps({"access_token": "test-only"})}
                    def response(_self, endpoint, params):
                        return {"startPageToken": "1"} if endpoint else {"changes": [], "newStartPageToken": "1"}
                    with patch("src.changes.remote_settings", return_value=settings), \
                         patch.object(DriveChanges, "_request", response):
                        run(cfg)
                        observer.tick()
                        measure("simulated_drive_idle_check", lambda: run(cfg, check_changes=True), observer)
            finally:
                observer.tree.close()
        print(json.dumps({"files": args.files, "repeats": args.repeats, "median_ms": timings,
                          "recursive_rclone_commands_per_check": counts,
                          "note": "All file operations used temporary local remotes. Drive HTTP was simulated; add real API latency."}, indent=2))


if __name__ == "__main__":
    main()
