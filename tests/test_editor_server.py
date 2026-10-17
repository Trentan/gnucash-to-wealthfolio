import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from gnucash_to_wealthfolio.editor_server import create_editor_server
from gnucash_to_wealthfolio.mapper import (
    AccountMapEntry,
    CategoryMapEntry,
    MappingConfig,
    save_mapping_config,
)
from gnucash_to_wealthfolio.models import GncAccount, GncBook


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestEditorServer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.mapping_file = self.root / "mapping.json"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir(parents=True)
        self.source_db = self.root / "app.db"
        self.target_db = self.root / "app_migrated.db"

        # Create dummy backup
        dummy_backup = self.backup_dir / "app_backup_20261014_100000.db"
        dummy_backup.write_bytes(b"SQLite dummy")

        # Create sample mapping config
        self.guid = "test-acc-guid-1"
        self.bank_guid = "test-bank-guid-1"
        cfg = MappingConfig(
            accounts={
                self.bank_guid: AccountMapEntry(
                    gnc_guid=self.bank_guid,
                    gnc_path="Assets:Bank Accounts:Test Bank",
                    role="MIGRATE_CASH_ACTIVE",
                    wf_account_id="wf-bank-1",
                    tx_count=1,
                ),
                self.guid: AccountMapEntry(
                    gnc_guid=self.guid,
                    gnc_path="Expenses:Other Expense Item",
                    role="EXPENSE",
                    tx_count=5,
                ),
            },
            categories={
                self.guid: CategoryMapEntry(
                    gnc_guid=self.guid,
                    gnc_path="Expenses:Other Expense Item",
                    taxonomy_id="spending_categories",
                    category_id="cat_other_expense",
                    category_key="other_expense",
                    category_name="Other Expenses",
                    parent_id="cat_other_expense",
                    is_custom=False,
                )
            },
        )
        save_mapping_config(cfg, self.mapping_file)

        # Create dummy book
        from decimal import Decimal
        from gnucash_to_wealthfolio.models import GncSplit, GncTransaction

        self.book = GncBook(
            accounts={
                self.bank_guid: GncAccount(
                    guid=self.bank_guid,
                    name="Test Bank",
                    full_path="Assets:Bank Accounts:Test Bank",
                    acct_type="BANK",
                    parent_guid=None,
                    commodity="AUD",
                ),
                self.guid: GncAccount(
                    guid=self.guid,
                    name="Other Expense Item",
                    full_path="Expenses:Other Expense Item",
                    acct_type="EXPENSE",
                    parent_guid=None,
                    commodity="AUD",
                ),
            },
            transactions=[
                GncTransaction(
                    guid="trn-1",
                    currency="AUD",
                    date_posted="2026-03-15",
                    date_entered="2026-03-15",
                    description="Woolworths Metro",
                    splits=[
                        GncSplit(
                            guid="sp-1",
                            account_guid=self.bank_guid,
                            value=Decimal("-50.00"),
                            quantity=Decimal("-50.00"),
                        ),
                        GncSplit(
                            guid="sp-2",
                            account_guid=self.guid,
                            value=Decimal("50.00"),
                            quantity=Decimal("50.00"),
                        ),
                    ],
                )
            ],
        )

        self.port = get_free_port()
        self.server = create_editor_server(
            book=self.book,
            mapping_path=self.mapping_file,
            source_db=self.source_db,
            target_db=self.target_db,
            backup_dir=self.backup_dir,
            port=self.port,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmpdir.cleanup()

    def test_get_api_state(self):
        url = f"http://127.0.0.1:{self.port}/api/state"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("accounts", data)
            self.assertIn("parents", data)
            self.assertIn("backups", data)
            self.assertEqual(len(data["accounts"]), 1)
            acc = data["accounts"][0]
            self.assertEqual(acc["gnc_guid"], self.guid)
            self.assertTrue(acc["is_other_expense"])
            self.assertEqual(acc["tx_count"], 5)

    def test_post_api_mapping(self):
        url = f"http://127.0.0.1:{self.port}/api/mapping"
        payload = json.dumps(
            {
                "updates": [
                    {
                        "gnc_guid": self.guid,
                        "parent_id": "cat_food",
                        "subcat_name": "Restaurants & Dining",
                    }
                ]
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")

        # Verify state reflects change
        state_url = f"http://127.0.0.1:{self.port}/api/state"
        with urllib.request.urlopen(state_url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            acc = data["accounts"][0]
            self.assertEqual(acc["parent_id"], "cat_food")
            self.assertEqual(acc["category_name"], "Restaurants & Dining")
            self.assertTrue(acc["is_custom"])
            self.assertFalse(acc["is_other_expense"])

    def test_backup_prune_and_delete(self):
        # Create extra backups
        for i in range(4):
            (self.backup_dir / f"app_backup_2026101{i}_110000.db").write_bytes(b"data")

        prune_url = f"http://127.0.0.1:{self.port}/api/backups/prune"
        req = urllib.request.Request(
            prune_url,
            data=json.dumps({"keep_latest": 2}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertGreater(len(data["deleted"]), 0)


    def test_serve_index_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            content = resp.read().decode("utf-8")
            self.assertIn("Wealthfolio Importer", content)
            self.assertIn("Destructive Sync Notice", content)
            self.assertIn("Granular Mismatch Resolver", content)

    def test_transaction_override_endpoint(self):
        url = f"http://127.0.0.1:{self.port}/api/transaction_override"
        payload = json.dumps(
            {
                "gnc_trn_guid": "sample-trn-guid-1",
                "payee_pattern": "ATO PAYMENT",
                "parent_id": "cat_fees",
                "subcat_name": "Taxes & Levies",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")

        # Verify saved in mapping.json
        from gnucash_to_wealthfolio.mapper import load_mapping_config

        cfg = load_mapping_config(self.mapping_file)
        self.assertIn("sample-trn-guid-1", cfg.transaction_overrides)
        self.assertEqual(cfg.transaction_overrides["sample-trn-guid-1"]["parent_id"], "cat_fees")
        self.assertEqual(
            cfg.transaction_overrides["sample-trn-guid-1"]["category_name"], "Taxes & Levies"
        )
        self.assertEqual(cfg.rules[0].pattern, "ATO PAYMENT")

    def test_get_api_transactions(self):
        url = f"http://127.0.0.1:{self.port}/api/transactions?page=1&limit=50"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["filtered_total"], 1)
            tx = data["transactions"][0]
            self.assertEqual(tx["guid"], "trn-1")
            self.assertEqual(tx["payee"], "Woolworths Metro")
            self.assertEqual(tx["amount"], 50.0)
            self.assertEqual(tx["direction"], "outflow")
            self.assertEqual(tx["counterpart"], "Expenses:Other Expense Item")
            self.assertEqual(tx["budget_group"], "OTHER")

        # Test filtering by query
        url = f"http://127.0.0.1:{self.port}/api/transactions?q=woolworths"
        with urllib.request.urlopen(urllib.request.Request(url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["filtered_total"], 1)

        url = f"http://127.0.0.1:{self.port}/api/transactions?q=nonexistent"
        with urllib.request.urlopen(urllib.request.Request(url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["filtered_total"], 0)

    def test_post_budget_groups(self):
        url = f"http://127.0.0.1:{self.port}/api/budget_groups"
        payload = json.dumps({"category_id": "cat_food", "budget_group": "NEEDS"}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["budget_group"], "NEEDS")

        # Verify state reflects updated budget group
        state_url = f"http://127.0.0.1:{self.port}/api/state"
        with urllib.request.urlopen(urllib.request.Request(state_url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            food_parent = next((p for p in data["parents"] if p["id"] == "cat_food"), None)
            self.assertIsNotNone(food_parent)
            self.assertEqual(food_parent["budget_group"], "NEEDS")

    def test_post_custom_parent(self):
        url = f"http://127.0.0.1:{self.port}/api/categories/custom_parent"
        payload = json.dumps(
            {
                "name": "Side Hustle",
                "budget_group": "INCOME",
                "color": "#10B981",
                "taxonomy_id": "income_sources",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["category"]["category_name"], "Side Hustle")

        # Verify in state
        state_url = f"http://127.0.0.1:{self.port}/api/state"
        with urllib.request.urlopen(urllib.request.Request(state_url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            sh = next((p for p in data["parents"] if p["name"] == "Side Hustle"), None)
            self.assertIsNotNone(sh)
            self.assertEqual(sh["budget_group"], "INCOME")

    def test_post_accounts_mapping(self):
        # 1. Update bank account name and group
        url = f"http://127.0.0.1:{self.port}/api/accounts_mapping"
        payload = json.dumps(
            {
                "updates": [
                    {
                        "gnc_guid": self.bank_guid,
                        "role": "MIGRATE_CASH_ACTIVE",
                        "wf_name": "Primary Everyday Account",
                        "wf_account_type": "CASH",
                        "wf_group": "Daily Banking",
                        "is_active": 1,
                        "is_archived": 0,
                    }
                ]
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["updated"], 1)

        # Verify in state
        state_url = f"http://127.0.0.1:{self.port}/api/state"
        with urllib.request.urlopen(urllib.request.Request(state_url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("gnc_accounts", data)
            bank_acc = next((a for a in data["gnc_accounts"] if a["gnc_guid"] == self.bank_guid), None)
            self.assertIsNotNone(bank_acc)
            self.assertEqual(bank_acc["wf_name"], "Primary Everyday Account")
            self.assertEqual(bank_acc["wf_group"], "Daily Banking")
            self.assertTrue(bank_acc["is_imported"])

        # 2. Change bank account to IGNORE
        payload = json.dumps(
            {
                "updates": [
                    {
                        "gnc_guid": self.bank_guid,
                        "role": "IGNORE",
                    }
                ]
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

        with urllib.request.urlopen(urllib.request.Request(state_url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            bank_acc = next((a for a in data["gnc_accounts"] if a["gnc_guid"] == self.bank_guid), None)
            self.assertIsNotNone(bank_acc)
            self.assertEqual(bank_acc["role"], "IGNORE")
            self.assertIsNone(bank_acc["wf_account_id"])
            self.assertFalse(bank_acc["is_imported"])

        # 3. Change back to MIGRATE_CARD_ARCHIVED
        payload = json.dumps(
            {
                "updates": [
                    {
                        "gnc_guid": self.bank_guid,
                        "role": "MIGRATE_CARD_ARCHIVED",
                        "wf_name": "Old Visa Card",
                    }
                ]
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

        with urllib.request.urlopen(urllib.request.Request(state_url)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            bank_acc = next((a for a in data["gnc_accounts"] if a["gnc_guid"] == self.bank_guid), None)
            self.assertIsNotNone(bank_acc)
            self.assertEqual(bank_acc["role"], "MIGRATE_CARD_ARCHIVED")
            self.assertIsNotNone(bank_acc["wf_account_id"])
            self.assertTrue(bank_acc["is_imported"])
            self.assertEqual(bank_acc["is_archived"], 1)
            self.assertEqual(bank_acc["wf_name"], "Old Visa Card")

    def test_transaction_override_scopes_and_refresh(self):
        tx_url = f"http://127.0.0.1:{self.port}/api/transactions"
        override_url = f"http://127.0.0.1:{self.port}/api/transaction_override"
        refresh_url = f"http://127.0.0.1:{self.port}/api/refresh"

        # 1. Search by GUID
        with urllib.request.urlopen(urllib.request.Request(f"{tx_url}?q=trn-1")) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["filtered_total"], 1)
            self.assertEqual(data["transactions"][0]["guid"], "trn-1")

        # 2. Scope: transaction override
        payload = json.dumps(
            {
                "scope": "transaction",
                "gnc_trn_guid": "trn-1",
                "parent_id": "cat_food",
                "subcat_name": "Groceries Metro",
                "budget_group": "NEEDS",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            override_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["scope"], "transaction")

        # Verify transaction shows updated classification
        with urllib.request.urlopen(urllib.request.Request(f"{tx_url}?q=trn-1")) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tx = data["transactions"][0]
            self.assertEqual(tx["budget_group"], "NEEDS")
            self.assertEqual(tx["category_name"], "Food & Dining")
            self.assertEqual(tx["subcategory_name"], "Groceries Metro")
            self.assertTrue(tx["is_override"])
            self.assertFalse(tx["needs_review"])

        # 3. Scope: payee rule
        payload = json.dumps(
            {
                "scope": "rule",
                "gnc_trn_guid": "trn-1",
                "payee_pattern": "  Woolworths   Metro  ",
                "parent_id": "cat_groceries",
                "subcat_name": "Woolies Supermarket",
                "budget_group": "NEEDS",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            override_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["scope"], "rule")

        # Verify transaction now inherits rule match
        with urllib.request.urlopen(urllib.request.Request(f"{tx_url}?q=trn-1")) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tx = data["transactions"][0]
            self.assertEqual(tx["budget_group"], "NEEDS")
            self.assertEqual(tx["category_name"], "Groceries")
            self.assertEqual(tx["subcategory_name"], "Woolies Supermarket")

        # 4. Refresh cache endpoint
        req = urllib.request.Request(refresh_url, data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "ok")

        # 5. Scope: account mapping
        payload = json.dumps(
            {
                "scope": "account",
                "gnc_trn_guid": "trn-1",
                "counterpart_guid": self.guid,
                "parent_id": "cat_housing",
                "subcat_name": "Household Expenses",
                "budget_group": "NEEDS",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            override_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["scope"], "account")

    def test_bulk_assign_endpoint(self):
        bulk_url = f"http://127.0.0.1:{self.port}/api/bulk_assign"
        tx_url = f"http://127.0.0.1:{self.port}/api/transactions"

        # 1. Bulk assign via counterpart account
        payload = json.dumps(
            {
                "trn_guids": ["trn-1"],
                "scope": "account",
                "parent_id": "cat_transport",
                "subcat_name": "Vehicle Fuel",
                "budget_group": "NEEDS",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            bulk_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["scope"], "account")
            self.assertEqual(res["updated_count"], 1)

        # Verify transaction table reflects mass update
        with urllib.request.urlopen(urllib.request.Request(f"{tx_url}?q=trn-1")) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tx = data["transactions"][0]
            self.assertEqual(tx["budget_group"], "NEEDS")
            self.assertEqual(tx["category_name"], "Transportation")
            self.assertEqual(tx["subcategory_name"], "Vehicle Fuel")

        # 2. Bulk assign via payee rule
        payload = json.dumps(
            {
                "trn_guids": ["trn-1"],
                "scope": "rule",
                "parent_id": "cat_groceries",
                "subcat_name": "Supermarkets",
                "budget_group": "NEEDS",
                "custom_pattern": "Woolworths",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            bulk_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["scope"], "rule")

        # Verify transaction table reflects rule update
        with urllib.request.urlopen(urllib.request.Request(f"{tx_url}?q=trn-1")) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tx = data["transactions"][0]
            self.assertEqual(tx["budget_group"], "NEEDS")
            self.assertEqual(tx["category_name"], "Groceries")
            self.assertEqual(tx["subcategory_name"], "Supermarkets")

    def test_wealthfolio_preview_and_direct_filtering(self):
        preview_url = f"http://127.0.0.1:{self.port}/api/wealthfolio_preview"
        with urllib.request.urlopen(urllib.request.Request(preview_url)) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertIn("summary", data)
            self.assertIn("total_transactions", data["summary"])
            self.assertIn("direct_count", data["summary"])
            self.assertIn("categories", data)
            self.assertIn("accounts", data)

        # Test direct filter on transactions
        tx_direct_url = f"http://127.0.0.1:{self.port}/api/transactions?direct=1"
        with urllib.request.urlopen(urllib.request.Request(tx_direct_url)) as resp:
            self.assertEqual(resp.status, 200)
            tx_data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("direct_total", tx_data)
            for t in tx_data["transactions"]:
                self.assertTrue(t["is_direct"])


if __name__ == "__main__":
    unittest.main()

