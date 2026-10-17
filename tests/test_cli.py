import argparse
import unittest
from pathlib import Path

from gnucash_to_wealthfolio.cli import main


class TestCLI(unittest.TestCase):
    def test_cli_edit_subcommand_parser(self):
        # Test edit argument parsing without starting server
        # We can pass --help or check that parser recognizes edit
        with self.assertRaises(SystemExit) as cm:
            main(["edit", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_cli_sync_ui_flag_parser(self):
        with self.assertRaises(SystemExit) as cm:
            main(["sync", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_launch_wealthfolio_nonexistent(self):
        from gnucash_to_wealthfolio.cli import launch_wealthfolio
        res = launch_wealthfolio(Path("non_existent_wealthfolio.exe"))
        self.assertFalse(res)

    def test_launch_wealthfolio_calls_startfile(self):
        from unittest.mock import patch
        from gnucash_to_wealthfolio.cli import launch_wealthfolio
        dummy_exe = Path(__file__)  # exists
        with patch("sys.platform", "win32"), \
             patch("subprocess.run") as mock_kill, \
             patch("os.startfile") as mock_startfile:
            res = launch_wealthfolio(dummy_exe)
            self.assertTrue(res)
            mock_kill.assert_called_once()
            mock_startfile.assert_called_once_with(str(dummy_exe))


if __name__ == "__main__":
    unittest.main()
