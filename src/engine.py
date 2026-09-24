from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import unicodedata

from .config import Settings, SyncError, atomic_write, write_json
from .locking import BusyError, credential_lock, file_lock, operation_lock
from .rclone import require_rclone


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InventoryConflictError(SyncError):
    def __init__(self, message, rows):
        super().__init__(message)
        self.rows = rows


class RcloneError(SyncError):

    def __init__(self, command, exit_code, detail):
        self.exit_code = exit_code
        super().__init__(f"rclone could not complete {command} (exit code {exit_code}). "
                         f"{detail or 'Open Activity for details.'}")


@contextmanager
def run_lock(settings):
    with operation_lock(), file_lock(settings.state / "run.lock"):
        from .installation import worker_gate
        worker_gate(settings)
        if (settings.state / "retired").exists():
            raise SyncError("This connection has been replaced. Open Settings to manage the active connection.")
        yield


def indexed(entries):
    result = {}
    for entry in entries:
        key = unicodedata.normalize("NFC", entry["Path"])
        if key in result:
            raise SyncError(f"Duplicate file or folder name: {entry['Path']!r}. Rename one of the matching items before syncing.")
        result[key] = entry
    return result


def size_conflicts(local_entries, remote_entries, limit):
    left, right = indexed(local_entries), indexed(remote_entries)
    if not limit:
        return []
    return sorted(left[key]["Path"] for key in left.keys() & right.keys()
                  if left[key]["Size"] > limit or right[key]["Size"] > limit)


def native_folder(folder):
    folder = Path(folder)
    if not folder.is_dir() or folder.is_symlink():
        raise SyncError(f"The local folder is missing or is a symbolic link: {folder}. Choose an existing folder on your local disk.")
    # Checking only ismount() misses a subdirectory of a FUSE mount.
    resolved = folder.resolve()
    mount = (0, "")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        raw = before.split()[4]
        decoded = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), raw)
        point = Path(decoded)
        if resolved == point or point in resolved.parents:
            if len(point.parts) > mount[0]:
                mount = len(point.parts), after.split()[0]
    if mount[1].startswith("fuse") or mount[1] in ("nfs", "nfs4", "cifs", "smb3", "9p"):
        raise SyncError("Choose a folder on a local disk. Cloud mounts and network folders are not supported.")


def common_flags(cfg):
    flags = [f"--config={cfg.rclone_config}", "--ask-password=false",
             "--drive-export-formats=url", "--drive-use-trash=true",
             f"--contimeout={cfg.connect_timeout_seconds}s", f"--timeout={cfg.io_timeout_seconds}s"]
    if cfg.google_docs == "skip":
        flags.append("--drive-skip-gdocs")
    if cfg.fast_list:
        flags.append("--fast-list")
    return flags


def filter_text(cfg):
    return "+ /" + cfg.health_file + "\n" + "".join("- " + pattern + "\n" for pattern in cfg.excludes)


def transfer_flags(cfg):
    upload = f"{cfg.upload_kib}K" if cfg.upload_kib else "off"
    download = f"{cfg.download_kib}K" if cfg.download_kib else "off"
    return [f"--transfers={cfg.transfers}", f"--checkers={cfg.checkers}",
            f"--drive-chunk-size={cfg.drive_chunk_mib}M", f"--bwlimit={upload}:{download}",
            f"--retries={cfg.retries}", f"--retries-sleep={cfg.retry_delay_seconds}s",
            "--stats=10s", "--stats-one-line", "--color=NEVER", f"--log-level={cfg.log_level}"]


def bisync_command(cfg, workdir=None, initialize=False, preview=False, filters=None):
    backup = Path(cfg.backup_dir) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    args = [cfg.rclone_binary, "bisync", cfg.local_dir, cfg.remote, *common_flags(cfg),
            f"--workdir={workdir or cfg.state / 'bisync'}",
            f"--max-size={str(cfg.max_size_bytes) + 'B' if cfg.max_size_bytes else 'off'}",
            "--check-access", f"--check-filename={cfg.health_file}", f"--compare={cfg.compare}",
            f"--conflict-resolve={cfg.conflict_resolve}", "--conflict-loser=num",
            "--resilient", "--recover", "--max-lock=2m", f"--max-delete={cfg.max_delete_percent}",
            f"--backup-dir1={backup}", *transfer_flags(cfg)]
    if cfg.create_empty_dirs:
        args.append("--create-empty-src-dirs")
    if initialize:
        args.extend(["--resync-mode=path2", "--track-renames=false"])
    elif cfg.track_renames:
        args.append("--track-renames")
    if cfg.excludes:
        args.append(f"--filters-file={filters or cfg.state / 'filters.txt'}")
    if preview:
        args.append("--dry-run")
    return args


class Runner:
    def __init__(self, logger=None):
        self.log = logger or logging.getLogger("localdrive")
        self.children = set()
        self.stop_signalled = set()
        # Signal handlers may interrupt the main thread while it owns this lock.
        self.lock = threading.RLock()
        self.interrupted = False

    def interrupt(self, *_args):
        with self.lock:
            # A second SIGINT makes rclone abandon graceful cleanup. Duplicate
            # stop requests must not turn a normal shutdown into a forced exit.
            if self.interrupted:
                return
            self.interrupted = True
            for child in self.children:
                self.interrupt_child(child)

    def interrupt_child(self, child):
        if child not in self.stop_signalled:
            self.stop_signalled.add(child)
            try:
                child.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    def call(self, args, **kwargs):
        for index, arg in enumerate(args):
            if arg == "--config":
                path = args[index + 1]
                break
            if arg.startswith("--config="):
                path = arg.split("=", 1)[1]
                break
        else:
            return self._call(args, **kwargs)
        with credential_lock(path, shared=True):
            return self._call(args, **kwargs)

    def _call(self, args, stream=False, timeout=600, env=None, private=False, pass_fds=()):
        if self.interrupted:
            raise SyncError("Sync stopped. The next sync will try to recover from the interruption.")
        # Never log command lines/config contents: remotes can contain private paths.
        child = subprocess.Popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL if private else subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", start_new_session=True,
                                 env=env, pass_fds=pass_fds)
        with self.lock:
            self.children.add(child)
            # A stop can arrive between the initial check and Popen returning.
            if self.interrupted:
                self.interrupt_child(child)
        try:
            if stream:
                for line in child.stdout:
                    self.log.info(line.rstrip())
                child.wait()
                output = ""
            else:
                output, _ = child.communicate(timeout=timeout)
            if child.returncode:
                detail = "Account setup failed. Check your answers and try again." if private else output.strip()[-2000:]
                raise RcloneError(args[1], child.returncode, detail)
            if self.interrupted:
                raise SyncError("Sync stopped. The next sync will try to recover from the interruption.")
            return output
        except subprocess.TimeoutExpired as error:
            child.send_signal(signal.SIGINT)
            try:
                child.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
            raise SyncError("rclone took too long to respond. Check your connection and the timeout values in Reliability settings.") from error
        finally:
            child.stdout.close()
            with self.lock:
                self.children.discard(child)
                self.stop_signalled.discard(child)


def check_rclone(cfg, runner):
    version = require_rclone(cfg.rclone_binary)
    remotes = runner.call([cfg.rclone_binary, "listremotes", *common_flags(cfg)], timeout=30).splitlines()
    if cfg.remote.split(":", 1)[0] + ":" not in remotes:
        raise SyncError("The remote is missing. Use Connect account or select its rclone configuration file in Settings.")
    return version


def preflight(cfg, runner, check_health=True, inspect_conflicts=False):
    native_folder(cfg.local_dir)
    if shutil.disk_usage(cfg.local_dir).free < cfg.min_free_bytes:
        raise SyncError("The local disk has less free space than the minimum in Safety settings. Free up disk space, then try again.")
    if check_health:
        marker = Path(cfg.local_dir) / cfg.health_file
        if not marker.is_file() or marker.is_symlink() or marker.read_text() != cfg.health_content:
            raise SyncError("The local sync health marker is missing or has changed. Sync is stopped to protect your files. Check the local folder and restore the marker.")
        try:
            content = runner.call([cfg.rclone_binary, "cat", cfg.remote.rstrip("/") + "/" + cfg.health_file,
                                   *common_flags(cfg)], timeout=120)
        except RcloneError as error:
            # cat can report a missing file as "directory not found" (exit 3).
            # Neither case proves that the remote root itself is accessible.
            if error.exit_code in (3, 4):
                raise SyncError(f"The remote sync health marker {cfg.health_file!r} or its parent folder is missing or unavailable. "
                                "Sync is stopped to protect your files. Verify the selected remote account and folder; "
                                "if the marker was deleted, restore the original from remote Trash or a verified backup. "
                                "Do not reset the sync history. " + str(error)) from error
            raise SyncError(f"Could not read the remote sync health marker {cfg.health_file!r}. " + str(error)) from error
        if content != cfg.health_content:
            raise SyncError("The remote sync health marker has changed. Check the selected remote folder and restore the original marker before syncing.")

    def listing(path):
        args = [cfg.rclone_binary, "lsjson", path, "--recursive",
                *([] if inspect_conflicts else ["--no-mimetype"]),
                "--max-size=off", "--min-size=off", *common_flags(cfg)]
        if cfg.excludes:
            args.append(f"--filter-from={cfg.state / 'filters.txt'}")
        try:
            return json.loads(runner.call(args, timeout=max(600, cfg.io_timeout_seconds * 2)))
        except json.JSONDecodeError as error:
            raise SyncError("Could not read the file list from rclone. Sync has not started. Try again after checking Activity for details.") from error

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(listing, cfg.local_dir)
        right = pool.submit(listing, cfg.remote)
        if inspect_conflicts:
            return left.result(), right.result()
        from .conflicts import inventory_conflicts
        rows = list(inventory_conflicts(left.result(), right.result(), cfg.max_size_bytes, cfg.health_file).values())
        try:
            conflicts = size_conflicts(left.result(), right.result(), cfg.max_size_bytes)
        except SyncError as error:
            raise InventoryConflictError(str(error), rows) from error
        collisions = [row for row in rows if row["kind"] == "File / folder conflict"]
        if collisions:
            raise InventoryConflictError("File / folder conflict: " + repr(collisions[0]["path"]) + ". Rename one item before syncing.", rows)
    if conflicts:
        raise InventoryConflictError(f"Files exceed the size limit ({len(conflicts)}): " + ", ".join(repr(p) for p in conflicts[:10]) +
                        ". Each file exists in both folders, and at least one version exceeds the size limit. "
                        "Review these files. Sync is stopped to prevent unwanted deletions or overwrites.", rows)
    runner.log.info("Safety checks passed: local disk verified, health markers match, and no file size conflicts found.")
    return left.result(), right.result()


def check_identity(cfg):
    path = cfg.state / "identity.json"
    if not path.exists():
        raise SyncError("Saved connection details are missing. Open Settings to create or import a connection. Do not reset the sync history manually.")
    if json.loads(path.read_text()).get("fingerprint") != cfg.fingerprint():
        raise SyncError("Folder paths or file filters changed after setup and no longer match the saved sync history. "
                        "Use File → New connection with an empty local folder. Your existing files are kept.")


def configure_logger(cfg, preview):
    cfg.state.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("localdrive.worker." + cfg.profile_id + (".preview" if preview else ""))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(cfg.state / ("preview.log" if preview else "sync.log"),
                                  maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    return logger


def initialize(cfg, runner):
    approved = cfg.state / "setup-approved.json"
    if (cfg.state / "baseline-established").exists():
        raise SyncError("The file that records completed setup is missing from this connection. "
                        "Review Activity, then restore the setup marker and sync history. The app cannot safely restart setup automatically.")
    if not approved.exists() or json.loads(approved.read_text()).get("fingerprint") != cfg.fingerprint():
        raise SyncError("Confirm the initial download in Settings before syncing this folder.")
    folder = Path(cfg.local_dir)
    folder.mkdir(parents=True, exist_ok=True)
    native_folder(folder)
    started = cfg.state / "setup-started"
    if not started.exists():
        if any(folder.iterdir()):
            raise SyncError("Choose an empty local folder for the first sync. Your existing files are unchanged.")
        atomic_write(started, timestamp() + "\n")
    marker = folder / cfg.health_file
    if marker.exists() and (marker.is_symlink() or marker.read_text() != cfg.health_content):
        raise SyncError("The local folder contains a sync health marker from another setup or one that has changed. Check the folder before trying again.")
    if not marker.exists():
        atomic_write(marker, cfg.health_content, 0o644)
    runner.call([cfg.rclone_binary, "copyto", str(marker), cfg.remote.rstrip("/") + "/" + cfg.health_file,
                 "--immutable", *common_flags(cfg)], stream=True)
    preflight(cfg, runner)
    runner.log.info("Downloading initial files. Wait for setup to finish before editing this folder. If versions conflict during setup, the remote version is used and the local version is backed up.")
    args = [cfg.rclone_binary, "copy", cfg.remote, cfg.local_dir, "--ignore-existing",
            f"--max-size={str(cfg.max_size_bytes) + 'B' if cfg.max_size_bytes else 'off'}",
            *common_flags(cfg), *transfer_flags(cfg)]
    if cfg.create_empty_dirs:
        args.append("--create-empty-src-dirs")
    if cfg.excludes:
        args.append(f"--filter-from={cfg.state / 'filters.txt'}")
    runner.call(args, stream=True)
    preflight(cfg, runner)
    runner.call(bisync_command(cfg, initialize=True), stream=True)
    atomic_write(cfg.state / "baseline-established", timestamp() + "\n")
    atomic_write(cfg.state / "initialized", "Initial bisync completed " + timestamp() + "\n")
    approved.unlink()


def run(cfg, preview=False, check_changes=False):
    cfg.validate()
    with run_lock(cfg):
        log = configure_logger(cfg, preview)
        runner = Runner(log)
        old_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, runner.interrupt)
        status_path = cfg.state / ("preview-status.json" if preview else "status.json")
        started = timestamp()
        phase = "preview" if preview else "checking" if check_changes and cfg.initialized else "syncing"
        write_json(status_path, {"phase": phase, "started": started, "pid": os.getpid()})
        try:
            check_identity(cfg)
            check_rclone(cfg, runner)
            if cfg.initialized and not preview:
                from . import history
                history.recover(cfg, runner)
            if cfg.excludes:
                expected = filter_text(cfg)
                path = cfg.state / "filters.txt"
                if path.exists() and path.read_text() != expected:
                    raise SyncError("The file exclusion rules were changed outside the app. Restore the original filters file before syncing.")
                if not path.exists():
                    atomic_write(path, expected)
            checkpoint = None
            if cfg.initialized and not preview:
                from .changes import ProbeUnavailable, check, commit
                try:
                    checkpoint = check(cfg, runner)
                except (ProbeUnavailable, SyncError, OSError, ValueError, TypeError):
                    log.info("Fast change check unavailable; running full reconciliation.")
                if runner.interrupted:
                    raise SyncError("Sync stopped. The next sync will try again.")
                if check_changes and checkpoint is not None and not checkpoint["needed"]:
                    # Seed the durable copy on upgrade, using the unchanged,
                    # previously reconciled history verified by the fast gate.
                    from .history import CHECKPOINT, save
                    if not (cfg.state / CHECKPOINT).exists():
                        save(cfg)
                    commit(cfg, checkpoint)
                    finished = timestamp()
                    write_json(status_path, {"phase": "unchanged", "started": started, "finished": finished,
                                            "detection": checkpoint["mode"], "reconciled": False})
                    log.info("No changes detected (%s); full reconciliation skipped.", checkpoint["mode"])
                    return 0
            if not preview:
                from .changes import invalidate
                invalidate(cfg)
                write_json(status_path, {"phase": "syncing", "started": started, "pid": os.getpid()})
            if preview:
                if not cfg.initialized:
                    raise SyncError("Wait for the first sync to finish before previewing changes.")
                preflight(cfg, runner)
                # rclone dry runs can write bookkeeping: isolate that state, too.
                with tempfile.TemporaryDirectory(prefix="preview-", dir=cfg.state) as scratch:
                    work = Path(scratch) / "bisync"
                    shutil.copytree(cfg.state / "bisync", work)
                    filters = None
                    if cfg.excludes:
                        filters = Path(scratch) / "filters.txt"
                        shutil.copy2(cfg.state / "filters.txt", filters)
                        checksum = cfg.state / "filters.txt.md5"
                        if checksum.exists():
                            shutil.copy2(checksum, Path(scratch) / "filters.txt.md5")
                    runner.call(bisync_command(cfg, workdir=work, preview=True, filters=filters), stream=True)
            elif not cfg.initialized:
                initialize(cfg, runner)
            else:
                preflight(cfg, runner)
                runner.call(bisync_command(cfg), stream=True)
            if not preview:
                from .history import save
                save(cfg)
            if checkpoint is not None:
                commit(cfg, checkpoint, reconciled=True)
            finished = timestamp()
            write_json(status_path, {"phase": "success", "started": started, "finished": finished,
                                    "reconciled": not preview})
            if not preview:
                atomic_write(cfg.state / "last-success", finished + "\n")
            log.info("Preview finished. Your files are unchanged." if preview else "Sync finished successfully.")
            return 0
        except Exception as error:
            write_json(status_path, {"phase": "stopped" if runner.interrupted else "waiting" if isinstance(error, BusyError) else "error", "started": started,
                                    "finished": timestamp(), "message": str(error),
                                    **({"conflicts": {"fingerprint": cfg.fingerprint(), "rows": error.rows}}
                                       if isinstance(error, InventoryConflictError) else {})})
            if runner.interrupted:
                log.info("Sync stopped by request. Saved history will be recovered automatically on the next run.")
            elif isinstance(error, BusyError):
                log.info("Sync deferred: %s", error)
            else:
                log.error("Sync stopped: %s", error)
            return 130 if runner.interrupted else 75 if isinstance(error, BusyError) else 1
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
