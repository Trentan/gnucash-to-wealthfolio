from decimal import Decimal
import pytest
from gnucash_to_wealthfolio.migrator import _find_matching_wf_activity

def test_transfer_matching_within_window():
    existing_activities = [
        {"id": "act-101", "account_id": "inv-acc", "amount": Decimal("10000.00"), "date": "2025-11-03", "claimed": False},
        {"id": "act-102", "account_id": "inv-acc", "amount": Decimal("5000.00"), "date": "2025-11-05", "claimed": False},
    ]

    # Transfer occurs on 2025-11-01 (within 3 days of 2025-11-03)
    match_id = _find_matching_wf_activity("2025-11-01", Decimal("10000.00"), existing_activities, window_days=3)
    assert match_id == "act-101"
    assert existing_activities[0]["claimed"] is True

    # Same transfer cannot be claimed a second time
    second_match = _find_matching_wf_activity("2025-11-01", Decimal("10000.00"), existing_activities, window_days=3)
    assert second_match is None

    # Amount mismatch returns None
    no_match = _find_matching_wf_activity("2025-11-05", Decimal("9999.00"), existing_activities, window_days=3)
    assert no_match is None


def test_transfer_matching_end_to_end_migration(tmp_path):
    import sqlite3
    from gnucash_to_wealthfolio.mapper import AccountMapEntry, MappingConfig
    from gnucash_to_wealthfolio.migrator import run_migration
    from gnucash_to_wealthfolio.models import GncAccount, GncBook, GncSplit, GncTransaction

    # Setup source Wealthfolio DB with an existing investment account and pre-existing activity
    source_db = tmp_path / "source.db"
    conn = sqlite3.connect(source_db)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY, name TEXT, account_type TEXT, "group" TEXT, currency TEXT,
            is_default INTEGER, is_active INTEGER, created_at TEXT, updated_at TEXT,
            platform_id TEXT, account_number TEXT, meta TEXT, provider TEXT,
            provider_account_id TEXT, tracking_mode TEXT, is_archived INTEGER
        );
        CREATE TABLE activities (
            id TEXT PRIMARY KEY, account_id TEXT, asset_id TEXT, activity_type TEXT,
            activity_type_override TEXT, source_type TEXT, subtype TEXT, status TEXT,
            activity_date TEXT, settlement_date TEXT, quantity TEXT, unit_price TEXT,
            amount TEXT, fee TEXT, tax TEXT, currency TEXT, fx_rate TEXT,
            notes TEXT, metadata TEXT, source_system TEXT, source_record_id TEXT,
            source_group_id TEXT, idempotency_key TEXT, import_run_id TEXT,
            is_user_modified INTEGER, needs_review INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE taxonomy_categories (
            id TEXT PRIMARY KEY, taxonomy_id TEXT, parent_id TEXT, key TEXT,
            name TEXT, color TEXT, description TEXT, sort_order INTEGER,
            created_at TEXT, updated_at TEXT, icon TEXT
        );
        CREATE TABLE activity_taxonomy_assignments (
            id TEXT PRIMARY KEY, activity_id TEXT, taxonomy_id TEXT, category_id TEXT,
            weight INTEGER, source TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE spending_activity_splits (
            id TEXT PRIMARY KEY, activity_id TEXT, taxonomy_id TEXT, category_id TEXT,
            amount TEXT, note TEXT, sort_order INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE budget_groups (
            id TEXT PRIMARY KEY, name TEXT, key TEXT, color TEXT, icon TEXT,
            sort_order INTEGER, is_system INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE budget_group_assignments (
            id TEXT PRIMARY KEY, group_id TEXT, taxonomy_id TEXT, category_id TEXT,
            is_system INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE spending_categorization_rules (
            id TEXT PRIMARY KEY, name TEXT, pattern TEXT, match_type TEXT, taxonomy_id TEXT,
            category_id TEXT, activity_type TEXT, priority INTEGER, is_global INTEGER,
            account_id TEXT, preset_id TEXT, preset_rule_key TEXT, preset_version TEXT,
            preset_modified INTEGER, created_at TEXT, updated_at TEXT, amount_op TEXT,
            amount_value TEXT, amount_value2 TEXT
        );
        CREATE TABLE app_settings (
            setting_key TEXT PRIMARY KEY, setting_value TEXT
        );
        CREATE TABLE holdings_snapshots (
            id TEXT PRIMARY KEY, account_id TEXT
        );
        CREATE TABLE snapshot_positions (
            id TEXT PRIMARY KEY, snapshot_id TEXT
        );
        CREATE TABLE daily_account_valuation (
            id TEXT PRIMARY KEY, account_id TEXT
        );
        """
    )
    # Insert existing investment account
    cur.execute(
        """
        INSERT INTO accounts (id, name, account_type, "group", currency, is_default, is_active, created_at, updated_at, is_archived)
        VALUES ('wf-inv-acc', 'Brokerage Account', 'SECURITIES', 'Investments', 'AUD', 0, 1, '2025-01-01', '2025-01-01', 0);
        """
    )
    # Insert existing activity on investment account (date 2025-11-03, amount 1000.00)
    cur.execute(
        """
        INSERT INTO activities (id, account_id, activity_type, status, activity_date, amount, currency, created_at, updated_at)
        VALUES ('act-wf-existing-1', 'wf-inv-acc', 'TRANSFER_IN', 'POSTED', '2025-11-03T00:00:00.000Z', '1000.00', 'AUD', '2025-11-03', '2025-11-03');
        """
    )
    conn.commit()
    conn.close()

    # Construct mock GnuCash book with:
    # 1. Bank account (migrated)
    # 2. Investment account (linked to wf-inv-acc)
    # 3. Tx 1: Transfer from Bank to Investment on 2025-11-02 ($1000.00) -> Should MATCH act-wf-existing-1
    # 4. Tx 2: Transfer from Bank to Investment on 2025-11-10 ($250.00) -> Should NOT match (unmatched)
    gnc_accounts = {
        "bank-guid": GncAccount(
            guid="bank-guid",
            name="Bank Checking",
            full_path="Assets:Current:Bank Checking",
            acct_type="BANK",
            parent_guid=None,
            commodity="AUD",
        ),
        "inv-guid": GncAccount(
            guid="inv-guid",
            name="Brokerage",
            full_path="Assets:Investments:Brokerage",
            acct_type="STOCK",
            parent_guid=None,
            commodity="AUD",
        ),
    }

    tx_matched = GncTransaction(
        guid="tx-matched",
        currency="AUD",
        date_posted="2025-11-02",
        date_entered="2025-11-02",
        description="Transfer to Brokerage",
        splits=[
            GncSplit(guid="sp-m-bank", account_guid="bank-guid", value=Decimal("-1000.00"), quantity=Decimal("-1000.00")),
            GncSplit(guid="sp-m-inv", account_guid="inv-guid", value=Decimal("1000.00"), quantity=Decimal("1000.00")),
        ],
    )

    tx_unmatched = GncTransaction(
        guid="tx-unmatched",
        currency="AUD",
        date_posted="2025-11-10",
        date_entered="2025-11-10",
        description="Second Transfer to Brokerage",
        splits=[
            GncSplit(guid="sp-u-bank", account_guid="bank-guid", value=Decimal("-250.00"), quantity=Decimal("-250.00")),
            GncSplit(guid="sp-u-inv", account_guid="inv-guid", value=Decimal("250.00"), quantity=Decimal("250.00")),
        ],
    )

    book = GncBook(accounts=gnc_accounts, transactions=[tx_matched, tx_unmatched])

    config = MappingConfig(
        accounts={
            "bank-guid": AccountMapEntry(
                gnc_guid="bank-guid",
                gnc_path="Assets:Current:Bank Checking",
                role="MIGRATE_CASH_ACTIVE",
                wf_account_id="wf-bank-acc",
                wf_name="Bank Checking",
                wf_account_type="CASH",
                wf_group="Banking",
                is_active=1,
            ),
            "inv-guid": AccountMapEntry(
                gnc_guid="inv-guid",
                gnc_path="Assets:Investments:Brokerage",
                role="LINKED_INVESTMENT_ACCOUNT",
                linked_wf_account_id="wf-inv-acc",
                linked_wf_name="Brokerage Account",
                suppress_asset_creation=True,
            ),
        }
    )

    target_db = tmp_path / "target.db"
    backup_dir = tmp_path / "backups"

    stats = run_migration(book, config, source_db, target_db, backup_dir)
    assert stats.matched_transfers_count == 1

    # Verify target DB activities
    target_conn = sqlite3.connect(target_db)
    t_cur = target_conn.cursor()

    # Total activities in target DB:
    # 1 pre-existing activity ('act-wf-existing-1')
    # 1 matched transfer activity on bank account (TRANSFER_OUT)
    # 1 external transfer activity on bank account (WITHDRAWAL) - no phantom activity on investment account!
    # Total = 3
    t_cur.execute("SELECT id, account_id, activity_type, source_group_id, amount, metadata FROM activities ORDER BY activity_date;")
    acts = t_cur.fetchall()
    target_conn.close()

    assert len(acts) == 3

    # Matched transfer check
    import json
    matched_bank_acts = [a for a in acts if a[1] == "wf-bank-acc" and Decimal(str(a[4])) == Decimal("1000.00")]
    assert len(matched_bank_acts) == 1
    m_act = matched_bank_acts[0]
    assert m_act[2] == "TRANSFER_OUT"
    assert m_act[3] == "act-wf-existing-1"
    meta = json.loads(m_act[5])
    assert meta["matched_existing_wf_activity_id"] == "act-wf-existing-1"
    assert meta["match_type"] == "exact_amount_window"

    # Unmatched transfer check: external WITHDRAWAL on bank, zero activities injected into wf-inv-acc
    unmatched_acts = [a for a in acts if a[1] == "wf-bank-acc" and Decimal(str(a[4])) == Decimal("250.00")]
    assert len(unmatched_acts) == 1
    u_act = unmatched_acts[0]
    assert u_act[2] == "WITHDRAWAL"
    assert u_act[3] is None  # no source_group_id
    inv_acts = [a for a in acts if a[1] == "wf-inv-acc"]
    assert len(inv_acts) == 1  # only the pre-existing one

