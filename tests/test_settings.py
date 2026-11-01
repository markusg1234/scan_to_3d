import tempfile
import unittest
from pathlib import Path

from settings import load_settings, save_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "settings.json"

    def test_saved_folders_come_back(self):
        save_settings({"photos": "C:\\photos", "output": "D:\\models"}, self.path)
        self.assertEqual(load_settings(self.path), {"photos": "C:\\photos", "output": "D:\\models"})

    def test_missing_file_gives_no_settings(self):
        self.assertEqual(load_settings(self.path), {})

    def test_damaged_file_gives_no_settings(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_settings(self.path), {})

    def test_unexpected_contents_give_no_settings(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(load_settings(self.path), {})

    def test_failed_save_is_ignored(self):
        save_settings({"photos": "C:\\photos"}, self.path.parent)


if __name__ == "__main__":
    unittest.main()
