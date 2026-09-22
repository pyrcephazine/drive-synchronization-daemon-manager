from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .config import SERVICE, TIMER, WATCH_SERVICE, Settings, SyncError, atomic_write, config_root, write_json
from .locking import BusyError, file_lock, operation_lock
from .rclone import require_rclone

PREVIEW = "rclone-local-sync-preview.service"
UNITS = (SERVICE, PREVIEW, TIMER, WATCH_SERVICE)
ACTIVE = ("active", "activating", "deactivating")
STATE_FILES = ("identity.json", "setup-approved.json", "baseline-established", "filters.txt", "retired")


def control_root(units=None):
    return (units or config_root() / "systemd/user").parent / "rclone-local-sync-management"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_record(path):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError) as error:
        raise SyncError(f"Cannot read installation record: {path}") from error


def snapshot(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "link", "target": os.readlink(path)}
    if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
        raise SyncError(f"Expected a regular installation file: {path}")
    return {"kind": "file", "text": path.read_text(), "mode": stat.S_IMODE(info.st_mode)}


def text_file(text, mode=0o600):
    return {"kind": "file", "text": text, "mode": mode}


def json_file(value):
    return text_file(json.dumps(value, indent=2) + "\n")


def restore(path, value):
    path = Path(path)
    if value is not None and value["kind"] == "file":
        atomic_write(path, value["text"], value["mode"])
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if value is None:
        path.unlink(missing_ok=True)
    else:
        temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
        try:
            temporary.symlink_to(value["target"])
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def worker_gate(cfg):
    root = control_root()
    if (root / "transaction.json").exists():
        raise BusyError("Installation recovery is pending. Open the app or run --repair.")
    if (root / "removed.json").exists():
        raise SyncError("This connection was removed. Create a new connection in the app.")
    manifest = root / "manifest.json"
    if manifest.exists() and read_record(manifest).get("fingerprint") != cfg.fingerprint():
        raise SyncError("This is not the active connection. Open Settings to manage it.")


class Installation:
    def __init__(self, manager):
        self.manager = manager

    @property
    def root(self):
        return control_root(self.manager.units)

    @property
    def manifest_path(self):
        return self.root / "manifest.json"

    @property
    def journal_path(self):
        return self.root / "transaction.json"

    @property
    def removed_path(self):
        return self.root / "removed.json"

    def links(self):
        m = self.manager
        return {TIMER: m.units / "timers.target.wants" / TIMER,
                WATCH_SERVICE: m.units / "default.target.wants" / WATCH_SERVICE}

    def artifacts(self, cfg):
        m = self.manager
        result = {path: text_file(content, 0o644) for path, content in m.generated_files(cfg).items()}
        result[m.owner_path] = json_file({"config": str(m.config_path)})
        for unit, path in self.links().items():
            enabled = cfg.schedule_enabled and cfg.start_at_login and (unit != WATCH_SERVICE or cfg.watch_local)
            result[path] = {"kind": "link", "target": str(m.units / unit)} if enabled else None
        return result

    def manifest(self, cfg, artifacts):
        return {"version": 1, "generation": uuid.uuid4().hex,
                "config": str(self.manager.config_path), "launcher": str(self.manager.launcher),
                "fingerprint": cfg.fingerprint(), "settings_hash": digest(asdict(cfg)),
                "files": {str(path): {"hash": digest(value), "value": value} for path, value in artifacts.items()}}

    def load_manifest(self):
        manifest = read_record(self.manifest_path)
        if (manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict)
                or manifest.get("config") != str(self.manager.config_path)):
            raise SyncError("Installation ownership is missing or belongs to another settings file.")
        fixed = {self.manager.owner_path, self.manager.autostart,
                 *(self.manager.units / unit for unit in UNITS), *self.links().values()}
        for name, item in manifest["files"].items():
            if Path(name) not in fixed and not self.cleanup_path(Path(name)):
                raise SyncError("The installation manifest contains an unrecognized path.")
            if not isinstance(item, dict) or digest(item.get("value")) != item.get("hash"):
                raise SyncError("The installation manifest is damaged.")
            self.validate_value(item.get("value"))
        return manifest

    @staticmethod
    def validate_value(value):
        if value is None:
            return
        if not isinstance(value, dict):
            raise SyncError("Invalid installation file record.")
        if value.get("kind") == "file":
            if (not isinstance(value.get("text"), str) or not isinstance(value.get("mode"), int)
                    or value["mode"] < 0 or value["mode"] > 0o777):
                raise SyncError("Invalid installation file contents or permissions.")
        elif value.get("kind") == "link":
            if not isinstance(value.get("target"), str) or not value["target"] or "\0" in value["target"]:
                raise SyncError("Invalid installation link.")
        else:
            raise SyncError("Invalid installation file type.")

    def equivalent(self, path, actual, wanted):
        if actual == wanted:
            return True
        if actual and wanted:
            if actual["kind"] == wanted["kind"] == "link":
                return (path.parent / actual["target"]).resolve() == (path.parent / wanted["target"]).resolve()
            if path == self.manager.owner_path and actual["kind"] == wanted["kind"] == "file":
                try:
                    return json.loads(actual["text"]) == json.loads(wanted["text"])
                except ValueError:
                    pass
        return False

    def validate_connection(self, cfg):
        cfg.validate()
        if not self.manager.launcher.is_file():
            raise SyncError("The installed program is missing. Reinstall the app.")
        require_rclone(cfg.rclone_binary)
        identity = read_record(cfg.state / "identity.json")
        if identity.get("fingerprint") != cfg.fingerprint() or (cfg.state / "retired").exists():
            raise SyncError("The saved connection identity does not match. Restore the original settings and history.")
        if cfg.initialized:
            if not (cfg.state / "baseline-established").is_file():
                raise SyncError("The setup history is missing. Restore it from a verified backup.")
            for side in ("path1", "path2"):
                if not list((cfg.state / "bisync").glob(f"*.{side}.lst")):
                    raise SyncError("Sync history is missing. Restore it from a verified backup.")
            marker = Path(cfg.local_dir) / cfg.health_file
            if not marker.is_file() or marker.read_text() != cfg.health_content:
                raise SyncError("The local health marker is missing or changed. Restore the original marker.")
        elif ((cfg.state / "baseline-established").exists()
              or read_record(cfg.state / "setup-approved.json").get("fingerprint") != cfg.fingerprint()):
            raise SyncError("Setup approval is missing. Restore the original connection records.")

    def inspect(self):
        m = self.manager
        issues = []
        def issue(code, path, message, automatic=False, replaceable=False):
            issues.append({"code": code, "path": str(path), "message": message,
                           "automatic": automatic, "replaceable": replaceable})
        if self.removed_path.exists() and not self.journal_path.exists():
            return {"state": "removed", "issues": [], "token": "removed"}
        if self.journal_path.exists():
            issue("transaction", self.journal_path, "An interrupted installation needs recovery.", True)
            return self.report(issues)
        if not m.config_path.exists():
            if self.manifest_path.exists() or any((m.units / unit).exists() for unit in UNITS):
                issue("settings", m.config_path, "Connection settings are missing. Restore the original settings.")
            return self.report(issues, "unconfigured")
        try:
            cfg = Settings.load(m.config_path)
            self.validate_connection(cfg)
            expected = self.artifacts(cfg)
            manifest = self.load_manifest() if self.manifest_path.exists() else None
            if manifest and manifest["fingerprint"] != cfg.fingerprint():
                raise SyncError("The installation belongs to another connection. Restore the original settings.")
            if manifest and manifest.get("settings_hash") != digest(asdict(cfg)):
                raise SyncError("Settings changed outside the app. Restore the saved settings before repairing services.")
            if manifest:
                if manifest.get("launcher") != str(m.launcher):
                    issue("launcher", m.launcher, "This installation uses a different program location. Review repair.", replaceable=True)
                for path, item in manifest["files"].items():
                    if digest(item.get("value")) != item.get("hash"):
                        raise SyncError("The installation manifest is damaged. Restore it from a verified backup.")
            all_match = True
            for path, wanted in expected.items():
                actual = snapshot(path)
                if not self.equivalent(path, actual, wanted):
                    all_match = False
                    is_link = path in self.links().values()
                    code = "missing" if actual is None and wanted is not None else "modified"
                    # Missing enablement links are ambiguous: systemctl disable
                    # removes them too. Respect external disables automatically.
                    automatic = code == "missing" and not is_link and manifest is not None
                    if is_link and actual and actual["kind"] == "link" and not path.exists():
                        automatic = manifest is not None and actual == manifest["files"].get(str(path), {}).get("value")
                    issue("disabled" if is_link and actual is None and wanted else code, path,
                          ("Activation is disabled outside the app." if is_link and actual is None and wanted
                           else "Restore missing file." if code == "missing" else "Review the changed installation file."),
                          automatic, replaceable=True)
            if manifest is None:
                issue("manifest", self.manifest_path, "Register installation ownership." if all_match else
                      "Ownership cannot be established automatically. Review the installation.", all_match,
                      replaceable=all_match)
            for unit in UNITS:
                props = m.properties(unit)
                path = m.units / unit
                if props.get("UnitFileState") in ("masked", "masked-runtime") or props.get("LoadState") == "masked":
                    issue("masked", path, "This unit is masked outside the app.", replaceable=not props.get("DropInPaths"))
                fragment = props.get("FragmentPath")
                if fragment and Path(fragment) not in (path, Path("/dev/null")):
                    issue("override", fragment, "A different service definition takes precedence. Review it outside the app.")
                if props.get("DropInPaths"):
                    issue("override", path, "This unit has external overrides. Review them outside the app.")
                if props.get("NeedDaemonReload") == "yes" or (props.get("LoadState") == "not-found" and path.is_file()):
                    issue("reload", path, "Reload the restored service definitions.", manifest is not None)
                if unit in self.links() and props.get("UnitFileState") == "disabled" and snapshot(self.links()[unit]):
                    issue("disabled", path, "This unit is disabled outside the app.", replaceable=True)
            if manifest:
                for path, item in manifest["files"].items():
                    if Path(path) not in expected and snapshot(path) is not None:
                        matching = digest(snapshot(path)) == item["hash"] and self.cleanup_path(Path(path))
                        issue("obsolete", path, "Archive this obsolete installation file.", matching, matching)
        except (SyncError, OSError, ValueError, TypeError, KeyError) as error:
            issue("invalid", m.config_path, str(error))
        return self.report(issues)

    def report(self, issues, empty="healthy"):
        state = "repairable" if issues and all(i["automatic"] for i in issues) else "review" if issues else empty
        observations = []
        for item in issues:
            try:
                observations.append(snapshot(item["path"]))
            except (OSError, SyncError, ValueError):
                observations.append("unreadable")
        return {"state": state, "issues": issues, "token": digest([issues, observations])}

    def schedules(self):
        return {unit: self.manager.properties(unit).get("ActiveState") == "active" for unit in (TIMER, WATCH_SERVICE)}

    def stop_scheduling(self, extra=()):
        for unit in (TIMER, WATCH_SERVICE, *extra):
            if unit != self.manager.actor:
                self.manager.ctl("stop", unit, check=False)

    def activate(self, schedule):
        for unit, active in schedule.items():
            if unit != self.manager.actor:
                self.manager.ctl("start" if active else "stop", unit)

    def cleanup_path(self, path):
        m = self.manager
        return ((path.parent == m.units and path.suffix in (".service", ".timer"))
                or (path.parent in (m.units / "timers.target.wants", m.units / "default.target.wants")
                    and path.suffix in (".service", ".timer")) or path == m.autostart)

    def allowed_paths(self, journal):
        m = self.manager
        allowed = {m.config_path, m.owner_path, m.autostart, self.manifest_path, self.removed_path,
                   *(m.units / unit for unit in UNITS), *self.links().values()}
        for raw in journal["connections"]:
            cfg = Settings(**raw)
            cfg.validate(check_paths=False)
            allowed.update(cfg.state / name for name in STATE_FILES)
        for value in journal.get("cleanup", []):
            path = Path(value)
            if not self.cleanup_path(path):
                raise SyncError("The recovery journal contains an unrecognized cleanup path.")
            allowed.add(path)
        return allowed

    def validate_journal(self, journal):
        try:
            self._validate_journal(journal)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise SyncError("The recovery journal is damaged. Restore it from a verified backup.") from error

    def _validate_journal(self, journal):
        if journal.get("version") != 1 or journal.get("config") != str(self.manager.config_path):
            raise SyncError("The recovery journal belongs to another installation.")
        if (not re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", journal.get("id", ""))
                or journal.get("phase") not in ("prepared", "committed")
                or journal.get("action") not in ("update", "schedule", "repair", "cleanup", "remove")):
            raise SyncError("The recovery journal has an invalid transaction record.")
        allowed = self.allowed_paths(journal)
        for entry in journal["entries"]:
            if Path(entry["path"]) not in allowed:
                raise SyncError("The recovery journal contains an unrecognized file path.")
            for value in (entry["old"], entry["new"]):
                self.validate_value(value)
        schedules = {TIMER, WATCH_SERVICE, *(Path(p).name for p in journal.get("cleanup", [])
                                            if Path(p).parent == self.manager.units and Path(p).suffix == ".timer")}
        if set(journal["before_schedule"]) - schedules or set(journal["after_schedule"]) - schedules:
            raise SyncError("The recovery journal contains an unrecognized service.")

    def finish(self, journal, forward):
        self.validate_journal(journal)
        self.stop_scheduling(set(journal["before_schedule"]) - {TIMER, WATCH_SERVICE})
        for entry in journal["entries"]:
            # An external edit after the crash must be reviewed, not overwritten.
            actual = snapshot(entry["path"])
            if actual not in (entry["old"], entry["new"]):
                raise SyncError("A file changed after the interrupted update: " + entry["path"])
            restore(entry["path"], entry["new" if forward else "old"])
        self.manager.ctl("daemon-reload")
        self.activate(journal["after_schedule" if forward else "before_schedule"])
        archive = self.root / "archive" / journal["id"]
        write_json(archive / "transaction.json", journal)
        restore(self.journal_path, None)

    def recover(self):
        if not self.journal_path.exists():
            return False
        journal = read_record(self.journal_path)
        self.validate_journal(journal)
        forward = journal["phase"] == "committed" or journal["action"] == "remove"
        if journal["action"] == "schedule":
            self.finish(journal, forward)
        else:
            self.manager.assert_idle(None)
            with operation_lock(), ExitStack() as locks:
                for state in sorted({Settings(**raw).state for raw in journal["connections"]}):
                    locks.enter_context(file_lock(state / "run.lock"))
                self.finish(journal, forward)
        return True

    def transact(self, changes, cfg=None, existing=None, action="update", cleanup=(), schedule=None):
        if self.journal_path.exists():
            raise SyncError("Recover the pending installation change before starting another.")
        before = self.schedules()
        after = schedule if schedule is not None else {
            TIMER: bool(cfg and cfg.schedule_enabled), WATCH_SERVICE: bool(cfg and cfg.schedule_enabled and cfg.watch_local)}
        for path in cleanup:
            path = Path(path)
            if path.parent == self.manager.units and path.suffix == ".timer":
                before[path.name] = self.manager.properties(path.name).get("ActiveState") == "active"
                after[path.name] = False
        journal = {"version": 1, "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8],
                   "config": str(self.manager.config_path), "phase": "prepared", "action": action,
                   "connections": [asdict(c) for c in (existing, cfg) if c is not None],
                   "before_schedule": before, "after_schedule": after, "cleanup": list(map(str, cleanup)),
                   "entries": [{"path": str(path), "old": snapshot(path), "new": value} for path, value in changes.items()]}
        self.validate_journal(journal)
        write_json(self.journal_path, journal)
        try:
            self.stop_scheduling(set(before) - {TIMER, WATCH_SERVICE})
            for entry in journal["entries"]:
                if snapshot(entry["path"]) != entry["old"]:
                    raise SyncError("An installation file changed during the update: " + entry["path"])
                restore(entry["path"], entry["new"])
            self.manager.ctl("daemon-reload")
            journal["phase"] = "committed"
            write_json(self.journal_path, journal)
        except Exception:
            # Keep the durable journal if rollback itself fails. No worker may
            # use the installation until a later recovery completes.
            self.finish(journal, action == "remove")
            raise
        # Never roll back after activation: a worker may already have started.
        self.activate(after)
        write_json(self.root / "archive" / journal["id"] / "transaction.json", journal)
        restore(self.journal_path, None)

    def repair(self, reviewed_token=None):
        report = self.inspect()
        if report["state"] in ("healthy", "removed", "unconfigured"):
            return report
        approved = reviewed_token is not None and reviewed_token == report["token"]
        if reviewed_token is not None and not approved:
            raise SyncError("The installation changed. Review repair again.")
        if any(not i["automatic"] and not (approved and i["replaceable"]) for i in report["issues"]):
            raise SyncError("Review the installation issues before repairing it.")
        m = self.manager
        cfg = Settings.load(m.config_path)
        self.validate_connection(cfg)
        m.assert_idle(cfg)
        with operation_lock(), file_lock(cfg.state / "run.lock"):
            changes = self.artifacts(cfg)
            old = self.load_manifest() if self.manifest_path.exists() else None
            obsolete = []
            if old:
                for path, item in old["files"].items():
                    if Path(path) not in changes and snapshot(path) is not None:
                        if not self.cleanup_path(Path(path)) or digest(snapshot(path)) != item["hash"]:
                            raise SyncError("An obsolete file changed. Review cleanup before removing it.")
                        changes[Path(path)] = None
                        obsolete.append(path)
            manifest = self.manifest(cfg, self.artifacts(cfg))
            changes[self.manifest_path] = json_file(manifest)
            # Preserve observed external stops. Repair does not mean Resume.
            schedule = self.schedules()
            schedule[TIMER] = schedule[TIMER] and cfg.schedule_enabled
            schedule[WATCH_SERVICE] = schedule[WATCH_SERVICE] and cfg.schedule_enabled and cfg.watch_local
            self.transact(changes, cfg, cfg, action="repair", cleanup=obsolete, schedule=schedule)
        self.event("Installation repaired.")
        return self.inspect()

    def event(self, message):
        path = self.root / "activity.log"
        previous = path.read_text()[-16000:] if path.exists() else ""
        atomic_write(path, previous + datetime.now().astimezone().isoformat(timespec="seconds") + " " + message + "\n")

    def cleanup_candidates(self):
        m = self.manager
        result = []
        known = self.load_manifest()["files"] if self.manifest_path.exists() else {}
        for path in sorted(m.units.glob("*")):
            if (path.name in UNITS and not self.removed_path.exists()) or path.suffix not in (".service", ".timer"):
                continue
            try:
                value = snapshot(path)
                related = "rclone" in path.name or (value and value["kind"] == "file" and
                            "rclone-local-sync" in value["text"])
                if not related:
                    continue
                owned = str(path) in known and digest(value) == known[str(path)]["hash"]
                result.append({"path": str(path), "hash": digest(value), "owned": owned,
                               "message": "Obsolete app file" if owned else "Legacy or unrecognized service"})
            except (OSError, SyncError, UnicodeError):
                continue
        return result

    def cleanup(self, selected):
        m = self.manager
        candidates = {item["path"]: item for item in self.cleanup_candidates()}
        changes = {}
        for item in selected:
            current = candidates.get(item["path"])
            if current is None or current["hash"] != item["hash"]:
                raise SyncError("Cleanup files changed. Review cleanup again.")
            path = Path(item["path"])
            worker = path.with_suffix(".service").name if path.suffix == ".timer" else path.name
            if m.properties(worker).get("ActiveState") in ACTIVE:
                raise BusyError("Stop the selected legacy service before cleaning it up: " + path.name)
            changes[path] = None
            for directory in (m.units / "timers.target.wants", m.units / "default.target.wants"):
                link = directory / path.name
                value = snapshot(link)
                if value is not None:
                    if value["kind"] != "link" or (link.parent / value["target"]).resolve() != path.resolve():
                        raise SyncError("An activation link points elsewhere. Review it outside the app: " + str(link))
                    changes[link] = None
        if not changes:
            return
        cfg = Settings.load(m.config_path) if m.config_path.exists() else None
        m.assert_idle(cfg)
        with operation_lock():
            # Removing owned activation links disables these exact units. No
            # broad systemctl disable is allowed to remove unreviewed links.
            self.transact(changes, cfg, cfg, action="cleanup", cleanup=changes,
                          schedule=self.schedules())
        self.event("Archived obsolete services: " + ", ".join(Path(i["path"]).name for i in selected))

    def remove(self):
        m = self.manager
        cfg = Settings.load(m.config_path) if m.config_path.exists() else None
        m.assert_idle(cfg)
        manifest = self.load_manifest() if self.manifest_path.exists() else None
        if manifest is not None and cfg is None:
            raise SyncError("Connection settings are missing. Restore the original settings before removing this connection.")
        if manifest is None and cfg is None and self.removed_path.exists():
            removed = read_record(self.removed_path)
            return [name for name in removed.get("kept_files", []) if snapshot(name) is not None]
        if manifest is None and cfg is not None:
            raise SyncError("Register installation ownership with Repair before removing the connection.")
        changes = {self.removed_path: json_file({"config": str(m.config_path), "removed": True}),
                   m.config_path: None, self.manifest_path: None}
        leftovers = []
        if manifest:
            for name, item in manifest["files"].items():
                path = Path(name)
                if path not in self.artifacts(cfg) and not self.cleanup_path(path):
                    raise SyncError("The manifest contains an unrecognized installation file.")
                value = snapshot(path)
                if value is None or digest(value) == item["hash"]:
                    changes[path] = None
                else:
                    leftovers.append(str(path))
            changes[cfg.state / "retired"] = text_file("Connection removed.\n")
        changes[self.removed_path] = json_file({"config": str(m.config_path), "removed": True, "kept_files": leftovers})
        with operation_lock():
            self.transact(changes, None, cfg, action="remove", cleanup=[p for p in changes if self.cleanup_path(p)])
        self.event("Connection removed." + (" Modified files kept: " + ", ".join(leftovers) if leftovers else ""))
        return leftovers
