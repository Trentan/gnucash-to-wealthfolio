import tempfile
import time
import unittest
from pathlib import Path

from gnucash_to_wealthfolio.backups import (
    BackupInfo,
    delete_backup,
    list_backups,
    prune_backups,
)


class TestBackups(unittest.TestCase):
    def test_list_and_prune_backups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = Path(tmpdir)

            # Create 5 dummy backup files with staggered modification times
            created_files = []
            for i in range(5):
                f = backup_dir / f"app_backup_2026101{i}_120000.db"
                f.write_bytes(b"SQLite dummy data" * 100)
                # Stagger mtimes
                mtime = 1700000000 + i * 100
                import os
                os.utime(f, (mtime, mtime))
                created_files.append(f)

            # Also create an irrelevant file (e.g. .txt or subfolder) to ensure it's ignored
            (backup_dir / "ignore_me.txt").write_text("hello")

            backups = list_backups(backup_dir)
            self.assertEqual(len(backups), 5)
            # Should be sorted descending by timestamp/mtime (newest first)
            self.assertEqual(backups[0].filename, "app_backup_20261014_120000.db")
            self.assertEqual(backups[-1].filename, "app_backup_20261010_120000.db")
            self.assertGreater(backups[0].size_bytes, 0)

            # Prune keeping latest 3
            deleted = prune_backups(backup_dir, keep_latest=3)
            self.assertEqual(len(deleted), 2)
            self.assertIn("app_backup_20261010_120000.db", deleted)
            self.assertIn("app_backup_20261011_120000.db", deleted)

            remaining = list_backups(backup_dir)
            self.assertEqual(len(remaining), 3)
            self.assertEqual(
                [b.filename for b in remaining],
                [
                    "app_backup_20261014_120000.db",
                    "app_backup_20261013_120000.db",
                    "app_backup_20261012_120000.db",
                ],
            )

            # Delete a specific single backup
            success = delete_backup(backup_dir, "app_backup_20261013_120000.db")
            self.assertTrue(success)
            self.assertEqual(len(list_backups(backup_dir)), 2)

            # Safety check: attempting to delete invalid path or non-existent file
            self.assertFalse(delete_backup(backup_dir, "non_existent.db"))
            self.assertFalse(delete_backup(backup_dir, "../outside.db"))

    def test_list_and_prune_mapping_backups(self):
        from gnucash_to_wealthfolio.backups import list_mapping_backups, prune_mapping_backups
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            mapping_dir = Path(tmpdir) / "mappings"
            mapping_dir.mkdir(parents=True)

            # Create 25 dummy mapping json files
            for i in range(25):
                f = mapping_dir / f"mapping_20261015_{i:02d}0000.json"
                f.write_text('{"version": 2}')
                mtime = 1700000000 + i * 10
                os.utime(f, (mtime, mtime))

            # Non-json file to be ignored
            (mapping_dir / "note.txt").write_text("info")

            backups = list_mapping_backups(mapping_dir)
            self.assertEqual(len(backups), 25)
            self.assertEqual(backups[0].filename, "mapping_20261015_240000.json")

            deleted = prune_mapping_backups(mapping_dir, keep_latest=20)
            self.assertEqual(len(deleted), 5)
            self.assertEqual(len(list_mapping_backups(mapping_dir)), 20)


    def test_list_and_delete_backups_with_subfolder(self):
        from gnucash_to_wealthfolio.backups import get_wealthfolio_backup_dir, get_mapping_backup_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_root = Path(tmpdir)
            wf_sub = get_wealthfolio_backup_dir(backup_root)
            wf_sub.mkdir(parents=True)

            # File in subfolder
            f1 = wf_sub / "app_backup_20261021_100000.db"
            f1.write_bytes(b"data1")
            import os
            os.utime(f1, (1700000200, 1700000200))

            # Legacy file in root
            f2 = backup_root / "app_backup_20261020_090000.db"
            f2.write_bytes(b"data2")
            os.utime(f2, (1700000100, 1700000100))

            all_backups = list_backups(backup_root)
            self.assertEqual(len(all_backups), 2)
            self.assertEqual(all_backups[0].filename, "app_backup_20261021_100000.db")

            # Delete file in subfolder
            deleted = delete_backup(backup_root, "app_backup_20261021_100000.db")
            self.assertTrue(deleted)
            self.assertEqual(len(list_backups(backup_root)), 1)


if __name__ == "__main__":
    unittest.main()

