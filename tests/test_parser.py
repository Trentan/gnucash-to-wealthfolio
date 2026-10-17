import unittest
from decimal import Decimal
from pathlib import Path
from gnucash_to_wealthfolio.ids import stable_id, deterministic_uuid
from gnucash_to_wealthfolio.parser import parse_rational, parse_gnucash_file


class TestParser(unittest.TestCase):
    def test_parse_rational_and_ids(self):
        self.assertEqual(parse_rational("-12345/100"), Decimal("-123.45"))
        self.assertEqual(parse_rational("0/100"), Decimal("0.00"))
        self.assertEqual(parse_rational("500/100"), Decimal("5.00"))
        sid = stable_id("budget_group_assignment", ["spending_categories", "cat_food"])
        self.assertEqual(len(sid), 64)
        self.assertEqual(sid, stable_id("budget_group_assignment", ["spending_categories", "cat_food"]))
        uid = deterministic_uuid("gnucash_account", "abc-123")
        self.assertEqual(len(uid), 36)
        self.assertEqual(uid, deterministic_uuid("gnucash_account", "abc-123"))

    def test_parse_sample_gnucash_file(self):
        sample_path = Path(__file__).parent / "fixtures" / "sample_book.gnucash"
        book = parse_gnucash_file(sample_path)
        self.assertEqual(len(book.accounts), 23)
        self.assertEqual(len(book.transactions), 25)
        checking = [
            a for a in book.accounts.values() if a.full_path == "Assets:Bank Accounts:Checking Account"
        ]
        self.assertEqual(len(checking), 1)
        self.assertEqual(checking[0].commodity, "AUD")


if __name__ == "__main__":
    unittest.main()
