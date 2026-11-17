import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from gnucash_to_wealthfolio.config import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_MAPPING_PATH,
    get_active_backup_dir,
    get_active_mapping_path,
    get_default_gnucash_file,
    get_default_wealthfolio_db_path,
    get_wealthfolio_executable_path,
    load_app_settings,
    save_app_settings,
)


class TestConfig(unittest.TestCase):
    def test_env_var_override_for_db_path(self):
        fake_path = Path("/custom/wealthfolio/app.db")
        with patch.dict(os.environ, {"WEALTHFOLIO_DB_PATH": str(fake_path)}):
            with patch.object(Path, "exists", return_value=True):
                self.assertEqual(get_default_wealthfolio_db_path(), fake_path)

    def test_platform_default_db_paths(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("sys.platform", "win32"), patch.object(Path, "exists", return_value=False):
                p = get_default_wealthfolio_db_path()
                self.assertTrue(str(p).endswith("app.db"))
                self.assertIn("com.teymz.wealthfolio", str(p))

            with patch("sys.platform", "darwin"), patch.object(Path, "exists", return_value=False):
                p = get_default_wealthfolio_db_path()
                self.assertIn("Application Support", str(p))

            with patch("sys.platform", "linux"), patch.object(Path, "exists", return_value=False):
                p = get_default_wealthfolio_db_path()
                self.assertTrue(".config" in str(p) or ".local" in str(p) or "app.db" in str(p))

    def test_get_default_gnucash_file_auto_detect(self):
        with patch.object(Path, "glob", return_value=[Path("test_book.gnucash")]):
            detected = get_default_gnucash_file(search_dir=Path("."))
            self.assertEqual(detected, Path("test_book.gnucash"))

    def test_app_settings_persistence_and_resolution(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_file = Path(tmpdir) / "app_settings.json"
            self.assertEqual(load_app_settings(settings_file), {})

            save_app_settings({"backup_dir": "G:\\Drive\\Backups"}, settings_file)
            loaded = load_app_settings(settings_file)
            self.assertEqual(loaded.get("backup_dir"), "G:\\Drive\\Backups")

            # Test resolution with mock settings
            with patch("gnucash_to_wealthfolio.config.load_app_settings", return_value={"backup_dir": "G:\\Drive\\Backups", "mapping_path": "C:\\custom\\mapping.json"}):
                # When no CLI override or default provided, it uses persistent setting
                self.assertEqual(get_active_backup_dir(DEFAULT_BACKUP_DIR), Path("G:\\Drive\\Backups"))
                self.assertEqual(get_active_mapping_path(DEFAULT_MAPPING_PATH), Path("C:\\custom\\mapping.json"))

                # When explicit CLI override provided, CLI takes precedence
                cli_custom_backup = Path("D:\\CustomBackup")
                self.assertEqual(get_active_backup_dir(cli_custom_backup), cli_custom_backup)
                cli_custom_mapping = Path("D:\\CustomMapping.json")
                self.assertEqual(get_active_mapping_path(cli_custom_mapping), cli_custom_mapping)


if __name__ == "__main__":
    unittest.main()
