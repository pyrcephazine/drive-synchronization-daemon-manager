#!/usr/bin/python3

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import __version__
from src.config import APP_NAME, Settings, SyncError, atomic_write, config_root, data_root, default_config_path, find_rclone, write_json
from src.installation import digest, snapshot
from src.icons import APP_ICON_FILENAME, ICON_FILENAMES
from src.rclone import MIN_VERSION_TEXT, require_rclone
from src.service import desktop_entry

LAUNCHERS = ("drive-synchronization-daemon-manager", "drive-synchronization-manager", "rclone-local-sync")


def copy_app(destination):
    destination = Path(destination)
    files = [ROOT / "README.md", *(ROOT / "bin" / name for name in LAUNCHERS)]
    files.extend(sorted((ROOT / "src").glob("*.py")))
    files.extend(ROOT / "data/icons" / name for name in ICON_FILENAMES)
    installed = []
    for source in files:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        # An upgrade must never leave an active worker importing a partial file.
        content = source.read_text()
        if source.name == "README.md":
            content = "".join(line for line in content.splitlines(keepends=True)
                              if not line.startswith("!["))
        atomic_write(target, content, 0o755 if source.parent.name == "bin" else 0o644)
        installed.append(target)
    return installed


def symlink_launcher(target, link):
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise SystemExit(f"Another launcher already uses this path: {link}. Review it before installing. It has not been replaced.")
        return
    link.symlink_to(os.path.relpath(target, link.parent))


def install_user():
    settings = default_config_path()
    require_rclone(Settings.load(settings).rclone_binary if settings.exists() else find_rclone())
    destination = data_root() / "rclone-local-sync/app"
    launcher = Path.home() / ".local/bin/drive-synchronization-daemon-manager"
    for name in LAUNCHERS:
        link = Path.home() / ".local/bin" / name
        if (link.exists() or link.is_symlink()) and (not link.is_symlink() or link.resolve() != destination / "bin" / name):
            raise SystemExit(f"Another launcher already uses this path: {link}. Review it before installing. It has not been replaced.")
    installed = copy_app(destination)
    for name in LAUNCHERS:
        symlink_launcher(destination / "bin" / name, Path.home() / ".local/bin" / name)
    desktop = data_root() / "applications/rclone-local-sync.desktop"
    atomic_write(desktop, desktop_entry(launcher), 0o644)
    icon = data_root() / "icons/hicolor/scalable/apps/rclone-local-sync.svg"
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "data/icons" / APP_ICON_FILENAME, icon)
    owned = [*installed,
             *(Path.home() / ".local/bin" / name for name in LAUNCHERS), desktop, icon]
    write_json(destination / "installed-files.json", {
        "version": 1, "files": {str(path): digest(snapshot(path)) for path in owned}})
    for command in (["update-desktop-database", str(desktop.parent)], ["gtk-update-icon-cache", "-f", "-t", str(data_root() / "icons/hicolor")]):
        if shutil.which(command[0]):
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print(f"Installed for this user: {launcher}")
    print("Open the app, then open Settings to create or import a connection. Your sync schedule and history are unchanged by installation.")


def build_deb():
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    output = dist / f"drive-synchronization-daemon-manager_{__version__}_all.deb"
    with tempfile.TemporaryDirectory(prefix="localdrive-deb-") as directory:
        stage = Path(directory)
        app = stage / "usr/share/rclone-local-sync"
        copy_app(app)
        for name in LAUNCHERS:
            symlink_launcher(app / "bin" / name, stage / "usr/bin" / name)
        desktop = stage / "usr/share/applications/rclone-local-sync.desktop"
        atomic_write(desktop, desktop_entry("/usr/bin/drive-synchronization-daemon-manager"), 0o644)
        icon = stage / "usr/share/icons/hicolor/scalable/apps/rclone-local-sync.svg"
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "data/icons" / APP_ICON_FILENAME, icon)
        docs = stage / "usr/share/doc/drive-synchronization-daemon-manager"
        docs.mkdir(parents=True)
        shutil.copy2(ROOT / "README.md", docs / "README.md")
        (docs / "data").mkdir()
        shutil.copy2(ROOT / "data/overview.png", docs / "data/overview.png")
        size_kib = sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) // 1024 + 1
        atomic_write(stage / "DEBIAN/control", f"""Package: drive-synchronization-daemon-manager
Version: {__version__}
Section: net
Priority: optional
Architecture: all
Maintainer: Drive Synchronization Daemon Manager contributors <noreply@localhost>
Installed-Size: {size_kib}
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, rclone (>= {MIN_VERSION_TEXT}), systemd, xdg-utils
Recommends: gir1.2-ayatanaappindicator3-0
Provides: drive-synchronization-manager, rclone-local-sync
Replaces: drive-synchronization-manager, rclone-local-sync
Conflicts: drive-synchronization-manager, rclone-local-sync
Description: Keep local and remote folders in sync with rclone
 Sync files in both directions while working with copies on your local disk.
 Manage sync schedules, file filters, and recovery settings in a GTK 3 app
 with a system tray icon. Requires a systemd user session and X11 or
 XWayland for desktop windows.
""", 0o644)
        # mkdtemp is 0700 and user umasks vary. Never ship those modes into /usr.
        stage.chmod(0o755)
        for path in stage.rglob("*"):
            if path.is_symlink():
                continue
            path.chmod(0o755 if path.is_dir() or path.parent == app / "bin" else 0o644)
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(output)], check=True)
    print(output)


def main():
    parser = argparse.ArgumentParser(description='Build a .deb without root, or install the same application for this user.')
    parser.add_argument("action", choices=("build-deb", "install-user"))
    args = parser.parse_args()
    install_user() if args.action == "install-user" else build_deb()


if __name__ == "__main__":
    try:
        main()
    except SyncError as error:
        raise SystemExit(str(error)) from error
