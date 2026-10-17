import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from gnucash_to_wealthfolio.mapper import AccountMapEntry, MappingConfig, build_mapping_config
from gnucash_to_wealthfolio.migrator import run_migration
from gnucash_to_wealthfolio.parser import parse_gnucash_file
from gnucash_to_wealthfolio.verifier import verify_migration


class TestMigrator(unittest.TestCase):
    def test_full_migration_and_cent_exact_reconciliation(self):
        fixture_dir = Path(__file__).parent / "fixtures"
        gnc_file = fixture_dir / "sample_book.gnucash"
        source_db = fixture_dir / "sample_wealthfolio.db"

        book = parse_gnucash_file(gnc_file)

        base_cfg = MappingConfig(
            accounts={
                "acc-checking": AccountMapEntry(
                    gnc_guid="acc-checking",
                    gnc_path="Assets:Bank Accounts:Checking Account",
                    role="MIGRATE_CASH_ACTIVE",
                    wf_account_id="wf-acc-checking",
                    wf_name="Checking Account",
                    wf_account_type="CASH",
                    wf_group="Banking",
                    is_active=1,
                    is_custom=True,
                ),
                "acc-savings": AccountMapEntry(
                    gnc_guid="acc-savings",
                    gnc_path="Assets:Bank Accounts:Savings Account",
                    role="MIGRATE_CASH_ACTIVE",
                    wf_account_id="wf-acc-savings",
                    wf_name="Savings Account",
                    wf_account_type="CASH",
                    wf_group="Banking",
                    is_active=1,
                    is_custom=True,
                ),
                "acc-visa": AccountMapEntry(
                    gnc_guid="acc-visa",
                    gnc_path="Liabilities:Credit Cards:Visa Card",
                    role="MIGRATE_CARD_ACTIVE",
                    wf_account_id="wf-acc-visa",
                    wf_name="Visa Card",
                    wf_account_type="CREDIT_CARD",
                    wf_group="Credit Cards",
                    is_active=1,
                    is_custom=True,
                ),
            }
        )
        cfg = build_mapping_config(book, existing_config=base_cfg)

        tmp_dir = Path(tempfile.mkdtemp(prefix="wf_mig_test_"))
        try:
            target_db = tmp_dir / "app_migrated.db"
            backup_dir = tmp_dir / "backups"

            stats = run_migration(book, cfg, source_db, target_db, backup_dir)
            self.assertEqual(stats.accounts_created, 4)
            self.assertGreater(stats.activities_created, 20)
            self.assertGreater(stats.categories_created, 5)

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

            # Verify that 100% of spending categories have budget group assignments
            import sqlite3
            con = sqlite3.connect(target_db)
            unassigned_count = con.execute(
                """
                SELECT COUNT(*) FROM taxonomy_categories
                WHERE taxonomy_id = 'spending_categories'
                  AND id NOT IN (SELECT category_id FROM budget_group_assignments WHERE taxonomy_id = 'spending_categories');
                """
            ).fetchone()[0]
            con.close()
            self.assertEqual(unassigned_count, 0, "All spending categories must have budget group assignments")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_empty_alternative_assets_clean_run(self):
        fixture_dir = Path(__file__).parent / "fixtures"
        gnc_file = fixture_dir / "sample_book.gnucash"
        source_db = fixture_dir / "sample_wealthfolio.db"

        book = parse_gnucash_file(gnc_file)
        cfg = build_mapping_config(book)
        cfg.alternative_assets = []

        tmp_dir = Path(tempfile.mkdtemp(prefix="wf_empty_alt_"))
        try:
            target_db = tmp_dir / "app_migrated.db"
            backup_dir = tmp_dir / "backups"
            stats = run_migration(book, cfg, source_db, target_db, backup_dir)
            self.assertEqual(stats.alt_assets_created, 0)
            self.assertEqual(stats.alt_quotes_created, 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
