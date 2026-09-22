import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import __version__
from src.config import APP_NAME

package = ROOT / f"dist/drive-synchronization-daemon-manager_{__version__}_all.deb"
dependencies = subprocess.check_output(["dpkg-deb", "--field", str(package), "Depends"], text=True)
assert "rclone (>= 1.66.0)" in dependencies, dependencies
archive = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(package)])
with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
    for member in contents.getmembers():
        assert member.uid == 0 and member.gid == 0, member.name
        assert "rclone.conf" not in member.name and "/home/" not in member.name, member.name
        if member.isdir():
            assert member.mode == 0o755, (member.name, oct(member.mode))
        elif member.isfile():
            expected = 0o755 if "/bin/" in member.name else 0o644
            assert member.mode == expected, (member.name, oct(member.mode))
with tempfile.TemporaryDirectory(prefix="localdrive-package-") as temporary:
    subprocess.run(["dpkg-deb", "--extract", str(package), temporary], check=True)
    root = Path(temporary)
    app = root / "usr/share/rclone-local-sync"
    assert (app / "src/cli.py").is_file()
    assert not (app / "src/localdrive").exists()
    for name in ("drive-synchronization-daemon-manager", "drive-synchronization-manager", "rclone-local-sync"):
        output = subprocess.check_output([str(root / "usr/bin" / name), "--version"], text=True, cwd=temporary)
        assert output.strip() == APP_NAME + " " + __version__, output
    output = subprocess.check_output([
        sys.executable, "-I", "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); from src.icons import application_icon_path; print(application_icon_path())",
        str(app),
    ], text=True, cwd=temporary)
    assert Path(output.strip()) == app / "data/icons/drive-multidisk.svg", output
    subprocess.run(["desktop-file-validate", str(root / "usr/share/applications/rclone-local-sync.desktop")], check=True)
    icons = root / "usr/share/rclone-local-sync/data/icons"
    expected_icons = {"drive-multidisk.svg", "drive-check.svg", "drive-synchronizing.svg", "drive-noread.svg"}
    assert {path.name for path in icons.glob("*.svg")} == expected_icons
    for name in expected_icons:
        assert (icons / name).read_bytes() == (ROOT / "data/icons" / name).read_bytes(), name
    desktop_icon = root / "usr/share/icons/hicolor/scalable/apps/rclone-local-sync.svg"
    assert desktop_icon.read_bytes() == (ROOT / "data/icons/drive-multidisk.svg").read_bytes()
print("PASS: package ownership/modes, no user data, relocated launcher, desktop entry, exact supplied SVGs")
