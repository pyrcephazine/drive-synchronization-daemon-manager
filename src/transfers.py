import ast
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import unicodedata

from .config import SyncError
from .engine import (Runner, bisync_command, check_identity, check_rclone,
                     indexed, preflight, run_lock, timestamp, common_flags)


def format_bytes(value):
    if value is None or value < 0:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1000 or unit == "PB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1000


def format_time(value):
    if not value:
        return "Not checked yet"
    return datetime.fromisoformat(value).astimezone().strftime("%b %d, %H:%M:%S")


def totals(entries):
    sizes = [entry.get("size") for entry in entries]
    return {"files": len(sizes), "bytes": sum(n for n in sizes if n is not None and n >= 0),
            "unknown": sum(n is None or n < 0 for n in sizes)}


def format_totals(value):
    if value is None:
        return "Not checked yet"
    count = value["files"]
    size = format_bytes(value["bytes"])
    if value["unknown"]:
        size += f" · Files with unknown size: {value['unknown']}"
    return f"{count:,} {'file' if count == 1 else 'files'} · {size}"


def eligible(entries, cfg):
    return {key: item for key, item in indexed(entries).items()
            if not item.get("IsDir") and item["Path"] != cfg.health_file
            and (not cfg.max_size_bytes or item["Size"] <= cfg.max_size_bytes)}


def inventory_totals(entries):
    return totals([{"size": item["Size"]} for item in entries.values()])


def history_token(cfg):
    paths = [cfg.state / name for name in ("status.json", "last-success", "identity.json", "initialized")]
    paths.extend(sorted((cfg.state / "bisync").glob("*")))
    values = []
    for path in paths:
        try:
            stat = path.stat()
            values.append((path.name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            values.append((path.name, None))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def unquote(value):
    # rclone uses Go strconv.Quote for paths containing whitespace, quotes or
    # controls. Its emitted escapes are also Python string-literal escapes.
    # literal_eval does not evaluate expressions or execute the path as code.
    if value.startswith('"'):
        try:
            decoded = ast.literal_eval(value)
        except (ValueError, SyntaxError) as error:
            raise SyncError("Could not read a file name in the transfer preview. Use Sync → Preview changes to see the details.") from error
        if not isinstance(decoded, str):
            raise SyncError("The transfer preview contains an invalid file name. Use Sync → Preview changes to see the details.")
        return decoded
    return value


QUOTED = r'("(?:[^"\\]|\\.)*")'
ENDPOINTS = re.compile(r'^Synching Path1 ' + QUOTED + r' with Path2 ' + QUOTED + r'$')
EVENT = re.compile(r'^-\s+(!?Path[12]|!?WARNING)\s+(.+?)\s+- (.*)$')


def parse_plan(text, local, remote, health_file):
    roots, success, conflict = {}, False, None
    skipped_transfer = False
    result = {"uploads": [], "downloads": [], "other": []}
    seen = set()
    for line in text.splitlines():
        try:
            event = json.loads(line)
            msg = event["msg"]
        except (ValueError, KeyError, TypeError) as error:
            raise SyncError("Could not read the transfer preview from rclone. Select Refresh to try again.") from error
        header = ENDPOINTS.match(msg)
        if header:
            roots = {"1": unquote(header[1]), "2": unquote(header[2])}
        if msg == "Bisync successful":
            success = True
        if event.get("skipped") in ("copy", "delete", "rename", "move into backup dir"):
            skipped_transfer = True
        match = EVENT.match(msg)
        if not match:
            if re.match(r'^-\s+\S+\s+Queue ', msg):
                raise SyncError("The transfer list cannot display this preview format. Use Sync → Preview changes to see the details.")
            continue
        tag, action, path = match.groups()
        if tag in ("WARNING", "!WARNING") and action == "New or changed in both paths":
            conflict = unquote(path)
        if not action.startswith("Queue "):
            continue
        if action == "Queue copy to Path2":
            group, side, inventory = "uploads", "2", local
        elif action == "Queue copy to Path1":
            group, side, inventory = "downloads", "1", remote
        elif action == "Queue delete" and tag in ("Path1", "Path2"):
            group, side = "other", tag[-1]
            inventory = local if side == "1" else remote
        else:
            raise SyncError("The transfer list cannot display the preview format from this rclone version. Use Sync → Preview changes to see the details.")
        full_path = unquote(path)
        root = roots.get(side)
        if not root or not full_path.startswith(root):
            raise SyncError("Could not identify the destination folder for a transfer. Use Sync → Preview changes to see the details.")
        name = full_path[len(root):]
        if name == health_file:
            continue
        # indent() strips the internal ! prefix, including on conflict queues.
        # Numbered destinations do not exist yet during a dry run; their size
        # belongs to the original file announced by the preceding warning.
        conflict_copy = bool(conflict and unicodedata.normalize("NFC", name) not in inventory
                             and re.fullmatch(re.escape(conflict) + r'\.conflict\d+', name))
        original = conflict if conflict_copy else name
        item = inventory.get(unicodedata.normalize("NFC", original))
        # A queue can contain empty directories as well as files.
        operation = "Delete" if group == "other" else "Conflict copy" if conflict_copy else "Copy"
        if item and item.get("IsDir"):
            if operation == "Copy":
                operation = "Create folder"
            elif operation == "Delete":
                operation = "Delete folder"
            group = "other"
        size = item.get("Size") if item else None
        key = group, side, name
        if key not in seen:
            seen.add(key)
            result[group].append({"path": name, "source_path": original,
                                  "size": size if size is not None and size >= 0 else None,
                                  "action": operation, "side": "Local" if side == "1" else "Remote"})
    if not roots or not success:
        raise SyncError("The transfer preview did not finish. Select Refresh to try again.")
    if skipped_transfer and not any(result.values()):
        raise SyncError("Some pending changes cannot be shown in this list. Use Sync → Preview changes to see the details.")
    for entries in result.values():
        entries.sort(key=lambda item: item["path"].casefold())
    return result


def remote_capacity(cfg, runner):
    try:
        output = runner.call([cfg.rclone_binary, "about", cfg.remote, "--json",
                              *common_flags(cfg), "--log-level=ERROR", "--retries=1"], timeout=60)
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError("rclone returned an unexpected storage report.")
        # Missing values mean unsupported/unlimited, NOT zero.
        return {key: data[key] for key in ("total", "used", "free", "trashed", "other")
                if isinstance(data.get(key), int) and not isinstance(data[key], bool) and data[key] >= 0}
    except (SyncError, OSError, ValueError) as error:
        return {"quota_error": "Remote storage information is unavailable. " + str(error)}


def scan(cfg, runner=None, previous=None, force=False, cache=False):
    cfg.validate()
    runner = runner or Runner()
    # Performance/safety choices can alter a preview even when they do not
    # change the immutable bisync identity (notably conflict resolution).
    plan_settings = hashlib.sha256(json.dumps(asdict(cfg), sort_keys=True).encode()).hexdigest()
    observation, plan_history = None, None
    if cache and cfg.initialized:
        from .changes import ProbeUnavailable, history_signature, observe
        try:
            with run_lock(cfg):
                check_identity(cfg)
                before = history_token(cfg)
                plan_history = history_signature(cfg)
            same_connection = isinstance(previous, dict) and previous.get("fingerprint") == cfg.fingerprint()
            prior = previous.get("observation") if same_connection else None
            observation, changed, _mode = observe(cfg, runner, prior)
            if (same_connection and not force and not changed and not previous.get("error")
                    and previous.get("plan_settings") == plan_settings
                    and previous.get("plan_history") == plan_history
                    and 0 <= time.monotonic() - previous.get("planned_at", 0) < cfg.full_scan_interval_seconds
                    and history_token(cfg) == before):
                snapshot = deepcopy(previous)
                snapshot.update(observation=observation, checked_at=timestamp(), history=before, stale=False)
                disk = shutil.disk_usage(cfg.local_dir)
                if disk.free < cfg.min_free_bytes:
                    raise SyncError("Not enough local free space for sync.")
                snapshot["local"].update(total=disk.total, used=disk.used, free=disk.free)
                # Quota is independent of the folder's pending changes.
                eligible_totals = snapshot["remote"].get("eligible")
                snapshot["remote"] = remote_capacity(cfg, runner)
                if eligible_totals is not None:
                    snapshot["remote"]["eligible"] = eligible_totals
                snapshot["stale"] = history_token(cfg) != before
                return snapshot
        except (ProbeUnavailable, SyncError, OSError, ValueError, TypeError):
            observation, plan_history = None, None
    snapshot = {"fingerprint": cfg.fingerprint(), "started_at": timestamp(), "checked_at": None,
                "uploads": [], "downloads": [], "other": [], "local": {}, "remote": {},
                "error": None, "stale": False, "history": None,
                "observation": observation, "plan_history": plan_history, "plan_settings": plan_settings,
                "planned_at": time.monotonic()}
    try:
        disk = shutil.disk_usage(cfg.local_dir)
        snapshot["local"] = {"total": disk.total, "used": disk.used, "free": disk.free,
                             "reserve": cfg.min_free_bytes}
    except OSError as error:
        snapshot["local"]["error"] = str(error)
    with ThreadPoolExecutor(max_workers=1) as pool:
        capacity = pool.submit(remote_capacity, cfg, runner)
        try:
            if not cfg.initialized:
                raise SyncError("Wait for the first sync to finish before checking pending changes.")
            check_rclone(cfg, runner)
            with tempfile.TemporaryDirectory(prefix="drive-transfers-") as temporary:
                scratch = Path(temporary)
                with run_lock(cfg):
                    check_identity(cfg)
                    snapshot["history"] = history_token(cfg)
                    shutil.copytree(cfg.state / "bisync", scratch / "bisync")
                    for name in ("filters.txt", "filters.txt.md5"):
                        if (cfg.state / name).exists():
                            shutil.copy2(cfg.state / name, scratch / name)
                # The lock is now released. Only this disposable history is used.
                local_entries, remote_entries = preflight(replace(cfg, state_dir=str(scratch)), runner)
                local, remote = indexed(local_entries), indexed(remote_entries)
                snapshot["local"]["eligible"] = inventory_totals(eligible(local_entries, cfg))
                snapshot["remote"]["eligible"] = inventory_totals(eligible(remote_entries, cfg))
                logfile = scratch / "plan.jsonl"
                args = bisync_command(cfg, workdir=scratch / "bisync", preview=True,
                                      filters=scratch / "filters.txt")
                # Do not inherit a DEBUG log or long retry schedule for a UI scan.
                args += ["--use-json-log", f"--log-file={logfile}", "--log-level=INFO",
                         "--stats=0", "--retries=1", "--retries-sleep=0s"]
                try:
                    runner.call(args, timeout=max(120, cfg.io_timeout_seconds * 2))
                except SyncError as error:
                    detail = ""
                    if logfile.exists():
                        messages = []
                        for line in logfile.read_text().splitlines():
                            try:
                                event = json.loads(line)
                                if event.get("level") in ("error", "critical", "warning"):
                                    messages.append(event["msg"])
                            except (ValueError, KeyError):
                                continue
                        detail = "\n".join(messages[-4:])
                    raise SyncError(detail or str(error)) from error
                snapshot.update(parse_plan(logfile.read_text(), local, remote, cfg.health_file))
        except (SyncError, OSError, ValueError) as error:
            snapshot["error"] = str(error)
        snapshot["remote"].update(capacity.result())
    if snapshot["history"]:
        snapshot["stale"] = history_token(cfg) != snapshot["history"]
    snapshot["checked_at"] = timestamp()
    return snapshot
