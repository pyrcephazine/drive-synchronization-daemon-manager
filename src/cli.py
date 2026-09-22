import argparse
import json
from pathlib import Path
import sys
import time

from . import __version__
from .config import APP_NAME, Settings, SyncError, default_config_path, detect_legacy
from .engine import BusyError, run
from .service import Manager


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--version", action="version", version=APP_NAME + " " + __version__)
    parser.add_argument("--config", type=Path, default=default_config_path(), help="Path to the app's JSON settings file, separate from the rclone configuration file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--worker", action="store_true", help="Run one background sync without opening a window (normally started by systemd)")
    group.add_argument("--watch", action="store_true", help="Watch local changes and request background sync (normally started by systemd)")
    group.add_argument("--preview", action="store_true", help="Preview sync changes while keeping files and sync history unchanged")
    group.add_argument("--status", action="store_true", help="Show background sync status as JSON")
    group.add_argument("--transfers", action="store_true", help="Show pending transfers and storage information as JSON without changing synced files")
    group.add_argument("--adopt-existing", action="store_true", help="Import the supported existing Google Drive connection to manage it in the app")
    group.add_argument("--diagnose", action="store_true", help="Show installation health as JSON without making changes")
    group.add_argument("--repair", action="store_true", help="Recover interrupted changes and repair missing owned installation files")
    group.add_argument("--remove-connection", action="store_true", help="Remove the active connection and its owned services; preserve synced files and credentials")
    group.add_argument("--uninstall-user", action="store_true", help="Remove this user's installation and owned services; preserve connection data")
    group.add_argument("--demo", action="store_true", help="Explore the app with sample data while keeping files and sync settings unchanged")
    parser.add_argument("--tray", action="store_true", help="Start with the tray icon visible and the window hidden")
    parser.add_argument("--check-changes", action="store_true", help="With --worker, skip full reconciliation when change checks are unchanged")
    args = parser.parse_args()
    if args.check_changes and not args.worker:
        parser.error("--check-changes requires --worker")
    launcher = Path(sys.argv[0]).resolve()
    canonical = launcher.with_name("drive-synchronization-daemon-manager")
    if canonical.is_file():
        launcher = canonical
    try:
        manager = Manager(args.config, launcher,
                          actor="rclone-local-sync-preview.service" if args.preview else
                          "rclone-local-sync.service" if args.worker else
                          "rclone-local-sync-watch.service" if args.watch else None)
        if args.diagnose:
            report = manager.inspect_installation()
            print(json.dumps(report, indent=2))
            return 0 if report["state"] in ("healthy", "removed", "unconfigured") else 1
        if args.repair:
            print(json.dumps(manager.maintain(retry=True), indent=2))
            return 0
        if args.remove_connection or args.uninstall_user:
            if args.uninstall_user:
                from .uninstall import uninstall_user
                leftovers = uninstall_user(manager)
            else:
                leftovers = manager.remove_connection()
            print(json.dumps({"removed": True, "kept_modified_files": leftovers}, indent=2))
            return 0
        if args.worker or args.preview or args.watch:
            # A newly started unit may overlap the final durable commit.
            for attempt in range(30):
                try:
                    manager.maintain()
                    break
                except BusyError:
                    if attempt == 29:
                        raise
                    time.sleep(0.1)
        if args.worker or args.preview:
            manager.require_healthy()
            return run(Settings.load(args.config), preview=args.preview, check_changes=args.check_changes)
        if args.watch:
            if manager.inspect_installation()["state"] == "removed":
                return 0
            from .watcher import run as watch
            return watch(args.config, manager=manager)
        if args.status:
            print(json.dumps(Manager(args.config, launcher).status(Settings.load(args.config)), indent=2))
            return 0
        if args.transfers:
            from .transfers import scan
            snapshot = scan(Settings.load(args.config))
            print(json.dumps(snapshot, indent=2))
            return 1 if snapshot["error"] else 0
        if args.adopt_existing:
            if args.config.exists():
                raise SyncError("A connection is already set up. Open Settings to manage it.")
            cfg = detect_legacy()
            if cfg is None:
                raise SyncError("No supported Google Drive connection was found to import. Open Settings in the app to create a connection.")
            Manager(args.config, launcher).apply(cfg)
            print("Google Drive connection imported with its sync history. Automatic sync is on.")
            return 0
        from .gui import main as graphical_main
        return graphical_main(args.config, launcher, tray=args.tray, demo=args.demo)
    except BusyError as error:
        print(str(error), file=sys.stderr)
        return 75
    except (SyncError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
