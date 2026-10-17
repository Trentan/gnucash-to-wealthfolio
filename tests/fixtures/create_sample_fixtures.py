import gzip
import sqlite3
from pathlib import Path
import xml.etree.ElementTree as ET

FIXTURES_DIR = Path(__file__).parent
GNUCASH_FIXTURE = FIXTURES_DIR / "sample_book.gnucash"
WEALTHFOLIO_FIXTURE = FIXTURES_DIR / "sample_wealthfolio.db"


def build_sample_gnucash():
    namespaces = {
        "xmlns:gnc": "http://www.gnucash.org/XML/gnc",
        "xmlns:act": "http://www.gnucash.org/XML/act",
        "xmlns:book": "http://www.gnucash.org/XML/book",
        "xmlns:cmdty": "http://www.gnucash.org/XML/cmdty",
        "xmlns:slot": "http://www.gnucash.org/XML/slot",
        "xmlns:split": "http://www.gnucash.org/XML/split",
        "xmlns:trn": "http://www.gnucash.org/XML/trn",
        "xmlns:ts": "http://www.gnucash.org/XML/ts",
    }
    root = ET.Element("gnc-v2", namespaces)
    book = ET.SubElement(root, "gnc:book", {"version": "2.0.0"})

    # Book ID
    b_id = ET.SubElement(book, "book:id", {"type": "guid"})
    b_id.text = "sample-book-guid-12345"

    accounts_data = [
        # (guid, name, type, parent_guid)
        ("acc-root", "Root Account", "ROOT", None),
        ("acc-assets", "Assets", "ASSET", "acc-root"),
        ("acc-bank", "Bank Accounts", "ASSET", "acc-assets"),
        ("acc-checking", "Checking Account", "BANK", "acc-bank"),
        ("acc-savings", "Savings Account", "BANK", "acc-bank"),
        ("acc-cash", "Cash", "CASH", "acc-assets"),
        ("acc-liabilities", "Liabilities", "LIABILITY", "acc-root"),
        ("acc-credit", "Credit Cards", "LIABILITY", "acc-liabilities"),
        ("acc-visa", "Visa Card", "CREDIT", "acc-credit"),
        ("acc-expenses", "Expenses", "EXPENSE", "acc-root"),
        ("acc-food", "Food & Dining", "EXPENSE", "acc-expenses"),
        ("acc-groceries", "Groceries", "EXPENSE", "acc-food"),
        ("acc-restaurants", "Restaurants", "EXPENSE", "acc-food"),
        ("acc-utilities", "Utilities", "EXPENSE", "acc-expenses"),
        ("acc-electric", "Electricity", "EXPENSE", "acc-utilities"),
        ("acc-auto", "Auto", "EXPENSE", "acc-expenses"),
        ("acc-gas", "Gas & Fuel", "EXPENSE", "acc-auto"),
        ("acc-income", "Income", "INCOME", "acc-root"),
        ("acc-salary", "Salary", "INCOME", "acc-income"),
        ("acc-invest-inc", "Investment Income", "INCOME", "acc-income"),
        ("acc-dividends", "Dividends", "INCOME", "acc-invest-inc"),
        ("acc-equity", "Equity", "EQUITY", "acc-root"),
        ("acc-opening", "Opening Balances", "EQUITY", "acc-equity"),
    ]

    for guid, name, acct_type, parent_guid in accounts_data:
        acct_el = ET.SubElement(book, "gnc:account", {"version": "2.0.0"})
        ET.SubElement(acct_el, "act:name").text = name
        ET.SubElement(acct_el, "act:id", {"type": "guid"}).text = guid
        ET.SubElement(acct_el, "act:type").text = acct_type
        if parent_guid:
            ET.SubElement(acct_el, "act:parent", {"type": "guid"}).text = parent_guid
        cmdty = ET.SubElement(acct_el, "act:commodity")
        ET.SubElement(cmdty, "cmdty:space").text = "CURRENCY"
        ET.SubElement(cmdty, "cmdty:id").text = "AUD"

    # 25 Transactions
    txs_data = [
        # (guid, date, desc, [(acct_guid, val_num, val_denom, memo)])
        ("tx-01", "2026-01-01", "Opening Balance Checking", [("acc-checking", 100000, 100, ""), ("acc-opening", -100000, 100, "")]),
        ("tx-02", "2026-01-01", "Opening Balance Savings", [("acc-savings", 500000, 100, ""), ("acc-opening", -500000, 100, "")]),
        ("tx-03", "2026-01-01", "Opening Balance Visa", [("acc-visa", -25000, 100, ""), ("acc-opening", 25000, 100, "")]),
        ("tx-04", "2026-01-05", "Employer Paycheck", [("acc-checking", 320000, 100, "Salary Net"), ("acc-salary", -320000, 100, "Gross")]),
        ("tx-05", "2026-01-06", "Woolworths Groceries", [("acc-checking", -8550, 100, ""), ("acc-groceries", 8550, 100, "Weekly food")]),
        ("tx-06", "2026-01-08", "Coles Supermarket", [("acc-checking", -12025, 100, ""), ("acc-groceries", 12025, 100, "")]),
        ("tx-07", "2026-01-10", "Local Cafe", [("acc-visa", -1450, 100, "Coffee"), ("acc-restaurants", 1450, 100, "")]),
        ("tx-08", "2026-01-12", "Italian Restaurant", [("acc-visa", -7800, 100, "Dinner"), ("acc-restaurants", 7800, 100, "")]),
        ("tx-09", "2026-01-15", "Power Corp Electricity", [("acc-checking", -14500, 100, "Q1 Bill"), ("acc-electric", 14500, 100, "")]),
        ("tx-10", "2026-01-16", "BP Service Station", [("acc-visa", -6500, 100, "Fuel"), ("acc-gas", 6500, 100, "")]),
        ("tx-11", "2026-01-18", "Transfer to Savings", [("acc-checking", -50000, 100, "Savings Goal"), ("acc-savings", 50000, 100, "Monthly Transfer")]),
        ("tx-12", "2026-01-20", "Credit Card Repayment", [("acc-checking", -25000, 100, "Visa Payment"), ("acc-visa", 25000, 100, "Payment Received")]),
        ("tx-13", "2026-01-22", "Vanguard Dividend", [("acc-checking", 15000, 100, "Distribution"), ("acc-dividends", -15000, 100, "")]),
        ("tx-14", "2026-01-24", "ATM Cash Out", [("acc-checking", -10000, 100, "ATM"), ("acc-cash", 10000, 100, "Pocket Cash")]),
        ("tx-15", "2026-01-25", "Coffee with Cash", [("acc-cash", -550, 100, ""), ("acc-restaurants", 550, 100, "Morning espresso")]),
        ("tx-16", "2026-01-26", "Groceries Refund", [("acc-checking", 1550, 100, "Refund"), ("acc-groceries", -1550, 100, "Returned item")]),
        ("tx-17", "2026-01-28", "Aldi Supermarket", [("acc-checking", -6240, 100, ""), ("acc-groceries", 6240, 100, "")]),
        ("tx-18", "2026-01-30", "Shell Petrol", [("acc-visa", -7200, 100, ""), ("acc-gas", 7200, 100, "")]),
        ("tx-19", "2026-02-01", "Sushi Lunch", [("acc-visa", -2800, 100, ""), ("acc-restaurants", 2800, 100, "")]),
        ("tx-20", "2026-02-05", "Employer Paycheck Feb", [("acc-checking", 320000, 100, "Salary"), ("acc-salary", -320000, 100, "")]),
        ("tx-21", "2026-02-08", "Supermarket & Utilities Split", [
            ("acc-checking", -15000, 100, "Split Shop"),
            ("acc-groceries", 10000, 100, "Groceries leg"),
            ("acc-electric", 5000, 100, "Lightbulbs leg"),
        ]),
        ("tx-22", "2026-02-10", "Bakery Snack", [("acc-cash", -850, 100, ""), ("acc-restaurants", 850, 100, "")]),
        ("tx-23", "2026-02-12", "Transfer Savings to Checking", [("acc-savings", -20000, 100, ""), ("acc-checking", 20000, 100, "")]),
        ("tx-24", "2026-02-14", "Valentine Dinner", [("acc-visa", -12500, 100, ""), ("acc-restaurants", 12500, 100, "")]),
        ("tx-25", "2026-02-15", "Caltex Fuel", [("acc-visa", -5800, 100, ""), ("acc-gas", 5800, 100, "")]),
    ]

    for t_guid, date, desc, splits in txs_data:
        trn_el = ET.SubElement(book, "gnc:transaction", {"version": "2.0.0"})
        ET.SubElement(trn_el, "trn:id", {"type": "guid"}).text = t_guid
        cmdty = ET.SubElement(trn_el, "trn:currency")
        ET.SubElement(cmdty, "cmdty:space").text = "CURRENCY"
        ET.SubElement(cmdty, "cmdty:id").text = "AUD"
        dp = ET.SubElement(trn_el, "trn:date-posted")
        ET.SubElement(dp, "ts:date").text = f"{date} 00:00:00 +0000"
        de = ET.SubElement(trn_el, "trn:date-entered")
        ET.SubElement(de, "ts:date").text = f"{date} 00:00:00 +0000"
        ET.SubElement(trn_el, "trn:description").text = desc

        splits_el = ET.SubElement(trn_el, "trn:splits")
        for idx, (acc_guid, num, denom, memo) in enumerate(splits):
            sp_el = ET.SubElement(splits_el, "trn:split")
            ET.SubElement(sp_el, "split:id", {"type": "guid"}).text = f"sp-{t_guid}-{idx+1}"
            ET.SubElement(sp_el, "split:reconciled-state").text = "c"
            ET.SubElement(sp_el, "split:value").text = f"{num}/{denom}"
            ET.SubElement(sp_el, "split:quantity").text = f"{num}/{denom}"
            ET.SubElement(sp_el, "split:account", {"type": "guid"}).text = acc_guid
            if memo:
                ET.SubElement(sp_el, "split:memo").text = memo

    # Gzip write
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with gzip.open(GNUCASH_FIXTURE, "wb") as gz:
        gz.write(xml_bytes)
    print(f"Created {GNUCASH_FIXTURE} ({len(xml_bytes)} bytes uncompressed)")


def build_sample_wealthfolio():
    if WEALTHFOLIO_FIXTURE.exists():
        WEALTHFOLIO_FIXTURE.unlink()

    # Copy schema from output/app_migrated.db
    source_db = Path("output/app_migrated.db")
    if not source_db.exists():
        raise FileNotFoundError("output/app_migrated.db not found to copy schema from")

    conn_src = sqlite3.connect(source_db)
    conn_dst = sqlite3.connect(WEALTHFOLIO_FIXTURE)

    # Copy DDL
    src_cur = conn_src.cursor()
    dst_cur = conn_dst.cursor()

    src_cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%';")
    tables = src_cur.fetchall()
    for (sql,) in tables:
        dst_cur.execute(sql)

    # Copy seed data for taxonomies, categories, budget groups
    for tbl in ["taxonomies", "taxonomy_categories", "budget_groups", "budget_group_assignments", "app_settings"]:
        try:
            src_cur.execute(f"SELECT * FROM {tbl};")
            rows = src_cur.fetchall()
            if rows:
                col_names = [d[0] for d in src_cur.description]
                placeholders = ", ".join(["?"] * len(col_names))
                cols_str = ", ".join([f'"{c}"' for c in col_names])
                dst_cur.executemany(f"INSERT INTO {tbl} ({cols_str}) VALUES ({placeholders});", rows)
        except Exception as e:
            print(f"Warning copying {tbl}: {e}")

    # Add 1 pre-existing brokerage account
    now_iso = "2026-01-01T00:00:00.000Z"
    dst_cur.execute(
        """
        INSERT INTO accounts (
            id, name, account_type, "group", currency, is_default, is_active,
            created_at, updated_at, platform_id, account_number, meta,
            provider, provider_account_id, tracking_mode, is_archived
        ) VALUES (
            'acc_existing_brokerage', 'Interactive Brokers', 'INVESTMENT', 'Investments',
            'AUD', 0, 1, ?, ?, NULL, 'U1234567', '{}',
            NULL, NULL, 'MANUAL', 0
        );
        """,
        (now_iso, now_iso),
    )

    conn_dst.commit()
    conn_src.close()
    conn_dst.close()
    print(f"Created {WEALTHFOLIO_FIXTURE}")


if __name__ == "__main__":
    build_sample_gnucash()
    build_sample_wealthfolio()
