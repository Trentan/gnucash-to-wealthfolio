from dataclasses import dataclass
from datetime import datetime
import fnmatch
import json
from pathlib import Path

from gnucash_to_wealthfolio.models import AccountRole, GncAccount

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class TaxonomyRule:
    patterns: list[str]
    parent_id: str
    budget_group: str
    default_name: str


def load_standard_templates() -> tuple[list[TaxonomyRule], list[TaxonomyRule]]:
    template_file = TEMPLATES_DIR / "standard_categories.json"
    if not template_file.exists():
        return [], []

    data = json.loads(template_file.read_text(encoding="utf-8"))
    spending_rules = [
        TaxonomyRule(
            patterns=r["patterns"],
            parent_id=r["parent_id"],
            budget_group=r["budget_group"],
            default_name=r["default_name"],
        )
        for r in data.get("spending_rules", [])
    ]
    income_rules = [
        TaxonomyRule(
            patterns=r["patterns"],
            parent_id=r["parent_id"],
            budget_group=r["budget_group"],
            default_name=r["default_name"],
        )
        for r in data.get("income_rules", [])
    ]
    return spending_rules, income_rules


def load_rule_mining_templates() -> tuple[tuple[str, ...], set[str]]:
    template_file = TEMPLATES_DIR / "standard_categories.json"
    if not template_file.exists():
        return (), set()

    data = json.loads(template_file.read_text(encoding="utf-8"))
    prefixes = tuple(data.get("rule_mining_store_prefixes", []))
    generic_leafs = set(data.get("rule_mining_generic_leaf_names", []))
    return prefixes, generic_leafs


def _is_archived(acct: GncAccount, latest_tx_date: str | None = None) -> bool:
    path_lower = acct.full_path.lower()
    for kw in ("archived", "closed", "old", "inactive"):
        if kw in path_lower:
            return True

    if latest_tx_date:
        try:
            tx_dt = datetime.strptime(latest_tx_date[:10], "%Y-%m-%d")
            # 24 months = 730 days from reference anchor
            # If tx is older than 2024-10-15 (for 2026), it's dormant
            ref_dt = datetime(2026, 10, 15)
            if (ref_dt - tx_dt).days > 730:
                return True
        except ValueError:
            pass

    return False


def classify_gnc_account_tier1(
    acct: GncAccount, latest_tx_date: str | None = None
) -> tuple[str, str, str, str]:
    """Classify a GnuCash account based on its structural GnuCash account type.
    
    Returns:
        (role, wf_name, wf_account_type, wf_group)
    """
    archived = _is_archived(acct, latest_tx_date)
    acct_type = (acct.acct_type or "").upper()
    name = acct.name or acct.full_path.split(":")[-1] or "Account"

    # GnuCash system placeholder / balancing accounts should be ignored
    path_lower = acct.full_path.lower()
    if any(k in path_lower for k in ("imbalance", "orphan", "template root")):
        return AccountRole.IGNORE, name, "", ""

    if acct_type == "BANK":
        role = AccountRole.MIGRATE_CASH_ARCHIVED if archived else AccountRole.MIGRATE_CASH_ACTIVE
        group = "Archived" if archived else "Banking"
        return role, name, "CASH", group

    if acct_type == "CASH":
        role = AccountRole.MIGRATE_CASH_ARCHIVED if archived else AccountRole.MIGRATE_CASH_ACTIVE
        group = "Archived" if archived else "Cash"
        return role, name, "CASH", group

    if acct_type == "CREDIT":
        role = AccountRole.MIGRATE_CARD_ARCHIVED if archived else AccountRole.MIGRATE_CARD_ACTIVE
        group = "Archived" if archived else "Credit Cards"
        return role, name, "CREDIT_CARD", group

    if acct_type == "EXPENSE" or acct.full_path.startswith("Expenses"):
        return AccountRole.EXPENSE, name, "", ""

    if acct_type == "INCOME" or acct.full_path.startswith("Income"):
        return AccountRole.INCOME, name, "", ""

    if acct_type == "EQUITY" or acct.full_path.startswith("Equity"):
        return AccountRole.EQUITY, name, "", ""

    if acct_type in ("STOCK", "MUTUAL"):
        return AccountRole.EXTERNAL_SAVINGS_INVESTMENT, name, "", ""

    if acct_type == "LIABILITY":
        return AccountRole.EXTERNAL_LOAN_PROPERTY, name, "", ""

    if acct_type == "ASSET":
        return AccountRole.EXTERNAL_LOAN_PROPERTY, name, "", ""

    return AccountRole.IGNORE, name, "", ""


def match_taxonomy_tier2(
    path: str, rules: list[TaxonomyRule]
) -> tuple[str, str, str] | None:
    """Evaluate declarative taxonomy rules against an account path.
    
    Returns:
        (parent_id, budget_group, subcat_name) or None if no pattern matches.
    """
    path_lower = path.lower()
    parts = [p.strip() for p in path.split(":") if p.strip()]
    leaf = parts[-1] if parts else "General"

    for rule in rules:
        for pat in rule.patterns:
            if fnmatch.fnmatch(path_lower, pat.lower()):
                # Determine subcategory label: preserve intermediate hierarchy if deeply nested
                if len(parts) > 3:
                    subcat_name = " › ".join(parts[2:])
                elif len(parts) > 1:
                    subcat_name = " › ".join(parts[1:])
                else:
                    subcat_name = leaf if leaf.lower() not in pat.lower() else rule.default_name
                return rule.parent_id, rule.budget_group, subcat_name

    return None
