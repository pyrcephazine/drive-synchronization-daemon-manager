from pathlib import Path

from .config import SyncError, data_root
from .installation import digest, read_record, restore, snapshot
from .locking import process_lock, file_lock


def uninstall_user(manager):
    with process_lock("manager"), file_lock(manager.units / ".rclone-local-sync-manager.lock"):
        manager.installation.recover()
        manager.assert_owner()
        return _uninstall_user(manager)


def _uninstall_user(manager):
    app = data_root() / "rclone-local-sync/app"
    inventory_path = app / "installed-files.json"
    inventory = read_record(inventory_path)
    if inventory.get("version") != 1 or not isinstance(inventory.get("files"), dict):
        raise SyncError("Reinstall the app to register its files before uninstalling it.")
    outside = {Path.home() / ".local/bin" / name for name in ("drive-synchronization-daemon-manager", "drive-synchronization-manager", "rclone-local-sync")}
    outside.update({data_root() / "applications/rclone-local-sync.desktop",
                    data_root() / "icons/hicolor/scalable/apps/rclone-local-sync.svg"})
    for name in inventory["files"]:
        path = Path(name)
        if not path.is_absolute() or ".." in path.parts or (app not in path.parents and path not in outside):
            raise SyncError("The installation file list contains an unrecognized path.")
    leftovers = manager.installation.remove()
    if leftovers:
        # A changed service may still reference this executable. Leave the app
        # installed so cleanup never strands that reference.
        return leftovers
    manager.ctl("daemon-reload")
    from .service import command
    command(["gapplication", "action", "io.github.localdrivesync.App", "quit"], check=False)
    directories = {app}
    for name, expected in inventory["files"].items():
        path = Path(name)
        actual = snapshot(path)
        if actual is not None and digest(actual) != expected:
            leftovers.append(str(path))
            continue
        restore(path, None)
        if app in path.parents:
            directories.update(parent for parent in path.parents if parent == app or app in parent.parents)
            if path.suffix == ".py":
                cache = path.parent / "__pycache__"
                for compiled in cache.glob(path.stem + ".cpython-*.pyc"):
                    if compiled.is_file() and not compiled.is_symlink():
                        compiled.unlink()
                directories.add(cache)
    if not leftovers:
        restore(inventory_path, None)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except (OSError, FileNotFoundError):
            pass
    return leftovers
