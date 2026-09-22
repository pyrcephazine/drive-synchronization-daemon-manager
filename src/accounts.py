import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading

from .config import SyncError, atomic_write, state_root
from .engine import Runner
from .locking import BusyError, credential_lock, file_lock, operation_lock
from .rclone import require_rclone


def read_config(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SyncError("Choose a regular rclone configuration file, not a symbolic link.") from error
    with os.fdopen(fd, encoding="utf-8", newline="") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SyncError("Choose a regular rclone configuration file.")
        content = stream.read()
    if content.startswith("RCLONE_ENCRYPT_V"):
        raise SyncError("Unlock the rclone configuration file before connecting an account.")
    return content


def cleanup_staging():
    work = state_root() / "rclone-local-sync/account-setup"
    try:
        with file_lock(work.with_suffix(".lock")):
            if work.exists():
                shutil.rmtree(work)
    except BusyError:
        pass


class AccountSetup:
    def __init__(self, binary, config_path):
        self.binary = binary
        supplied = Path(config_path).expanduser()
        if not supplied.is_absolute():
            raise SyncError("Choose an absolute path for the rclone configuration file.")
        self.target = supplied.parent.resolve() / supplied.name
        self.runner = Runner()
        self.guard = threading.RLock()
        self.closed = False
        self.lock = None
        self.work = (state_root() / "rclone-local-sync/account-setup").resolve()
        self.path = self.work / "rclone.conf"
        if self.target == self.path or self.work in self.target.parents:
            raise SyncError("Choose a configuration file outside the account setup folder.")
        self.name = None
        self.state = None

    def open(self):
        with self.guard:
            if self.closed:
                raise SyncError("Account setup was cancelled.")
            require_rclone(self.binary)
            session_lock = file_lock(self.work.with_suffix(".lock"))
            self.lock_handle = session_lock.__enter__()
            self.lock = session_lock
            try:
                # Recover unfinished staging from an interrupted app. This path
                # belongs only to this wizard and is never a user's config file.
                if self.work.exists():
                    shutil.rmtree(self.work)
                self.work.mkdir(mode=0o700, parents=True)
                with credential_lock(self.target):
                    self.original = read_config(self.target)
                atomic_write(self.path, self.original or "")
                return json.loads(self.call(["config", "providers"]))
            except Exception:
                self.close()
                raise

    def call(self, args, **answers):
        # rclone's environment-backed flags keep passwords out of argv. No RC
        # server, socket, shell, credentials in app settings, or diagnostic log.
        env = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
        env.update({"RCLONE_" + key.upper(): value for key, value in answers.items()})
        return self.runner.call(
            [self.binary, *args, "--config", str(self.path),
             "--ask-password=false", "--log-level=ERROR"], env=env, private=True,
            pass_fds=(self.lock_handle.fileno(),))

    def question(self, args, **answers):
        try:
            result = json.loads(self.call(args + ["--non-interactive"], **answers))
        except ValueError as error:
            raise SyncError("Could not read the account setup response.") from error
        if not isinstance(result, dict) or not isinstance(result.get("State"), str):
            raise SyncError("rclone returned an incomplete setup response.")
        self.state = result["State"]
        return result

    def remotes(self):
        return [remote.removesuffix(":") for remote in self.call(["listremotes"]).splitlines()]

    def start(self, name, provider):
        with self.guard:
            if self.name is not None:
                raise SyncError("Cancel this setup before starting another account.")
            if not re.fullmatch(r"[\w][\w .-]*", name) or name.endswith(" "):
                raise SyncError("Use a name containing letters, numbers, spaces, dots, or hyphens.")
            remotes = self.remotes()
            if name.casefold() in {remote.casefold() for remote in remotes}:
                raise SyncError("An account already uses this name. Choose another name.")
            result = self.question(["config", "create", name, provider, "--all"])
            self.name = name
            return result

    def answer(self, value):
        with self.guard:
            if not self.name or not self.state:
                raise SyncError("Start account setup before answering a question.")
            return self.question(["config", "update", self.name, "--continue"], state=self.state, result=value)

    def save(self):
        with self.guard, operation_lock(), credential_lock(self.target):
            if self.closed or not self.name or self.state != "":
                raise SyncError("Complete account setup before saving.")
            if self.name not in self.remotes():
                raise SyncError("Account setup did not create the account. Try again.")
            if read_config(self.target) != self.original:
                raise SyncError("The rclone configuration changed during setup. Cancel and reconnect the account.")
            content = read_config(self.path)
            if not content:
                raise SyncError("Account setup returned an empty configuration.")
            atomic_write(self.target, content)
            return self.name + ":"

    def close(self):
        self.runner.interrupt()
        # Stop OAuth waits before removing staging or releasing the session lock.
        with self.runner.lock:
            for child in self.runner.children:
                try:
                    child.terminate()
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                except ProcessLookupError:
                    pass
        with self.guard:
            self.closed = True
            if self.lock is not None:
                shutil.rmtree(self.work, ignore_errors=True)
                self.lock.__exit__(None, None, None)
                self.lock = None
