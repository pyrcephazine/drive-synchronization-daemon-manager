from pathlib import Path


ICON_DIR = Path(__file__).resolve().parents[1] / "data/icons"
APP_ICON_FILENAME = "drive-multidisk.svg"
INDICATOR_FILES = {
    "repair": "drive-noread.svg",
    "repairing": "drive-synchronizing.svg",
    "ready": "drive-check.svg",
    "syncing": "drive-synchronizing.svg",
    "preview": "drive-synchronizing.svg",
    "paused": "drive-noread.svg",
    "error": "drive-noread.svg",
    "setup": "drive-noread.svg",
    "unconfigured": "drive-noread.svg",
}
ICON_FILENAMES = (APP_ICON_FILENAME, *sorted(set(INDICATOR_FILES.values())))


def application_icon_path():
    return str(ICON_DIR / APP_ICON_FILENAME)


def icon_path(phase):
    return str(ICON_DIR / INDICATOR_FILES.get(phase, "drive-noread.svg"))
