from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import shutil
import sqlite3

from gnucash_to_wealthfolio.ids import deterministic_uuid, stable_id
from gnucash_to_wealthfolio.mapper import AccountRole, CategoryMapEntry, MappingConfig
from gnucash_to_wealthfolio.models import GncBook, GncSplit, GncTransaction


@dataclass
class MigrationStats:
    accounts_created: int
    categories_created: int
    activities_created: int
    taxonomy_assignments_created: int
    activity_splits_created: int
    rules_created: int
    alt_assets_created: int = 0
    alt_quotes_created: int = 0
    matched_transfers_count: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _format_notes(trn: GncTransaction, split: GncSplit) -> str:
    desc = trn.description.strip()
    memo = split.memo.strip()
    if desc and memo and memo.lower() not in desc.lower():
        return f"{desc} — {memo}"
    return desc or memo or "GnuCash Transaction"


def _find_matching_wf_activity(
    candidate_date: str,
    amount: Decimal,
    candidate_activities: list[dict],
    window_days: int = 3,
) -> str | None:
    """Find an existing unclaimed Wealthfolio activity matching date window and amount."""
    d_candidate_str = candidate_date.split("T")[0]
    d_candidate = datetime.strptime(d_candidate_str, "%Y-%m-%d").date()

    for act in candidate_activities:
        if act.get("claimed"):
            continue
        act_date_str = str(act["date"]).split("T")[0]
        d_act = datetime.strptime(act_date_str, "%Y-%m-%d").date()
        if (
            abs((d_candidate - d_act).days) <= window_days
            and abs(Decimal(str(amount)) - Decimal(str(act["amount"]))) < Decimal("0.001")
        ):
            act["claimed"] = True
            return str(act["id"])
    return None


def _normalize_split_amounts(
    raw_items: list[tuple[CategoryMapEntry, Decimal, str]],
    target_total: Decimal,
) -> list[tuple[CategoryMapEntry, Decimal, str]]:
    """Ensure positive split amounts sum to target_total and each is > 0.00."""
    positive = [(cat, amt, memo) for cat, amt, memo in raw_items if amt > Decimal("0.00")]
    if not positive:
        return []
    raw_sum = sum((amt for _, amt, _ in positive), Decimal("0.00"))
    if raw_sum <= Decimal("0.00"):
        return []

    scaled: list[tuple[CategoryMapEntry, Decimal, str]] = []
    running = Decimal("0.00")
    for idx, (cat, amt, memo) in enumerate(positive):
        if idx == len(positive) - 1:
            part = (target_total - running).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            part = ((amt / raw_sum) * target_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if part <= Decimal("0.00"):
                part = Decimal("0.01")
            running += part
        scaled.append((cat, part, memo))

    # Fix any last-element rounding underflow
    if scaled[-1][1] <= Decimal("0.00"):
        return [(positive[0][0], target_total, positive[0][2])]
    return scaled


def _purge_orphan_valuations_and_snapshots(cur: sqlite3.Cursor) -> int:
    """Purge orphaned daily_account_valuation and holdings_snapshots whose account does not exist."""
    cur.execute(
        """
        DELETE FROM snapshot_positions
        WHERE snapshot_id IN (
            SELECT id FROM holdings_snapshots WHERE account_id NOT IN (SELECT id FROM accounts)
        );
        """
    )
    cur.execute(
        "DELETE FROM holdings_snapshots WHERE account_id NOT IN (SELECT id FROM accounts);"
    )
    deleted_snapshots = cur.rowcount if cur.rowcount != -1 else 0

    cur.execute(
        "DELETE FROM daily_account_valuation WHERE account_id NOT IN (SELECT id FROM accounts);"
    )
    deleted_valuations = cur.rowcount if cur.rowcount != -1 else 0

    return deleted_snapshots + deleted_valuations


ALTERNATIVE_ASSET_SPECS: list[dict] = []


def _migrate_alternative_assets_and_liabilities(
    book: GncBook,
    cur: sqlite3.Cursor,
    now: str,
    asset_specs: list[dict] | None = None,
    config: MappingConfig | None = None,
) -> tuple[int, int]:
    """Migrate Properties, Vehicles, Superannuation, and Loans/Mortgages into assets + quotes."""
    specs = asset_specs if asset_specs is not None else ALTERNATIVE_ASSET_SPECS
    if not specs:
        return 0, 0

    by_path = {a.full_path: a for a in book.accounts.values()}
    asset_id_by_path: dict[str, str] = {}
    for spec in specs:
        gnc_acc = by_path.get(spec["gnc_path"])
        if gnc_acc is not None:
            asset_id_by_path[spec["gnc_path"]] = deterministic_uuid(
                "gnucash_alt_asset", gnc_acc.guid
            )

    today_str = datetime.now().strftime("%Y-%m-%d")
    assets_created = 0
    quotes_created = 0

    # Map path to account config if available for suppression / linked investment checks
    acct_by_path = {a.gnc_path: a for a in config.accounts.values()} if config is not None else {}

    for spec in specs:
        if spec.get("suppress_asset_creation"):
            continue
        mapped_acc = acct_by_path.get(spec["gnc_path"])
        if mapped_acc is not None:
            if getattr(mapped_acc, "suppress_asset_creation", False):
                continue
            if getattr(mapped_acc, "role", None) == AccountRole.LINKED_INVESTMENT_ACCOUNT:
                continue

        gnc_acc = by_path.get(spec["gnc_path"])
        if gnc_acc is None:
            continue
        asset_id = asset_id_by_path[spec["gnc_path"]]

        # Delete any prior migrated row (cascade deletes quotes)
        cur.execute("DELETE FROM assets WHERE id = ?;", (asset_id,))

        meta: dict[str, str] = {
            "sub_type": spec["sub_type"],
            "source_system": "gnucash",
            "gnucash_guid": gnc_acc.guid,
            "gnucash_path": gnc_acc.full_path,
        }
        if spec["kind"] == "LIABILITY":
            meta["tracking_mode"] = "manual"
            meta["original_amount"] = spec["original_amount"]
            meta["origination_date"] = spec["origination_date"]
            linked_path = spec.get("linked_gnc_path")
            if linked_path and linked_path in asset_id_by_path:
                meta["linked_asset_id"] = asset_id_by_path[linked_path]
        else:
            meta["purchase_price"] = spec["purchase_price"]
            meta["purchase_date"] = spec["purchase_date"]
            if "address" in spec:
                meta["address"] = spec["address"]

        cur.execute(
            """
            INSERT INTO assets (
                id, kind, name, display_code, notes, metadata, is_active,
                quote_mode, quote_ccy, instrument_type, instrument_symbol,
                instrument_exchange_mic, provider_config, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 'MANUAL', 'AUD', NULL, NULL, NULL, NULL, ?, ?);
            """,
            (
                asset_id,
                spec["kind"],
                spec["name"],
                spec["display_code"],
                f"Migrated from GnuCash: {gnc_acc.full_path}",
                json.dumps(meta),
                now,
                now,
            ),
        )
        assets_created += 1

        # Build chronological daily closing balances from GnuCash splits
        daily_deltas: dict[str, Decimal] = {}
        for trn in book.transactions:
            for sp in trn.splits:
                if sp.account_guid == gnc_acc.guid and sp.value != Decimal("0.00"):
                    daily_deltas[trn.date_posted] = (
                        daily_deltas.get(trn.date_posted, Decimal("0.00")) + sp.value
                    )

        running = Decimal("0.00")
        min_date = spec["min_quote_date"]
        quote_by_day: dict[str, Decimal] = {}
        for day in sorted(daily_deltas.keys()):
            running += daily_deltas[day]
            if day >= min_date and abs(running) > Decimal("0.00"):
                quote_by_day[day] = abs(running)

        final_abs = abs(running)
        if final_abs > Decimal("0.00"):
            quote_by_day[today_str] = final_abs

        quote_rows = []
        for day, val in sorted(quote_by_day.items()):
            val_str = f"{val:.2f}"
            q_id = f"{asset_id}_{day}_MANUAL"
            ts_str = f"{day}T12:00:00+00:00"
            quote_rows.append(
                (
                    q_id,
                    asset_id,
                    day,
                    "MANUAL",
                    val_str,
                    val_str,
                    val_str,
                    val_str,
                    val_str,
                    "0",
                    "AUD",
                    None,
                    now,
                    ts_str,
                )
            )

        cur.executemany(
            """
            INSERT OR REPLACE INTO quotes (
                id, asset_id, day, source, open, high, low, close, adjclose,
                volume, currency, notes, created_at, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            quote_rows,
        )
        quotes_created += len(quote_rows)

    return assets_created, quotes_created


def run_migration(
    book: GncBook,
    config: MappingConfig,
    source_db: Path,
    target_db: Path,
    backup_dir: Path,
) -> MigrationStats:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target_db.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"app_backup_{ts}.db"
    if target_db.exists():
        target_db.unlink()
    src_conn = sqlite3.connect(str(source_db))
    try:
        for dst_path in (backup_path, target_db):
            dst_conn = sqlite3.connect(str(dst_path))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
    finally:
        src_conn.close()

    now = _now_iso()
    conn = sqlite3.connect(str(target_db))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.cursor()

        # 1. Clean any prior gnucash migration rows for idempotency
        cur.execute("DELETE FROM activities WHERE source_system = 'gnucash';")
        cur.execute("DELETE FROM accounts WHERE provider = 'gnucash';")
        cur.execute("DELETE FROM spending_categorization_rules WHERE id LIKE '________-____-5___-____-____________' AND (name LIKE 'Auto: %' OR name LIKE 'Payee: %');")
        cur.execute("DELETE FROM taxonomy_categories WHERE id LIKE 'cat_gnc_%';")
        cur.execute(
            "DELETE FROM taxonomy_categories WHERE id IN ('cat_family_pets', 'cat_investment_property', 'cat_income_government');"
        )

        # 1a. Clean stale cached snapshots & daily valuations for migrated accounts
        gnc_acc_ids = [
            a.wf_account_id
            for a in config.accounts.values()
            if a.wf_account_id is not None
        ]
        if gnc_acc_ids:
            placeholders = ",".join("?" for _ in gnc_acc_ids)
            cur.execute(
                f"""
                DELETE FROM snapshot_positions 
                WHERE snapshot_id IN (
                    SELECT id FROM holdings_snapshots WHERE account_id IN ({placeholders})
                );
                """,
                gnc_acc_ids,
            )
            cur.execute(
                f"DELETE FROM holdings_snapshots WHERE account_id IN ({placeholders});",
                gnc_acc_ids,
            )
            cur.execute(
                f"DELETE FROM daily_account_valuation WHERE account_id IN ({placeholders});",
                gnc_acc_ids,
            )

        # 1b. Purge any historical dangling valuation / snapshot records for non-existent accounts
        _purge_orphan_valuations_and_snapshots(cur)

        # 1c. Migrate standalone Alternative Assets & Liabilities (Properties, Vehicles, Super, Loans)
        alt_specs = getattr(config, "alternative_assets", None)
        alt_assets_created, alt_quotes_created = _migrate_alternative_assets_and_liabilities(
            book, cur, now, asset_specs=alt_specs, config=config
        )

        # 2. Insert migrated accounts
        migrated_accounts = [
            a for a in config.accounts.values()
            if a.wf_account_id is not None and a.role != AccountRole.LINKED_INVESTMENT_ACCOUNT
        ]
        accounts_created = 0
        for acc in sorted(migrated_accounts, key=lambda x: (x.is_archived, x.wf_name or "")):
            meta_json = json.dumps(
                {
                    "source_system": "gnucash",
                    "gnucash_guid": acc.gnc_guid,
                    "gnucash_path": acc.gnc_path,
                }
            )
            # Ensure is_active is 1 so Wealthfolio's portfolio engine builds historical
            # valuations back to the earliest transaction (e.g. 2007), while is_archived
            # cleanly organizes closed accounts into the Archived section.
            is_active_val = 1
            cur.execute(
                """
                INSERT INTO accounts (
                    id, name, account_type, "group", currency, is_default, is_active,
                    created_at, updated_at, platform_id, account_number, meta,
                    provider, provider_account_id, tracking_mode, is_archived
                ) VALUES (?, ?, ?, ?, 'AUD', 0, ?, ?, ?, NULL, NULL, ?, 'gnucash', ?, 'TRANSACTIONS', ?);
                """,
                (
                    acc.wf_account_id,
                    acc.wf_name,
                    acc.wf_account_type,
                    acc.wf_group,
                    is_active_val,
                    now,
                    now,
                    meta_json,
                    acc.gnc_guid,
                    acc.is_archived,
                ),
            )
            accounts_created += 1

        # 3. Insert custom root categories and budget group assignments
        cur.execute("SELECT key, id, name FROM budget_groups;")
        budget_group_id_by_key = {}
        for row in cur.fetchall():
            k, bg_id, n = row[0], row[1], row[2]
            budget_group_id_by_key[k] = bg_id
            budget_group_id_by_key[n.lower()] = bg_id

        if "investment" not in budget_group_id_by_key:
            inv_group_id = "163fb3bd-ead8-4650-80d1-ef35adbf62b5"
            inv_key = "custom_cee210d6-684d-46fd-b765-03e47a3182fb"
            cur.execute(
                """
                INSERT OR IGNORE INTO budget_groups (
                    id, name, key, color, icon, sort_order, is_system, created_at, updated_at
                ) VALUES (?, 'Investment', ?, '#2A573F', 'DollarSign', 0, 0, ?, ?);
                """,
                (inv_group_id, inv_key, now, now),
            )
            budget_group_id_by_key["investment"] = inv_group_id
            budget_group_id_by_key[inv_key] = inv_group_id

        cur.execute(
            "DELETE FROM budget_group_assignments WHERE category_id IN ('cat_family_pets', 'cat_investment_property', 'cat_income_government');"
        )

        categories_created = 0
        sort_order = 500
        for root_cat in config.custom_root_categories:
            cur.execute(
                """
                INSERT OR REPLACE INTO taxonomy_categories (
                    id, taxonomy_id, parent_id, key, name, color, description,
                    sort_order, created_at, updated_at, icon
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    root_cat.category_id,
                    root_cat.taxonomy_id,
                    root_cat.category_key,
                    root_cat.category_name,
                    root_cat.color,
                    f"Migrated from GnuCash: {root_cat.gnc_path}",
                    sort_order,
                    now,
                    now,
                    root_cat.icon,
                ),
            )
            categories_created += 1
            sort_order += 10

        # 4. Insert custom Level-2 subcategories and any mapped/override categories
        cur.execute("SELECT taxonomy_id, id FROM taxonomy_categories;")
        existing_cat_keys = set(cur.fetchall())

        for cat in sorted(config.categories.values(), key=lambda c: c.gnc_path):
            pair = (cat.taxonomy_id, cat.category_id)
            if pair in existing_cat_keys:
                continue
            existing_cat_keys.add(pair)
            parent_id = cat.parent_id
            if parent_id and (cat.taxonomy_id, parent_id) not in existing_cat_keys:
                parent_id = None
            cur.execute(
                """
                INSERT OR IGNORE INTO taxonomy_categories (
                    id, taxonomy_id, parent_id, key, name, color, description,
                    sort_order, created_at, updated_at, icon
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    cat.category_id,
                    cat.taxonomy_id,
                    parent_id,
                    cat.category_key,
                    cat.category_name,
                    cat.color,
                    f"Migrated from GnuCash: {cat.gnc_path}",
                    sort_order,
                    now,
                    now,
                    cat.icon,
                ),
            )
            categories_created += 1
            sort_order += 1

        # 4a-0. Fallback subcategories to guarantee no transaction is assigned directly to a root category
        fallback_subcats = [
            (
                "cat_gnc_other_expenses_uncat",
                "spending_categories",
                "cat_other_expense",
                "gnc_other_expenses_uncat",
                "Uncategorized Expense",
                "#808080",
            ),
            (
                "cat_gnc_transfers_internal",
                "spending_categories",
                "cat_other_expense",
                "gnc_transfers_internal",
                "Internal Transfers",
                "#808080",
            ),
            (
                "cat_gnc_other_income_uncat",
                "income_sources",
                "cat_income_other",
                "gnc_other_income_uncat",
                "Uncategorized Income",
                "#808080",
            ),
            (
                "cat_gnc_investment_income_uncat",
                "income_sources",
                "cat_income_investment",
                "gnc_investment_income_uncat",
                "General Investment Income",
                "#808080",
            ),
        ]
        for f_id, f_tax, f_parent, f_key, f_name, f_color in fallback_subcats:
            pair = (f_tax, f_id)
            if pair not in existing_cat_keys:
                existing_cat_keys.add(pair)
                cur.execute(
                    """
                    INSERT OR IGNORE INTO taxonomy_categories (
                        id, taxonomy_id, parent_id, key, name, color, description,
                        sort_order, created_at, updated_at, icon
                    ) VALUES (?, ?, ?, ?, ?, ?, 'Fallback subcategory to prevent direct root assignment', ?, ?, ?, NULL);
                    """,
                    (f_id, f_tax, f_parent, f_key, f_name, f_color, sort_order, now, now),
                )
                categories_created += 1
                sort_order += 1

        # 4a. Categories referenced in transaction overrides
        tx_overrides = getattr(config, "transaction_overrides", {})
        for trn_guid, ov in tx_overrides.items():
            ov_cat_id = ov.get("category_id")
            if not ov_cat_id:
                continue
            tax_id = ov.get("taxonomy_id", "spending_categories")
            pair = (tax_id, ov_cat_id)
            if pair in existing_cat_keys:
                continue
            existing_cat_keys.add(pair)
            ov_name = ov.get("category_name", "Custom Category")
            ov_parent = ov.get("parent_id")
            if ov_parent and (tax_id, ov_parent) not in existing_cat_keys:
                ov_parent = None
            ov_key = ov_cat_id.replace("cat_", "")
            cur.execute(
                """
                INSERT OR IGNORE INTO taxonomy_categories (
                    id, taxonomy_id, parent_id, key, name, color, description,
                    sort_order, created_at, updated_at, icon
                ) VALUES (?, ?, ?, ?, ?, '#808080', 'Transaction override category', ?, ?, ?, NULL);
                """,
                (
                    ov_cat_id,
                    tax_id,
                    ov_parent,
                    ov_key,
                    ov_name,
                    sort_order,
                    now,
                    now,
                ),
            )
            categories_created += 1
            sort_order += 1

        # 4b. Categories referenced in payee / auto rules
        for rule in getattr(config, "rules", []):
            rule_cat_id = rule.category_id
            if not rule_cat_id:
                continue
            pair = (rule.taxonomy_id, rule_cat_id)
            if pair in existing_cat_keys:
                continue
            existing_cat_keys.add(pair)
            rule_name = (
                getattr(rule, "name", "").replace("Payee: ", "").replace("Auto: ", "")
                or "Rule Category"
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO taxonomy_categories (
                    id, taxonomy_id, parent_id, key, name, color, description,
                    sort_order, created_at, updated_at, icon
                ) VALUES (?, ?, NULL, ?, ?, '#808080', 'Rule category', ?, ?, ?, NULL);
                """,
                (
                    rule_cat_id,
                    rule.taxonomy_id,
                    rule_cat_id.replace("cat_", ""),
                    rule_name,
                    sort_order,
                    now,
                    now,
                ),
            )
            categories_created += 1
            sort_order += 1

        # 4c. Budget group assignments (all categories are guaranteed to exist in taxonomy_categories)
        # 1. Custom root categories
        for root_cat in config.custom_root_categories:
            if (
                root_cat.budget_group_id
                and root_cat.taxonomy_id in ("spending_categories", "savings_categories")
            ):
                resolved_group_id = budget_group_id_by_key.get(
                    root_cat.budget_group_id.lower(), root_cat.budget_group_id
                )
                if resolved_group_id and (root_cat.taxonomy_id, root_cat.category_id) in existing_cat_keys:
                    bga_id = stable_id(
                        "budget_group_assignment", [root_cat.taxonomy_id, root_cat.category_id]
                    )
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO budget_group_assignments (
                            id, group_id, taxonomy_id, category_id, is_system, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 0, ?, ?);
                        """,
                        (bga_id, resolved_group_id, root_cat.taxonomy_id, root_cat.category_id, now, now),
                    )

        # 2. Config categories with explicit budget_group_id
        for cat in config.categories.values():
            bg_id = cat.budget_group_id
            if bg_id and cat.taxonomy_id == "spending_categories":
                resolved_group_id = budget_group_id_by_key.get(bg_id.lower())
                if resolved_group_id and (cat.taxonomy_id, cat.category_id) in existing_cat_keys:
                    bga_id = stable_id(
                        "budget_group_assignment", [cat.taxonomy_id, cat.category_id]
                    )
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO budget_group_assignments (
                            id, group_id, taxonomy_id, category_id, is_system, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 0, ?, ?);
                        """,
                        (bga_id, resolved_group_id, cat.taxonomy_id, cat.category_id, now, now),
                    )

        # 3. Budget group overrides
        bg_overrides = getattr(config, "budget_group_overrides", {})
        for cat_id, bg_name in bg_overrides.items():
            resolved_group_id = budget_group_id_by_key.get(bg_name.lower())
            if resolved_group_id and ("spending_categories", cat_id) in existing_cat_keys:
                bga_id = stable_id("budget_group_assignment", ["spending_categories", cat_id])
                cur.execute(
                    """
                    INSERT OR REPLACE INTO budget_group_assignments (
                        id, group_id, taxonomy_id, category_id, is_system, created_at, updated_at
                    ) VALUES (?, ?, 'spending_categories', ?, 0, ?, ?);
                    """,
                    (bga_id, resolved_group_id, cat_id, now, now),
                )

        # 4. Guarantee ALL spending categories (including subcategories) have a budget group assignment!
        # In Wealthfolio, any spending category not in budget_group_assignments shows up as "Uncategorized"
        # in the spending and budget reports. We inherit from parent or default to 'other'.
        cur.execute(
            """
            SELECT id, parent_id
            FROM taxonomy_categories
            WHERE taxonomy_id = 'spending_categories';
            """
        )
        all_spending_cats = cur.fetchall()

        # Map each category to its current assigned budget_group_id
        cur.execute(
            """
            SELECT category_id, group_id
            FROM budget_group_assignments
            WHERE taxonomy_id = 'spending_categories';
            """
        )
        assigned_budget_groups = dict(cur.fetchall())

        # For any category missing a budget group, resolve by climbing parents, then fallback to 'other'
        cat_parents = {cid: pid for cid, pid in all_spending_cats}
        other_group_id = budget_group_id_by_key.get("other") or budget_group_id_by_key.get("needs")

        for cid, pid in all_spending_cats:
            if cid in assigned_budget_groups:
                continue
            # Try to resolve from parent
            curr = pid
            resolved_gid = None
            visited = set()
            while curr and curr not in visited:
                visited.add(curr)
                if curr in assigned_budget_groups:
                    resolved_gid = assigned_budget_groups[curr]
                    break
                curr = cat_parents.get(curr)

            if not resolved_gid:
                resolved_gid = other_group_id

            if resolved_gid:
                bga_id = stable_id("budget_group_assignment", ["spending_categories", cid])
                cur.execute(
                    """
                    INSERT OR REPLACE INTO budget_group_assignments (
                        id, group_id, taxonomy_id, category_id, is_system, created_at, updated_at
                    ) VALUES (?, ?, 'spending_categories', ?, 0, ?, ?);
                    """,
                    (bga_id, resolved_gid, cid, now, now),
                )
                assigned_budget_groups[cid] = resolved_gid

        # 5. Convert and insert activities, taxonomy assignments, and splits
        migrated_guids = {
            g: a for g, a in config.accounts.items() if a.wf_account_id is not None
        }
        subcats_by_id = {c.category_id: c for c in config.categories.values()}

        # Lookup of root categories (parent_id IS NULL) to guarantee every activity is assigned to a subcategory
        cur.execute("SELECT id FROM taxonomy_categories WHERE parent_id IS NULL;")
        root_cat_ids = {r[0] for r in cur.fetchall()}

        def _resolve_to_subcat(cid: str, tax: str) -> str:
            if cid not in root_cat_ids:
                return cid
            if cid == "cat_other_expense":
                return "cat_gnc_other_expenses_uncat"
            if cid == "cat_income_other":
                return "cat_gnc_other_income_uncat"
            if cid == "cat_income_investment":
                return "cat_gnc_investment_income_uncat"
            gen_id = f"cat_gnc_{cid}_general"
            pair = (tax, gen_id)
            if pair not in existing_cat_keys:
                existing_cat_keys.add(pair)
                cur.execute(
                    """
                    INSERT OR IGNORE INTO taxonomy_categories (
                        id, taxonomy_id, parent_id, key, name, color, description,
                        sort_order, created_at, updated_at, icon
                    ) VALUES (?, ?, ?, ?, 'General', '#808080', 'General subcategory to prevent direct root assignment', 999, ?, ?, NULL);
                    """,
                    (gen_id, tax, cid, f"gnc_{cid}_general", now, now),
                )
            return gen_id

        def _get_or_create_spending_bridge(cat_entry: CategoryMapEntry) -> str:
            if cat_entry.taxonomy_id == "spending_categories":
                return _resolve_to_subcat(cat_entry.category_id, "spending_categories")

            bridge_id = f"cat_sp_{cat_entry.category_id}"
            pair = ("spending_categories", bridge_id)
            if pair not in existing_cat_keys:
                existing_cat_keys.add(pair)
                # Map investments/trusts to cat_investment_property, otherwise cat_other_expense
                target_parent = (
                    "cat_investment_property"
                    if "Trust" in cat_entry.gnc_path or "Invest" in cat_entry.gnc_path
                    else "cat_other_expense"
                )
                cur.execute(
                    """
                    INSERT OR IGNORE INTO taxonomy_categories (
                        id, taxonomy_id, parent_id, key, name, color, description,
                        sort_order, created_at, updated_at, icon
                    ) VALUES (?, 'spending_categories', ?, ?, ?, '#808080', ?, 999, ?, ?, NULL);
                    """,
                    (
                        bridge_id,
                        target_parent,
                        f"sp_{cat_entry.category_key}"[:40],
                        cat_entry.category_name,
                        f"Spending bridge for {cat_entry.gnc_path}",
                        now,
                        now,
                    ),
                )
                bg_id = budget_group_id_by_key.get("investment" if target_parent == "cat_investment_property" else "other")
                if bg_id:
                    bga_id = stable_id("budget_group_assignment", ["spending_categories", bridge_id])
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO budget_group_assignments (
                            id, group_id, taxonomy_id, category_id, is_system, created_at, updated_at
                        ) VALUES (?, ?, 'spending_categories', ?, 0, ?, ?);
                        """,
                        (bga_id, bg_id, bridge_id, now, now),
                    )
            return bridge_id

        # Query pre-existing candidate activities from target DB for LINKED_INVESTMENT_ACCOUNTs
        linked_investment_guids = {
            g: a
            for g, a in config.accounts.items()
            if a.role == AccountRole.LINKED_INVESTMENT_ACCOUNT and a.linked_wf_account_id
        }
        candidate_activities_by_wf_id: dict[str, list[dict]] = {}
        if linked_investment_guids:
            linked_wf_ids = list({
                wid.strip()
                for a in linked_investment_guids.values()
                if a.linked_wf_account_id
                for wid in a.linked_wf_account_id.split(",")
                if wid.strip()
            })
            if linked_wf_ids:
                placeholders = ",".join("?" for _ in linked_wf_ids)
                cur.execute(
                    f"""
                    SELECT id, account_id, activity_type, activity_date, amount
                    FROM activities
                    WHERE account_id IN ({placeholders})
                    """,
                    linked_wf_ids,
                )
                for r in cur.fetchall():
                    act_wf_id = r[1]
                    if act_wf_id not in candidate_activities_by_wf_id:
                        candidate_activities_by_wf_id[act_wf_id] = []
                    raw_amt = r[4]
                    amt_dec = (
                        Decimal(str(raw_amt))
                        if raw_amt is not None and str(raw_amt).strip() not in ("", "None")
                        else Decimal("0.00")
                    )
                    candidate_activities_by_wf_id[act_wf_id].append(
                        {
                            "id": r[0],
                            "account_id": r[1],
                            "activity_type": r[2],
                            "date": r[3],
                            "amount": amt_dec,
                            "claimed": False,
                        }
                    )

        matched_transfers_count = 0
        activities_rows: list[tuple] = []
        assignment_rows: list[tuple] = []
        split_rows: list[tuple] = []

        for trn in book.transactions:
            mig_splits = [
                s for s in trn.splits if s.value != Decimal("0.00") and s.account_guid in migrated_guids
            ]
            if not mig_splits:
                continue

            ext_splits = [
                s for s in trn.splits if s.value != Decimal("0.00") and s.account_guid not in migrated_guids
            ]
            has_pos_mig = any(s.value > Decimal("0.00") for s in mig_splits)
            has_neg_mig = any(s.value < Decimal("0.00") for s in mig_splits)
            is_internal_transfer = has_pos_mig and has_neg_mig and len(ext_splits) == 0

            activity_date = f"{trn.date_posted}T00:00:00.000Z"

            for s_m in mig_splits:
                acc_entry = migrated_guids[s_m.account_guid]
                act_id = deterministic_uuid("gnucash_split", s_m.guid)
                abs_val = abs(s_m.value)
                amount_str = f"{abs_val:.2f}"
                notes = _format_notes(trn, s_m)

                act_type = "WITHDRAWAL" if s_m.value < Decimal("0.00") else "DEPOSIT"
                subtype: str | None = None
                source_group_id: str | None = None
                flow_external: bool = False
                matched_meta_extra: dict | None = None

                # Check if this split transfers to/from a LINKED_INVESTMENT_ACCOUNT
                linked_inv_ext = [s for s in ext_splits if s.account_guid in linked_investment_guids]
                if linked_inv_ext and len(mig_splits) == 1:
                    matched_inv_split = linked_inv_ext[0]
                    inv_acc_entry = linked_investment_guids[matched_inv_split.account_guid]
                    target_wf_inv_acc_ids = [
                        wid.strip()
                        for wid in (inv_acc_entry.linked_wf_account_id or "").split(",")
                        if wid.strip()
                    ]
                    cands: list[dict] = []
                    for wid in target_wf_inv_acc_ids:
                        cands.extend(candidate_activities_by_wf_id.get(wid, []))

                    # For a bank inflow, we can only pair with an existing TRANSFER_OUT activity
                    # For a bank outflow, we can only pair with an existing TRANSFER_IN activity
                    needed_peer_type = "TRANSFER_IN" if s_m.value < Decimal("0.00") else "TRANSFER_OUT"
                    compat_cands = [c for c in cands if c.get("activity_type") == needed_peer_type]

                    match_id = _find_matching_wf_activity(
                        activity_date, abs_val, compat_cands, window_days=3
                    )

                    if match_id:
                        # Matched with existing Wealthfolio paired transfer activity!
                        act_type = "TRANSFER_OUT" if s_m.value < Decimal("0.00") else "TRANSFER_IN"
                        flow_external = False
                        source_group_id = match_id
                        matched_meta_extra = {
                            "matched_existing_wf_activity_id": match_id,
                            "match_type": "exact_amount_window",
                        }
                        matched_transfers_count += 1
                        xfer_asgn_id = stable_id(
                            "activity_taxonomy_assignment", [act_id, "spending_categories"]
                        )
                        assignment_rows.append(
                            (
                                xfer_asgn_id,
                                act_id,
                                "spending_categories",
                                "cat_gnc_transfers_internal",
                                10000,
                                "manual",
                                now,
                                now,
                            )
                        )
                    else:
                        # External transfer to/from linked investment portfolio (already tracked in WF)
                        # Do NOT inject phantom cash activities into the investment account.
                        source_group_id = None
                        flow_external = True
                        if s_m.value < Decimal("0.00"):
                            act_type = "WITHDRAWAL"
                            asgn_id = stable_id(
                                "activity_taxonomy_assignment", [act_id, "spending_categories"]
                            )
                            assignment_rows.append(
                                (
                                    asgn_id,
                                    act_id,
                                    "spending_categories",
                                    "cat_investment_property",
                                    10000,
                                    "manual",
                                    now,
                                    now,
                                )
                            )
                        else:
                            act_type = "DEPOSIT"
                            asgn_id = stable_id(
                                "activity_taxonomy_assignment", [act_id, "income_sources"]
                            )
                            assignment_rows.append(
                                (
                                    asgn_id,
                                    act_id,
                                    "income_sources",
                                    "cat_gnc_investment_income_uncat",
                                    10000,
                                    "manual",
                                    now,
                                    now,
                                )
                            )
                elif (is_internal_transfer or (has_pos_mig and has_neg_mig)) and len(mig_splits) >= 2:
                    # Internal transfer between migrated accounts
                    act_type = "TRANSFER_OUT" if s_m.value < Decimal("0.00") else "TRANSFER_IN"
                    source_group_id = f"gnc_xfer_{trn.guid}"
                    flow_external = False
                    # Assign internal transfer category to both TRANSFER_OUT and TRANSFER_IN
                    # so Wealthfolio's spending and activity views never display '+ Categorize'
                    xfer_asgn_id = stable_id(
                        "activity_taxonomy_assignment", [act_id, "spending_categories"]
                    )
                    assignment_rows.append(
                        (
                            xfer_asgn_id,
                            act_id,
                            "spending_categories",
                            "cat_gnc_transfers_internal",
                            10000,
                            "manual",
                            now,
                            now,
                        )
                    )
                elif s_m.value < Decimal("0.00"):
                    # Outflow from migrated account to external destination
                    pos_ext = [s for s in ext_splits if s.value > Decimal("0.00")]
                    if not pos_ext:
                        pos_ext = ext_splits

                    roles = {
                        config.accounts[s.account_guid].role
                        for s in pos_ext
                        if s.account_guid in config.accounts
                    }

                    if roles == {AccountRole.EXTERNAL_SAVINGS_INVESTMENT}:
                        act_type = "WITHDRAWAL"
                        first_path = book.accounts[pos_ext[0].account_guid].full_path
                        sav_cat = (
                            "cat_savings_retirement"
                            if "Superannuation" in first_path
                            else "cat_savings_investments"
                        )
                        asgn_id = stable_id(
                            "activity_taxonomy_assignment", [act_id, "savings_categories"]
                        )
                        assignment_rows.append(
                            (asgn_id, act_id, "savings_categories", sav_cat, 10000, "manual", now, now)
                        )
                        cat_entry = config.categories.get(pos_ext[0].account_guid)
                        if cat_entry and cat_entry.taxonomy_id == "spending_categories":
                            sp_asgn_id = stable_id(
                                "activity_taxonomy_assignment", [act_id, "spending_categories"]
                            )
                            assignment_rows.append(
                                (
                                    sp_asgn_id,
                                    act_id,
                                    "spending_categories",
                                    cat_entry.category_id,
                                    10000,
                                    "manual",
                                    now,
                                    now,
                                )
                            )
                    else:
                        # Expense / Fee / Loan / Kids / Equity outflow
                        if len(pos_ext) == 1:
                            cp_g = pos_ext[0].account_guid
                            c_path = book.accounts[cp_g].full_path if cp_g in book.accounts else ""
                            c_entry = config.categories.get(cp_g)
                            if (c_entry and (c_entry.category_id == "cat_fees" or c_entry.parent_id == "cat_fees")) or "fee" in c_path.lower():
                                act_type = "FEE"
                            else:
                                act_type = "WITHDRAWAL"
                        else:
                            act_type = "WITHDRAWAL"

                        # Build category legs
                        leg_by_cat: dict[str, tuple[CategoryMapEntry, Decimal, str]] = {}
                        tx_overrides = getattr(config, "transaction_overrides", {})
                        for cs in pos_ext:
                            cs_acc = book.accounts.get(cs.account_guid)
                            cs_path = cs_acc.full_path if cs_acc else ""

                            # Check 1: Explicit transaction override
                            if trn.guid in tx_overrides:
                                ov = tx_overrides[trn.guid]
                                cat_id = ov.get("category_id", "cat_other_expense")
                                cat_name = ov.get("category_name", "Other Expenses")
                                parent_id = ov.get("parent_id")
                                cat_entry = CategoryMapEntry(
                                    gnc_guid=cs.account_guid,
                                    gnc_path=cs_path or "Expenses",
                                    taxonomy_id="spending_categories",
                                    category_id=cat_id,
                                    category_key=cat_id.replace("cat_", ""),
                                    category_name=cat_name,
                                    parent_id=parent_id,
                                    is_seeded=True,
                                )
                            else:
                                # Check 2: Payee rule pattern
                                matched_rule = None
                                desc_lower = trn.description.lower()
                                for r in getattr(config, "rules", []):
                                    if r.match_type == "exact" and r.pattern.lower() == desc_lower:
                                        matched_rule = r
                                        break
                                    elif r.match_type == "contains" and r.pattern.lower() in desc_lower:
                                        matched_rule = r
                                        break
                                if matched_rule:
                                    cat_id = matched_rule.category_id
                                    sub_entry = subcats_by_id.get(cat_id)
                                    tax_id = matched_rule.taxonomy_id or "spending_categories"
                                    if sub_entry:
                                        cat_name = sub_entry.category_name
                                        parent_id = sub_entry.parent_id
                                    else:
                                        cat_name = matched_rule.name.replace("Auto: ", "").replace("Payee: ", "")
                                        parent_id = None
                                    cat_entry = CategoryMapEntry(
                                        gnc_guid=cs.account_guid,
                                        gnc_path=cs_path or "Expenses",
                                        taxonomy_id=tax_id,
                                        category_id=cat_id,
                                        category_key=cat_id.replace("cat_", ""),
                                        category_name=cat_name,
                                        parent_id=parent_id,
                                        is_seeded=True,
                                    )
                                else:
                                    # Check 3: Mapped category from config
                                    cat_entry = config.categories.get(cs.account_guid)
                                    if cat_entry is None:
                                        cat_id = "cat_gnc_other_expenses_uncat"
                                        cat_name = "Uncategorized Expense"
                                        parent_id = "cat_other_expense"
                                        cat_entry = CategoryMapEntry(
                                            gnc_guid=cs.account_guid,
                                            gnc_path=cs_path or "Expenses",
                                            taxonomy_id="spending_categories",
                                            category_id=cat_id,
                                            category_key=cat_id.replace("cat_", ""),
                                            category_name=cat_name,
                                            parent_id=parent_id,
                                            is_seeded=True,
                                        )
                            prev = leg_by_cat.get(cat_entry.category_id)
                            leg_memo = cs.memo.strip() or trn.description.strip()
                            if prev is None:
                                leg_by_cat[cat_entry.category_id] = (cat_entry, cs.value, leg_memo)
                            else:
                                leg_by_cat[cat_entry.category_id] = (
                                    cat_entry,
                                    prev[1] + cs.value,
                                    prev[2],
                                )

                        legs = sorted(
                            leg_by_cat.values(), key=lambda item: item[1], reverse=True
                        )
                        if legs:
                            primary_cat = legs[0][0]
                            primary_subcat_id = _resolve_to_subcat(primary_cat.category_id, primary_cat.taxonomy_id)
                            asgn_id = stable_id(
                                "activity_taxonomy_assignment", [act_id, primary_cat.taxonomy_id]
                            )
                            assignment_rows.append(
                                (
                                    asgn_id,
                                    act_id,
                                    primary_cat.taxonomy_id,
                                    primary_subcat_id,
                                    10000,
                                    "manual",
                                    now,
                                    now,
                                )
                            )
                            # Wealthfolio Spending View Guarantee:
                            # Ensure clean spending_categories assignment with granular counterpart name
                            if primary_cat.taxonomy_id != "spending_categories":
                                sp_bridge_id = _get_or_create_spending_bridge(primary_cat)
                                sp_asgn_id = stable_id(
                                    "activity_taxonomy_assignment", [act_id, "spending_categories"]
                                )
                                assignment_rows.append(
                                    (
                                        sp_asgn_id,
                                        act_id,
                                        "spending_categories",
                                        sp_bridge_id,
                                        10000,
                                        "manual",
                                        now,
                                        now,
                                    )
                                )
                            if len(legs) > 1:
                                norm_legs = _normalize_split_amounts(legs, abs_val)
                                if len(norm_legs) > 1:
                                    for idx, (l_cat, l_amt, l_memo) in enumerate(norm_legs):
                                        sp_id = deterministic_uuid(
                                            "gnucash_activity_split", f"{s_m.guid}:{idx}"
                                        )
                                        split_subcat_id = _resolve_to_subcat(l_cat.category_id, l_cat.taxonomy_id)
                                        split_rows.append(
                                            (
                                                sp_id,
                                                act_id,
                                                l_cat.taxonomy_id,
                                                split_subcat_id,
                                                f"{l_amt:.2f}",
                                                l_memo,
                                                idx,
                                                now,
                                                now,
                                            )
                                        )
                else:
                    # Inflow to migrated account (s_m.value > 0)
                    neg_ext = [s for s in ext_splits if s.value < Decimal("0.00")]
                    if not neg_ext:
                        neg_ext = ext_splits

                    roles = {
                        config.accounts[s.account_guid].role
                        for s in neg_ext
                        if s.account_guid in config.accounts
                    }

                    if AccountRole.EXPENSE in roles:
                        # Refund of an expense
                        act_type = "CREDIT"
                        subtype = "REFUND"
                        exp_cs = next(
                            s
                            for s in neg_ext
                            if config.accounts.get(s.account_guid)
                            and config.accounts[s.account_guid].role == AccountRole.EXPENSE
                        )
                        cat_entry = config.categories.get(exp_cs.account_guid)
                        if cat_entry:
                            ref_subcat_id = _resolve_to_subcat(cat_entry.category_id, cat_entry.taxonomy_id)
                            asgn_id = stable_id(
                                "activity_taxonomy_assignment", [act_id, cat_entry.taxonomy_id]
                            )
                            assignment_rows.append(
                                (
                                    asgn_id,
                                    act_id,
                                    cat_entry.taxonomy_id,
                                    ref_subcat_id,
                                    10000,
                                    "manual",
                                    now,
                                    now,
                                )
                            )
                    else:
                        # Income inflow
                        first_cs = neg_ext[0] if neg_ext else None
                        first_path = (
                            book.accounts[first_cs.account_guid].full_path
                            if first_cs and first_cs.account_guid in book.accounts
                            else ""
                        )
                        cat_entry = (
                            config.categories.get(first_cs.account_guid) if first_cs else None
                        )
                        if (cat_entry and (cat_entry.category_id == "cat_income_interest" or cat_entry.parent_id == "cat_income_interest")) or "interest" in first_path.lower():
                            act_type = "INTEREST"
                        else:
                            act_type = "DEPOSIT"
                        if cat_entry is not None:
                            target_tax = cat_entry.taxonomy_id
                            target_cat = _resolve_to_subcat(cat_entry.category_id, target_tax)
                            if target_tax == "spending_categories":
                                act_type = "CREDIT"
                        else:
                            target_tax = "income_sources"
                            if "Trust" in first_path or "Shares" in first_path or "Invest" in first_path:
                                target_cat = "cat_gnc_investment_income_uncat"
                            elif any(k in first_path.lower() for k in ("kids", "children")):
                                target_cat = "cat_income_reimbursements"
                            else:
                                target_cat = "cat_gnc_other_income_uncat"

                        asgn_id = stable_id(
                            "activity_taxonomy_assignment", [act_id, target_tax]
                        )
                        assignment_rows.append(
                            (
                                asgn_id,
                                act_id,
                                target_tax,
                                target_cat,
                                10000,
                                "manual",
                                now,
                                now,
                            )
                        )

                meta_dict = {
                    "gnucash_trn_guid": trn.guid,
                    "gnucash_split_guid": s_m.guid,
                    "reconciled": s_m.reconciled_state,
                }
                if source_group_id:
                    meta_dict["flow"] = {"is_external": flow_external}
                    meta_dict["transfer"] = {"kind": "internal_cash", "source": "wealthfolio"}
                if matched_meta_extra:
                    meta_dict.update(matched_meta_extra)
                meta_str = json.dumps(meta_dict)

                activities_rows.append(
                    (
                        act_id,
                        acc_entry.wf_account_id,
                        None,
                        act_type,
                        None,
                        None,
                        subtype,
                        "POSTED",
                        activity_date,
                        None,
                        "0",
                        "0",
                        amount_str,
                        "0",
                        None,
                        "AUD",
                        None,
                        notes,
                        meta_str,
                        "gnucash",
                        s_m.guid,
                        source_group_id,
                        f"gnucash:{s_m.guid}",
                        None,
                        0,
                        0,
                        now,
                        now,
                    )
                )

        cur.executemany(
            """
            INSERT INTO activities (
                id, account_id, asset_id, activity_type, activity_type_override,
                source_type, subtype, status, activity_date, settlement_date,
                quantity, unit_price, amount, fee, tax, currency, fx_rate,
                notes, metadata, source_system, source_record_id, source_group_id,
                idempotency_key, import_run_id, is_user_modified, needs_review,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            );
            """,
            activities_rows,
        )

        cur.executemany(
            """
            INSERT OR REPLACE INTO activity_taxonomy_assignments (
                id, activity_id, taxonomy_id, category_id, weight, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            assignment_rows,
        )

        cur.executemany(
            """
            INSERT INTO spending_activity_splits (
                id, activity_id, taxonomy_id, category_id, amount, note, sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            split_rows,
        )

        # 6. Insert auto-categorization rules
        rules_created = 0
        for rule in config.rules:
            cur.execute(
                """
                INSERT OR REPLACE INTO spending_categorization_rules (
                    id, name, pattern, match_type, taxonomy_id, category_id,
                    activity_type, priority, is_global, account_id, preset_id,
                    preset_rule_key, preset_version, preset_modified, created_at,
                    updated_at, amount_op, amount_value, amount_value2
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, NULL, NULL, 0, ?, ?, NULL, NULL, NULL);
                """,
                (
                    rule.id,
                    rule.name,
                    rule.pattern,
                    rule.match_type,
                    rule.taxonomy_id,
                    rule.category_id,
                    rule.activity_type,
                    rule.priority,
                    now,
                    now,
                ),
            )
            rules_created += 1

        # 7. Opt all migrated accounts into spending.account_ids
        cur.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = 'spending.account_ids';"
        )
        row = cur.fetchone()
        existing_ids: list[str] = json.loads(row[0]) if row and row[0] else []
        merged_ids = list(
            dict.fromkeys(
                existing_ids
                + [a.wf_account_id for a in migrated_accounts if a.wf_account_id is not None]
            )
        )
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (setting_key, setting_value) VALUES ('spending.enabled', 'true');"
        )
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (setting_key, setting_value) VALUES ('spending.account_ids', ?);",
            (json.dumps(merged_ids),),
        )

        conn.commit()

        return MigrationStats(
            accounts_created=accounts_created,
            categories_created=categories_created,
            activities_created=len(activities_rows),
            taxonomy_assignments_created=len(assignment_rows),
            activity_splits_created=len(split_rows),
            rules_created=rules_created,
            alt_assets_created=alt_assets_created,
            alt_quotes_created=alt_quotes_created,
            matched_transfers_count=matched_transfers_count,
        )
    finally:
        conn.close()
