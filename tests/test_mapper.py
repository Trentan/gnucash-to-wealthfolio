import unittest
from pathlib import Path
from gnucash_to_wealthfolio.parser import parse_gnucash_file
from gnucash_to_wealthfolio.mapper import build_mapping_config


class TestMapper(unittest.TestCase):
    def test_account_and_multilevel_category_mapping(self):
        sample_path = Path(__file__).parent / "fixtures" / "sample_book.gnucash"
        book = parse_gnucash_file(sample_path)
        cfg = build_mapping_config(book)

        self.assertIn("acc-checking", cfg.accounts)
        self.assertIn("acc-visa", cfg.accounts)
        self.assertIn("acc-groceries", cfg.categories)

        root_ids = {
            "cat_bills",
            "cat_education",
            "cat_entertainment",
            "cat_fees",
            "cat_food",
            "cat_gifts",
            "cat_groceries",
            "cat_health",
            "cat_housing",
            "cat_other_expense",
            "cat_personal",
            "cat_shopping",
            "cat_transport",
            "cat_travel",
            "cat_income_employment",
            "cat_income_investment",
            "cat_income_other",
            "cat_income_selfemploy",
            "cat_savings",
        } | {r.category_id for r in cfg.custom_root_categories}
        for cat in cfg.categories.values():
            if cat.parent_id is not None:
                self.assertIn(
                    cat.parent_id,
                    root_ids,
                    f"{cat.gnc_path} has non-root parent {cat.parent_id}",
                )

    def test_custom_category_mapping_persistence(self):
        from gnucash_to_wealthfolio.models import GncAccount, GncBook
        from gnucash_to_wealthfolio.mapper import (
            CategoryMapEntry,
            update_category_mapping,
            save_mapping_config,
            load_mapping_config,
        )
        import tempfile

        # Create dummy book with one expense account
        book = GncBook(accounts={}, transactions=[])
        acc = GncAccount(
            guid="test-guid-123",
            name="Unusual Expense",
            full_path="Expenses:Unusual Expense",
            acct_type="EXPENSE",
            parent_guid=None,
            commodity="AUD",
        )
        book.accounts[acc.guid] = acc

        # Initial build: should default to Other Expenses parent
        initial_cfg = build_mapping_config(book)
        entry = initial_cfg.categories[acc.guid]
        self.assertFalse(entry.is_custom)
        self.assertEqual(entry.parent_id, "cat_other_expense")

        # Now customize it using update_category_mapping
        with tempfile.TemporaryDirectory() as tmpdir:
            map_file = Path(tmpdir) / "mapping.json"
            save_mapping_config(initial_cfg, map_file)

            updated_cfg = update_category_mapping(
                initial_cfg,
                gnc_guid=acc.guid,
                parent_id="cat_shopping",
                subcat_name="Special Shopping Item",
                mapping_path=map_file,
            )
            self.assertTrue(updated_cfg.categories[acc.guid].is_custom)
            self.assertEqual(updated_cfg.categories[acc.guid].parent_id, "cat_shopping")
            self.assertEqual(updated_cfg.categories[acc.guid].category_name, "Special Shopping Item")

            # Verify saved to file and reloadable
            reloaded = load_mapping_config(map_file)
            self.assertTrue(reloaded.categories[acc.guid].is_custom)

            # Re-running build_mapping_config with existing_config should PRESERVE the custom entry
            rebuilt_cfg = build_mapping_config(book, existing_config=reloaded)
            rebuilt_entry = rebuilt_cfg.categories[acc.guid]
            self.assertTrue(rebuilt_entry.is_custom)
            self.assertEqual(rebuilt_entry.parent_id, "cat_shopping")
            self.assertEqual(rebuilt_entry.category_name, "Special Shopping Item")

    def test_custom_account_mapping_preserved(self):
        sample_path = Path(__file__).parent / "fixtures" / "sample_book.gnucash"
        book = parse_gnucash_file(sample_path)
        initial_cfg = build_mapping_config(book)

        bank_guid = "acc-checking"
        acc_entry = initial_cfg.accounts[bank_guid]
        acc_entry.role = "MIGRATE_CASH_ACTIVE"
        acc_entry.wf_account_id = "test-uuid-bank-custom"
        acc_entry.wf_name = "My Custom Checking"
        acc_entry.wf_account_type = "CASH"
        acc_entry.wf_group = "Personal Banking"
        acc_entry.is_active = 1
        acc_entry.is_custom = True

        rebuilt_cfg = build_mapping_config(book, existing_config=initial_cfg)
        rebuilt_acc = rebuilt_cfg.accounts[bank_guid]
        self.assertTrue(rebuilt_acc.is_custom)
        self.assertEqual(rebuilt_acc.role, "MIGRATE_CASH_ACTIVE")
        self.assertEqual(rebuilt_acc.wf_account_id, "test-uuid-bank-custom")
        self.assertEqual(rebuilt_acc.wf_name, "My Custom Checking")
        self.assertEqual(rebuilt_acc.wf_group, "Personal Banking")

    def test_schema_v1_to_v2_upgrade(self):
        from gnucash_to_wealthfolio.mapper import upgrade_mapping_config_if_needed, load_mapping_config
        import tempfile, json

        v1_payload = {
            "accounts": {
                "g1": {
                    "gnc_guid": "g1",
                    "gnc_path": "Assets:Bank",
                    "role": "MIGRATE_CASH_ACTIVE",
                    "wf_account_id": "wf-1",
                    "is_custom": True,
                }
            },
            "categories": {
                "c1": {
                    "gnc_guid": "c1",
                    "gnc_path": "Expenses:Food",
                    "taxonomy_id": "spending_categories",
                    "category_id": "cat_food",
                    "category_key": "food",
                    "category_name": "Food",
                    "parent_id": None,
                    "is_custom": True,
                }
            },
            "custom_root_categories": [],
            "rules": [],
            "transaction_overrides": {"tx1": {"category_id": "cat_food"}},
            "budget_group_overrides": {"cat_food": "NEEDS"},
        }
        upgraded = upgrade_mapping_config_if_needed(v1_payload)
        self.assertEqual(upgraded["version"], 2)
        self.assertIn("metadata", upgraded)
        self.assertEqual(upgraded["metadata"]["currency"], "AUD")
        self.assertIn("alternative_assets", upgraded)
        self.assertEqual(upgraded["accounts"]["g1"]["is_custom"], True)
        self.assertEqual(upgraded["budget_group_overrides"]["cat_food"], "NEEDS")

    def test_atomic_mapping_save_and_backup_creation(self):
        import tempfile
        from gnucash_to_wealthfolio.mapper import MappingConfig, save_mapping_config, load_mapping_config
        from gnucash_to_wealthfolio.backups import list_mapping_backups

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            mapping_path = tmp_dir / "config" / "mapping.json"
            backup_dir = tmp_dir / "backups"

            cfg = MappingConfig(version=2)
            save_mapping_config(cfg, mapping_path, backup_dir=backup_dir)
            self.assertTrue(mapping_path.exists())

            # Second save must trigger a backup snapshot in backups/mappings/
            cfg.version = 2
            save_mapping_config(cfg, mapping_path, backup_dir=backup_dir)
            backups = list_mapping_backups(backup_dir / "mappings")
            self.assertGreaterEqual(len(backups), 1)
            self.assertTrue(backups[0].filename.endswith(".json"))


if __name__ == "__main__":
    unittest.main()

