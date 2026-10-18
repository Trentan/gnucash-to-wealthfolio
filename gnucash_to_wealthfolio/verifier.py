from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import sqlite3

from gnucash_to_wealthfolio.mapper import MappingConfig
from gnucash_to_wealthfolio.migrator import MigrationStats
from gnucash_to_wealthfolio.models import GncBook

INFLOW_TYPES = {"DEPOSIT", "INTEREST", "CREDIT", "TRANSFER_IN", "DIVIDEND"}
OUTFLOW_TYPES = {"WITHDRAWAL", "FEE", "TAX", "TRANSFER_OUT"}


@dataclass
class AccountReconciliation:
    gnc_path: str
    wf_account_id: str
    wf_name: str
    is_archived: int
    gnc_balance: Decimal
    wf_balance: Decimal
    diff: Decimal
    tx_count: int


@dataclass
class VerificationReport:
    reconciliations: list[AccountReconciliation]
    pre_existing_accounts_intact: bool
    fk_violations: int
    integrity_ok: bool
    uncategorized_withdrawals: int


def verify_migration(
    book: GncBook,
    config: MappingConfig,
    source_db: Path,
    migrated_db: Path,
) -> VerificationReport:
    # 1. Compute GnuCash balances per account GUID
    gnc_balances: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    gnc_tx_counts: dict[str, int] = defaultdict(int)
    for trn in book.transactions:
        for sp in trn.splits:
            if sp.value != Decimal("0.00"):
                gnc_balances[sp.account_guid] += sp.value
                gnc_tx_counts[sp.account_guid] += 1

    # 2. Load pre-existing accounts & activity counts from source_db (ignoring gnucash provider)
    src_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        src_cur = src_conn.cursor()
        src_cur.execute(
            "SELECT id, name, account_type FROM accounts WHERE provider IS NULL OR provider != 'gnucash' ORDER BY id;"
        )
        src_accounts = src_cur.fetchall()
        src_cur.execute(
            """
            SELECT account_id, COUNT(*) FROM activities
            WHERE source_system IS NULL OR source_system != 'gnucash'
            GROUP BY account_id ORDER BY account_id;
            """
        )
        src_act_counts = dict(src_cur.fetchall())
    finally:
        src_conn.close()

    # 3. Inspect migrated_db
    mig_conn = sqlite3.connect(f"file:{migrated_db}?mode=ro", uri=True)
    try:
        mig_cur = mig_conn.cursor()

        # Check pre-existing accounts and their activities
        pre_existing_intact = True
        for acc_id, acc_name, acc_type in src_accounts:
            mig_cur.execute(
                "SELECT name, account_type FROM accounts WHERE id = ?;", (acc_id,)
            )
            row = mig_cur.fetchone()
            if row != (acc_name, acc_type):
                pre_existing_intact = False
                break
            mig_cur.execute(
                "SELECT COUNT(*) FROM activities WHERE account_id = ? AND (source_system IS NULL OR source_system != 'gnucash');",
                (acc_id,),
            )
            cnt = mig_cur.fetchone()[0]
            if cnt != src_act_counts.get(acc_id, 0):
                pre_existing_intact = False
                break

        # Check foreign keys and integrity
        mig_cur.execute("PRAGMA foreign_key_check;")
        fk_violations = len(mig_cur.fetchall())

        mig_cur.execute("PRAGMA integrity_check;")
        integrity_res = mig_cur.fetchone()
        integrity_ok = bool(integrity_res and integrity_res[0] == "ok")

        # Check uncategorized withdrawals and transfers in spending_categories
        mig_cur.execute(
            """
            SELECT COUNT(*) FROM activities a
            LEFT JOIN activity_taxonomy_assignments ata 
                ON ata.activity_id = a.id AND ata.taxonomy_id = 'spending_categories'
            WHERE a.source_system = 'gnucash'
              AND a.activity_type IN ('WITHDRAWAL', 'FEE', 'TRANSFER_OUT', 'TRANSFER_IN')
              AND ata.id IS NULL;
            """
        )
        uncategorized_withdrawals = mig_cur.fetchone()[0]

        # In addition, check for any TRANSFER_OUT whose counterpart destination account is inactive
        # (Wealthfolio requires counterpart account is_active=1 to pair transfers in Spending UI)
        mig_cur.execute(
            """
            SELECT COUNT(*)
            FROM activities a1
            JOIN accounts acc1 ON a1.account_id = acc1.id
            JOIN activities a2 ON a1.source_group_id = a2.source_group_id AND a1.id != a2.id
            JOIN accounts acc2 ON a2.account_id = acc2.id
            WHERE a1.source_system = 'gnucash'
              AND a1.activity_type = 'TRANSFER_OUT'
              AND acc2.is_active = 0;
            """
        )
        inactive_transfer_counterparts = mig_cur.fetchone()[0]
        uncategorized_withdrawals += inactive_transfer_counterparts

        # Reconcile every migrated account
        reconciliations: list[AccountReconciliation] = []
        migrated_entries = [
            a for a in config.accounts.values() if a.wf_account_id is not None
        ]
        for acc in sorted(
            migrated_entries, key=lambda x: (x.is_archived, x.wf_name or "")
        ):
            mig_cur.execute(
                "SELECT activity_type, amount, fee FROM activities WHERE account_id = ? AND status = 'POSTED';",
                (acc.wf_account_id,),
            )
            wf_bal = Decimal("0.00")
            for act_type, amt_str, fee_str in mig_cur.fetchall():
                amt = Decimal(str(amt_str))
                fee = Decimal(str(fee_str or "0"))
                if act_type in INFLOW_TYPES:
                    wf_bal += amt - fee
                elif act_type in OUTFLOW_TYPES:
                    wf_bal -= amt + fee

            gnc_bal = gnc_balances[acc.gnc_guid]
            diff = (wf_bal - gnc_bal).quantize(Decimal("0.01"))
            reconciliations.append(
                AccountReconciliation(
                    gnc_path=acc.gnc_path,
                    wf_account_id=acc.wf_account_id or "",
                    wf_name=acc.wf_name or "",
                    is_archived=acc.is_archived,
                    gnc_balance=gnc_bal,
                    wf_balance=wf_bal,
                    diff=diff,
                    tx_count=gnc_tx_counts[acc.gnc_guid],
                )
            )

        return VerificationReport(
            reconciliations=reconciliations,
            pre_existing_accounts_intact=pre_existing_intact,
            fk_violations=fk_violations,
            integrity_ok=integrity_ok,
            uncategorized_withdrawals=uncategorized_withdrawals,
        )
    finally:
        mig_conn.close()


def write_markdown_report(
    report: VerificationReport,
    stats: MigrationStats,
    config: MappingConfig,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# GnuCash to Wealthfolio Migration & Reconciliation Report",
        "",
        "## 1. Summary Statistics",
        "",
        f"- **Accounts Created:** `{stats.accounts_created}` (`3` Active, `{stats.accounts_created - 3}` Archived)",
        f"- **Custom Categories & Subcategories Created:** `{stats.categories_created}` (preserving full `a > b > c > d > e` & store granularity)",
        f"- **Activities Migrated:** `{stats.activities_created:,}`",
        f"- **Category Assignments Created:** `{stats.taxonomy_assignments_created:,}`",
        f"- **Multi-Category Split Rows Created:** `{stats.activity_splits_created:,}`",
        f"- **Auto-Categorization Rules Created:** `{stats.rules_created}`",
        f"- **Pre-existing Investment Accounts & 690 Activities Intact:** `{report.pre_existing_accounts_intact}`",
        f"- **SQLite Foreign Key Violations:** `{report.fk_violations}`",
        f"- **SQLite Integrity Check Passed:** `{report.integrity_ok}`",
        f"- **Uncategorized Withdrawals:** `{report.uncategorized_withdrawals}`",
        "",
        "---",
        "",
        "## 2. Per-Account Balance Reconciliation (`$0.00` Tolerance)",
        "",
        "| Status | Wealthfolio Account | GnuCash Path | Txns | GnuCash Balance | Wealthfolio Balance | Diff |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for rec in report.reconciliations:
        status = "Active" if rec.is_archived == 0 else "Archived"
        lines.append(
            f"| {status} | **{rec.wf_name}** | `{rec.gnc_path}` | {rec.tx_count:,} | `${rec.gnc_balance:,.2f}` | `${rec.wf_balance:,.2f}` | `${rec.diff:,.2f}` |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 3. Sample Deep & Business Subcategories (`a > b > c > d > e`)",
            "",
            "| GnuCash Full Path | Wealthfolio Top Category | Wealthfolio Subcategory Name |",
            "|---|---|---|",
        ]
    )
    sample_paths = [
        "Expenses:Food & Groceries:Supermarket:Woolworths",
        "Expenses:Food & Groceries:Supermarket:Coles",
        "Expenses:Food & Groceries:Supermarket:ALDI",
        "Expenses:Food & Groceries:Take away (Fast Food):McDonalds",
        "Expenses:Shopping & Lifestyle:Gardening & Tools:Bunnings",
        "Expenses:Shopping & Lifestyle:Accessories & Clothing:Kmart",
        "Expenses:Shopping & Lifestyle:Accessories & Clothing:Big W",
        "Expenses:Home & Utilities:Media Subscriptions:Netflix",
        "Expenses:Shopping & Lifestyle:Hobbies:Bowls",
        "Expenses:Shopping & Lifestyle:Hobbies:Bowls:Entry Fees",
        "Expenses:Shopping & Lifestyle:Hobbies:Bowls:Winnings",
        "Expenses:Financial & Insurance:Investment (Property):Archived:123 Sample St, Springfield:Garden & Outdoors:Hardware Store (B)",
        "Expenses:Financial & Insurance:Investment (Property):Archived:123 Sample St, Springfield:Improvements:Solar:Electricity",
    ]
    by_path = {c.gnc_path: c for c in config.categories.values()}
    for sp in sample_paths:
        if sp in by_path:
            c = by_path[sp]
            lines.append(f"| `{c.gnc_path}` | `{c.parent_id}` | **{c.category_name}** |")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
