import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from gnucash_to_wealthfolio.config import get_default_wealthfolio_db_path
from gnucash_to_wealthfolio.mapper import build_mapping_config, load_mapping_config
from gnucash_to_wealthfolio.migrator import run_migration
from gnucash_to_wealthfolio.parser import parse_gnucash_file
from gnucash_to_wealthfolio.verifier import verify_migration


LOCAL_REGRESSION_FILE = Path(os.environ.get("LOCAL_REGRESSION_GNUCASH", "local_regression.gnucash"))


@unittest.skipUnless(LOCAL_REGRESSION_FILE.exists(), "Local historical regression test only")
class TestHistoricalRegression(unittest.TestCase):
    def test_local_account_and_multilevel_category_mapping(self):
        book = parse_gnucash_file(LOCAL_REGRESSION_FILE)
        mapping_file = Path("config/mapping.json")
        cfg = load_mapping_config(mapping_file) if mapping_file.exists() else build_mapping_config(book)

        migrated = [a for a in cfg.accounts.values() if a.wf_account_id is not None]
        active = [a for a in migrated if a.is_active == 1 and a.is_archived == 0]
        archived = [a for a in migrated if a.is_archived == 1]
        self.assertGreaterEqual(len(active), 1)
        self.assertGreaterEqual(len(archived), 1)

        by_path = {c.gnc_path: c for c in cfg.categories.values()}
        woolies = by_path.get("Expenses:Food & Groceries:Supermarket:Woolworths")
        if woolies:
            self.assertEqual(woolies.parent_id, "cat_groceries")
            self.assertIn("Woolworths", woolies.category_name)

        bunnings = by_path.get("Expenses:Shopping & Lifestyle:Gardening & Tools:Bunnings")
        if bunnings:
            self.assertEqual(bunnings.parent_id, "cat_shopping")
            self.assertIn("Bunnings", bunnings.category_name)

        mcd = by_path.get("Expenses:Food & Groceries:Take away (Fast Food):McDonalds")
        if mcd:
            self.assertEqual(mcd.parent_id, "cat_food")
            self.assertIn("McDonalds", mcd.category_name)

    def test_local_cent_exact_balance(self):
        book = parse_gnucash_file(LOCAL_REGRESSION_FILE)
        self.assertGreaterEqual(len(book.accounts), 10)
        self.assertGreaterEqual(len(book.transactions), 10)

        mapping_file = Path("config/mapping.json")
        cfg = load_mapping_config(mapping_file) if mapping_file.exists() else build_mapping_config(book)

        source_db = get_default_wealthfolio_db_path()
        if not source_db.exists():
            source_db = Path("output/app_migrated.db")
        if not source_db.exists():
            self.skipTest("No reference Wealthfolio database available for local regression test")

        tmp_dir = Path(tempfile.mkdtemp(prefix="wf_reg_"))
        try:
            target_db = tmp_dir / "app_migrated.db"
            backup_dir = tmp_dir / "backups"
            stats = run_migration(book, cfg, source_db, target_db, backup_dir)
            self.assertGreaterEqual(stats.accounts_created, 25)
            self.assertGreater(stats.activities_created, 30000)

            report = verify_migration(book, cfg, source_db, target_db)
            self.assertTrue(report.pre_existing_accounts_intact)
            self.assertEqual(report.fk_violations, 0)
            self.assertTrue(report.integrity_ok)
            for rec in report.reconciliations:
                self.assertEqual(
                    rec.diff,
                    Decimal("0.00"),
                    f"Balance mismatch on {rec.gnc_path}: GNC={rec.gnc_balance} WF={rec.wf_balance}",
                )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
