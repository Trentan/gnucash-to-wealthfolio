import json
import sqlite3
from decimal import Decimal
from pathlib import Path
import pytest

from gnucash_to_wealthfolio.mapper import AccountMapEntry, MappingConfig
from gnucash_to_wealthfolio.migrator import run_migration
from gnucash_to_wealthfolio.models import AccountRole, GncAccount, GncBook, GncSplit, GncTransaction
from gnucash_to_wealthfolio.verifier import verify_migration


def _init_source_db(db_path: Path):
    """Initialize a mock Wealthfolio database with required tables, existing accounts, and orphan records."""
    conn = sqlite3.connect(db_path)
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
        CREATE TABLE assets (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, display_code TEXT, notes TEXT,
            metadata TEXT, is_active INTEGER, quote_mode TEXT, quote_ccy TEXT,
            instrument_type TEXT, instrument_symbol TEXT, instrument_exchange_mic TEXT,
            provider_config TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE quotes (
            id TEXT PRIMARY KEY, asset_id TEXT, day TEXT, source TEXT,
            open TEXT, high TEXT, low TEXT, close TEXT, adjclose TEXT,
            volume TEXT, currency TEXT, notes TEXT, created_at TEXT, timestamp TEXT
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

    # 1. Pre-existing Wealthfolio investment account
    cur.execute(
        """
        INSERT INTO accounts (id, name, account_type, "group", currency, is_default, is_active, created_at, updated_at, is_archived)
        VALUES ('wf-pre-inv', 'Pre-existing Brokerage', 'SECURITIES', 'Investments', 'AUD', 0, 1, '2025-01-01', '2025-01-01', 0);
        """
    )
    # 2. Pre-existing activity on investment account (for transfer matching)
    cur.execute(
        """
        INSERT INTO activities (id, account_id, activity_type, status, activity_date, amount, currency, created_at, updated_at)
        VALUES ('act-wf-inv-1', 'wf-pre-inv', 'DEPOSIT', 'POSTED', '2025-06-15T00:00:00.000Z', '1500.00', 'AUD', '2025-06-15', '2025-06-15');
        """
    )

    # 3. Orphan cache records whose accounts do NOT exist in the database
    cur.execute("INSERT INTO daily_account_valuation VALUES ('dav-orphan-1', 'acc-nonexistent-99');")
    cur.execute("INSERT INTO daily_account_valuation VALUES ('dav-orphan-2', 'acc-nonexistent-100');")
    cur.execute("INSERT INTO holdings_snapshots VALUES ('snap-orphan-1', 'acc-nonexistent-99');")
    cur.execute("INSERT INTO snapshot_positions VALUES ('pos-orphan-1', 'snap-orphan-1');")

    conn.commit()
    conn.close()


def test_end_to_end_migration_and_reconciliation(tmp_path: Path):
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"
    backup_dir = tmp_path / "backups"

    _init_source_db(source_db)

    # Set up GnuCash accounts:
    # 1. Active cash account (starts with opening deposit 2500, transfers 1500 to linked investment, transfers 500 to archived bank -> remaining 500.00)
    # 2. Archived bank account (receives 500 transfer from active cash, then 500 transferred to expense or out -> final balance 0.00)
    # 3. Linked investment account (represented in GnuCash as stock/brokerage account)
    # 4. Opening balances equity account
    # 5. An expense category account
    gnc_accounts = {
        "gnc-cash": GncAccount(
            guid="gnc-cash",
            name="Daily Cash Account",
            full_path="Assets:Bank:Daily Cash",
            acct_type="BANK",
            parent_guid=None,
            commodity="AUD",
        ),
        "gnc-archived-bank": GncAccount(
            guid="gnc-archived-bank",
            name="Old Savings Account",
            full_path="Assets:Bank:Old Savings",
            acct_type="BANK",
            parent_guid=None,
            commodity="AUD",
        ),
        "gnc-linked-inv": GncAccount(
            guid="gnc-linked-inv",
            name="External Portfolio Account",
            full_path="Assets:Investments:External Portfolio",
            acct_type="STOCK",
            parent_guid=None,
            commodity="AUD",
        ),
        "gnc-opening": GncAccount(
            guid="gnc-opening",
            name="Opening Balances",
            full_path="Equity:Opening Balances",
            acct_type="EQUITY",
            parent_guid=None,
            commodity="AUD",
        ),
        "gnc-expense": GncAccount(
            guid="gnc-expense",
            name="General Expenses",
            full_path="Expenses:General Expenses",
            acct_type="EXPENSE",
            parent_guid=None,
            commodity="AUD",
        ),
    }

    # Transactions:
    # Tx 1: Opening balance deposit of $2500.00 into gnc-cash on 2025-06-01
    tx_open = GncTransaction(
        guid="tx-open",
        currency="AUD",
        date_posted="2025-06-01",
        date_entered="2025-06-01",
        description="Opening Balance",
        splits=[
            GncSplit(guid="sp-open-cash", account_guid="gnc-cash", value=Decimal("2500.00"), quantity=Decimal("2500.00")),
            GncSplit(guid="sp-open-eq", account_guid="gnc-opening", value=Decimal("-2500.00"), quantity=Decimal("-2500.00")),
        ],
    )

    # Tx 2: Transfer from gnc-cash to linked investment of $1500.00 on 2025-06-15 (matches pre-existing Wealthfolio act-wf-inv-1)
    tx_match_xfer = GncTransaction(
        guid="tx-match-xfer",
        currency="AUD",
        date_posted="2025-06-15",
        date_entered="2025-06-15",
        description="Transfer to Investment Portfolio",
        splits=[
            GncSplit(guid="sp-m-cash", account_guid="gnc-cash", value=Decimal("-1500.00"), quantity=Decimal("-1500.00")),
            GncSplit(guid="sp-m-inv", account_guid="gnc-linked-inv", value=Decimal("1500.00"), quantity=Decimal("1500.00")),
        ],
    )

    # Tx 3: Transfer $500.00 from gnc-cash to gnc-archived-bank on 2025-06-20
    tx_to_archived = GncTransaction(
        guid="tx-to-archived",
        currency="AUD",
        date_posted="2025-06-20",
        date_entered="2025-06-20",
        description="Fund Old Savings",
        splits=[
            GncSplit(guid="sp-ta-cash", account_guid="gnc-cash", value=Decimal("-500.00"), quantity=Decimal("-500.00")),
            GncSplit(guid="sp-ta-arch", account_guid="gnc-archived-bank", value=Decimal("500.00"), quantity=Decimal("500.00")),
        ],
    )

    # Tx 4: Spend $500.00 out of gnc-archived-bank to zero it out completely on 2025-06-25
    tx_zero_archived = GncTransaction(
        guid="tx-zero-archived",
        currency="AUD",
        date_posted="2025-06-25",
        date_entered="2025-06-25",
        description="Final withdrawal closing account",
        splits=[
            GncSplit(guid="sp-za-arch", account_guid="gnc-archived-bank", value=Decimal("-500.00"), quantity=Decimal("-500.00")),
            GncSplit(guid="sp-za-exp", account_guid="gnc-expense", value=Decimal("500.00"), quantity=Decimal("500.00")),
        ],
    )

    book = GncBook(
        accounts=gnc_accounts,
        transactions=[tx_open, tx_match_xfer, tx_to_archived, tx_zero_archived],
    )

    config = MappingConfig(
        accounts={
            "gnc-cash": AccountMapEntry(
                gnc_guid="gnc-cash",
                gnc_path="Assets:Bank:Daily Cash",
                role=AccountRole.MIGRATE_CASH_ACTIVE,
                wf_account_id="wf-cash",
                wf_name="Daily Cash",
                wf_account_type="CASH",
                wf_group="Banking",
                is_active=1,
                is_archived=0,
            ),
            "gnc-archived-bank": AccountMapEntry(
                gnc_guid="gnc-archived-bank",
                gnc_path="Assets:Bank:Old Savings",
                role=AccountRole.MIGRATE_CASH_ARCHIVED,
                wf_account_id="wf-archived-bank",
                wf_name="Old Savings",
                wf_account_type="CASH",
                wf_group="Banking",
                is_active=0,
                is_archived=1,
            ),
            "gnc-linked-inv": AccountMapEntry(
                gnc_guid="gnc-linked-inv",
                gnc_path="Assets:Investments:External Portfolio",
                role=AccountRole.LINKED_INVESTMENT_ACCOUNT,
                linked_wf_account_id="wf-pre-inv",
                linked_wf_name="Pre-existing Brokerage",
                suppress_asset_creation=True,
            ),
        }
    )

    stats = run_migration(book, config, source_db, target_db, backup_dir)
    assert stats.matched_transfers_count == 1

    # Connect to target DB and inspect all requirements
    target_conn = sqlite3.connect(target_db)
    cur = target_conn.cursor()

    # 1. Orphan rows in daily_account_valuation and holdings_snapshots must be 0
    dav_orphan_count = cur.execute(
        "SELECT COUNT(*) FROM daily_account_valuation WHERE account_id NOT IN (SELECT id FROM accounts);"
    ).fetchone()[0]
    assert dav_orphan_count == 0, "No orphaned daily_account_valuation rows should exist"

    snap_orphan_count = cur.execute(
        "SELECT COUNT(*) FROM holdings_snapshots WHERE account_id NOT IN (SELECT id FROM accounts);"
    ).fetchone()[0]
    assert snap_orphan_count == 0, "No orphaned holdings_snapshots rows should exist"

    pos_orphan_count = cur.execute(
        "SELECT COUNT(*) FROM snapshot_positions WHERE snapshot_id NOT IN (SELECT id FROM holdings_snapshots);"
    ).fetchone()[0]
    assert pos_orphan_count == 0, "No orphaned snapshot_positions rows should exist"

    # 2. Account active and archived flags
    cur.execute("SELECT id, name, is_active, is_archived FROM accounts;")
    accounts_by_id = {row[0]: row for row in cur.fetchall()}

    # Pre-existing investment account intact
    assert "wf-pre-inv" in accounts_by_id
    assert accounts_by_id["wf-pre-inv"][2] == 1  # is_active
    assert accounts_by_id["wf-pre-inv"][3] == 0  # is_archived

    # Active cash account
    assert "wf-cash" in accounts_by_id
    assert accounts_by_id["wf-cash"][2] == 1  # is_active = 1
    assert accounts_by_id["wf-cash"][3] == 0  # is_archived = 0

    # Archived account
    assert "wf-archived-bank" in accounts_by_id
    assert accounts_by_id["wf-archived-bank"][2] == 0  # is_active = 0
    assert accounts_by_id["wf-archived-bank"][3] == 1  # is_archived = 1

    # Linked investment account is NOT duplicated in accounts or assets
    all_account_names = [row[1] for row in accounts_by_id.values()]
    assert all_account_names.count("Pre-existing Brokerage") == 1
    assert "External Portfolio Account" not in all_account_names

    cur.execute("SELECT COUNT(*) FROM assets WHERE name = 'External Portfolio Account';")
    assert cur.fetchone()[0] == 0

    # 3. Activity checks: exactly 1 activity on the active cash account was created with matched_existing_wf_activity_id
    # and no duplicate activity was created on the linked investment account
    cur.execute(
        "SELECT id, account_id, activity_type, amount, metadata, source_group_id FROM activities WHERE account_id = 'wf-cash';"
    )
    cash_activities = cur.fetchall()
    # Cash activities:
    # - DEPOSIT: 2500.00 (opening)
    # - TRANSFER_OUT: 1500.00 (matched to act-wf-inv-1)
    # - TRANSFER_OUT: 500.00 (to archived bank)
    assert len(cash_activities) == 3

    matched_acts = []
    for act in cash_activities:
        meta_str = act[4]
        if meta_str:
            meta = json.loads(meta_str)
            if meta.get("matched_existing_wf_activity_id") == "act-wf-inv-1":
                matched_acts.append(act)

    assert len(matched_acts) == 1, "Expected exactly 1 activity on cash account matched to act-wf-inv-1"
    matched_act = matched_acts[0]
    assert matched_act[2] == "TRANSFER_OUT"
    assert Decimal(str(matched_act[3])) == Decimal("1500.00")
    assert matched_act[5] == "act-wf-inv-1"  # source_group_id matched

    # Linked investment account must still have only its 1 pre-existing activity
    cur.execute("SELECT id FROM activities WHERE account_id = 'wf-pre-inv';")
    inv_activities = cur.fetchall()
    assert len(inv_activities) == 1
    assert inv_activities[0][0] == "act-wf-inv-1"

    # Archived bank account activities:
    # - TRANSFER_IN: 500.00 (from cash)
    # - WITHDRAWAL: 500.00 (to expense)
    cur.execute("SELECT activity_type, amount FROM activities WHERE account_id = 'wf-archived-bank';")
    arch_activities = cur.fetchall()
    assert len(arch_activities) == 2

    target_conn.close()

    # 4. Reconciliation verification: both active and archived accounts reconcile down to cent-exact $0.00
    report = verify_migration(book, config, source_db, target_db)
    assert report.pre_existing_accounts_intact is True
    assert report.fk_violations == 0
    assert report.integrity_ok is True

    reconciled_by_id = {r.wf_account_id: r for r in report.reconciliations}
    assert "wf-cash" in reconciled_by_id
    assert "wf-archived-bank" in reconciled_by_id

    # Active cash account: 2500 - 1500 - 500 = $500.00
    cash_rec = reconciled_by_id["wf-cash"]
    assert cash_rec.is_archived == 0
    assert cash_rec.gnc_balance == Decimal("500.00")
    assert cash_rec.wf_balance == Decimal("500.00")
    assert cash_rec.diff == Decimal("0.00")

    # Archived bank account: 500 - 500 = $0.00
    arch_rec = reconciled_by_id["wf-archived-bank"]
    assert arch_rec.is_archived == 1
    assert arch_rec.gnc_balance == Decimal("0.00")
    assert arch_rec.wf_balance == Decimal("0.00")
    assert arch_rec.diff == Decimal("0.00")
