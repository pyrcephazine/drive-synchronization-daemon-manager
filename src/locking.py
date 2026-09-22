from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import socket

from .config import SyncError, config_root


class BusyError(SyncError):
    pass


@contextmanager
def file_lock(path, shared=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never unlink lock files: waiters must keep referring to the same inode.
    with path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BusyError("Another operation is running. Wait for it to finish and try again.") from error
        yield lock


@contextmanager
def process_lock(purpose):
    # Abstract sockets have no directory entry to delete, and disappear when
    # the owning process exits. All configurations for this user share them.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as lock:
        try:
            lock.bind(f"\0drive-synchronization-manager:{os.getuid()}:{purpose}")
        except OSError as error:
            raise BusyError("Another operation is running. Wait for it to finish and try again.") from error
        yield


@contextmanager
def operation_lock():
    with process_lock("worker"), file_lock(config_root() / "rclone-local-sync/operation.lock"):
        yield


def credential_lock(path, shared=False):
    key = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
    return file_lock(config_root() / "rclone-local-sync/config-locks" / key, shared=shared)
