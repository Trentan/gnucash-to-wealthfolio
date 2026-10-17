import sqlite3
import tempfile
from pathlib import Path
from gnucash_to_wealthfolio.migrator import (
    _purge_orphan_valuations_and_snapshots,
    _migrate_alternative_assets_and_liabilities,
    run_migration,
)
from gnucash_to_wealthfolio.models import AccountRole, GncAccount, GncBook
from gnucash_to_wealthfolio.mapper import AccountMapEntry, MappingConfig


def test_purge_orphan_valuations_and_snapshots():
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cur.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT);")
    cur.execute("CREATE TABLE daily_account_valuation (id TEXT PRIMARY KEY, account_id TEXT);")
    cur.execute("CREATE TABLE holdings_snapshots (id TEXT PRIMARY KEY, account_id TEXT);")
    cur.execute("CREATE TABLE snapshot_positions (id TEXT PRIMARY KEY, snapshot_id TEXT);")

    # Insert valid account and valuation
    cur.execute("INSERT INTO accounts VALUES ('acc-1', 'Active Account');")
    cur.execute("INSERT INTO daily_account_valuation VALUES ('dav-1', 'acc-1');")
    cur.execute("INSERT INTO holdings_snapshots VALUES ('snap-1', 'acc-1');")
    cur.execute("INSERT INTO snapshot_positions VALUES ('pos-1', 'snap-1');")

    # Insert orphan valuation (account does not exist)
    cur.execute("INSERT INTO daily_account_valuation VALUES ('dav-orphan', 'acc-deleted');")
    cur.execute("INSERT INTO holdings_snapshots VALUES ('snap-orphan', 'acc-deleted');")
    cur.execute("INSERT INTO snapshot_positions VALUES ('pos-orphan', 'snap-orphan');")

    purged = _purge_orphan_valuations_and_snapshots(cur)
    assert purged == 2

    # Verify only valid records remain
    assert cur.execute("SELECT count(*) FROM daily_account_valuation").fetchone()[0] == 1
    assert cur.execute("SELECT count(*) FROM holdings_snapshots").fetchone()[0] == 1
    assert cur.execute("SELECT count(*) FROM snapshot_positions").fetchone()[0] == 1


def test_account_archive_flags_and_linked_investment_exclusion():
    fixture_dir = Path(__file__).parent / "fixtures"
    source_db = fixture_dir / "sample_wealthfolio.db"

    book = GncBook(
        accounts={
            "guid-act": GncAccount(
                guid="guid-act",
                name="Active Bank",
                full_path="Assets:Active Bank",
                acct_type="BANK",
                parent_guid=None,
                commodity="AUD",
            ),
            "guid-arch": GncAccount(
                guid="guid-arch",
                name="Archived Bank",
                full_path="Assets:Archived Bank",
                acct_type="BANK",
                parent_guid=None,
                commodity="AUD",
            ),
            "guid-linked": GncAccount(
                guid="guid-linked",
                name="Native Brokerage",
                full_path="Assets:Native Brokerage",
                acct_type="BANK",
                parent_guid=None,
                commodity="AUD",
            ),
        },
        transactions=[],
    )

    cfg = MappingConfig(
        accounts={
            "guid-act": AccountMapEntry(
                gnc_guid="guid-act",
                gnc_path="Assets:Active Bank",
                role=AccountRole.MIGRATE_CASH_ACTIVE,
                wf_account_id="wf-act",
                wf_name="Active Bank",
                wf_account_type="CASH",
                wf_group="Banking",
                is_active=1,
                is_archived=0,
            ),
            "guid-arch": AccountMapEntry(
                gnc_guid="guid-arch",
                gnc_path="Assets:Archived Bank",
                role=AccountRole.MIGRATE_CASH_ARCHIVED,
                wf_account_id="wf-arch",
                wf_name="Archived Bank",
                wf_account_type="CASH",
                wf_group="Banking",
                is_active=1,  # even if mistakenly marked 1, should be converted to 0
                is_archived=1,
            ),
            "guid-linked": AccountMapEntry(
                gnc_guid="guid-linked",
                gnc_path="Assets:Native Brokerage",
                role=AccountRole.LINKED_INVESTMENT_ACCOUNT,
                wf_account_id="wf-linked",
                wf_name="Native Brokerage",
                wf_account_type="INVESTMENT",
                wf_group="Investments",
                is_active=1,
                is_archived=0,
                linked_wf_account_id="acc_existing_brokerage",
            ),
        }
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        target_db = tmp_path / "test.db"
        backup_dir = tmp_path / "backups"

        run_migration(book, cfg, source_db, target_db, backup_dir)

        con = sqlite3.connect(target_db)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, name, is_active, is_archived FROM accounts WHERE provider = 'gnucash';"
        ).fetchall()
        con.close()

        # Should only insert the 2 migrated cash accounts, NOT the linked investment account
        account_map = {r[0]: (r[1], r[2], r[3]) for r in rows}
        assert "wf-linked" not in account_map
        assert "wf-act" in account_map
        assert "wf-arch" in account_map

        # Check active and archived flags
        assert account_map["wf-act"] == ("Active Bank", 1, 0)
        assert account_map["wf-arch"] == ("Archived Bank", 0, 1)


def test_suppress_asset_creation_in_alt_assets():
    book = GncBook(
        accounts={
            "guid-prop": GncAccount(
                guid="guid-prop",
                name="Test Property",
                full_path="Assets:Property:Test",
                acct_type="ASSET",
                parent_guid=None,
                commodity="AUD",
            ),
            "guid-linked-prop": GncAccount(
                guid="guid-linked-prop",
                name="Linked Investment",
                full_path="Assets:Investments:Managed",
                acct_type="ASSET",
                parent_guid=None,
                commodity="AUD",
            ),
        },
        transactions=[],
    )

    specs = [
        {
            "gnc_path": "Assets:Property:Test",
            "kind": "REAL_ESTATE",
            "name": "Test Property",
            "display_code": "PROP-1",
            "sub_type": "residential",
            "purchase_price": "500000.00",
            "purchase_date": "2020-01-01",
            "min_quote_date": "2020-01-01",
            "suppress_asset_creation": True,
        },
        {
            "gnc_path": "Assets:Investments:Managed",
            "kind": "INVESTMENT",
            "name": "Linked Investment",
            "display_code": "INV-1",
            "sub_type": "shares",
            "purchase_price": "10000.00",
            "purchase_date": "2020-01-01",
            "min_quote_date": "2020-01-01",
        },
    ]

    cfg = MappingConfig(
        accounts={
            "guid-linked-prop": AccountMapEntry(
                gnc_guid="guid-linked-prop",
                gnc_path="Assets:Investments:Managed",
                role=AccountRole.LINKED_INVESTMENT_ACCOUNT,
            )
        }
    )

    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE assets (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, display_code TEXT, notes TEXT,
            metadata TEXT, is_active INTEGER, quote_mode TEXT, quote_ccy TEXT,
            instrument_type TEXT, instrument_symbol TEXT, instrument_exchange_mic TEXT,
            provider_config TEXT, created_at TEXT, updated_at TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE quotes (
            id TEXT PRIMARY KEY, asset_id TEXT, day TEXT, source TEXT,
            open TEXT, high TEXT, low TEXT, close TEXT, adjclose TEXT,
            volume TEXT, currency TEXT, notes TEXT, created_at TEXT, timestamp TEXT
        );
        """
    )

    created_assets, _ = _migrate_alternative_assets_and_liabilities(
        book, cur, "2026-10-18T00:00:00.000Z", asset_specs=specs, config=cfg
    )
    assert created_assets == 0
    assert cur.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
