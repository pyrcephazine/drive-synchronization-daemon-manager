import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import SyncError, write_json


class ProbeUnavailable(Exception):
    pass


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def process_identity(pid):
    # PID plus kernel start time and boot ID prevents trusting a reused PID.
    started = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + started


def watcher_observation(cfg):
    data = read_json(cfg.state / "watch-status.json")
    try:
        age = time.monotonic() - data["heartbeat"]
        root = Path(cfg.local_dir).lstat()
        if (not cfg.watch_local or data["healthy"] is not True or not 0 <= age < 30
                or type(data["generation"]) is not int or data["generation"] < 0
                or not isinstance(data["session"], str) or not data["session"]
                or data["fingerprint"] != cfg.fingerprint()
                or data["process"] != process_identity(data["pid"])
                or data["root"] != [root.st_dev, root.st_ino]
                or not stat.S_ISDIR(root.st_mode)):
            return None
        return {"kind": "inotify", "session": data["session"], "generation": data["generation"]}
    except (KeyError, ValueError, TypeError, OSError, IndexError):
        return None


def local_observation(cfg):
    watched = watcher_observation(cfg)
    if watched is not None:
        return watched
    # Recovery path: enumerate metadata only, never follow symlinks or read
    # file contents. ctime/inode also catch atomic saves and restored mtimes.
    root = Path(cfg.local_dir)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ProbeUnavailable("The local folder is unavailable.")
    digest = hashlib.sha256()
    pending = [root]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                metadata = entry.stat(follow_symlinks=False)
                values = (str(Path(entry.path).relative_to(root)), metadata.st_mode,
                          metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
                          metadata.st_dev, metadata.st_ino)
                digest.update(repr(values).encode("utf-8", errors="surrogateescape") + b"\0")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
    return {"kind": "stat", "digest": digest.hexdigest(), "root": [info.st_dev, info.st_ino]}


def remote_settings(cfg):
    name = cfg.remote.split(":", 1)[0]
    # Config/environment overlays and wrapper backends need rclone's own
    # listing semantics; do not guess which account their tokens describe.
    prefix = "RCLONE_CONFIG_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_"
    if any(key.startswith((prefix, "RCLONE_DRIVE_")) for key in os.environ):
        raise ProbeUnavailable("Remote environment overrides require ordinary polling.")
    try:
        result = subprocess.run([cfg.rclone_binary, "config", "show", name,
                                 f"--config={cfg.rclone_config}", "--ask-password=false"],
                                capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            raise ProbeUnavailable("Could not read the selected remote's configuration.")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(result.stdout)
        return dict(parser[name])
    except (OSError, subprocess.TimeoutExpired, configparser.Error, KeyError):
        raise ProbeUnavailable("Incremental remote checking is unavailable.") from None


class DriveChanges:

    def __init__(self, cfg, runner, settings):
        self.cfg, self.runner, self.settings = cfg, runner, settings
        if settings.get("type") != "drive" or settings.get("service_account_file") or settings.get("service_account_credentials"):
            raise ProbeUnavailable("This remote uses ordinary listing-based polling.")
        self.scope = {"drive": settings.get("team_drive", ""),
                      "spaces": "appDataFolder" if settings.get("root_folder_id") == "appDataFolder" else "drive"}
        try:
            token = json.loads(settings.get("token", "{}"))
            identity = {**settings, "token": token.get("refresh_token") or token.get("access_token")}
            self.scope["account"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        except (ValueError, TypeError, AttributeError):
            raise ProbeUnavailable("Remote credentials are unavailable.") from None

    def _request(self, endpoint, params):
        query = {"supportsAllDrives": "true", **params}
        if self.scope["drive"]:
            query["driveId"] = self.scope["drive"]
        for attempt in range(2):
            try:
                token = json.loads(self.settings.get("token", "{}"))["access_token"]
                if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
                    raise ValueError("Invalid access token")
                request = Request("https://www.googleapis.com/drive/v3/changes" + endpoint + "?" + urlencode(query),
                                  headers={"Authorization": "Bearer " + token})
                with urlopen(request, timeout=min(30, self.cfg.io_timeout_seconds)) as response:
                    data = json.load(response)
                if not isinstance(data, dict):
                    raise ValueError("Invalid response")
                return data
            except HTTPError as error:
                if error.code != 401 or attempt:
                    raise ProbeUnavailable(f"Google Drive change check failed (HTTP {error.code}).") from None
                # about is inexpensive and causes rclone to refresh/persist its
                # token using the configured client, without duplicating OAuth.
                try:
                    from .engine import common_flags
                    self.runner.call([self.cfg.rclone_binary, "about", self.cfg.remote, "--json",
                                      *common_flags(self.cfg), "--retries=1"], timeout=60)
                    self.settings = remote_settings(self.cfg)
                except SyncError:
                    raise ProbeUnavailable("Remote authentication could not be refreshed.") from None
            except (OSError, URLError, ValueError, KeyError, TypeError):
                raise ProbeUnavailable("Google Drive change check is unavailable.") from None
        raise ProbeUnavailable("Google Drive change check is unavailable.")

    @staticmethod
    def _token(data, key):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ProbeUnavailable("Google Drive returned an incomplete change checkpoint.")
        return value

    def observe(self, previous):
        if not previous or previous.get("kind") != "drive" or previous.get("scope") != self.scope:
            data = self._request("/startPageToken", {})
            token = self._token(data, "startPageToken")
            return {"kind": "drive", "scope": self.scope, "cursor": token}, True
        cursor = self._token(previous, "cursor")
        changed, visited = False, set()
        # Bound work for a very busy account. Exceeding the bound falls back to
        # full sync; an incomplete page sequence can never mean "unchanged".
        for _ in range(100):
            if cursor in visited:
                raise ProbeUnavailable("Google Drive repeated a change page.")
            visited.add(cursor)
            data = self._request("", {"pageToken": cursor, "pageSize": 1000,
                                     "includeRemoved": "true", "includeItemsFromAllDrives": "true",
                                     "restrictToMyDrive": "false",
                                     "spaces": self.scope["spaces"],
                                     "fields": "nextPageToken,newStartPageToken,changes(fileId,removed)"})
            changes = data.get("changes")
            if not isinstance(changes, list):
                raise ProbeUnavailable("Google Drive returned an incomplete change page.")
            changed = changed or bool(changes)
            if data.get("nextPageToken"):
                cursor = self._token(data, "nextPageToken")
            else:
                token = self._token(data, "newStartPageToken")
                return {"kind": "drive", "scope": self.scope, "cursor": token}, changed
        raise ProbeUnavailable("Google Drive change history needs a full reconciliation.")


def remote_observation(cfg, runner, previous, allow_listing=True):
    from .engine import common_flags
    try:
        return (*DriveChanges(cfg, runner, remote_settings(cfg)).observe(previous), "Google Drive change feed")
    except ProbeUnavailable:
        if not allow_listing:
            raise
        # Unsupported authentication/backends, revoked cursors and API errors
        # all use an authoritative recursive listing. Never infer no changes
        # from the absence of remote notifications.
        args = [cfg.rclone_binary, "lsjson", cfg.remote, "--recursive", "--no-mimetype",
                "--max-size=off", "--min-size=off", *common_flags(cfg)]
        if "checksum" in cfg.compare:
            args.append("--hash")
        if cfg.excludes:
            args.append(f"--filter-from={cfg.state / 'filters.txt'}")
        entries = json.loads(runner.call(args, timeout=max(600, cfg.io_timeout_seconds * 2)))
        if not isinstance(entries, list) or any(not isinstance(entry, dict) or "Path" not in entry
                                                or "Size" not in entry or "ModTime" not in entry for entry in entries):
            raise ProbeUnavailable("Remote listing is incomplete.")
        records = sorted(json.dumps(entry, sort_keys=True, ensure_ascii=True) for entry in entries)
        digest = hashlib.sha256("\n".join(records).encode()).hexdigest()
        observation = {"kind": "listing", "digest": digest}
        return observation, observation != previous, "Remote file listing"


def observe(cfg, runner, previous=None, allow_listing=True):
    if not isinstance(previous, dict) or not isinstance(previous.get("remote"), dict):
        previous = {}
    local = local_observation(cfg)
    remote, remote_changed, mode = remote_observation(cfg, runner, previous.get("remote"), allow_listing=allow_listing)
    # Keep the BEFORE value if local events arrive while the remote is queried.
    return {"local": local, "remote": remote}, local != previous.get("local") or remote_changed, mode


def history_signature(cfg):
    paths = [cfg.state / name for name in ("initialized", "identity.json", "filters.txt", "filters.txt.md5")]
    paths.extend(sorted((cfg.state / "bisync").glob("*")))
    values = []
    for path in paths:
        try:
            info = path.stat()
            values.append((path.name, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            values.append((path.name, None))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def force_token(cfg):
    try:
        return (cfg.state / "force-sync").read_text()
    except FileNotFoundError:
        return ""


def check(cfg, runner):
    baseline = read_json(cfg.state / "change-baseline.json")
    valid = baseline.get("schema") == 1 and baseline.get("fingerprint") == cfg.fingerprint()
    prior = baseline.get("observation") if valid else None
    if not isinstance(prior, dict) or not isinstance(prior.get("remote"), dict):
        prior, valid = None, False
    local = local_observation(cfg)
    if valid and local != prior.get("local"):
        # Reconciliation will list both sides anyway. Retain the old remote
        # cursor so changes arriving meanwhile are still checked next time.
        observation, changed, mode = {"local": local, "remote": prior["remote"]}, True, "Local changes"
    else:
        remote, remote_changed, mode = remote_observation(cfg, runner, prior.get("remote") if prior else None)
        observation = {"local": local, "remote": remote}
        changed = not prior or local != prior.get("local") or remote_changed
    forced = force_token(cfg)
    now = time.time()
    try:
        age = now - float(baseline["reconciled_at"])
        overdue = not 0 <= age < cfg.full_scan_interval_seconds
    except (KeyError, ValueError, TypeError):
        overdue = True
    needed = (not valid or changed or overdue or forced != baseline.get("force")
              or history_signature(cfg) != baseline.get("history"))
    return {"schema": 1, "fingerprint": cfg.fingerprint(), "observation": observation,
            "force": forced, "reconciled_at": baseline.get("reconciled_at", 0),
            "needed": needed, "mode": mode}


def commit(cfg, checkpoint, reconciled=False):
    data = dict(checkpoint)
    if reconciled:
        data["reconciled_at"] = time.time()
    data["history"] = history_signature(cfg)
    write_json(cfg.state / "change-baseline.json", data)


def invalidate(cfg):
    # Leave bisync's history untouched. A failed or interrupted attempt must
    # never reuse an older "clean" optimization checkpoint.
    write_json(cfg.state / "change-baseline.json", {"schema": 0})
