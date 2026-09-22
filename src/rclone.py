import re
import subprocess

from .config import SyncError


MIN_VERSION = (1, 66, 0)
MIN_VERSION_TEXT = ".".join(map(str, MIN_VERSION))


def require_rclone(binary):
    try:
        result = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyncError(f"Install rclone {MIN_VERSION_TEXT} or later before opening the app.") from error
    match = re.search(r"^rclone v(\d+)\.(\d+)\.(\d+)", result.stdout, re.MULTILINE)
    if result.returncode or not match or tuple(map(int, match.groups())) < MIN_VERSION:
        raise SyncError(f"Install rclone {MIN_VERSION_TEXT} or later before opening the app.")
    return result.stdout.splitlines()[0]
