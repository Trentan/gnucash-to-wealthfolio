import unittest
from gnucash_to_wealthfolio.models import GncAccount
from gnucash_to_wealthfolio.mapper import AccountRole
from gnucash_to_wealthfolio.heuristics import (
    classify_gnc_account_tier1,
    match_taxonomy_tier2,
    load_standard_templates,
)


class TestHeuristics(unittest.TestCase):
    def test_tier1_bank_account_classification(self):
        acct = GncAccount(
            guid="a1",
            name="Everyday Checking",
            full_path="Assets:Bank:Everyday Checking",
            acct_type="BANK",
            parent_guid=None,
            commodity="AUD",
        )
        role, wf_name, wf_type, wf_group = classify_gnc_account_tier1(acct, latest_tx_date="2026-10-01")
        self.assertEqual(role, AccountRole.MIGRATE_CASH_ACTIVE)
        self.assertEqual(wf_name, "Everyday Checking")
        self.assertEqual(wf_type, "CASH")
        self.assertEqual(wf_group, "Banking")

    def test_tier1_archived_heuristic_by_date_and_name(self):
        # 1. By name containing 'Archived'
        acct = GncAccount(
            guid="a2",
            name="Old Closed Bank",
            full_path="Assets:Archived:Old Closed Bank",
            acct_type="BANK",
            parent_guid=None,
            commodity="AUD",
        )
        role, _, _, _ = classify_gnc_account_tier1(acct)
        self.assertEqual(role, AccountRole.MIGRATE_CASH_ARCHIVED)

        # 2. By transaction inactivity > 24 months
        acct_dormant = GncAccount(
            guid="a3",
            name="Old Visa",
            full_path="Liabilities:Old Visa",
            acct_type="CREDIT",
            parent_guid=None,
            commodity="AUD",
        )
        role, _, wf_type, _ = classify_gnc_account_tier1(acct_dormant, latest_tx_date="2022-01-01")
        self.assertEqual(role, AccountRole.MIGRATE_CARD_ARCHIVED)
        self.assertEqual(wf_type, "CREDIT_CARD")

    def test_tier2_template_pattern_matching(self):
        spending_rules, income_rules = load_standard_templates()

        # Test Groceries match
        match = match_taxonomy_tier2("Expenses:Food:Supermarket:Woolworths", spending_rules)
        self.assertIsNotNone(match)
        parent_id, budget_group, subcat_name = match
        self.assertEqual(parent_id, "cat_groceries")
        self.assertEqual(budget_group, "needs")
        self.assertEqual(subcat_name, "Supermarket › Woolworths")

        # Test Utilities match
        match = match_taxonomy_tier2("Expenses:Utilities:Electricity", spending_rules)
        self.assertIsNotNone(match)
        parent_id, budget_group, subcat_name = match
        self.assertEqual(parent_id, "cat_bills")
        self.assertEqual(budget_group, "needs")

        # Test Income Salary match
        match = match_taxonomy_tier2("Income:Employer:Salary", income_rules)
        self.assertIsNotNone(match)
        parent_id, budget_group, _ = match
        self.assertEqual(parent_id, "cat_income_employment")
        self.assertEqual(budget_group, "income")


if __name__ == "__main__":
    unittest.main()
