import json
import os
import re
import shutil
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser

from dataclasses import asdict
from decimal import Decimal
from gnucash_to_wealthfolio.backups import (
    backup_mapping_file,
    delete_backup,
    list_backups,
    list_mapping_backups,
    prune_backups,
    prune_mapping_backups,
)
from gnucash_to_wealthfolio.ids import deterministic_uuid, stable_id
from gnucash_to_wealthfolio.mapper import (
    ROOT_COLORS,
    AccountMapEntry,
    AccountRole,
    CategoryMapEntry,
    MappingConfig,
    RuleEntry,
    _slugify,
    build_mapping_config,
    load_mapping_config,
    save_mapping_config,
    update_budget_group_mapping,
    update_category_mapping,
)
from gnucash_to_wealthfolio.migrator import run_migration
from gnucash_to_wealthfolio.models import GncBook, MappingMetadata
from gnucash_to_wealthfolio.verifier import verify_migration

BUDGET_GROUPS = [
    {"id": "NEEDS", "name": "Needs", "color": "#4F6B92", "icon": "Home"},
    {"id": "WANTS", "name": "Wants", "color": "#8E7CB3", "icon": "Sparkles"},
    {"id": "INVESTMENT", "name": "Investment", "color": "#2A573F", "icon": "DollarSign"},
    {"id": "SAVINGS", "name": "Savings", "color": "#6B8E54", "icon": "PiggyBank"},
    {"id": "GIVING", "name": "Giving", "color": "#A35742", "icon": "Gift"},
    {"id": "PERSONAL", "name": "Personal Care", "color": "#B89A4C", "icon": "User"},
    {"id": "OTHER", "name": "Other Expenses", "color": "#9C998E", "icon": "MoreHorizontal"},
    {"id": "INCOME", "name": "Income Sources", "color": "#3B82F6", "icon": "Landmark"},
]

DEFAULT_PARENTS = [
    {"id": "cat_housing", "name": "Housing", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_housing"]},
    {"id": "cat_family_pets", "name": "Children, Family & Pets", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_family_pets"]},
    {"id": "cat_groceries", "name": "Groceries", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_groceries"]},
    {"id": "cat_transport", "name": "Transportation", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_transport"]},
    {"id": "cat_education", "name": "Education", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_education"]},
    {"id": "cat_bills", "name": "Bills & Utilities", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_bills"]},
    {"id": "cat_health", "name": "Health & Wellness", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_health"]},
    {"id": "cat_fees", "name": "Fees & Charges", "taxonomy_id": "spending_categories", "budget_group": "NEEDS", "color": ROOT_COLORS["cat_fees"]},
    {"id": "cat_food", "name": "Food & Dining", "taxonomy_id": "spending_categories", "budget_group": "WANTS", "color": ROOT_COLORS["cat_food"]},
    {"id": "cat_shopping", "name": "Shopping", "taxonomy_id": "spending_categories", "budget_group": "WANTS", "color": ROOT_COLORS["cat_shopping"]},
    {"id": "cat_travel", "name": "Travel", "taxonomy_id": "spending_categories", "budget_group": "WANTS", "color": ROOT_COLORS["cat_travel"]},
    {"id": "cat_entertainment", "name": "Entertainment", "taxonomy_id": "spending_categories", "budget_group": "WANTS", "color": ROOT_COLORS["cat_entertainment"]},
    {"id": "cat_investment_property", "name": "Investments", "taxonomy_id": "spending_categories", "budget_group": "INVESTMENT", "color": ROOT_COLORS["cat_investment_property"]},
    {"id": "cat_savings", "name": "Savings", "taxonomy_id": "spending_categories", "budget_group": "SAVINGS", "color": ROOT_COLORS.get("cat_savings", "#6B8E54")},
    {"id": "cat_gifts", "name": "Gifts & Donations", "taxonomy_id": "spending_categories", "budget_group": "GIVING", "color": ROOT_COLORS["cat_gifts"]},
    {"id": "cat_personal", "name": "Personal Care", "taxonomy_id": "spending_categories", "budget_group": "PERSONAL", "color": ROOT_COLORS["cat_personal"]},
    {"id": "cat_other_expense", "name": "Other Expenses", "taxonomy_id": "spending_categories", "budget_group": "OTHER", "color": ROOT_COLORS["cat_other_expense"]},
    {"id": "cat_income_employment", "name": "Employment Income", "taxonomy_id": "income_sources", "budget_group": "INCOME", "color": ROOT_COLORS["cat_income_employment"]},
    {"id": "cat_income_investment", "name": "Investment Income", "taxonomy_id": "income_sources", "budget_group": "INCOME", "color": ROOT_COLORS["cat_income_investment"]},
    {"id": "cat_income_government", "name": "Government & Support", "taxonomy_id": "income_sources", "budget_group": "INCOME", "color": ROOT_COLORS["cat_income_government"]},
    {"id": "cat_income_other", "name": "Other Income", "taxonomy_id": "income_sources", "budget_group": "INCOME", "color": ROOT_COLORS["cat_income_other"]},
]


def _build_all_transactions(server):
    if not hasattr(server, "book") or not server.book or not server.book.transactions:
        return []

    mapping_path = server.mapping_path
    if not mapping_path.exists():
        cfg = None
    else:
        cfg = load_mapping_config(mapping_path)

    if cfg is None:
        return []

    parent_map = {p["id"]: dict(p) for p in DEFAULT_PARENTS}
    for p_id, bg in getattr(cfg, "budget_group_overrides", {}).items():
        if p_id in parent_map:
            parent_map[p_id]["budget_group"] = bg.upper()

    for cr in getattr(cfg, "custom_root_categories", []):
        default_bg = "INCOME" if cr.taxonomy_id == "income_sources" else "OTHER"
        if cr.category_id in parent_map and parent_map[cr.category_id].get("budget_group"):
            default_bg = parent_map[cr.category_id]["budget_group"]
        bg = (cr.budget_group_id or default_bg).upper()
        if cr.category_id in getattr(cfg, "budget_group_overrides", {}):
            bg = cfg.budget_group_overrides[cr.category_id].upper()
        parent_map[cr.category_id] = {
            "id": cr.category_id,
            "name": cr.category_name,
            "taxonomy_id": cr.taxonomy_id,
            "budget_group": bg,
            "color": cr.color,
        }

    migrated_guids = {g: a for g, a in cfg.accounts.items() if a.wf_account_id is not None}
    by_guid = server.book.accounts
    tx_overrides = getattr(cfg, "transaction_overrides", {})
    subcat_by_id = {c.category_id: c for c in cfg.categories.values()}
    for cr in getattr(cfg, "custom_root_categories", []):
        subcat_by_id[cr.category_id] = cr

    tx_list = []
    for trn in server.book.transactions:
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
        is_xfer = (has_pos_mig and has_neg_mig and len(ext_splits) == 0) or (
            has_pos_mig and has_neg_mig and len(mig_splits) >= 2
        )

        for sm in mig_splits:
            val = sm.value
            direction = "outflow" if val < 0 else "inflow"
            amount = float(abs(val))
            acc_entry = cfg.accounts.get(sm.account_guid)
            acc_name = (
                acc_entry.gnc_path
                if acc_entry
                else (by_guid[sm.account_guid].full_path if sm.account_guid in by_guid else "")
            )

            cp_path = ""
            cp_guid = ""
            is_override = False
            is_direct = False

            if is_xfer:
                cps = [s for s in mig_splits if s.guid != sm.guid]
                if cps:
                    cp_guid = cps[0].account_guid
                    c_acc = by_guid.get(cp_guid)
                    cp_path = c_acc.full_path if c_acc else ""
                bg = "TRANSFER"
                cat_name = "Transfer"
                cat_id = "transfer"
                subcat = "Internal Transfer"
                is_direct = False
            elif direction == "outflow":
                pos_ext = [s for s in ext_splits if s.value > Decimal("0.00")] or ext_splits
                if pos_ext:
                    cp_guid = pos_ext[0].account_guid
                    c_acc = by_guid.get(cp_guid)
                    cp_path = c_acc.full_path if c_acc else ""

                if trn.guid in tx_overrides:
                    ov = tx_overrides[trn.guid]
                    parent_id = ov.get("parent_id")
                    cat_id = ov.get("category_id") or parent_id or "cat_other_expense"
                    subcat = ov.get("category_name", "Assigned Expense")
                    cat_name = (
                        parent_map.get(parent_id, {}).get("name")
                        or parent_map.get(cat_id, {}).get("name", "Other Expenses")
                    )
                    bg = (
                        ov.get("budget_group")
                        or (parent_map.get(parent_id, {}).get("budget_group") if parent_id else None)
                        or parent_map.get(cat_id, {}).get("budget_group", "OTHER")
                    )
                    is_override = True
                    is_direct = bool(not parent_id and cat_id in parent_map)
                else:
                    matched_rule = None
                    desc_lower = trn.description.lower()
                    for r in cfg.rules:
                        if r.pattern.lower() in desc_lower:
                            matched_rule = r
                            break

                    if matched_rule:
                        rule_cat_id = matched_rule.category_id
                        sub_entry = subcat_by_id.get(rule_cat_id)
                        if sub_entry:
                            parent_id = sub_entry.parent_id or rule_cat_id
                            cat_id = rule_cat_id
                            cat_name = parent_map.get(parent_id, {}).get("name", "Other Expenses")
                            subcat = sub_entry.category_name
                            bg = (
                                (sub_entry.budget_group_id.upper() if sub_entry.budget_group_id else None)
                                or parent_map.get(parent_id, {}).get("budget_group", "OTHER")
                            )
                        elif rule_cat_id in parent_map:
                            cat_id = rule_cat_id
                            cat_name = parent_map[rule_cat_id]["name"]
                            subcat = matched_rule.name.replace("Payee: ", "").replace("Auto: ", "")
                            bg = parent_map[rule_cat_id].get("budget_group", "OTHER")
                        else:
                            cat_id = rule_cat_id
                            cat_name = parent_map.get(rule_cat_id, {}).get("name", "Other Expenses")
                            subcat = matched_rule.name.replace("Payee: ", "").replace("Auto: ", "")
                            bg = "OTHER"
                        is_override = True
                        is_direct = False
                    else:
                        cat_entry = cfg.categories.get(cp_guid)
                        if cat_entry:
                            cat_id = cat_entry.parent_id or cat_entry.category_id
                            cat_name = (
                                parent_map.get(cat_id, {}).get("name")
                                or (parent_map.get(cat_entry.parent_id, {}).get("name") if cat_entry.parent_id else None)
                                or ("Other Income" if cat_entry.taxonomy_id == "income_sources" else "Other Expenses")
                            )
                            subcat = cat_entry.category_name
                            bg = (
                                (cat_entry.budget_group_id.upper() if cat_entry.budget_group_id else None)
                                or (parent_map.get(cat_id, {}).get("budget_group") if cat_id in parent_map else None)
                                or ("INCOME" if cat_entry.taxonomy_id == "income_sources" else "OTHER")
                            )
                            is_direct = bool(not cat_entry.parent_id or cat_entry.category_id in parent_map)
                        elif cp_path and cp_path.startswith("Expenses:"):
                            cat_id = "cat_other_expense"
                            cat_name = "Other Expenses"
                            subcat = cp_path.split(":")[-1] or "Uncategorized Expense"
                            bg = "OTHER"
                            is_direct = False
                        else:
                            cat_id = "cat_other_expense"
                            cat_name = "Other Expenses"
                            subcat = "Uncategorized Expense"
                            bg = "OTHER"
                            is_direct = True
            else:  # inflow
                neg_ext = [s for s in ext_splits if s.value < Decimal("0.00")] or ext_splits
                if neg_ext:
                    cp_guid = neg_ext[0].account_guid
                    c_acc = by_guid.get(cp_guid)
                    cp_path = c_acc.full_path if c_acc else ""

                if trn.guid in tx_overrides:
                    ov = tx_overrides[trn.guid]
                    cat_id = ov.get("parent_id", "cat_income_other")
                    cat_name = parent_map.get(cat_id, {}).get("name", "Other Income")
                    subcat = ov.get("category_name", "Assigned Income")
                    bg = "INCOME"
                    is_override = True
                    is_direct = bool(not ov.get("parent_id") and cat_id in parent_map)
                else:
                    cat_entry = cfg.categories.get(cp_guid)
                    if cat_entry:
                        cat_id = cat_entry.parent_id or cat_entry.category_id
                        cat_name = (
                            parent_map.get(cat_id, {}).get("name")
                            or (parent_map.get(cat_entry.parent_id, {}).get("name") if cat_entry.parent_id else None)
                            or ("Other Expenses" if cat_entry.taxonomy_id == "spending_categories" else "Other Income")
                        )
                        subcat = cat_entry.category_name
                        if cat_entry.taxonomy_id == "income_sources":
                            bg = "INCOME"
                        else:
                            bg = (
                                (cat_entry.budget_group_id.upper() if cat_entry.budget_group_id else None)
                                or (parent_map.get(cat_id, {}).get("budget_group") if cat_id in parent_map else None)
                                or "NEEDS"
                            )
                        is_direct = bool(not cat_entry.parent_id or cat_entry.category_id in parent_map)
                    else:
                        cat_id = "cat_income_other"
                        cat_name = parent_map.get("cat_income_other", {}).get("name", "Other Income")
                        subcat = "Uncategorized Income"
                        bg = "INCOME"
                        is_direct = True

            # Determine exact Wealthfolio activity_type matching migrator logic
            if is_xfer:
                act_type = "TRANSFER_OUT" if sm.value < Decimal("0.00") else "TRANSFER_IN"
            elif direction == "outflow":
                cp_acc = by_guid.get(pos_ext[0].account_guid) if (pos_ext and len(pos_ext) == 1) else None
                cp_entry = cfg.categories.get(pos_ext[0].account_guid) if (pos_ext and len(pos_ext) == 1) else None
                if pos_ext and len(pos_ext) == 1 and (
                    (cp_entry and (cp_entry.category_id == "cat_fees" or cp_entry.parent_id == "cat_fees"))
                    or "fee" in (cp_acc.full_path.lower() if cp_acc else "")
                ):
                    act_type = "FEE"
                else:
                    act_type = "WITHDRAWAL"
            else:  # inflow
                roles = {
                    cfg.accounts[s.account_guid].role
                    for s in (neg_ext or ext_splits)
                    if s.account_guid in cfg.accounts
                }
                if AccountRole.EXPENSE in roles:
                    act_type = "CREDIT"
                elif (cat_entry and (cat_entry.category_id == "cat_income_interest" or cat_entry.parent_id == "cat_income_interest")) or "interest" in cp_path.lower():
                    act_type = "INTEREST"
                elif cat_entry and cat_entry.taxonomy_id == "spending_categories":
                    act_type = "CREDIT"
                else:
                    act_type = "DEPOSIT"

            tx_list.append(
                {
                    "guid": trn.guid,
                    "split_guid": sm.guid,
                    "date": trn.date_posted,
                    "account": acc_name,
                    "account_guid": sm.account_guid,
                    "counterpart": cp_path,
                    "counterpart_guid": cp_guid,
                    "payee": trn.description.strip(),
                    "memo": sm.memo.strip(),
                    "amount": amount,
                    "direction": direction,
                    "activity_type": act_type,
                    "budget_group": bg,
                    "category_name": cat_name,
                    "category_id": cat_id,
                    "subcategory_name": subcat,
                    "is_override": is_override,
                    "is_direct": is_direct,
                    "needs_review": (not cat_id or cat_id == "cat_unmapped" or is_direct),
                }
            )

    return tx_list


class EditorRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy logging during standard tests
        pass

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message: str, status: int = 400):
        self._send_json({"error": message, "status": "error"}, status=status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_index_html()
            return

        if path == "/api/state":
            self._handle_get_state()
            return

        if path == "/api/transactions":
            self._handle_get_transactions(parsed.query)
            return

        if path == "/api/backups":
            self._handle_get_backups()
            return

        if path == "/api/onboarding/status":
            self._handle_get_onboarding_status()
            return

        if path == "/api/mapping/export":
            self._handle_get_mapping_export()
            return

        if path == "/api/backups/mappings":
            self._handle_get_mapping_backups()
            return

        if path == "/api/wealthfolio_preview":
            self._handle_get_wealthfolio_preview()
            return

        if path == "/api/rules":
            self._handle_get_rules()
            return

        self._send_error("Not Found", status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except Exception as e:
            self._send_error(f"Invalid JSON: {e}", status=400)
            return

        if path == "/api/onboarding/initialize":
            self._handle_post_onboarding_initialize(payload)
            return

        if path == "/api/mapping/import":
            self._handle_post_mapping_import(payload)
            return

        if path == "/api/mapping":
            self._handle_post_mapping(payload)
            return

        if path == "/api/refresh":
            self.server._transactions_cache = None
            self._send_json({"status": "ok", "message": "Transactions cache cleared and refreshed"})
            return

        if path == "/api/backups/prune":
            self._handle_post_backups_prune(payload)
            return

        if path == "/api/backups/mappings/prune":
            self._handle_post_mapping_backups_prune(payload)
            return

        if path == "/api/transaction_override":
            self._handle_post_transaction_override(payload)
            return

        if path == "/api/bulk_assign":
            self._handle_post_bulk_assign(payload)
            return

        if path == "/api/budget_groups":
            self._handle_post_budget_groups(payload)
            return

        if path == "/api/accounts_mapping":
            self._handle_post_accounts_mapping(payload)
            return

        if path == "/api/categories/custom_parent":
            self._handle_post_custom_parent(payload)
            return

        if path == "/api/backups/mappings/restore":
            self._handle_post_mapping_backup_restore(payload)
            return

        if path == "/api/backups/mappings/create":
            self._handle_post_mapping_backup_create(payload)
            return

        if path == "/api/paths/update":
            self._handle_post_paths_update(payload)
            return

        if path == "/api/paths/pick":
            self._handle_post_paths_pick(payload)
            return

        if path == "/api/paths/open_location":
            self._handle_post_paths_open_location(payload)
            return

        if path == "/api/rules":
            self._handle_post_rules(payload)
            return

        if path == "/api/alternative_assets":
            self._handle_post_alternative_assets(payload)
            return

        if path == "/api/mapping/subtree":
            self._handle_post_mapping_subtree(payload)
            return

        if path == "/api/migrate":
            self._handle_post_migrate(payload)
            return

        if path == "/api/launch":
            self._handle_post_launch()
            return

        self._send_error("Not Found", status=404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/rules/"):
            rule_id = urllib.parse.unquote(path[len("/api/rules/"):])
            self._handle_delete_rule(rule_id)
            return

        if path.startswith("/api/backups/mappings/"):
            filename = urllib.parse.unquote(path[len("/api/backups/mappings/"):])
            mapping_backup_dir = self.server.backup_dir / "mappings"
            target_file = mapping_backup_dir / filename
            if target_file.exists():
                try:
                    target_file.unlink()
                    self._send_json({"status": "ok", "deleted": filename})
                except OSError as e:
                    self._send_error(f"Could not delete mapping backup: {e}", status=500)
            else:
                self._send_error(f"Mapping backup '{filename}' not found", status=404)
            return

        if path.startswith("/api/backups/"):
            filename = urllib.parse.unquote(path[len("/api/backups/"):])
            ok = delete_backup(self.server.backup_dir, filename)
            if ok:
                self._send_json({"status": "ok", "deleted": filename})
            else:
                self._send_error(f"Could not delete backup '{filename}'", status=400)
            return

        self._send_error("Not Found", status=404)

    def _serve_index_html(self):
        html_path = Path(__file__).parent / "web" / "index.html"
        if not html_path.exists():
            body = b"<!DOCTYPE html><html><body><h1>Wealthfolio Importer Editor</h1><p>web/index.html not found</p></body></html>"
        else:
            body = html_path.read_bytes()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_get_transactions(self, query_string: str):
        if not hasattr(self.server, "_transactions_cache") or self.server._transactions_cache is None:
            self.server._transactions_cache = _build_all_transactions(self.server)

        all_txs = self.server._transactions_cache

        params = urllib.parse.parse_qs(query_string)
        page = int(params.get("page", ["1"])[0]) if params.get("page") else 1
        limit = int(params.get("limit", ["100"])[0]) if params.get("limit") else 100
        limit = max(10, min(500, limit))

        q = params.get("q", [""])[0].strip().lower()
        budget_group = params.get("budget_group", [""])[0].strip().upper()
        category_id = params.get("category_id", [""])[0].strip()
        account_filter = params.get("account", [""])[0].strip().lower()
        counterpart_filter = params.get("counterpart", [""])[0].strip().lower()
        activity_type_filter = params.get("activity_type", [""])[0].strip().upper()
        needs_review_filter = params.get("needs_review", ["0"])[0].strip().lower() in ("1", "true")
        direct_filter = params.get("direct", ["0"])[0].strip().lower() in ("1", "true")
        uncategorized_filter = params.get("uncategorized", ["0"])[0].strip().lower() in ("1", "true")
        sort_col = params.get("sort", ["date"])[0].strip()
        order = params.get("order", ["desc"])[0].strip().lower()

        filtered = all_txs
        if needs_review_filter:
            filtered = [t for t in filtered if t["needs_review"]]
        if direct_filter:
            filtered = [t for t in filtered if t.get("is_direct", False)]
        if uncategorized_filter:
            filtered = [
                t for t in filtered
                if (
                    t.get("direction") == "outflow"
                    and (
                        not t.get("category_id")
                        or t.get("category_id") in ("cat_unmapped", "cat_gnc_other_expenses_uncat")
                        or (
                            t.get("subcategory_name") in ("Uncategorized Expense", "Other Expenses")
                            and not t.get("counterpart")
                        )
                        or (
                            t.get("is_direct", False)
                            and (not t.get("subcategory_name") or t.get("subcategory_name") == t.get("category_name"))
                        )
                    )
                )
            ]
        if activity_type_filter and activity_type_filter != "ALL":
            filtered = [t for t in filtered if t.get("activity_type") == activity_type_filter]
        if budget_group and budget_group != "ALL":
            filtered = [t for t in filtered if t["budget_group"] == budget_group]
        if category_id and category_id != "ALL":
            filtered = [t for t in filtered if t["category_id"] == category_id]
        if account_filter:
            filtered = [t for t in filtered if account_filter in t["account"].lower()]
        if counterpart_filter:
            filtered = [t for t in filtered if counterpart_filter in t["counterpart"].lower()]
        if q:
            filtered = [
                t
                for t in filtered
                if q in t["payee"].lower()
                or q in t["memo"].lower()
                or q in t["counterpart"].lower()
                or q in t["account"].lower()
                or q in t["subcategory_name"].lower()
                or q in t["category_name"].lower()
                or q in t.get("activity_type", "").lower()
                or q in t["guid"].lower()
                or q in t["split_guid"].lower()
            ]

        rev = (order == "desc")
        if sort_col == "amount":
            filtered = sorted(filtered, key=lambda t: t["amount"], reverse=rev)
        elif sort_col == "payee":
            filtered = sorted(filtered, key=lambda t: t["payee"].lower(), reverse=rev)
        elif sort_col == "account":
            filtered = sorted(filtered, key=lambda t: t["account"].lower(), reverse=rev)
        elif sort_col == "counterpart":
            filtered = sorted(filtered, key=lambda t: t["counterpart"].lower(), reverse=rev)
        elif sort_col == "activity_type":
            filtered = sorted(filtered, key=lambda t: t.get("activity_type", ""), reverse=rev)
        elif sort_col == "budget_group":
            filtered = sorted(filtered, key=lambda t: t["budget_group"], reverse=rev)
        elif sort_col == "category":
            filtered = sorted(filtered, key=lambda t: t["category_name"].lower(), reverse=rev)
        elif sort_col == "subcategory":
            filtered = sorted(filtered, key=lambda t: t["subcategory_name"].lower(), reverse=rev)
        else:  # date
            filtered = sorted(filtered, key=lambda t: t["date"], reverse=rev)

        filtered_total = len(filtered)
        total_pages = max(1, (filtered_total + limit - 1) // limit)
        page = max(1, min(page, total_pages))
        start = (page - 1) * limit
        end = start + limit
        items = filtered[start:end]

        uncategorized_total = sum(
            1 for t in all_txs
            if (
                t.get("direction") == "outflow"
                and (
                    not t.get("category_id")
                    or t.get("category_id") in ("cat_unmapped", "cat_gnc_other_expenses_uncat")
                    or (
                        t.get("subcategory_name") in ("Uncategorized Expense", "Other Expenses")
                        and not t.get("counterpart")
                    )
                    or (
                        t.get("is_direct", False)
                        and (not t.get("subcategory_name") or t.get("subcategory_name") == t.get("category_name"))
                    )
                )
            )
        )

        from collections import Counter
        type_counts = Counter(t.get("activity_type", "WITHDRAWAL") for t in all_txs)

        self._send_json(
            {
                "total": len(all_txs),
                "filtered_total": filtered_total,
                "direct_total": sum(1 for t in all_txs if t.get("is_direct", False)),
                "uncategorized_total": uncategorized_total,
                "type_counts": dict(type_counts),
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "transactions": items,
            }
        )

    def _handle_get_wealthfolio_preview(self):
        if not hasattr(self.server, "_transactions_cache") or self.server._transactions_cache is None:
            self.server._transactions_cache = _build_all_transactions(self.server)

        all_txs = self.server._transactions_cache
        mapping_path = self.server.mapping_path
        cfg = load_mapping_config(mapping_path) if mapping_path.exists() else MappingConfig()

        parent_map = {p["id"]: dict(p) for p in DEFAULT_PARENTS}
        for p_id, bg in getattr(cfg, "budget_group_overrides", {}).items():
            if p_id in parent_map:
                parent_map[p_id]["budget_group"] = bg.upper()
        for cr in getattr(cfg, "custom_root_categories", []):
            bg = (cr.budget_group_id or ("INCOME" if cr.taxonomy_id == "income_sources" else "OTHER")).upper()
            if cr.category_id in getattr(cfg, "budget_group_overrides", {}):
                bg = cfg.budget_group_overrides[cr.category_id].upper()
            parent_map[cr.category_id] = {
                "id": cr.category_id,
                "name": cr.category_name,
                "taxonomy_id": cr.taxonomy_id,
                "budget_group": bg,
                "color": cr.color,
            }

        cat_tree: dict[str, dict] = {}
        for p_id, p_info in parent_map.items():
            cat_tree[p_id] = {
                "id": p_id,
                "name": p_info["name"],
                "taxonomy_id": p_info.get("taxonomy_id", "spending_categories"),
                "budget_group": p_info.get("budget_group", "OTHER"),
                "color": p_info.get("color", "#808080"),
                "total_amount": 0.0,
                "tx_count": 0,
                "direct_amount": 0.0,
                "direct_count": 0,
                "subcategories": {},
            }

        bg_totals = {bg: {"count": 0, "amount": 0.0} for bg in ["NEEDS", "WANTS", "INVESTMENT", "SAVINGS", "GIVING", "PERSONAL", "OTHER", "INCOME"]}

        total_income = 0.0
        total_spending = 0.0
        direct_tx_count = 0
        direct_tx_amount = 0.0

        for t in all_txs:
            cid = t.get("category_id")
            amt = float(t.get("amount", 0.0))
            bg = t.get("budget_group", "OTHER")
            is_direct = bool(t.get("is_direct", False))
            subcat = t.get("subcategory_name", "General")

            if t["direction"] == "inflow":
                total_income += amt
            elif t["direction"] == "outflow":
                total_spending += amt

            if bg in bg_totals:
                bg_totals[bg]["count"] += 1
                bg_totals[bg]["amount"] += amt

            if is_direct:
                direct_tx_count += 1
                direct_tx_amount += amt

            if cid not in cat_tree:
                cat_tree[cid] = {
                    "id": cid,
                    "name": t.get("category_name", cid),
                    "taxonomy_id": "income_sources" if bg == "INCOME" else "spending_categories",
                    "budget_group": bg,
                    "color": "#808080",
                    "total_amount": 0.0,
                    "tx_count": 0,
                    "direct_amount": 0.0,
                    "direct_count": 0,
                    "subcategories": {},
                }

            cat_tree[cid]["total_amount"] += amt
            cat_tree[cid]["tx_count"] += 1

            if is_direct:
                cat_tree[cid]["direct_amount"] += amt
                cat_tree[cid]["direct_count"] += 1

            if subcat not in cat_tree[cid]["subcategories"]:
                cat_tree[cid]["subcategories"][subcat] = {
                    "name": subcat,
                    "amount": 0.0,
                    "tx_count": 0,
                    "is_direct": is_direct,
                }
            cat_tree[cid]["subcategories"][subcat]["amount"] += amt
            cat_tree[cid]["subcategories"][subcat]["tx_count"] += 1

        cat_list = []
        for cid, cdata in cat_tree.items():
            if cdata["tx_count"] == 0:
                continue
            sub_list = sorted(cdata["subcategories"].values(), key=lambda s: s["amount"], reverse=True)
            cdata["subcategories"] = sub_list
            cat_list.append(cdata)

        cat_list.sort(key=lambda c: c["total_amount"], reverse=True)

        acc_list = []
        for gnc_guid, acc in cfg.accounts.items():
            if acc.wf_account_id is not None:
                acc_name = acc.wf_name or acc.gnc_path.split(":")[-1]
                acc_type = getattr(acc, "wf_account_type", None) or ("LOAN" if getattr(acc, "is_mortgage_loan", False) else ("CREDIT_CARD" if "CARD" in acc.role else "CASH"))
                acc_group = getattr(acc, "wf_group", None) or ("Loans" if getattr(acc, "is_mortgage_loan", False) else ("Archived" if acc.is_archived else ("Credit Cards" if "CARD" in acc.role else "Banking")))
                acc_status = "ARCHIVED" if acc.is_archived else "ACTIVE"
                acc_list.append({
                    "gnc_guid": gnc_guid,
                    "gnc_path": acc.gnc_path,
                    "name": acc_name,
                    "wf_name": acc_name,
                    "type": acc_type,
                    "group": acc_group,
                    "status": acc_status,
                    "role": acc.role,
                    "is_active": acc.is_active,
                    "is_archived": acc.is_archived,
                    "tx_count": acc.tx_count,
                })

        acc_list.sort(key=lambda a: (0 if a["is_active"] else 1, -a["tx_count"]))

        self._send_json({
            "status": "ok",
            "summary": {
                "total_transactions": len(all_txs),
                "direct_count": direct_tx_count,
                "direct_amount": round(direct_tx_amount, 2),
                "spending_total": round(total_spending, 2),
                "total_spending": round(total_spending, 2),
                "income_total": round(total_income, 2),
                "total_income": round(total_income, 2),
                "accounts_count": len(acc_list),
                "accounts_imported": len(acc_list),
                "active_accounts": sum(1 for a in acc_list if a["is_active"]),
                "accounts_active": sum(1 for a in acc_list if a["is_active"]),
                "archived_accounts": sum(1 for a in acc_list if a["is_archived"]),
                "accounts_archived": sum(1 for a in acc_list if a["is_archived"]),
                "budget_groups": {k: {"count": v["count"], "amount": round(v["amount"], 2)} for k, v in bg_totals.items()},
            },
            "categories": cat_list,
            "accounts": acc_list,
        })

    def _handle_get_state(self):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            cfg = MappingConfig()
        else:
            cfg = load_mapping_config(mapping_path)

        bg_overrides = getattr(cfg, "budget_group_overrides", {})

        tx_counts = {}
        for acc in cfg.accounts.values():
            tx_counts[acc.gnc_guid] = acc.tx_count

        parent_map = {p["id"]: dict(p) for p in DEFAULT_PARENTS}
        for p_id, bg in bg_overrides.items():
            if p_id in parent_map:
                parent_map[p_id]["budget_group"] = bg.upper()

        seen_parent_ids = set()
        seen_parent_names = set()
        parents_data = []
        for p in DEFAULT_PARENTS:
            p_id = p["id"]
            p_name = p["name"].strip().lower()
            if p_id in seen_parent_ids or p_name in seen_parent_names:
                continue
            p_copy = dict(p)
            if p_id in bg_overrides:
                p_copy["budget_group"] = bg_overrides[p_id].upper()
            parents_data.append(p_copy)
            seen_parent_ids.add(p_id)
            seen_parent_names.add(p_name)

        for cr in getattr(cfg, "custom_root_categories", []):
            cr_id = cr.category_id
            cr_name = cr.category_name.strip().lower()
            if cr_id in seen_parent_ids or cr_name in seen_parent_names:
                continue
            bg = (cr.budget_group_id or ("INCOME" if cr.taxonomy_id == "income_sources" else "OTHER")).upper()
            if cr_id in bg_overrides:
                bg = bg_overrides[cr_id].upper()
            p_dict = {
                "id": cr_id,
                "name": cr.category_name,
                "taxonomy_id": cr.taxonomy_id,
                "budget_group": bg,
                "color": cr.color,
                "is_custom": True,
            }
            parents_data.append(p_dict)
            parent_map[cr_id] = p_dict
            seen_parent_ids.add(cr_id)
            seen_parent_names.add(cr_name)

        parents_data.sort(key=lambda p: p["name"].lower())

        txs = getattr(self.server, "_transactions_cache", None)
        if txs is None:
            txs = _build_all_transactions(self.server)
            self.server._transactions_cache = txs

        # Map counterpart accounts to their predominant Wealthfolio activity type and total transaction volume
        from collections import Counter, defaultdict
        cp_type_counts = {}
        cp_total_amounts = defaultdict(float)
        for t in txs:
            cpg = t.get("counterpart_guid")
            act = t.get("activity_type")
            amt = t.get("amount", 0.0)
            if cpg:
                cp_total_amounts[cpg] += amt
                if act:
                    if cpg not in cp_type_counts:
                        cp_type_counts[cpg] = Counter()
                    cp_type_counts[cpg][act] += 1

        def _infer_activity_type(guid, gnc_path, taxonomy_id):
            if guid in cp_type_counts and cp_type_counts[guid]:
                return cp_type_counts[guid].most_common(1)[0][0]
            if taxonomy_id == "income_sources" or gnc_path.startswith("Income:"):
                return "DEPOSIT"
            return "WITHDRAWAL"

        accounts_data = []
        seen_category_guids = set()

        for guid, cat in sorted(cfg.categories.items(), key=lambda kv: kv[1].gnc_path):
            seen_category_guids.add(guid)
            cnt = tx_counts.get(guid, 0)
            is_unmapped = cnt > 0 and (not cat.category_id or cat.category_id == "cat_unmapped")
            is_other = (
                cnt > 0
                and (
                    cat.parent_id in ("cat_other_expense", "cat_income_other")
                    or cat.category_id in ("cat_other_expense", "cat_income_other")
                    or (not cat.parent_id and cat.category_id in parent_map)
                )
            )
            is_generic_fallback = (
                cnt > 0
                and (
                    cat.category_id in ("cat_gnc_other_expenses_uncat", "cat_gnc_other_income_uncat")
                    or cat.category_name in ("Uncategorized Expense", "Uncategorized Income")
                )
            )
            bg = "OTHER"
            if getattr(cat, "budget_group_id", None):
                bg = cat.budget_group_id.upper()
            elif cat.category_id in bg_overrides:
                bg = bg_overrides[cat.category_id].upper()
            elif cat.parent_id and cat.parent_id in parent_map:
                bg = parent_map[cat.parent_id]["budget_group"]
            elif cat.category_id in parent_map:
                bg = parent_map[cat.category_id]["budget_group"]
            elif cat.taxonomy_id == "income_sources":
                bg = "INCOME"

            accounts_data.append(
                {
                    "gnc_guid": guid,
                    "gnc_path": cat.gnc_path,
                    "category_name": cat.category_name,
                    "parent_id": cat.parent_id,
                    "category_id": cat.category_id,
                    "taxonomy_id": cat.taxonomy_id,
                    "budget_group": bg,
                    "activity_type": _infer_activity_type(guid, cat.gnc_path, cat.taxonomy_id),
                    "is_custom": getattr(cat, "is_custom", False),
                    "is_seeded": cat.is_seeded,
                    "tx_count": cnt,
                    "total_amount": round(cp_total_amounts.get(guid, 0.0), 2),
                    "is_other_expense": is_other,
                    "is_fallback": is_generic_fallback or is_unmapped,
                    "is_needs_review": is_unmapped or is_generic_fallback,
                    "is_root": not cat.parent_id,
                    "color": cat.color,
                }
            )

        # Ensure all GnuCash counterpart accounts interacting with migrated accounts are listed
        migrated_guids = {
            g for g, a in cfg.accounts.items() if (a.role and a.role.startswith("MIGRATE_"))
        }
        for trn in self.server.book.transactions:
            mig_splits = [s for s in trn.splits if s.account_guid in migrated_guids]
            if not mig_splits:
                continue
            ext_splits = [s for s in trn.splits if s.account_guid not in migrated_guids]
            for s in ext_splits:
                cp_guid = s.account_guid
                if cp_guid not in seen_category_guids:
                    seen_category_guids.add(cp_guid)
                    cp_acc = self.server.book.accounts.get(cp_guid)
                    if cp_acc:
                        cnt = tx_counts.get(cp_guid, 0)
                        accounts_data.append(
                            {
                                "gnc_guid": cp_guid,
                                "gnc_path": cp_acc.full_path,
                                "category_name": cp_acc.name or cp_acc.full_path.split(":")[-1],
                                "parent_id": "cat_other_expense",
                                "category_id": "cat_unmapped",
                                "taxonomy_id": "spending_categories",
                                "budget_group": "OTHER",
                                "activity_type": _infer_activity_type(cp_guid, cp_acc.full_path, "spending_categories"),
                                "is_custom": False,
                                "is_seeded": False,
                                "tx_count": cnt,
                                "is_other_expense": True,
                                "is_fallback": True,
                                "is_needs_review": True,
                                "is_root": True,
                                "color": "#6B7280",
                            }
                        )

        accounts_data.sort(key=lambda a: a["gnc_path"].lower())

        # Extract granular activities categorized to generic Other Expenses (for backward-compatibility)
        other_transactions = []
        db_to_query = (
            self.server.target_db if self.server.target_db.exists() else self.server.source_db
        )
        if db_to_query.exists():
            import sqlite3
            try:
                conn = sqlite3.connect(f"file:{db_to_query.resolve()}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("""
                    SELECT a.id, a.activity_date, acc.name, a.amount, a.notes, a.metadata, tc.name
                    FROM activities a
                    JOIN accounts acc ON a.account_id = acc.id
                    JOIN activity_taxonomy_assignments ata ON a.id = ata.activity_id AND ata.taxonomy_id = 'spending_categories'
                    JOIN taxonomy_categories tc ON ata.category_id = tc.id
                    WHERE tc.id = 'cat_other_expense' OR tc.parent_id = 'cat_other_expense'
                    ORDER BY a.activity_date DESC
                    LIMIT 300;
                """)
                by_guid = self.server.book.accounts
                by_trn_guid = {t.guid: t for t in self.server.book.transactions}
                for row in cur.fetchall():
                    act_id, act_date, acc_name, amount, notes, meta_str, cat_name = row
                    gnc_trn_guid = None
                    counter_path = ""
                    if meta_str:
                        try:
                            meta = json.loads(meta_str)
                            gnc_trn_guid = meta.get("gnucash_trn_guid")
                            if gnc_trn_guid and gnc_trn_guid in by_trn_guid:
                                trn_obj = by_trn_guid[gnc_trn_guid]
                                for s in trn_obj.splits:
                                    if s.value > 0 and s.account_guid in by_guid:
                                        counter_path = by_guid[s.account_guid].full_path
                                        break
                        except Exception:
                            pass

                    other_transactions.append(
                        {
                            "id": act_id,
                            "date": act_date[:10] if act_date else "",
                            "account": acc_name,
                            "amount": float(amount) if amount else 0.0,
                            "notes": notes or "",
                            "gnc_trn_guid": gnc_trn_guid,
                            "gnc_path": counter_path,
                            "category_name": cat_name,
                        }
                    )
                conn.close()
            except Exception:
                pass

        backups = list_backups(self.server.backup_dir)
        total_backup_bytes = sum(b.size_bytes for b in backups)
        backups_data = [
            {
                "filename": b.filename,
                "size_mb": b.size_mb,
                "created_at": b.created_at,
            }
            for b in backups
        ]

        # Compute ending balances per account GUID
        from collections import defaultdict
        from decimal import Decimal
        gnc_balances = defaultdict(lambda: Decimal("0.00"))
        for trn in self.server.book.transactions:
            for sp in trn.splits:
                if sp.value != Decimal("0.00"):
                    gnc_balances[sp.account_guid] += sp.value

        # GnuCash Balance Sheet Accounts (Assets, Liabilities, Equity)
        alt_specs_by_path = {
            spec["gnc_path"]: spec
            for spec in getattr(cfg, "alternative_assets", [])
            if isinstance(spec, dict) and "gnc_path" in spec
        }
        gnc_accounts_data = []
        for guid, acc in sorted(cfg.accounts.items(), key=lambda kv: kv[1].gnc_path):
            gnc_acc = self.server.book.accounts.get(guid)
            acct_type = gnc_acc.acct_type if gnc_acc else "UNKNOWN"
            is_bs = acct_type in (
                "BANK",
                "CASH",
                "CREDIT",
                "LIABILITY",
                "ASSET",
                "EQUITY",
                "MUTUAL",
                "STOCK",
            ) or acc.role not in ("EXPENSE", "INCOME")

            alt_spec = alt_specs_by_path.get(acc.gnc_path)
            alt_asset_name = alt_spec["name"] if alt_spec else None
            alt_kind = alt_spec["kind"] if alt_spec else None
            alt_display_code = alt_spec["display_code"] if alt_spec else None

            path_lower = acc.gnc_path.lower()
            is_mortgage_loan = (
                acc.gnc_path.startswith("Liabilities:Loans")
                or any(k in path_lower for k in ("mortgage", "home loan", "investment loan", "property loan"))
                or (acc.role and "LOAN" in acc.role)
                or (alt_spec is not None and alt_spec.get("kind") == "LIABILITY")
            )
            if not alt_asset_name and is_mortgage_loan and acc.gnc_path != "Liabilities:Loans":
                leaf = acc.gnc_path.split(":")[-1]
                alt_asset_name = f"Mortgage - {leaf}"
                alt_display_code = "Mortgage"

            gnc_accounts_data.append(
                {
                    "gnc_guid": guid,
                    "gnc_path": acc.gnc_path,
                    "role": acc.role,
                    "acct_type": acct_type,
                    "is_balance_sheet": is_bs,
                    "is_mortgage_loan": is_mortgage_loan,
                    "alt_asset_name": alt_asset_name,
                    "alt_kind": alt_kind,
                    "alt_display_code": alt_display_code,
                    "wf_account_id": acc.wf_account_id,
                    "wf_name": acc.wf_name or alt_asset_name,
                    "wf_account_type": acc.wf_account_type,
                    "wf_group": acc.wf_group,
                    "is_active": acc.is_active,
                    "is_archived": acc.is_archived,
                    "tx_count": acc.tx_count,
                    "gnc_balance": float(gnc_balances[guid]),
                    "is_imported": (acc.wf_account_id is not None),
                    "is_custom": getattr(acc, "is_custom", False),
                }
            )

        txs = getattr(self.server, "_transactions_cache", None)
        if txs is None:
            txs = _build_all_transactions(self.server)
            self.server._transactions_cache = txs
        direct_tx_count = sum(1 for t in txs if t.get("is_direct", False))
        root_cat_count = sum(1 for a in accounts_data if a.get("is_root") and a.get("tx_count", 0) > 0)

        data = {
            "paths": {
                "gnucash": str(getattr(self.server, "gnucash_path", "") or ""),
                "mapping": str(self.server.mapping_path.resolve() if self.server.mapping_path else ""),
                "wealthfolio_db": str(self.server.source_db.resolve() if self.server.source_db else ""),
                "target_db": str(self.server.target_db.resolve() if self.server.target_db else ""),
                "backup_dir": str(self.server.backup_dir.resolve() if self.server.backup_dir else ""),
            },
            "book": {
                "accounts_count": len(self.server.book.accounts),
                "transactions_count": len(self.server.book.transactions),
            },
            "parents": parents_data,
            "budget_groups": BUDGET_GROUPS,
            "budget_group_overrides": bg_overrides,
            "accounts": accounts_data,
            "gnc_accounts": gnc_accounts_data,
            "other_transactions": other_transactions,
            "backups": backups_data,
            "total_backup_size_mb": round(total_backup_bytes / (1024 * 1024), 2),
            "direct_tx_count": direct_tx_count,
            "root_categories_count": root_cat_count,
        }
        self._send_json(data)

    def _handle_get_onboarding_status(self):
        mapping_path = self.server.mapping_path
        exists = mapping_path.exists()
        accounts_count = 0
        categories_count = 0
        version = 2
        currency = "AUD"
        if exists:
            try:
                cfg = load_mapping_config(mapping_path)
                accounts_count = len(cfg.accounts)
                categories_count = len(cfg.categories)
                version = cfg.version
                currency = getattr(cfg.metadata, "currency", "AUD") if cfg.metadata else "AUD"
            except Exception:
                pass

        self._send_json({
            "is_first_launch": not exists or (accounts_count == 0 and categories_count == 0),
            "mapping_exists": exists,
            "mapping_path": str(mapping_path),
            "accounts_count": accounts_count,
            "categories_count": categories_count,
            "version": version,
            "currency": currency,
        })

    def _handle_post_onboarding_initialize(self, payload: dict):
        mapping_path = self.server.mapping_path
        book = self.server.book
        existing = load_mapping_config(mapping_path) if mapping_path.exists() else None

        cfg = build_mapping_config(book, existing_config=existing)
        if "currency" in payload:
            cfg.metadata.currency = payload["currency"]

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({
            "status": "ok",
            "message": "Initialized mapping configuration with standard templates and heuristics",
            "accounts_mapped": len(cfg.accounts),
            "categories_mapped": len(cfg.categories),
        })

    def _handle_get_mapping_export(self):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=404)
            return

        body = mapping_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="mapping.json"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_post_mapping_import(self, payload: dict):
        mapping_path = self.server.mapping_path
        mapping_content = payload.get("mapping")
        if not mapping_content or not isinstance(mapping_content, dict):
            self._send_error("Invalid mapping payload", status=400)
            return

        try:
            # Upgrade and validate
            from gnucash_to_wealthfolio.mapper import upgrade_mapping_config_if_needed
            upgraded = upgrade_mapping_config_if_needed(mapping_content)
            meta_raw = upgraded.get("metadata", {})
            metadata = MappingMetadata(
                generated_at=meta_raw.get("generated_at", ""),
                updated_at=meta_raw.get("updated_at", ""),
                gnucash_book_id=meta_raw.get("gnucash_book_id"),
                source_file=meta_raw.get("source_file"),
                currency=meta_raw.get("currency", "AUD"),
            )
            tmp_cfg = MappingConfig(
                version=upgraded.get("version", 2),
                metadata=metadata,
                accounts={k: AccountMapEntry(**v) for k, v in upgraded.get("accounts", {}).items()},
                categories={k: CategoryMapEntry(**v) for k, v in upgraded.get("categories", {}).items()},
                custom_root_categories=[CategoryMapEntry(**c) for c in upgraded.get("custom_root_categories", [])],
                rules=[RuleEntry(**r) for r in upgraded.get("rules", [])],
                alternative_assets=upgraded.get("alternative_assets", []),
                transaction_overrides=upgraded.get("transaction_overrides", {}),
                budget_group_overrides=upgraded.get("budget_group_overrides", {}),
            )
            save_mapping_config(tmp_cfg, mapping_path, backup_dir=self.server.backup_dir)
            self.server._transactions_cache = None
            self._send_json({"status": "ok", "message": "Successfully imported and backed up mapping configuration"})
        except Exception as e:
            self._send_error(f"Failed to import mapping: {e}", status=400)

    def _handle_get_mapping_backups(self):
        mapping_backup_dir = self.server.backup_dir / "mappings"
        backups = list_mapping_backups(mapping_backup_dir)
        total_bytes = sum(b.size_bytes for b in backups)
        self._send_json({
            "backups": [
                {
                    "filename": b.filename,
                    "size_mb": b.size_mb,
                    "size_bytes": b.size_bytes,
                    "created_at": b.created_at,
                }
                for b in backups
            ],
            "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        })

    def _handle_post_mapping_backups_prune(self, payload: dict):
        keep_latest = payload.get("keep_latest", 20)
        mapping_backup_dir = self.server.backup_dir / "mappings"
        deleted = prune_mapping_backups(mapping_backup_dir, keep_latest=keep_latest)
        self._send_json({"status": "ok", "deleted": deleted})

    def _handle_post_mapping_backup_restore(self, payload: dict):
        filename = payload.get("filename", "").strip()
        if not filename:
            self._send_error("Filename is required to restore a mapping snapshot", status=400)
            return

        mapping_backup_dir = self.server.backup_dir / "mappings"
        target_snapshot = (mapping_backup_dir / filename).resolve()
        # Security check: must reside inside mapping_backup_dir
        if not target_snapshot.is_file() or not target_snapshot.is_relative_to(mapping_backup_dir.resolve()):
            self._send_error(f"Snapshot file '{filename}' not found", status=404)
            return

        mapping_path = self.server.mapping_path
        try:
            # First validate that the snapshot can be parsed and upgraded
            content = json.loads(target_snapshot.read_text(encoding="utf-8"))
            from gnucash_to_wealthfolio.mapper import upgrade_mapping_config_if_needed
            upgraded = upgrade_mapping_config_if_needed(content)

            # Create an automatic pre-restore safety backup of current mapping if it exists
            if mapping_path.exists():
                backup_mapping_file(mapping_path, mapping_backup_dir)

            # Copy snapshot over mapping_path
            shutil.copy2(target_snapshot, mapping_path)
            self.server._transactions_cache = None

            self._send_json({
                "status": "ok",
                "message": f"Successfully restored snapshot '{filename}'",
                "filename": filename,
            })
        except Exception as e:
            self._send_error(f"Failed to restore mapping snapshot: {e}", status=500)

    def _handle_post_mapping_backup_create(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("Cannot create snapshot: mapping.json does not exist yet", status=400)
            return

        mapping_backup_dir = self.server.backup_dir / "mappings"
        mapping_backup_dir.mkdir(parents=True, exist_ok=True)

        name_suffix = payload.get("label", "").strip()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if name_suffix:
            # Sanitize label
            clean_label = re.sub(r"[^a-zA-Z0-9_\-]", "_", name_suffix)[:40]
            filename = f"mapping_{timestamp}_{clean_label}.json"
        else:
            filename = f"mapping_{timestamp}.json"

        target_file = mapping_backup_dir / filename
        try:
            shutil.copy2(mapping_path, target_file)
            self._send_json({
                "status": "ok",
                "message": f"Saved snapshot '{filename}'",
                "filename": filename,
            })
        except Exception as e:
            self._send_error(f"Failed to save snapshot: {e}", status=500)

    def _handle_get_backups(self):
        backups = list_backups(self.server.backup_dir)
        total_backup_bytes = sum(b.size_bytes for b in backups)
        self._send_json(
            {
                "backups": [
                    {
                        "filename": b.filename,
                        "size_mb": b.size_mb,
                        "created_at": b.created_at,
                    }
                    for b in backups
                ],
                "total_size_mb": round(total_backup_bytes / (1024 * 1024), 2),
            }
        )

    def _handle_post_mapping(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json does not exist", status=500)
            return

        cfg = load_mapping_config(mapping_path)
        updates = payload.get("updates")
        if updates is None:
            # Single update payload format
            updates = [payload]

        count = 0
        for upd in updates:
            guid = upd.get("gnc_guid")
            parent_id = upd.get("parent_id")
            subcat_name = upd.get("subcat_name", "")
            budget_group = upd.get("budget_group")
            if not guid:
                continue
            update_category_mapping(
                cfg,
                gnc_guid=guid,
                parent_id=parent_id,
                subcat_name=subcat_name,
                budget_group=budget_group,
            )
            # Group mass replace: clear any individual transaction overrides for this counterpart
            matching = [
                trn.guid for trn in self.server.book.transactions
                if any(s.account_guid == guid for s in trn.splits)
            ]
            for g in matching:
                cfg.transaction_overrides.pop(g, None)
            count += 1

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({"status": "ok", "updated": count})

    def _handle_post_accounts_mapping(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json does not exist", status=500)
            return

        cfg = load_mapping_config(mapping_path)
        updates = payload.get("updates")
        if updates is None:
            updates = [payload]

        count = 0
        for upd in updates:
            guid = upd.get("gnc_guid")
            if not guid or guid not in cfg.accounts:
                continue

            entry = cfg.accounts[guid]
            entry.is_custom = True
            new_role = upd.get("role", entry.role)
            entry.role = new_role

            if new_role.startswith("MIGRATE_"):
                if not entry.wf_account_id:
                    entry.wf_account_id = deterministic_uuid("gnucash_account", guid)
                entry.wf_name = (
                    upd.get("wf_name")
                    or entry.wf_name
                    or entry.gnc_path.split(":")[-1]
                )
                if "CARD" in new_role:
                    def_type = "CREDIT_CARD"
                    def_group = "Credit Cards"
                elif "LOAN" in new_role:
                    def_type = "LOAN"
                    def_group = "Loans"
                else:
                    def_type = "CASH"
                    def_group = "Banking"

                entry.wf_account_type = (
                    upd.get("wf_account_type")
                    or entry.wf_account_type
                    or def_type
                )
                entry.wf_group = (
                    upd.get("wf_group")
                    or entry.wf_group
                    or ("Archived" if "ARCHIVED" in new_role else def_group)
                )
                entry.is_active = 1
                entry.is_archived = int(
                    upd.get("is_archived", 1 if "ARCHIVED" in new_role else 0)
                )
            else:
                entry.wf_account_id = None
                if "wf_name" in upd:
                    entry.wf_name = upd.get("wf_name")
                if "wf_account_type" in upd:
                    entry.wf_account_type = upd.get("wf_account_type")
                if "wf_group" in upd:
                    entry.wf_group = upd.get("wf_group")
                entry.is_active = int(upd.get("is_active", 0))
                entry.is_archived = int(upd.get("is_archived", 0))

            count += 1

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({"status": "ok", "updated": count})

    def _handle_post_transaction_override(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        cfg = load_mapping_config(mapping_path)
        trn_guid = payload.get("gnc_trn_guid")
        raw_pattern = payload.get("payee_pattern", "")
        clean_pattern = re.sub(r"\s+", " ", raw_pattern).strip() if raw_pattern else ""
        counterpart_guid = payload.get("counterpart_guid")
        scope = payload.get("scope")
        parent_id = payload.get("parent_id")
        subcat_name = (payload.get("subcat_name") or "").strip()
        budget_group = payload.get("budget_group")

        slug = _slugify(subcat_name or clean_pattern)[:36].strip("_")
        short_hash = stable_id("gnc_cat", [subcat_name or clean_pattern])[:10]
        cat_id = f"cat_gnc_{slug}_{short_hash}" if parent_id else (parent_id or "cat_other_expense")

        if scope is None:
            if clean_pattern:
                rule_id = deterministic_uuid("gnucash_rule_user_payee", clean_pattern.lower())
                cfg.rules = [
                    r
                    for r in cfg.rules
                    if r.pattern.lower() != clean_pattern.lower()
                    and re.sub(r"\s+", " ", r.pattern).strip().lower() != clean_pattern.lower()
                ]
                cfg.rules.insert(
                    0,
                    RuleEntry(
                        id=rule_id,
                        name=f"Payee: {clean_pattern[:30]}",
                        pattern=clean_pattern,
                        match_type="contains",
                        taxonomy_id="spending_categories",
                        category_id=cat_id,
                        activity_type="WITHDRAWAL",
                        priority=1,
                    ),
                )
            if trn_guid:
                cfg.transaction_overrides[trn_guid] = {
                    "category_id": cat_id,
                    "category_name": subcat_name,
                    "parent_id": parent_id,
                    "budget_group": budget_group,
                }
        elif scope == "account":
            if not counterpart_guid:
                self._send_error("counterpart_guid is required for account mapping scope", status=400)
                return
            if counterpart_guid in cfg.categories:
                update_category_mapping(
                    cfg,
                    gnc_guid=counterpart_guid,
                    parent_id=parent_id,
                    subcat_name=subcat_name,
                    budget_group=budget_group,
                )
            else:
                c_acc = self.server.book.accounts.get(counterpart_guid)
                path = c_acc.full_path if c_acc else "Expenses:Custom"
                cfg.categories[counterpart_guid] = CategoryMapEntry(
                    gnc_guid=counterpart_guid,
                    gnc_path=path,
                    taxonomy_id="spending_categories",
                    category_id=cat_id,
                    category_key=f"gnc_{slug}_{short_hash}",
                    category_name=subcat_name or path.split(":")[-1],
                    parent_id=parent_id,
                    budget_group_id=budget_group.lower() if budget_group else None,
                    is_seeded=False,
                    is_custom=True,
                    color=ROOT_COLORS.get(parent_id or cat_id, "#808080"),
                )
            # Group mass replace: clear all single transaction overrides for this counterpart
            matching = [
                trn.guid for trn in self.server.book.transactions
                if any(s.account_guid == counterpart_guid for s in trn.splits)
            ]
            for g in matching:
                cfg.transaction_overrides.pop(g, None)
            if trn_guid and trn_guid in cfg.transaction_overrides:
                del cfg.transaction_overrides[trn_guid]

        elif scope == "rule":
            if not clean_pattern:
                self._send_error("payee_pattern is required for payee rule scope", status=400)
                return

            rule_id = deterministic_uuid("gnucash_rule_user_payee", clean_pattern.lower())
            # Ensure custom subcategory exists in cfg.categories so migrator and UI recognize it
            dummy_guid = deterministic_uuid("gnc_cat_rule", cat_id).replace("-", "")
            if dummy_guid not in cfg.categories:
                cfg.categories[dummy_guid] = CategoryMapEntry(
                    gnc_guid=dummy_guid,
                    gnc_path=f"Custom:Rule:{subcat_name or clean_pattern}",
                    taxonomy_id="spending_categories",
                    category_id=cat_id,
                    category_key=f"gnc_{slug}_{short_hash}",
                    category_name=subcat_name or clean_pattern,
                    parent_id=parent_id,
                    budget_group_id=budget_group.lower() if budget_group else None,
                    is_seeded=False,
                    is_custom=True,
                    color=ROOT_COLORS.get(parent_id or cat_id, "#808080"),
                )

            # Remove any rules with the exact or whitespace-normalized pattern
            cfg.rules = [
                r
                for r in cfg.rules
                if r.pattern.lower() != clean_pattern.lower()
                and re.sub(r"\s+", " ", r.pattern).strip().lower() != clean_pattern.lower()
            ]
            cfg.rules.insert(
                0,
                RuleEntry(
                    id=rule_id,
                    name=f"Payee: {clean_pattern[:30]}",
                    pattern=clean_pattern,
                    match_type="contains",
                    taxonomy_id="spending_categories",
                    category_id=cat_id,
                    activity_type="WITHDRAWAL",
                    priority=1,
                ),
            )
            # Group mass replace: clear single transaction overrides matching this payee pattern
            matching = [
                trn.guid for trn in self.server.book.transactions
                if clean_pattern.lower() in trn.description.lower()
            ]
            for g in matching:
                cfg.transaction_overrides.pop(g, None)
            if trn_guid and trn_guid in cfg.transaction_overrides:
                del cfg.transaction_overrides[trn_guid]

        else:  # scope == "transaction"
            if not trn_guid:
                self._send_error("gnc_trn_guid is required for transaction scope", status=400)
                return
            cfg.transaction_overrides[trn_guid] = {
                "category_id": cat_id,
                "category_name": subcat_name,
                "parent_id": parent_id,
                "budget_group": budget_group,
            }

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({"status": "ok", "message": "Override saved successfully", "scope": scope})

    def _handle_post_bulk_assign(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        trn_guids = payload.get("trn_guids", [])
        if not trn_guids:
            self._send_error("trn_guids list is required", status=400)
            return

        scope = payload.get("scope", "account")
        parent_id = payload.get("parent_id")
        subcat_name = (payload.get("subcat_name") or "").strip()
        budget_group = payload.get("budget_group")
        custom_pattern = (payload.get("custom_pattern") or "").strip()

        cfg = load_mapping_config(mapping_path)
        book = self.server.book
        by_trn = {t.guid: t for t in book.transactions}

        slug = _slugify(subcat_name)[:36].strip("_")
        short_hash = stable_id("gnc_cat", [subcat_name])[:10]
        cat_id = f"cat_gnc_{slug}_{short_hash}" if parent_id else (parent_id or "cat_other_expense")

        migrated_guids = {g for g, a in cfg.accounts.items() if a.wf_account_id is not None}
        updated_count = 0

        if scope == "account":
            cp_guids = set()
            for tg in trn_guids:
                trn = by_trn.get(tg)
                if not trn:
                    continue
                ext_splits = [s for s in trn.splits if s.account_guid not in migrated_guids and s.value != Decimal("0.00")]
                pos_ext = [s for s in ext_splits if s.value > Decimal("0.00")] or ext_splits
                for s in pos_ext:
                    cp_guids.add(s.account_guid)

            for c_guid in cp_guids:
                if c_guid in cfg.categories:
                    update_category_mapping(
                        cfg,
                        gnc_guid=c_guid,
                        parent_id=parent_id,
                        subcat_name=subcat_name,
                        budget_group=budget_group,
                    )
                else:
                    c_acc = book.accounts.get(c_guid)
                    path = c_acc.full_path if c_acc else "Expenses:Custom"
                    cfg.categories[c_guid] = CategoryMapEntry(
                        gnc_guid=c_guid,
                        gnc_path=path,
                        taxonomy_id="spending_categories",
                        category_id=cat_id,
                        category_key=f"gnc_{slug}_{short_hash}",
                        category_name=subcat_name or path.split(":")[-1],
                        parent_id=parent_id,
                        budget_group_id=budget_group.lower() if budget_group else None,
                        is_seeded=False,
                        is_custom=True,
                        color=ROOT_COLORS.get(parent_id or cat_id, "#808080"),
                    )
                # Clean up individual transaction overrides for this counterpart
                matching = [
                    trn.guid for trn in book.transactions
                    if any(s.account_guid == c_guid for s in trn.splits)
                ]
                for g in matching:
                    cfg.transaction_overrides.pop(g, None)
                updated_count += 1

        elif scope == "rule":
            patterns = set()
            if custom_pattern:
                patterns.add(re.sub(r"\s+", " ", custom_pattern).strip())
            else:
                for tg in trn_guids:
                    trn = by_trn.get(tg)
                    if trn and trn.description:
                        clean = re.sub(r"\s+", " ", trn.description).strip()
                        if clean:
                            patterns.add(clean)

            dummy_guid = deterministic_uuid("gnc_cat_rule", cat_id).replace("-", "")
            if dummy_guid not in cfg.categories:
                cfg.categories[dummy_guid] = CategoryMapEntry(
                    gnc_guid=dummy_guid,
                    gnc_path=f"Custom:Rule:{subcat_name}",
                    taxonomy_id="spending_categories",
                    category_id=cat_id,
                    category_key=f"gnc_{slug}_{short_hash}",
                    category_name=subcat_name,
                    parent_id=parent_id,
                    budget_group_id=budget_group.lower() if budget_group else None,
                    is_seeded=False,
                    is_custom=True,
                    color=ROOT_COLORS.get(parent_id or cat_id, "#808080"),
                )

            for pat in patterns:
                rule_id = deterministic_uuid("gnucash_rule_user_payee", pat.lower())
                cfg.rules = [
                    r for r in cfg.rules
                    if r.pattern.lower() != pat.lower()
                    and re.sub(r"\s+", " ", r.pattern).strip().lower() != pat.lower()
                ]
                cfg.rules.insert(
                    0,
                    RuleEntry(
                        id=rule_id,
                        name=f"Payee: {pat[:30]}",
                        pattern=pat,
                        match_type="contains",
                        taxonomy_id="spending_categories",
                        category_id=cat_id,
                        activity_type="WITHDRAWAL",
                        priority=1,
                    ),
                )
                matching = [
                    trn.guid for trn in book.transactions
                    if pat.lower() in trn.description.lower()
                ]
                for g in matching:
                    cfg.transaction_overrides.pop(g, None)
                updated_count += 1

        else:  # scope == "transaction"
            for tg in trn_guids:
                cfg.transaction_overrides[tg] = {
                    "category_id": cat_id,
                    "category_name": subcat_name,
                    "parent_id": parent_id,
                    "budget_group": budget_group,
                }
                updated_count += 1

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({
            "status": "ok",
            "scope": scope,
            "updated_count": updated_count,
            "message": f"Successfully mass-applied {scope} mapping across {len(trn_guids)} transactions ({updated_count} {scope} targets updated)."
        })

    def _handle_post_budget_groups(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        category_id = payload.get("category_id")
        budget_group = payload.get("budget_group")
        if not category_id or not budget_group:
            self._send_error("category_id and budget_group are required", status=400)
            return

        cfg = load_mapping_config(mapping_path)
        update_budget_group_mapping(
            cfg,
            category_id=category_id,
            budget_group=budget_group,
            mapping_path=mapping_path,
            backup_dir=self.server.backup_dir,
        )
        self.server._transactions_cache = None
        self._send_json(
            {"status": "ok", "category_id": category_id, "budget_group": budget_group.upper()}
        )

    def _handle_post_custom_parent(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        name = payload.get("name", "").strip()
        budget_group = payload.get("budget_group", "OTHER").strip().upper()
        color = payload.get("color", "#6B7280").strip()
        taxonomy_id = payload.get("taxonomy_id", "spending_categories").strip()

        if not name:
            self._send_error("Name is required", status=400)
            return

        slug = _slugify(name)[:36].strip("_")
        cat_id = f"cat_{slug}"
        cat_key = slug

        cfg = load_mapping_config(mapping_path)
        existing_ids = {p["id"] for p in DEFAULT_PARENTS} | {c.category_id for c in cfg.custom_root_categories}
        existing_names = {p["name"].strip().lower() for p in DEFAULT_PARENTS} | {c.category_name.strip().lower() for c in cfg.custom_root_categories}
        if cat_id in existing_ids or name.strip().lower() in existing_names:
            self._send_error(f"Parent category '{name}' already exists", status=400)
            return

        new_root = CategoryMapEntry(
            gnc_guid=f"root_{slug}",
            gnc_path=f"Custom:{name}",
            taxonomy_id=taxonomy_id,
            category_id=cat_id,
            category_key=cat_key,
            category_name=name,
            parent_id=None,
            budget_group_id=budget_group.lower(),
            color=color,
            is_seeded=False,
            is_custom=True,
        )
        cfg.custom_root_categories.append(new_root)
        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({"status": "ok", "category": asdict(new_root)})

    def _handle_get_rules(self):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_json({"status": "ok", "rules": []})
            return
        cfg = load_mapping_config(mapping_path)
        rules = [asdict(r) for r in getattr(cfg, "rules", [])]
        self._send_json({"status": "ok", "rules": rules})

    def _handle_post_rules(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        name = payload.get("name", "").strip()
        pattern = payload.get("pattern", "").strip()
        match_type = payload.get("match_type", "contains").strip()
        taxonomy_id = payload.get("taxonomy_id", "spending_categories").strip()
        category_id = payload.get("category_id", "").strip()
        activity_type = payload.get("activity_type")
        priority = int(payload.get("priority", 100))

        if not pattern or not category_id:
            self._send_error("pattern and category_id are required", status=400)
            return

        cfg = load_mapping_config(mapping_path)
        rule_id = payload.get("id") or deterministic_uuid("gnucash_rule_user", f"{pattern}:{category_id}")
        new_rule = RuleEntry(
            id=rule_id,
            name=name or f"Rule: {pattern}",
            pattern=pattern,
            match_type=match_type,
            taxonomy_id=taxonomy_id,
            category_id=category_id,
            activity_type=activity_type,
            priority=priority,
        )

        existing_idx = next((i for i, r in enumerate(cfg.rules) if r.id == rule_id), None)
        if existing_idx is not None:
            cfg.rules[existing_idx] = new_rule
        else:
            cfg.rules.append(new_rule)

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({"status": "ok", "rule": asdict(new_rule)})

    def _handle_delete_rule(self, rule_id: str):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        cfg = load_mapping_config(mapping_path)
        initial_len = len(cfg.rules)
        cfg.rules = [r for r in cfg.rules if r.id != rule_id]
        if len(cfg.rules) < initial_len:
            save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
            self.server._transactions_cache = None
            self._send_json({"status": "ok", "deleted": rule_id})
        else:
            self._send_error(f"Rule '{rule_id}' not found", status=404)

    def _handle_post_alternative_assets(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        action = payload.get("action", "save")
        spec = payload.get("spec", {})
        gnc_path = spec.get("gnc_path", "").strip()

        if not gnc_path:
            self._send_error("spec.gnc_path is required", status=400)
            return

        cfg = load_mapping_config(mapping_path)
        if not hasattr(cfg, "alternative_assets") or cfg.alternative_assets is None:
            cfg.alternative_assets = []

        if action == "delete":
            cfg.alternative_assets = [s for s in cfg.alternative_assets if s.get("gnc_path") != gnc_path]
        else:
            # Upsert
            existing_idx = next((i for i, s in enumerate(cfg.alternative_assets) if s.get("gnc_path") == gnc_path), None)
            if existing_idx is not None:
                cfg.alternative_assets[existing_idx] = spec
            else:
                cfg.alternative_assets.append(spec)

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({"status": "ok", "action": action, "gnc_path": gnc_path})

    def _handle_post_mapping_subtree(self, payload: dict):
        mapping_path = self.server.mapping_path
        if not mapping_path.exists():
            self._send_error("mapping.json not found", status=500)
            return

        path_prefix = payload.get("path_prefix", "").strip()
        parent_id = payload.get("parent_id")
        budget_group = payload.get("budget_group")

        if not path_prefix or not parent_id:
            self._send_error("path_prefix and parent_id are required", status=400)
            return

        cfg = load_mapping_config(mapping_path)
        matched_guids = [
            guid for guid, entry in cfg.categories.items()
            if entry.gnc_path == path_prefix or entry.gnc_path.startswith(path_prefix + ":")
        ]

        if not matched_guids:
            # Fallback: check all accounts in book matching path_prefix
            matched_guids = [
                guid for guid, acct in self.server.book.accounts.items()
                if acct.full_path == path_prefix or acct.full_path.startswith(path_prefix + ":")
            ]

        count = 0
        for guid in matched_guids:
            acct = self.server.book.accounts.get(guid)
            leaf_name = acct.name if acct else (cfg.categories.get(guid).category_name if guid in cfg.categories else "General")
            update_category_mapping(
                cfg,
                gnc_guid=guid,
                parent_id=parent_id,
                subcat_name=leaf_name,
                budget_group=budget_group,
            )
            count += 1

        save_mapping_config(cfg, mapping_path, backup_dir=self.server.backup_dir)
        self.server._transactions_cache = None
        self._send_json({"status": "ok", "updated_count": count, "path_prefix": path_prefix})

    def _handle_post_backups_prune(self, payload: dict):
        keep_latest = payload.get("keep_latest", 3)
        deleted = prune_backups(self.server.backup_dir, keep_latest=keep_latest)
        self._send_json({"status": "ok", "deleted": deleted})

    def _handle_post_migrate(self, payload: dict):
        try:
            cfg = load_mapping_config(self.server.mapping_path)
            stats = run_migration(
                self.server.book,
                cfg,
                self.server.source_db,
                self.server.target_db,
                self.server.backup_dir,
            )
            report = verify_migration(
                self.server.book,
                cfg,
                self.server.source_db,
                self.server.target_db,
            )
            max_diff = max(abs(r.diff) for r in report.reconciliations) if report.reconciliations else 0.0

            should_install = payload.get("install", False)
            if should_install and max_diff == 0:
                from gnucash_to_wealthfolio.cli import _do_install
                _do_install(
                    self.server.target_db,
                    self.server.source_db,
                    self.server.backup_dir,
                    launch=payload.get("launch", False),
                )

            self._send_json(
                {
                    "status": "ok",
                    "max_diff": float(max_diff),
                    "pre_existing_accounts_intact": report.pre_existing_accounts_intact,
                    "fk_violations": report.fk_violations,
                    "integrity_ok": report.integrity_ok,
                    "uncategorized_withdrawals": report.uncategorized_withdrawals,
                    "activities_created": stats.activities_created,
                    "accounts_created": stats.accounts_created,
                    "categories_created": stats.categories_created,
                }
            )
        except Exception as e:
            traceback.print_exc()
            self._send_error(f"Migration failed: {e}", status=500)

    def _get_target_path(self, target: str) -> Path | None:
        if target == "gnucash":
            return getattr(self.server, "gnucash_path", None)
        elif target == "wealthfolio_db":
            return getattr(self.server, "source_db", None) or getattr(self.server, "target_db", None)
        elif target == "mapping":
            return getattr(self.server, "mapping_path", None)
        return None

    def _handle_post_paths_pick(self, payload: dict):
        target = payload.get("target", "gnucash")
        title = "Select File"
        filetypes = [("All Files", "*.*")]

        curr_path = self._get_target_path(target)
        initialdir = str(curr_path.parent) if curr_path and curr_path.parent.exists() else None
        initialfile = curr_path.name if curr_path else None

        if target == "gnucash":
            title = "Select GnuCash Data File"
            filetypes = [("GnuCash Files", "*.gnucash;*.xac"), ("All Files", "*.*")]
        elif target == "wealthfolio_db":
            title = "Select Wealthfolio SQLite Database (app.db)"
            filetypes = [("SQLite Database", "*.db;*.sqlite;*.sqlite3"), ("All Files", "*.*")]
        elif target == "mapping":
            title = "Select Category & Account Mapping JSON"
            filetypes = [("JSON Files", "*.json"), ("All Files", "*.*")]

        chosen_path = None
        try:
            picker_code = (
                "import json, sys\n"
                "try:\n"
                "    import tkinter as tk\n"
                "    from tkinter import filedialog\n"
                "    root = tk.Tk()\n"
                "    root.withdraw()\n"
                "    root.attributes('-topmost', True)\n"
                "    args = json.loads(sys.argv[1])\n"
                "    res = filedialog.askopenfilename(**args)\n"
                "    root.destroy()\n"
                "    print(res or '')\n"
                "except Exception:\n"
                "    print('')\n"
            )
            opts = {
                "title": title,
                "filetypes": filetypes,
            }
            if initialdir:
                opts["initialdir"] = initialdir
            if initialfile:
                opts["initialfile"] = initialfile

            sub = subprocess.run(
                [sys.executable, "-c", picker_code, json.dumps(opts)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            res = sub.stdout.strip()
            if res:
                chosen_path = str(Path(res).resolve())
        except Exception as e:
            traceback.print_exc()
            self._send_error(f"Native file picker error: {e}", status=500)
            return

        if chosen_path:
            self._send_json({"status": "ok", "path": chosen_path, "target": target})
        else:
            self._send_json({"status": "cancelled", "target": target})

    def _handle_post_paths_open_location(self, payload: dict):
        target = payload.get("target", "gnucash")
        target_path = self._get_target_path(target)

        if not target_path or not target_path.exists():
            # If the specific file doesn't exist yet, try its parent or fallback to cwd
            fallback_dir = target_path.parent if target_path and target_path.parent.exists() else Path.cwd()
            try:
                if sys.platform == "win32":
                    subprocess.Popen(["explorer.exe", str(fallback_dir)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(fallback_dir)])
                else:
                    subprocess.Popen(["xdg-open", str(fallback_dir)])
                self._send_json({"status": "ok", "opened": str(fallback_dir)})
            except Exception as e:
                self._send_error(f"Failed to open directory: {e}", status=500)
            return

        resolved = str(target_path.resolve())
        try:
            if sys.platform == "win32":
                # explorer /select,<path> highlights the exact file in Windows Explorer
                subprocess.Popen(["explorer.exe", f"/select,{resolved}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", resolved])
            else:
                subprocess.Popen(["xdg-open", str(target_path.parent)])
            self._send_json({"status": "ok", "opened": resolved})
        except Exception as e:
            self._send_error(f"Failed to open file location: {e}", status=500)

    def _handle_post_paths_update(self, payload: dict):
        gnucash_val = payload.get("gnucash")
        mapping_val = payload.get("mapping")
        wealthfolio_db_val = payload.get("wealthfolio_db")

        updated = {}
        if gnucash_val:
            g_path = Path(gnucash_val).resolve()
            if not g_path.exists():
                self._send_error(f"GnuCash file does not exist: {g_path}", status=400)
                return
            try:
                from gnucash_to_wealthfolio.parser import parse_gnucash_file
                new_book = parse_gnucash_file(g_path)
                self.server.book = new_book
                self.server.gnucash_path = g_path
                self.server._transactions_cache = None
                updated["gnucash"] = str(g_path)
            except Exception as e:
                self._send_error(f"Failed to parse GnuCash file: {e}", status=400)
                return

        if mapping_val:
            m_path = Path(mapping_val).resolve()
            self.server.mapping_path = m_path
            self.server._transactions_cache = None
            updated["mapping"] = str(m_path)

        if wealthfolio_db_val:
            w_path = Path(wealthfolio_db_val).resolve()
            self.server.source_db = w_path
            updated["wealthfolio_db"] = str(w_path)

        self._send_json({"status": "ok", "updated": updated})

    def _handle_post_launch(self):
        from gnucash_to_wealthfolio.cli import WEALTHFOLIO_EXE, launch_wealthfolio
        if WEALTHFOLIO_EXE.exists():
            success = launch_wealthfolio(WEALTHFOLIO_EXE)
            if success:
                self._send_json({"status": "ok", "message": f"Launched {WEALTHFOLIO_EXE.name}"})
            else:
                self._send_error(f"Failed to launch {WEALTHFOLIO_EXE.name}", status=500)
        else:
            self._send_error(f"Wealthfolio not found at {WEALTHFOLIO_EXE}", status=404)


class EditorServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        book: GncBook,
        mapping_path: Path,
        source_db: Path,
        target_db: Path,
        backup_dir: Path,
        gnucash_path: Path | None = None,
    ):
        super().__init__(server_address, EditorRequestHandler)
        self.book = book
        self.mapping_path = mapping_path
        self.source_db = source_db
        self.target_db = target_db
        self.backup_dir = backup_dir
        self.gnucash_path = gnucash_path
        self._transactions_cache = None


def create_editor_server(
    book: GncBook,
    mapping_path: Path,
    source_db: Path,
    target_db: Path,
    backup_dir: Path,
    port: int = 8765,
    gnucash_path: Path | None = None,
) -> EditorServer:
    server_address = ("127.0.0.1", port)
    return EditorServer(
        server_address,
        book=book,
        mapping_path=mapping_path,
        source_db=source_db,
        target_db=target_db,
        backup_dir=backup_dir,
        gnucash_path=gnucash_path,
    )


def start_editor_server(
    book: GncBook,
    mapping_path: Path,
    source_db: Path,
    target_db: Path,
    backup_dir: Path,
    port: int = 8765,
    open_browser: bool = True,
    gnucash_path: Path | None = None,
):
    """Start local editor server, bind to available port, and open in default browser."""
    actual_port = port
    server = None
    for attempt in range(10):
        try:
            server = create_editor_server(
                book=book,
                mapping_path=mapping_path,
                source_db=source_db,
                target_db=target_db,
                backup_dir=backup_dir,
                port=actual_port,
                gnucash_path=gnucash_path,
            )
            break
        except OSError:
            actual_port += 1

    if server is None:
        raise RuntimeError(f"Could not bind to any port in range {port} - {actual_port}")

    url = f"http://127.0.0.1:{actual_port}"
    print(f"\n==================================================================")
    print(f"  Wealthfolio Mapping Editor running at: {url}")
    print(f"  Press Ctrl+C in terminal to stop.")
    print(f"==================================================================\n")

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping editor server...")
    finally:
        server.server_close()
