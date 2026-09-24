import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone
import time

from .config import SyncError, atomic_write, write_json
from .locking import BusyError


CHECKPOINT = "history-checkpoint.json"
JOURNAL = "history-recovery.json"


def pair(directory, suffix=""):
    directory = Path(directory)
    if directory.is_symlink():
        return None
    left = sorted(directory.glob("*.path1.lst" + suffix))
    right = sorted(directory.glob("*.path2.lst" + suffix))
    if len(left) != 1 or len(right) != 1:
        return None
    if left[0].name.removesuffix(".path1.lst" + suffix) != right[0].name.removesuffix(".path2.lst" + suffix):
        return None
    for path in (left[0], right[0]):
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            return None
        with path.open("rb") as stream:
            if not stream.readline(64).startswith(b"# bisync listing v1 from"):
                return None
    return left[0], right[0]


def status(cfg):
    work = cfg.state / "bisync"
    if (cfg.state / JOURNAL).exists():
        return "recoverable"
    if pair(work):
        markers = ("baseline-established", "initialized")
        return "ready" if all((cfg.state / name).is_file() for name in markers) else "recoverable"
    if (cfg.state / CHECKPOINT).is_file() or pair(work, "-old"):
        return "recoverable"
    return "missing"


def checksum(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(cfg, files):
    return {"version": 1, "fingerprint": cfg.fingerprint(),
            "files": {name: {"text": text, "sha256": checksum(text)} for name, text in files.items()}}


def read_record(cfg, path):
    try:
        value = json.loads(path.read_text())
        if value["version"] != 1 or value["fingerprint"] != cfg.fingerprint():
            raise ValueError("Wrong connection")
        files = value["files"]
        if len(files) != 2:
            raise ValueError("Incomplete pair")
        for name, item in files.items():
            if Path(name).name != name or "/" in name or "\\" in name:
                raise ValueError("Invalid filename")
            if checksum(item["text"]) != item["sha256"] or not item["text"].startswith("# bisync listing v1 from"):
                raise ValueError("Invalid listing")
        left = [name for name in files if name.endswith(".path1.lst")]
        if len(left) != 1 or left[0].removesuffix(".path1.lst") + ".path2.lst" not in files:
            raise ValueError("Mismatched pair")
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise SyncError("The saved history checkpoint is damaged or belongs to another connection. "
                        "Original history was kept; restore a verified checkpoint before syncing.") from error


def save(cfg):
    paths = pair(cfg.state / "bisync")
    if paths is None:
        raise SyncError("Sync finished without a complete history pair. Open Repair before syncing again.")
    files = {path.name: path.read_text() for path in paths}
    if not all(text.startswith("# bisync listing v1 from") for text in files.values()):
        raise SyncError("Sync produced unreadable history. Original history was kept.")
    # One atomic, durable record keeps both sides from the same successful run.
    # Idle change checks never rewrite this checkpoint or scan file contents.
    write_json(cfg.state / CHECKPOINT, record(cfg, files))


def expired_lock(cfg):
    locks = list((cfg.state / "bisync").glob("*.lck"))
    for path in locks:
        if path.is_symlink() or not path.is_file():
            raise SyncError("The sync lock is not a regular file. Review the history directory.")
        try:
            lease = json.loads(path.read_text())
            expires = datetime.fromisoformat(lease["TimeExpires"].replace("Z", "+00:00"))
            expired = expires < datetime.now(timezone.utc)
        except (ValueError, KeyError, TypeError, AttributeError):
            expired = time.time() - path.stat().st_mtime >= 120
        if not expired:
            raise BusyError("The previous sync lock has not expired yet. The next scheduled run will retry.")
    return bool(locks)


def recover(cfg, runner):
    from .changes import invalidate
    from .engine import bisync_command

    stale_lock = expired_lock(cfg)
    if status(cfg) == "ready" and not stale_lock:
        return False
    work = cfg.state / "bisync"
    if work.is_symlink():
        raise SyncError("The history directory is a symbolic link. Restore the original state directory.")
    journal = cfg.state / JOURNAL
    if journal.exists():
        saved = read_record(cfg, journal)
    elif pair(work) and not stale_lock:
        saved = record(cfg, {p.name: p.read_text() for p in pair(work)})
    elif (cfg.state / CHECKPOINT).exists():
        try:
            saved = read_record(cfg, cfg.state / CHECKPOINT)
        except SyncError:
            paths = pair(work, "-old")
            if paths is None:
                raise
            runner.log.warning("Checkpoint unavailable; validating rclone's recovery copies instead.")
            saved = record(cfg, {p.name.removesuffix("-old"): p.read_text() for p in paths})
    else:
        paths = pair(work, "-old")
        if paths is None:
            raise SyncError("No complete recovery history is available. Restore a verified history checkpoint. "
                            "Rebuilding without history cannot distinguish new files from deletions.")
        saved = record(cfg, {p.name.removesuffix("-old"): p.read_text() for p in paths})
    if stale_lock:
        saved["native_backup"] = True
    if any(p.name not in saved["files"] for p in work.glob("*.lst")):
        raise SyncError("History contains another connection. Review the preserved history before syncing.")
    # Let rclone validate its own format, matching endpoints, and both listings.
    # check-sync=only reads metadata; it does not list or modify synced files.
    with tempfile.TemporaryDirectory(prefix="history-check-", dir=cfg.state) as directory:
        scratch = Path(directory)
        for name, item in saved["files"].items():
            atomic_write(scratch / name, item["text"])
        runner.call(bisync_command(cfg, workdir=scratch) + ["--check-sync=only"], timeout=180)
    if not journal.exists():
        archive = Path(tempfile.mkdtemp(prefix="history-recovery-", dir=cfg.state))
        if work.exists():
            shutil.copytree(work, archive / "bisync", symlinks=True)
        if (cfg.state / CHECKPOINT).exists():
            shutil.copy2(cfg.state / CHECKPOINT, archive / CHECKPOINT, follow_symlinks=False)
        write_json(journal, saved)
    # Replay a durable journal if interrupted between restoring the two sides.
    # Invalidate the fast path first, so the next run must reconcile both roots.
    invalidate(cfg)
    for name, item in saved["files"].items():
        atomic_write(work / name, item["text"])
        # rclone invalidates current listings when it clears an expired lease.
        # Give --recover the same validated pair; never delete its lock early.
        if saved.get("native_backup"):
            atomic_write(work / (name + "-old"), item["text"])
    if pair(work) is None:
        raise SyncError("History contains multiple connections. Review the preserved history before syncing.")
    if not (cfg.state / "baseline-established").exists():
        atomic_write(cfg.state / "baseline-established", "Recovered from validated sync history.\n")
    if not (cfg.state / "initialized").exists():
        atomic_write(cfg.state / "initialized", "Recovered from validated sync history.\n")
    journal.unlink()
    directory = os.open(cfg.state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    runner.log.info("Recovered validated sync history. The next sync will perform full reconciliation.")
    return True
