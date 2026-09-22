from dataclasses import asdict
from datetime import datetime
from functools import wraps
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from .config import (APP_NAME, SERVICE, TIMER, WATCH_SERVICE, Settings, SyncError, atomic_write, config_root,
                     detect_legacy)
from .engine import (BusyError, Runner, bisync_command, check_rclone, configure_logger,
                     filter_text, preflight, timestamp)
from .locking import file_lock, operation_lock, process_lock
from .installation import (ACTIVE, PREVIEW, UNITS, Installation, digest, json_file,
                           snapshot, text_file)


ANY_SETTINGS = object()


def command(args, check=True, timeout=20):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyncError(f"Could not run {args[0]}: {error}") from error
    if check and result.returncode:
        raise SyncError((result.stderr or result.stdout).strip() or f"{args[0]} could not complete the action (exit code {result.returncode}).")
    return result


def systemctl(*args, check=True):
    return command(["systemctl", "--user", *args], check=check)


def unit_quote(value):
    # systemd performs its own expansion even without a shell.
    value = str(value).replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"')
    if any(ord(c) < 32 for c in value):
        raise SyncError("Remove line breaks and other control characters from service file paths.")
    return '"' + value + '"'


def desktop_quote(value):
    value = str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + value + '"'


def render_service(cfg, config_path, launcher, preview=False):
    mode = "--preview" if preview else "--worker --check-changes"
    return f"""# Managed by {APP_NAME}. Change settings in the application.
[Unit]
Description={APP_NAME}: {'preview' if preview else 'background sync'}
Documentation=https://rclone.org/bisync/
After=network-online.target
Wants=network-online.target
{'Conflicts=rclone-gdrive-sync.service rclone-vfs-gdrive.service' if cfg.adopted_legacy else ''}

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {unit_quote(launcher)} {mode} --config {unit_quote(config_path)}
SuccessExitStatus=75
Nice={cfg.cpu_nice}
IOSchedulingClass=best-effort
IOSchedulingPriority={cfg.io_priority}
UMask=0022
TimeoutStartSec=infinity
TimeoutStopSec=2min
KillSignal=SIGINT
KillMode=mixed
SyslogIdentifier=rclone-local-sync
"""


def render_timer(cfg):
    return f"""# Managed by {APP_NAME}. Closing the application does not stop this timer.
[Unit]
Description={APP_NAME}: automatic sync schedule

[Timer]
OnStartupSec={cfg.startup_delay_seconds}s
OnUnitInactiveSec={cfg.interval_seconds}s
AccuracySec=5s
Unit={SERVICE}

[Install]
WantedBy=timers.target
"""


def render_watcher(cfg, config_path, launcher):
    return f"""# Managed by {APP_NAME}. Change settings in the application.
[Unit]
Description={APP_NAME}: watch local changes

[Service]
Type=simple
ExecStart=/usr/bin/python3 {unit_quote(launcher)} --watch --config {unit_quote(config_path)}
Nice={cfg.cpu_nice}
IOSchedulingClass=best-effort
IOSchedulingPriority={cfg.io_priority}
UMask=0077
Restart=on-failure
RestartSec=10s
TimeoutStopSec=15s

[Install]
WantedBy=default.target
"""


def desktop_entry(launcher, config_path=None, tray=False):
    suffix = " --tray" if tray else ""
    if config_path:
        suffix += " --config " + desktop_quote(config_path)
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Comment=Keep local and remote folders in sync with rclone
Exec={desktop_quote(launcher)}{suffix}
Icon=rclone-local-sync
Terminal=false
Categories=Network;FileTransfer;
StartupNotify=false
StartupWMClass=DriveSynchronizationDaemonManager
"""


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with process_lock("manager"), file_lock(self.units / ".rclone-local-sync-manager.lock"):
            self.installation.recover()
            self.assert_owner()
            return method(self, *args, **kwargs)
    return wrapped


class Manager:
    def __init__(self, config_path, launcher, actor=None):
        self.config_path = Path(config_path).resolve()
        self.launcher = Path(launcher).resolve()
        self.units = config_root() / "systemd/user"
        self.autostart = config_root() / "autostart/rclone-local-sync.desktop"
        self.actor = actor
        self.installation = Installation(self)

    @staticmethod
    def ctl(*args, **kwargs):
        return systemctl(*args, **kwargs)

    def generated_files(self, cfg):
        return {
            self.units / SERVICE: render_service(cfg, self.config_path, self.launcher),
            self.units / TIMER: render_timer(cfg),
            self.units / PREVIEW: render_service(cfg, self.config_path, self.launcher, preview=True),
            self.units / WATCH_SERVICE: render_watcher(cfg, self.config_path, self.launcher),
            self.autostart: desktop_entry(self.launcher, self.config_path, tray=True) + ("" if cfg.tray_at_login else "Hidden=true\n"),
        }

    def inspect_installation(self):
        return self.installation.inspect()

    def maintain(self, retry=False, reviewed_token=None):
        # One attempt per observed incident, shared by GUI, watcher and CLI.
        with process_lock("manager"), file_lock(self.units / ".rclone-local-sync-manager.lock"):
            report = self.inspect_installation()
            if report["state"] in ("healthy", "removed", "unconfigured"):
                return report
            attempt = self.installation.root / "repair-attempt.json"
            previous = read_json(attempt)
            if not retry and (report["state"] != "repairable" or previous.get("token") == report["token"]):
                if previous.get("token") == report["token"]:
                    report["error"] = previous.get("error")
                return report
            write = {"token": report["token"], "error": "Repair did not finish. Select Retry repair."}
            from .config import write_json
            try:
                # Busy incidents are deferred, not counted as failed attempts.
                self.assert_idle(None)
                with operation_lock():
                    write_json(attempt, write)
                self.installation.recover()
                self.assert_owner()
                result = self.installation.repair(reviewed_token)
                attempt.unlink(missing_ok=True)
                return result
            except BusyError:
                attempt.unlink(missing_ok=True)
                raise
            except (SyncError, OSError, ValueError) as error:
                write["token"] = self.inspect_installation()["token"]
                write["error"] = str(error)
                write_json(attempt, write)
                if retry:
                    raise
                result = self.inspect_installation()
                result["error"] = str(error)
                return result

    def require_healthy(self):
        health = self.inspect_installation()
        if health["state"] != "healthy":
            raise SyncError("Sync needs repair. Open the app or run --repair.")

    @serialized
    def cleanup(self, selected):
        return self.installation.cleanup(selected)

    @serialized
    def remove_connection(self):
        return self.installation.remove()

    @property
    def owner_path(self):
        return self.units / ".rclone-local-sync-connection.json"

    def assert_owner(self):
        if self.owner_path.exists():
            owner = read_json(self.owner_path).get("config")
            if owner != str(self.config_path):
                raise SyncError("Another settings file manages the active connection. Open the app with that file before replacing it.")
        else:
            # Upgrade older installations without silently redirecting their
            # single set of service units to another --config path.
            service = self.units / SERVICE
            if service.exists():
                for line in service.read_text().splitlines():
                    if line.startswith("ExecStart=") and " --config " in line:
                        if line.rsplit(" --config ", 1)[1] != unit_quote(self.config_path):
                            raise SyncError("Another settings file manages the active connection. Open that connection in Settings.")

    def properties(self, unit):
        output = systemctl("show", unit, "--no-pager", "-p", "ActiveState", "-p", "SubState",
                           "-p", "Result", "-p", "LoadState", "-p", "UnitFileState",
                           "-p", "NextElapseUSecMonotonic", "-p", "ExecMainStatus", "-p", "FragmentPath",
                           "-p", "DropInPaths", "-p", "NeedDaemonReload", check=False)
        if output.returncode and not output.stdout.strip():
            raise SyncError(output.stderr.strip() or "Cannot connect to the background sync service. Check that your systemd user session is running.")
        return dict(line.split("=", 1) for line in output.stdout.splitlines() if "=" in line)

    def status(self, cfg, health=None):
        health = health or self.inspect_installation()
        service = self.properties(SERVICE)
        timer = self.properties(TIMER)
        preview = self.properties("rclone-local-sync-preview.service")
        detail = read_json(cfg.state / "status.json")
        active = service.get("ActiveState") in ("active", "activating", "deactivating")
        previewing = preview.get("ActiveState") in ("active", "activating", "deactivating")
        if health["state"] not in ("healthy", "unconfigured", "removed"):
            phase, label = "repair", "Sync needs repair"
        elif health["state"] in ("removed", "unconfigured"):
            phase, label = "unconfigured", "Set up a sync connection"
        elif active:
            phase = "syncing"
            label = "Setting up sync…" if not cfg.initialized else "Checking for changes…" if detail.get("phase") == "checking" else "Syncing files…"
        elif previewing:
            phase, label = "preview", "Previewing changes…"
        elif service.get("Result") not in (None, "", "success") or detail.get("phase") == "error":
            phase, label = "error", "Sync needs attention"
        elif not cfg.initialized:
            phase, label = "setup", "Ready for first sync"
        elif timer.get("ActiveState") == "active":
            phase, label = "ready", "Automatic sync is on"
        else:
            phase, label = "paused", "Automatic sync paused"
        last = None
        try:
            last = datetime.fromtimestamp((cfg.state / "last-success").stat().st_mtime).astimezone().strftime("%b %d, %H:%M:%S")
        except OSError:
            pass
        return {"phase": phase, "label": label, "running": active, "previewing": previewing,
                "scheduled": timer.get("ActiveState") == "active", "last_success": last,
                "installation": health,
                "detail": detail, "service": service, "timer": timer}

    def assert_idle(self, cfg):
        for unit in (SERVICE, "rclone-local-sync-preview.service"):
            if unit != self.actor and self.properties(unit).get("ActiveState") in ACTIVE:
                raise BusyError("Wait for the sync or preview to finish before changing settings. "
                                "To stop it now, use Sync → Stop sync or preview.")
        if cfg is not None and cfg.adopted_legacy and self.properties("rclone-gdrive-sync.service").get("ActiveState") in ("active", "activating", "deactivating"):
            raise BusyError("The existing Google Drive sync is still running. Wait for it to finish, then try again.")

    @serialized
    def apply(self, cfg, expected=ANY_SETTINGS):
        if expected is not ANY_SETTINGS:
            current = self.config_path.read_text() if self.config_path.exists() else None
            if current != expected:
                raise SyncError("Settings changed while this window was open. Close it and reopen Settings.")
        cfg.validate()
        check_rclone(cfg, Runner())
        existing = Settings.load(self.config_path) if self.config_path.exists() else None
        if existing and existing.state_dir == cfg.state_dir and existing.fingerprint() != cfg.fingerprint():
            raise SyncError("To change the folders, size limit, Google document setting, file exclusions, or comparison method, "
                            "use File → New connection with an empty local folder. Your existing files and sync history are kept.")
        if cfg.initialized and not (cfg.state / "identity.json").exists():
            known = detect_legacy() if cfg.adopted_legacy else None
            if known is None:
                raise SyncError("This sync history cannot be imported automatically. Use File → New connection with an empty local folder. Your files are unchanged.")
            expected, requested = known.identity(), cfg.identity()
            expected.pop("profile_id")
            requested.pop("profile_id")
            if expected != requested or known.state_dir != cfg.state_dir:
                raise SyncError("Import the existing connection with its original settings first. Its file filters must match the saved sync history.")
        if not cfg.initialized and (cfg.state / "baseline-established").exists():
            raise SyncError("The file that records completed setup is missing from this connection. "
                            "Review Activity, then restore the setup marker and sync history.")
        if (cfg.state / "retired").exists():
            raise SyncError("This connection has been replaced. Create a new connection with an empty local folder.")
        if not cfg.initialized and not (cfg.state / "setup-started").exists():
            local = Path(cfg.local_dir)
            if local.exists() and (not local.is_dir() or any(local.iterdir())):
                raise SyncError("Choose an empty local folder for the new connection. Your existing folders are unchanged.")
        if existing and self.installation.manifest_path.exists():
            self.installation.validate_connection(existing)
        self.assert_idle(existing or cfg)
        with operation_lock(), file_lock((existing or cfg).state / "run.lock"):
            importing = cfg.adopted_legacy and not (cfg.state / "identity.json").exists()
            legacy_active = importing and self.properties("rclone-gdrive-sync.timer").get("ActiveState") == "active"
            try:
                if cfg.adopted_legacy and not (cfg.state / "identity.json").exists():
                    systemctl("stop", "rclone-gdrive-sync.timer")
                    runner = Runner(configure_logger(cfg, preview=True))
                    preflight(cfg, runner)
                    with tempfile.TemporaryDirectory(prefix="import-preview-", dir=cfg.state) as scratch:
                        work = Path(scratch) / "bisync"
                        shutil.copytree(cfg.state / "bisync", work)
                        runner.call(bisync_command(cfg, workdir=work, preview=True), stream=True)
                self._write_configuration(cfg, existing)
            except Exception:
                journal = read_json(self.installation.journal_path)
                if legacy_active and journal.get("phase") != "committed":
                    systemctl("start", "rclone-gdrive-sync.timer", check=False)
                raise
        if cfg.adopted_legacy:
            systemctl("disable", "rclone-gdrive-sync.timer")
        if cfg.schedule_enabled and not cfg.initialized:
            systemctl("start", "--no-block", SERVICE)
        return cfg

    def _write_configuration(self, cfg, existing):
        install = self.installation
        changes = install.artifacts(cfg)
        manifest = install.load_manifest() if install.manifest_path.exists() else None
        original_files = install.artifacts(existing) if existing else {}
        for path in changes:
            actual = snapshot(path)
            if actual is None:
                continue
            if manifest:
                known = manifest["files"].get(str(path))
                if not known or digest(actual) != known["hash"]:
                    raise SyncError(f"Installation file changed. Review repair first: {path}")
            elif not install.equivalent(path, actual, original_files.get(path)) and not install.equivalent(path, actual, changes[path]):
                raise SyncError(f"A file from another setup already exists: {path}. Review repair first.")
        changes[self.config_path] = json_file(asdict(cfg))
        identity = cfg.state / "identity.json"
        if not identity.exists():
            changes[identity] = json_file({"fingerprint": cfg.fingerprint(), "settings": cfg.identity()})
        elif read_json(identity).get("fingerprint") != cfg.fingerprint():
            raise SyncError("These settings do not match the saved connection. Create a new connection to change folders or filters.")
        if not cfg.initialized:
            changes[cfg.state / "setup-approved.json"] = json_file({"fingerprint": cfg.fingerprint()})
        elif not (cfg.state / "baseline-established").exists():
            if identity.exists():
                raise SyncError("The setup history is missing. Restore it from a verified backup.")
            changes[cfg.state / "baseline-established"] = text_file("Imported established baseline " + timestamp() + "\n")
        if cfg.excludes and not (cfg.state / "filters.txt").exists():
            changes[cfg.state / "filters.txt"] = text_file(filter_text(cfg))
        if existing and existing.state != cfg.state:
            changes[existing.state / "retired"] = text_file("Replaced " + timestamp() + "\n")
        changes[install.manifest_path] = json_file(install.manifest(cfg, install.artifacts(cfg)))
        changes[install.removed_path] = None
        install.transact(changes, cfg, existing)

    @serialized
    def pause(self):
        return self.change_schedule(False)

    @serialized
    def resume(self):
        self.require_healthy()
        return self.change_schedule(True)

    def change_schedule(self, enabled):
        cfg = Settings.load(self.config_path)
        previous = Settings.load(self.config_path)
        cfg.schedule_enabled = enabled
        install = self.installation
        if not install.manifest_path.exists() and enabled:
            raise SyncError("Register this installation with Repair before changing its schedule.")
        manifest = install.load_manifest() if install.manifest_path.exists() else None
        artifacts = install.artifacts(cfg)
        changes = {self.config_path: json_file(asdict(cfg))}
        for path in install.links().values():
            actual = snapshot(path)
            known = manifest["files"].get(str(path)) if manifest else None
            legacy_link = (not manifest and actual and actual["kind"] == "link"
                           and (path.parent / actual["target"]).resolve() == (self.units / path.name).resolve())
            if actual is not None and not legacy_link and (not known or digest(actual) != known["hash"]):
                raise SyncError("An activation link changed. Review repair first.")
            changes[path] = artifacts[path]
        if manifest:
            manifest["files"].update({str(path): {"hash": digest(artifacts[path]), "value": artifacts[path]}
                                      for path in install.links().values()})
            manifest["settings_hash"] = digest(asdict(cfg))
            manifest["generation"] = uuid.uuid4().hex
            changes[install.manifest_path] = json_file(manifest)
        install.transact(changes, cfg, previous, action="schedule")
        return cfg

    @serialized
    def sync_now(self):
        self.require_healthy()
        cfg = Settings.load(self.config_path)
        # A unique request survives a concurrent running worker and is only
        # acknowledged by a later successful reconciliation.
        atomic_write(cfg.state / "force-sync", uuid.uuid4().hex + "\n")
        systemctl("reset-failed", SERVICE, check=False)
        systemctl("start", "--no-block", SERVICE)

    @serialized
    def preview(self):
        self.require_healthy()
        cfg = Settings.load(self.config_path)
        self.assert_idle(cfg)
        systemctl("start", "--no-block", "rclone-local-sync-preview.service")

    @serialized
    def stop_current(self):
        systemctl("stop", "--no-block", SERVICE, "rclone-local-sync-preview.service")

    def logs(self, cfg, preview=False):
        events = self.installation.root / "activity.log"
        prefix = events.read_text()[-16000:] + "\n" if events.exists() else ""
        path = cfg.state / ("preview.log" if preview else "sync.log")
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 60000))
                return prefix + stream.read().decode("utf-8", errors="replace")
        if cfg.adopted_legacy:
            return command(["journalctl", "--user", "-u", "rclone-gdrive-sync.service", "-n", "100", "--no-pager"], check=False).stdout
        return prefix or "No activity recorded yet."
