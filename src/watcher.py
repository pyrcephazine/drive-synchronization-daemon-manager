import ctypes
import os
from pathlib import Path
import select
import signal
import stat
import struct
import time
import uuid

from .changes import process_identity
from .config import SERVICE, Settings, SyncError, write_json


MODIFY = 0x00000002
ATTRIB = 0x00000004
CLOSE_WRITE = 0x00000008
MOVED_FROM = 0x00000040
MOVED_TO = 0x00000080
CREATE = 0x00000100
DELETE = 0x00000200
DELETE_SELF = 0x00000400
MOVE_SELF = 0x00000800
UNMOUNT = 0x00002000
Q_OVERFLOW = 0x00004000
IGNORED = 0x00008000
ONLYDIR = 0x01000000
DONT_FOLLOW = 0x02000000
ISDIR = 0x40000000
MASK = MODIFY | ATTRIB | CLOSE_WRITE | MOVED_FROM | MOVED_TO | CREATE | DELETE | DELETE_SELF | MOVE_SELF | UNMOUNT
EVENT = struct.Struct("iIII")


class InotifyTree:
    def __init__(self, root):
        self.root = Path(root)
        self.fd = -1
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "Could not initialize inotify")
        try:
            info = self.root.lstat()
            self.root_identity = [info.st_dev, info.st_ino]
            self.directories = {}
            self._add_tree(self.root)
            info = self.root.lstat()
            if self.root_identity != [info.st_dev, info.st_ino]:
                raise OSError("The watched root changed while registering watches")
        except Exception:
            self.close()
            raise

    def _add_tree(self, root):
        pending = [root]
        while pending:
            folder = pending.pop()
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(folder), MASK | ONLYDIR | DONT_FOLLOW)
            if wd < 0:
                raise OSError(ctypes.get_errno(), "Could not watch every local directory")
            self.directories[wd] = folder
            # Watch BEFORE enumerating children. Reads do not generate any of
            # the subscribed events. Never follow a symlink outside this tree.
            with os.scandir(folder) as entries:
                pending.extend(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))

    def drain(self):
        changed, rebuild = False, False
        # Bound each batch so a sustained write storm cannot starve debounce,
        # heartbeat or shutdown handling.
        for _ in range(16):
            try:
                data = os.read(self.fd, 262144)
            except BlockingIOError:
                break
            if not data:
                return True, True
            offset = 0
            while offset < len(data):
                if len(data) - offset < EVENT.size:
                    return True, True
                wd, mask, _cookie, length = EVENT.unpack_from(data, offset)
                offset += EVENT.size + length
                if offset > len(data):
                    return True, True
                changed = True
                if (mask & (Q_OVERFLOW | UNMOUNT | IGNORED | DELETE_SELF | MOVE_SELF)
                        or wd not in self.directories
                        or mask & ISDIR and mask & (CREATE | DELETE | MOVED_FROM | MOVED_TO)):
                    rebuild = True
        return changed, rebuild

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class Observer:
    def __init__(self, cfg, config_path, manager=None):
        self.cfg, self.config_path = cfg, Path(config_path)
        self.manager = manager
        self.last_maintenance = 0.0
        self.tree = None
        self.session = uuid.uuid4().hex
        self.generation = 0
        self.process = process_identity(os.getpid())
        self.stopping = False
        self.last_heartbeat = 0.0
        self.retry_at = 0.0
        self.pending_since = None
        self.last_change = 0.0
        self.last_request = 0.0
        self.not_before = time.monotonic() + cfg.startup_delay_seconds

    def publish(self, healthy):
        root = None
        try:
            info = Path(self.cfg.local_dir).lstat()
            if stat.S_ISDIR(info.st_mode):
                root = [info.st_dev, info.st_ino]
        except OSError:
            pass
        now = time.monotonic()
        write_json(self.cfg.state / "watch-status.json", {
            "fingerprint": self.cfg.fingerprint(),
            "healthy": bool(healthy and self.tree and root == self.tree.root_identity),
            "root": root, "pid": os.getpid(), "process": self.process,
            "heartbeat": now, "session": self.session, "generation": self.generation,
        })
        self.last_heartbeat = now

    def dirty(self):
        self.generation += 1
        self.last_change = time.monotonic()
        if self.pending_since is None:
            self.pending_since = self.last_change

    def rebuild(self):
        self.dirty()
        self.publish(False)
        if self.tree:
            self.tree.close()
            self.tree = None
        try:
            self.tree = InotifyTree(self.cfg.local_dir)
        except (OSError, AttributeError):
            self.retry_at = time.monotonic() + 30
            return
        self.dirty()
        self.publish(True)

    def request_sync(self):
        from .service import systemctl
        if self.manager:
            self.manager.require_healthy()
        # Settings may be changing or scheduling may have just been paused.
        current = Settings.load(self.config_path)
        if not current.schedule_enabled or not current.watch_local or current.fingerprint() != self.cfg.fingerprint():
            return False
        result = systemctl("show", SERVICE, "rclone-local-sync-preview.service",
                           "-p", "ActiveState", "--value")
        if any(value in ("active", "activating", "deactivating") for value in result.stdout.splitlines()):
            return False
        systemctl("start", "--no-block", SERVICE)
        return True

    def tick(self):
        now = time.monotonic()
        if self.manager and now - self.last_maintenance >= 30:
            self.last_maintenance = now
            try:
                health = self.manager.maintain()
                if health["state"] == "removed":
                    self.stopping = True
                    return
            except (SyncError, OSError, ValueError):
                pass
        if self.tree is None and now >= self.retry_at:
            self.rebuild()
        if self.tree:
            try:
                info = Path(self.cfg.local_dir).lstat()
                if [info.st_dev, info.st_ino] != self.tree.root_identity or not stat.S_ISDIR(info.st_mode):
                    self.rebuild()
            except OSError:
                self.rebuild()
        if self.tree:
            changed, rebuild = self.tree.drain()
            if changed:
                self.dirty()
                if rebuild:
                    self.rebuild()
                else:
                    self.publish(True)
        if now - self.last_heartbeat >= 10:
            self.publish(self.tree is not None)
        if (self.tree is not None and self.pending_since is not None and self.cfg.initialized
                and now >= self.not_before
                and now - self.last_request >= 5
                and (now - self.last_change >= self.cfg.watch_debounce_seconds
                     or now - self.pending_since >= 30)):
            try:
                if self.request_sync():
                    self.pending_since = None
            except (SyncError, OSError, ValueError):
                pass  # The timer remains the fallback if systemd is unavailable.
            self.last_request = now

    def stop(self, *_args):
        self.stopping = True

    def run(self):
        old_handlers = {sig: signal.signal(sig, self.stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            self.rebuild()
            while not self.stopping:
                if self.tree:
                    select.select([self.tree.fd], [], [], 1)
                else:
                    time.sleep(1)
                self.tick()
        finally:
            self.publish(False)
            if self.tree:
                self.tree.close()
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
        return 0


def run(config_path, manager=None):
    cfg = Settings.load(config_path)
    cfg.validate()
    if not cfg.schedule_enabled or not cfg.watch_local:
        return 0
    # Only one writer per state directory, including manually launched observers.
    import fcntl
    cfg.state.mkdir(parents=True, exist_ok=True)
    with (cfg.state / "watch.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return Observer(cfg, config_path, manager=manager).run()
