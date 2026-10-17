import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from gnucash_to_wealthfolio.config import (
    get_default_wealthfolio_db_path,
    get_wealthfolio_executable_path,
    get_default_gnucash_file,
    DEFAULT_MAPPING_PATH,
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


if __name__ == "__main__":
    unittest.main()
