from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid


APP_NAME = "Drive Synchronization Daemon Manager"
# Keep the established service and storage identifiers so upgrades retain history.
SERVICE = "rclone-local-sync.service"
TIMER = "rclone-local-sync.timer"
WATCH_SERVICE = "rclone-local-sync-watch.service"


class SyncError(Exception):
    pass


def config_root():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def data_root():
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def default_config_path():
    return config_root() / "rclone-local-sync/config.json"


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def find_rclone():
    candidates = list((data_root() / "rclone-local-sync").glob("rclone-v*/rclone"))
    def version_key(path):
        return tuple(int(n) for n in re.findall(r"\d+", path.parent.name))
    for path in sorted(candidates, key=version_key, reverse=True):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("rclone") or "/usr/bin/rclone"


@dataclass
class Settings:
    schema: int = 1
    name: str = "My drive"
    profile_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    local_dir: str = field(default_factory=lambda: str(Path.home() / "drive"))
    remote: str = ""
    rclone_binary: str = field(default_factory=find_rclone)
    rclone_config: str = field(default_factory=lambda: str(config_root() / "rclone/rclone.conf"))
    state_dir: str = ""
    backup_dir: str = field(default_factory=lambda: str(data_root() / "rclone-local-sync/backups"))
    health_file: str = ""
    health_content: str = ""
    interval_seconds: int = 30
    startup_delay_seconds: int = 60
    watch_local: bool = True
    watch_debounce_seconds: int = 2
    full_scan_interval_seconds: int = 3600
    schedule_enabled: bool = True
    start_at_login: bool = True
    tray_at_login: bool = True
    close_to_tray: bool = True
    notify_errors: bool = True
    notify_success: bool = False
    max_size_bytes: int = 2_000_000_000
    excludes: list[str] = field(default_factory=list)
    google_docs: str = "url"
    conflict_resolve: str = "newer"
    max_delete_percent: int = 50
    min_free_bytes: int = 1_000_000_000
    compare: str = "size,modtime"
    track_renames: bool = True
    create_empty_dirs: bool = True
    fast_list: bool = True
    transfers: int = 4
    checkers: int = 8
    upload_kib: int = 0
    download_kib: int = 0
    drive_chunk_mib: int = 32
    retries: int = 3
    retry_delay_seconds: int = 30
    connect_timeout_seconds: int = 60
    io_timeout_seconds: int = 300
    cpu_nice: int = 10
    io_priority: int = 7
    log_level: str = "INFO"
    adopted_legacy: bool = False

    def __post_init__(self):
        if not self.state_dir:
            self.state_dir = str(state_root() / "rclone-local-sync" / self.profile_id)
        if not self.health_file:
            self.health_file = ".rclone-local-sync-health-" + self.profile_id
        if not self.health_content:
            self.health_content = APP_NAME + " health marker. Keep on both sides.\n" + self.profile_id + "\n"

    @classmethod
    def load(cls, path):
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema") != 1:
                raise SyncError("This app cannot read the settings file format. Your original file is unchanged.")
            return cls(**raw)
        except (OSError, ValueError, TypeError) as error:
            raise SyncError(f"Could not open the settings file: {error}") from error

    def save(self, path):
        write_json(path, asdict(self))

    @property
    def state(self):
        return Path(self.state_dir)

    @property
    def initialized(self):
        # Any completed-sync record prevents accidental first-time setup.
        # Repair validates the history before restoring missing marker files.
        return any((self.state / name).is_file() for name in
                   ("initialized", "baseline-established", "history-checkpoint.json"))

    def identity(self):
        # A changed view of the files must never silently reuse old deletion history.
        return {key: getattr(self, key) for key in (
            "local_dir", "remote", "rclone_config", "max_size_bytes", "excludes",
            "google_docs", "compare", "health_file", "health_content", "profile_id",
        )}

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True).encode()).hexdigest()

    def validate(self, check_paths=True):
        for key, value in asdict(self).items():
            if isinstance(value, str) and key != "health_content" and any(ord(c) < 32 for c in value):
                raise SyncError(f"Remove line breaks and other control characters from {key}.")
        for key in ("schedule_enabled", "start_at_login", "tray_at_login", "close_to_tray", "watch_local",
                    "notify_errors", "notify_success", "track_renames", "create_empty_dirs",
                    "fast_list", "adopted_legacy"):
            if type(getattr(self, key)) is not bool:
                raise SyncError(f"Set {key} to true or false in the settings file.")
        ranges = {
            "interval_seconds": (30, 604800), "startup_delay_seconds": (5, 86400),
            "watch_debounce_seconds": (1, 60), "full_scan_interval_seconds": (300, 604800),
            "max_size_bytes": (0, 10**15), "min_free_bytes": (0, 10**15),
            "max_delete_percent": (1, 100), "transfers": (1, 64), "checkers": (1, 128),
            "upload_kib": (0, 10**9), "download_kib": (0, 10**9),
            "drive_chunk_mib": (1, 1024), "retries": (1, 20), "retry_delay_seconds": (0, 3600),
            "connect_timeout_seconds": (5, 3600), "io_timeout_seconds": (30, 86400),
            "cpu_nice": (0, 19), "io_priority": (0, 7),
        }
        for key, (low, high) in ranges.items():
            if type(getattr(self, key)) is not int or not low <= getattr(self, key) <= high:
                raise SyncError(f"Set {key} to a whole number between {low} and {high}.")
        if self.max_size_bytes and self.max_size_bytes < 4096:
            raise SyncError("Set Maximum file size to at least 4,096 bytes, or 0 for no limit. This keeps the sync health marker included.")
        if self.drive_chunk_mib & (self.drive_chunk_mib - 1):
            raise SyncError("Set Google Drive upload chunk size to a power of 2, such as 16, 32, or 64 MiB.")
        if self.google_docs not in ("url", "skip"):
            raise SyncError("For Google Docs, Sheets, and Slides, choose Browser shortcuts (.url) or Skip Google documents.")
        if self.conflict_resolve not in ("newer", "none", "path1", "path2"):
            raise SyncError("Choose how to handle conflicting versions under When both versions change in Safety settings.")
        if self.compare not in ("size,modtime", "size,modtime,checksum"):
            raise SyncError("Choose a file comparison method under Compare files by in Safety settings.")
        if self.log_level not in ("INFO", "DEBUG", "NOTICE"):
            raise SyncError("Choose Standard, Important events only, or Detailed troubleshooting under Activity log detail in Reliability settings.")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.profile_id):
            raise SyncError("The connection ID must contain 1 to 64 letters, numbers, underscores, or hyphens.")
        if not re.fullmatch(r"[.a-zA-Z0-9_-]{1,120}", self.health_file) or self.health_file in (".", ".."):
            raise SyncError("The sync health marker needs a valid filename: 1 to 120 letters, numbers, periods, underscores, or hyphens. The names '.' and '..' are not allowed.")
        if not self.health_content or len(self.health_content) > 4096:
            raise SyncError("The sync health marker must contain 1 to 4,096 characters. Restore the original marker contents before syncing.")
        if not re.fullmatch(r"[\w][\w .-]*:.*", self.remote, flags=re.UNICODE):
            raise SyncError("Select a connected rclone remote under Remote folder, such as google: or google:Documents.")
        if any(part == ".." for part in self.remote.split(":", 1)[1].split("/")):
            raise SyncError("Remove '..' from the remote folder path. Enter the full path within the connected remote.")
        if not isinstance(self.excludes, list) or len(self.excludes) > 200:
            raise SyncError("Enter no more than 200 patterns under Files and folders to skip.")
        for pattern in self.excludes:
            if not isinstance(pattern, str) or not pattern or any(ord(c) < 32 for c in pattern):
                raise SyncError("Enter one nonempty rclone pattern per line under Files and folders to skip, such as *.tmp.")
        paths = {}
        for key in ("local_dir", "rclone_binary", "rclone_config", "state_dir", "backup_dir"):
            path = Path(getattr(self, key))
            if not path.is_absolute():
                raise SyncError(f"Enter a full path starting with / for {key}.")
            paths[key] = path.resolve()
        local = paths["local_dir"]
        if local in (Path("/"), Path.home(), Path("/home")):
            raise SyncError("Choose a separate folder for synced files. Your home folder, /home, and / cannot be used.")
        for key in ("state_dir", "backup_dir", "rclone_config", "rclone_binary"):
            path = paths[key]
            if local == path or local in path.parents or (key in ("state_dir", "backup_dir") and path in local.parents):
                raise SyncError(f"Choose a local folder that is separate from {key}. Sync files, app settings, history, and backups need separate locations.")
        if local == config_root().resolve() or local in config_root().resolve().parents:
            raise SyncError("Choose a local folder that does not contain app settings.")
        if check_paths:
            if not paths["rclone_binary"].is_file() or not os.access(paths["rclone_binary"], os.X_OK):
                raise SyncError("Install rclone 1.66 or later before opening the app.")
            if not paths["rclone_config"].is_file():
                raise SyncError("The rclone configuration file is missing. Use Connect account in Settings.")
            if Path(self.local_dir).is_symlink():
                raise SyncError("The local folder is a symbolic link. Choose the actual folder on your local disk.")


def detect_legacy():
    root = Path.home()
    wrapper = root / ".local/bin/rclone-gdrive-sync"
    state = root / ".local/state/rclone-gdrive-sync"
    try:
        script = wrapper.read_text()
        normalized_script = script.replace(f"rclone_sync_root={root}", "rclone_sync_root={HOME}")
        if hashlib.sha256(normalized_script.encode()).hexdigest() != "5b13c4ecda1153145fedabc0a29f4703613d7c68b9093def98f442aea4e12d16":
            return None
        expected = (
            f"rclone_sync_root={root}", 'rclone_sync_state="$rclone_sync_root/.local/state/rclone-gdrive-sync"',
            'rclone_sync_local="$rclone_sync_root/drive"', "rclone_sync_remote=google:",
            "rclone_sync_limit=2000000000", "--drive-export-formats=url",
            "--conflict-resolve=newer --conflict-loser=num", "--max-delete=50",
            "--compare=size,modtime", "--check-filename=.rclone-gdrive-sync-health",
        )
        if not all(part in script for part in expected) or not (state / "initialized").is_file():
            return None
        if not list((state / "bisync").glob("*.path1.lst")) or not list((state / "bisync").glob("*.path2.lst")):
            return None
        marker = root / "drive/.rclone-gdrive-sync-health"
        cfg = Settings(name="Google Drive", local_dir=str(root / "drive"), remote="google:",
                       state_dir=str(state), health_file=marker.name, health_content=marker.read_text(),
                       adopted_legacy=True)
        cfg.validate()
        return cfg
    except (OSError, SyncError):
        return None
