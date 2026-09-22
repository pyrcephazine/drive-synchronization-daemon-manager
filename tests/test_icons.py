from pathlib import Path
import unittest
from xml.etree import ElementTree

from src.icons import APP_ICON_FILENAME, ICON_DIR, ICON_FILENAMES, application_icon_path, icon_path


class ArtworkTests(unittest.TestCase):
    def test_application_uses_unbadged_drive(self):
        self.assertEqual(APP_ICON_FILENAME, "drive-multidisk.svg")
        self.assertEqual(Path(application_icon_path()), ICON_DIR / "drive-multidisk.svg")
        self.assertNotEqual(application_icon_path(), icon_path("ready"))

    def test_all_indicator_states_use_supplied_artwork(self):
        for phase, filename in {
            "ready": "drive-check.svg", "syncing": "drive-synchronizing.svg",
            "preview": "drive-synchronizing.svg", "paused": "drive-noread.svg",
            "error": "drive-noread.svg", "setup": "drive-noread.svg",
            "unconfigured": "drive-noread.svg",
        }.items():
            with self.subTest(phase=phase):
                self.assertEqual(Path(icon_path(phase)), ICON_DIR / filename)

    def test_unknown_state_does_not_claim_ready(self):
        self.assertEqual(Path(icon_path("unexpected")).name, "drive-noread.svg")

    def test_assets_are_valid_svgs_in_the_assets_directory(self):
        self.assertEqual(len(ICON_FILENAMES), 4)
        for filename in ICON_FILENAMES:
            with self.subTest(filename=filename):
                root = ElementTree.parse(ICON_DIR / filename).getroot()
                self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertEqual(root.get("viewBox"), "0 0 128 128")
                self.assertFalse((ICON_DIR.parents[1] / filename).exists(), "Artwork belongs in data/icons, not the repository root")
