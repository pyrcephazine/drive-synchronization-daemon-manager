from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import ast
import time
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import unicodedata
import uuid

from .config import SyncError, write_json
from .engine import (Runner, bisync_command, check_identity, check_rclone,
                     common_flags, filter_text, preflight, run_lock, timestamp)
from .transfers import EVENT, history_token, unquote


def groups(entries):
    result = defaultdict(list)
    for entry in entries:
        result[unicodedata.normalize("NFC", entry["Path"])].append(entry)
    return result


def inventory_conflicts(local, remote, limit, health_file):
    left, right = groups(local), groups(remote)
    rows = {}
    for key in left.keys() | right.keys():
        a, b = left[key], right[key]
        if key == health_file:
            continue
        kind = None
        if len(a) > 1 or len(b) > 1:
            kind = "Duplicate name"
        elif a and b:
            if a[0].get("IsDir") != b[0].get("IsDir"):
                kind = "File / folder conflict"
            elif not a[0].get("IsDir") and limit and max(a[0]["Size"], b[0]["Size"]) > limit:
                kind = "Size limit conflict"
        if kind:
            rows[key] = {"path": key, "kind": kind, "local": a, "remote": b}
    # An otherwise unique child of duplicate folders is still ambiguous.
    ambiguous = {key for key, row in rows.items() if row["kind"] in ("Duplicate name", "File / folder conflict")}
    for key, row in rows.items():
        if any(str(parent) in ambiguous for parent in PurePosixPath(key).parents):
            row["blocked"] = "Rename the conflicting parent folders, then refresh before replacing this file."
    return rows


def allowed(cfg, row, side):
    if side not in ("local", "remote") or row.get("blocked"):
        return False
    if row["kind"] in ("Duplicate name", "File / folder conflict"):
        return False
    for name in ("local", "remote"):
        entries = row[name]
        if len(entries) != 1 or entries[0].get("IsDir") or entries[0].get("IsLink"):
            return False
        entry = entries[0]
        path = entry["Path"]
        parts = PurePosixPath(path).parts
        if (not parts or path.startswith("/") or ".." in parts or "." in path.split("/")
                or "" in path.split("/") or "\x00" in path or path == cfg.health_file
                or entry.get("Size", -1) < 0):
            return False
        # Google document exports are representations, not replaceable originals.
        if entry.get("MimeType", "").startswith("application/vnd.google-apps.") or path.endswith(".url"):
            return False
    source = row[side][0]
    return not cfg.max_size_bytes or source["Size"] <= cfg.max_size_bytes


def reported_snapshot(cfg, detail):
    if detail.get("phase") != "error":
        return None
    report = detail.get("conflicts", {})
    if report.get("fingerprint") == cfg.fingerprint() and isinstance(report.get("rows"), list):
        rows = deepcopy(report["rows"])
    else:
        # Older installed workers only recorded the diagnostic. Parse exactly
        # our own quoted-name format; never infer overwrite permission from it.
        message = detail.get("message", "")
        prefix, suffix = "Duplicate file or folder name: ", ". Rename one of the matching items before syncing."
        if not message.startswith(prefix) or not message.endswith(suffix):
            return None
        try:
            path = ast.literal_eval(message[len(prefix):-len(suffix)])
        except (ValueError, SyntaxError):
            return None
        if not isinstance(path, str):
            return None
        rows = [{"path": path, "kind": "Duplicate name", "local": [], "remote": []}]
    for row in rows:
        if row["kind"] not in ("Duplicate name", "File / folder conflict"):
            row["blocked"] = "Checking the current versions before enabling replacement…"
    return {"fingerprint": cfg.fingerprint(), "history": None, "checked_at": detail.get("finished"),
            "rows": rows, "reported": True, "note": "Reported by the last sync. Refresh to check the current files."}


def hash_rows(cfg, runner, rows):
    def item(side, entry):
        root = cfg.local_dir if side == "local" else cfg.remote
        full = root.rstrip("/") + "/" + entry["Path"]
        result = json.loads(runner.call([cfg.rclone_binary, "lsjson", full, "--stat", "--hash", *common_flags(cfg)]))
        # --stat paths are relative to the item's parent, not the sync root.
        result["Path"] = entry["Path"]
        for key in ("Size", "ModTime", "ID", "IsDir"):
            if key in entry and result.get(key) != entry[key]:
                raise SyncError("The file changed during the conflict check. Refresh Conflicts.")
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        for row in rows:
            if not any(allowed(cfg, row, side) for side in ("local", "remote")):
                continue
            futures = {side: pool.submit(item, side, row[side][0]) for side in ("local", "remote")}
            for side, future in futures.items():
                row[side] = [future.result()]


def scan(cfg, runner=None, previous=None, force=False):
    cfg.validate()
    runner = runner or Runner()
    from .changes import observe, ProbeUnavailable
    observation = None
    same = previous and previous.get("fingerprint") == cfg.fingerprint()
    with tempfile.TemporaryDirectory(prefix="drive-conflict-snapshot-") as temporary:
        scratch = Path(temporary)
        with run_lock(cfg):
            check_identity(cfg)
            if not cfg.initialized:
                raise SyncError("Wait for the first sync to finish before resolving conflicts.")
            before = history_token(cfg)
            # Copy only bookkeeping under the lock, not the cloud queries.
            shutil.copytree(cfg.state / "bisync", scratch / "bisync")
            for name in ("identity.json", "initialized", "filters.txt", "filters.txt.md5"):
                if (cfg.state / name).exists():
                    shutil.copy2(cfg.state / name, scratch / name)
        try:
            observation, changed, _ = observe(cfg, runner, previous.get("observation") if same else None, allow_listing=False)
            if (same and not force and not changed and not previous.get("reported") and not previous.get("stale")
                    and previous.get("history") == before
                    and 0 <= time.monotonic() - previous.get("planned_at", 0) < cfg.full_scan_interval_seconds
                    and history_token(cfg) == before):
                result = deepcopy(previous)
                result.update(observation=observation, checked_at=timestamp())
                return result
        except (ProbeUnavailable, SyncError, OSError, ValueError, TypeError):
            pass
        result = _scan(replace(cfg, state_dir=str(scratch)), runner)
        result.update(history=before, observation=observation, planned_at=time.monotonic(),
                      stale=history_token(cfg) != before)
        return result


def _scan(cfg, runner):
    check_identity(cfg)
    if not cfg.initialized:
        raise SyncError("Wait for the first sync to finish before resolving conflicts.")
    check_rclone(cfg, runner)
    if cfg.excludes and (cfg.state / "filters.txt").read_text() != filter_text(cfg):
        raise SyncError("Restore the saved exclusion rules before resolving conflicts.")
    before = history_token(cfg)
    local, remote = preflight(cfg, runner, inspect_conflicts=True)
    rows = inventory_conflicts(local, remote, cfg.max_size_bytes, cfg.health_file)
    note = None
    if not rows:
        with tempfile.TemporaryDirectory(prefix="drive-conflicts-") as temporary:
            scratch = Path(temporary)
            shutil.copytree(cfg.state / "bisync", scratch / "bisync")
            for name in ("filters.txt", "filters.txt.md5"):
                if (cfg.state / name).exists():
                    shutil.copy2(cfg.state / name, scratch / name)
            logfile = scratch / "plan.jsonl"
            args = bisync_command(cfg, workdir=scratch / "bisync", preview=True, filters=scratch / "filters.txt")
            args += ["--use-json-log", f"--log-file={logfile}", "--log-level=INFO", "--stats=0", "--retries=1"]
            runner.call(args, timeout=max(120, cfg.io_timeout_seconds * 2))
            left, right = groups(local), groups(remote)
            events = [json.loads(line)["msg"] for line in logfile.read_text().splitlines()]
            if "Bisync successful" not in events:
                raise SyncError("Conflict preview did not finish. Check Activity and refresh.")
            for message in events:
                match = EVENT.match(message)
                if match and match[1] in ("WARNING", "!WARNING") and match[2] == "New or changed in both paths":
                    key = unicodedata.normalize("NFC", unquote(match[3]))
                    if key != cfg.health_file:
                        rows[key] = {"path": key, "kind": "Both versions changed", "local": left[key], "remote": right[key]}
    else:
        note = "Resolve these blocking conflicts, then refresh to check for files changed on both sides."
    hash_rows(cfg, runner, rows.values())
    return {"fingerprint": cfg.fingerprint(), "history": before, "checked_at": timestamp(),
            "rows": sorted(rows.values(), key=lambda row: row["path"].casefold()), "note": note}


def resolve(cfg, snapshot, row, side, runner=None):
    cfg.validate()
    runner = runner or Runner()
    with run_lock(cfg):
        if snapshot["fingerprint"] != cfg.fingerprint() or snapshot["history"] != history_token(cfg):
            raise SyncError("Sync state changed. Refresh Conflicts and review the file again.")
        current = _scan(cfg, runner)
        if row not in snapshot["rows"] or row not in current["rows"]:
            raise SyncError("The file changed since it was reviewed. Refresh Conflicts and try again.")
        if not allowed(cfg, row, side):
            raise SyncError("This conflict needs manual review. Rename duplicate items or adjust the file size limit, then refresh.")
        local_path = Path(cfg.local_dir) / row["local"][0]["Path"]
        root = Path(cfg.local_dir).resolve()
        if any(path.is_symlink() for path in (local_path, *local_path.parents)) or not local_path.is_file() or root not in local_path.resolve().parents:
            raise SyncError("The local file is missing, outside the sync folder, or a symbolic link. Refresh Conflicts.")
        remote_path = cfg.remote.rstrip("/") + "/" + row["remote"][0]["Path"]
        source, destination = (str(local_path), remote_path) if side == "local" else (remote_path, str(local_path))
        replaced_side = "remote" if side == "local" else "local"
        # The backup may exceed the sync size limit; it still needs disk space.
        required = row[replaced_side][0]["Size"] + cfg.min_free_bytes
        backup_root = Path(cfg.backup_dir)
        backup_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(backup_root).free < required:
            raise SyncError("Not enough local disk space to back up the version being replaced.")
        backup = Path(cfg.backup_dir) / "conflicts" / uuid.uuid4().hex
        backup.mkdir(parents=True, mode=0o700)
        write_json(backup / "resolution.json", {"path": row["path"], "keep": side, "created": timestamp()})
        flags = common_flags(cfg) + ["--retries=1"]
        # A failed backup must never proceed to replacement. Keep backups on
        # local disk for both directions and outside the synchronized tree.
        runner.call([cfg.rclone_binary, "copyto", destination, str(backup / "original"), "--immutable", *flags])
        # A large backup can take time. Recheck both reviewed versions before
        # replacing anything; remote providers do not offer a shared edit lock.
        local_now, remote_now = preflight(cfg, runner, inspect_conflicts=True)
        fresh = {"path": row["path"], "kind": row["kind"],
                 "local": groups(local_now)[row["path"]], "remote": groups(remote_now)[row["path"]]}
        hash_rows(cfg, runner, [fresh])
        for name, entries in (("local", local_now), ("remote", remote_now)):
            if fresh[name] != row[name]:
                raise SyncError(f"The file changed while making its backup. Refresh Conflicts. Backup saved in {backup}.")
        from .changes import invalidate
        invalidate(cfg)
        try:
            runner.call([cfg.rclone_binary, "copyto", source, destination, "--ignore-times", *flags])
        except (SyncError, OSError) as error:
            raise SyncError(f"Replacement did not finish. The previous version is saved in {backup}. {error}") from error
        return backup
