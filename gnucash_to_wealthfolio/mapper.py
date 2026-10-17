from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil

from gnucash_to_wealthfolio.heuristics import (
    classify_gnc_account_tier1,
    load_rule_mining_templates,
    load_standard_templates,
    match_taxonomy_tier2,
)
from gnucash_to_wealthfolio.ids import deterministic_uuid, stable_id
from gnucash_to_wealthfolio.models import (
    AccountRole,
    AlternativeAssetSpec,
    GncAccount,
    GncBook,
    MappingMetadata,
)


@dataclass
class AccountMapEntry:
    gnc_guid: str
    gnc_path: str
    role: str
    wf_account_id: str | None = None
    wf_name: str | None = None
    wf_account_type: str | None = None
    wf_group: str | None = None
    is_active: int = 0
    is_archived: int = 0
    tx_count: int = 0
    is_custom: bool = False


@dataclass
class CategoryMapEntry:
    gnc_guid: str
    gnc_path: str
    taxonomy_id: str
    category_id: str
    category_key: str
    category_name: str
    parent_id: str | None
    budget_group_id: str | None = None
    is_seeded: bool = False
    color: str = "#808080"
    icon: str | None = None
    is_custom: bool = False


@dataclass
class RuleEntry:
    id: str
    name: str
    pattern: str
    match_type: str
    taxonomy_id: str
    category_id: str
    activity_type: str | None
    priority: int


@dataclass
class MappingConfig:
    version: int = 2
    metadata: MappingMetadata = field(default_factory=MappingMetadata)
    accounts: dict[str, AccountMapEntry] = field(default_factory=dict)
    categories: dict[str, CategoryMapEntry] = field(default_factory=dict)
    custom_root_categories: list[CategoryMapEntry] = field(default_factory=list)
    rules: list[RuleEntry] = field(default_factory=list)
    alternative_assets: list[dict] = field(default_factory=list)
    transaction_overrides: dict[str, dict] = field(default_factory=dict)
    budget_group_overrides: dict[str, str] = field(default_factory=dict)


ROOT_COLORS: dict[str, str] = {
    "cat_bills": "#3B82F6",
    "cat_education": "#8B5CF6",
    "cat_entertainment": "#EC4899",
    "cat_fees": "#6B7280",
    "cat_food": "#F97316",
    "cat_gifts": "#F43F5E",
    "cat_groceries": "#22C55E",
    "cat_health": "#14B8A6",
    "cat_housing": "#EAB308",
    "cat_other_expense": "#6B7280",
    "cat_personal": "#D946EF",
    "cat_shopping": "#A855F7",
    "cat_transport": "#0EA5E9",
    "cat_travel": "#06B6D4",
    "cat_family_pets": "#EC4899",
    "cat_investment_property": "#6366F1",
    "cat_income_employment": "#22C55E",
    "cat_income_investment": "#10B981",
    "cat_income_other": "#84CC16",
    "cat_income_selfemploy": "#14B8A6",
    "cat_income_government": "#3B82F6",
    "cat_savings": "#6B8E54",
}


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "category"


def _make_custom_subcat(
    acct: GncAccount,
    taxonomy_id: str,
    parent_id: str,
    subcat_name: str,
) -> CategoryMapEntry:
    short_hash = stable_id("gnc_cat", [acct.full_path])[:10]
    slug = _slugify(subcat_name)[:36].strip("_")
    cat_id = f"cat_gnc_{slug}_{short_hash}"
    cat_key = f"gnc_{slug}_{short_hash}"
    return CategoryMapEntry(
        gnc_guid=acct.guid,
        gnc_path=acct.full_path,
        taxonomy_id=taxonomy_id,
        category_id=cat_id,
        category_key=cat_key,
        category_name=subcat_name,
        parent_id=parent_id,
        budget_group_id=None,
        is_seeded=False,
        color=ROOT_COLORS.get(parent_id, "#808080"),
        icon=None,
    )


def _seeded_entry(
    acct: GncAccount,
    taxonomy_id: str,
    category_id: str,
    category_key: str,
    category_name: str,
    parent_id: str | None,
) -> CategoryMapEntry:
    return CategoryMapEntry(
        gnc_guid=acct.guid,
        gnc_path=acct.full_path,
        taxonomy_id=taxonomy_id,
        category_id=category_id,
        category_key=category_key,
        category_name=category_name,
        parent_id=parent_id,
        budget_group_id=None,
        is_seeded=True,
        color=ROOT_COLORS.get(parent_id or category_id, "#808080"),
        icon=None,
    )


def _map_expense_account(acct: GncAccount) -> CategoryMapEntry:
    parts = [p.strip() for p in acct.full_path.split(":") if p.strip()]
    if len(parts) <= 1:
        return _seeded_entry(
            acct, "spending_categories", "cat_other_expense", "other_expense", "Other Expenses", None
        )

    top = parts[1]
    rest = parts[2:]

    if top == "Children, Family & Pets":
        if not rest:
            return CategoryMapEntry(
                gnc_guid=acct.guid,
                gnc_path=acct.full_path,
                taxonomy_id="spending_categories",
                category_id="cat_family_pets",
                category_key="family_pets",
                category_name="Children, Family & Pets",
                parent_id=None,
                budget_group_id="needs",
                is_seeded=False,
                color=ROOT_COLORS["cat_family_pets"],
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_family_pets", " › ".join(rest))

    if top == "Dining & Entertainment":
        if not rest:
            return _seeded_entry(acct, "spending_categories", "cat_food", "food", "Food & Dining", None)
        sub1 = rest[0]
        if sub1 == "Cafe (Coffee & Tea)" and len(rest) == 1:
            return _seeded_entry(
                acct, "spending_categories", "cat_food_coffee", "food_coffee", "Coffee Shops", "cat_food"
            )
        if sub1 == "Drinks & Alcohol" and len(rest) == 1:
            return _seeded_entry(
                acct, "spending_categories", "cat_food_alcohol", "food_alcohol", "Bars & Alcohol", "cat_food"
            )
        if sub1 == "Restaurant" and len(rest) == 1:
            return _seeded_entry(
                acct, "spending_categories", "cat_food_restaurants", "food_restaurants", "Restaurants", "cat_food"
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_entertainment", " › ".join(rest))

    if top == "Food & Groceries":
        if not rest:
            return _seeded_entry(acct, "spending_categories", "cat_groceries", "groceries", "Groceries", None)
        sub1 = rest[0]
        if sub1 == "Take away (Fast Food)":
            if len(rest) == 1:
                return _make_custom_subcat(acct, "spending_categories", "cat_food", "Take away (Fast Food)")
            return _make_custom_subcat(
                acct, "spending_categories", "cat_food", "Take away › " + " › ".join(rest[1:])
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_groceries", " › ".join(rest))

    if top == "Financial & Insurance":
        if not rest:
            return _seeded_entry(acct, "spending_categories", "cat_fees", "fees", "Fees & Charges", None)
        sub1 = rest[0]
        if sub1 == "Investment (Property)":
            prop_parts = rest[1:]
            if not prop_parts:
                return CategoryMapEntry(
                    gnc_guid=acct.guid,
                    gnc_path=acct.full_path,
                    taxonomy_id="spending_categories",
                    category_id="cat_investment_property",
                    category_key="investment_property",
                    category_name="Investments",
                    parent_id=None,
                    budget_group_id="investment",
                    is_seeded=False,
                    color=ROOT_COLORS["cat_investment_property"],
                )
            if prop_parts[0] == "Archived":
                if len(prop_parts) == 1:
                    sub_name = "Archived Properties"
                else:
                    prop_name = f"{prop_parts[1]} (Archived)"
                    tail = prop_parts[2:]
                    sub_name = " › ".join([prop_name, *tail]) if tail else prop_name
            else:
                sub_name = " › ".join(prop_parts)
            return _make_custom_subcat(acct, "spending_categories", "cat_investment_property", sub_name)

        if sub1 == "Investment (Shares)":
            return _make_custom_subcat(acct, "spending_categories", "cat_investment_property", " › ".join(rest))

        if sub1 == "Insurances":
            ins_parts = rest[1:]
            if not ins_parts:
                return _make_custom_subcat(acct, "spending_categories", "cat_fees", "Insurances")
            if ins_parts == ["Insurance Car"]:
                return _seeded_entry(
                    acct,
                    "spending_categories",
                    "cat_transport_insurance",
                    "transport_insurance",
                    "Auto Insurance",
                    "cat_transport",
                )
            if ins_parts[0] == "Insurance Car" and len(ins_parts) > 1:
                return _make_custom_subcat(
                    acct,
                    "spending_categories",
                    "cat_transport",
                    "Auto Insurance › " + " › ".join(ins_parts[1:]),
                )
            if ins_parts == ["Insurance Health"]:
                return _seeded_entry(
                    acct,
                    "spending_categories",
                    "cat_health_insurance",
                    "health_insurance",
                    "Health Insurance",
                    "cat_health",
                )
            if ins_parts == ["Insurance Home & Contents"]:
                return _seeded_entry(
                    acct,
                    "spending_categories",
                    "cat_housing_insurance",
                    "housing_insurance",
                    "Home Insurance",
                    "cat_housing",
                )
            return _make_custom_subcat(acct, "spending_categories", "cat_fees", " › ".join(rest))

        if sub1 == "Bank Fees & Charges":
            if rest == ["Bank Fees & Charges", "ATM Fees"]:
                return _seeded_entry(
                    acct, "spending_categories", "cat_fees_atm", "fees_atm", "ATM Fees", "cat_fees"
                )
            if rest == ["Bank Fees & Charges", "Account Fees"]:
                return _seeded_entry(
                    acct, "spending_categories", "cat_fees_bank", "fees_bank", "Bank Fees", "cat_fees"
                )
            if rest == ["Bank Fees & Charges", "Credit Card Interest"]:
                return _seeded_entry(
                    acct,
                    "spending_categories",
                    "cat_fees_interest",
                    "fees_interest",
                    "Interest Charges",
                    "cat_fees",
                )
        return _make_custom_subcat(acct, "spending_categories", "cat_fees", " › ".join(rest))

    if top == "Gifts & Donations":
        if not rest:
            return _seeded_entry(acct, "spending_categories", "cat_gifts", "gifts", "Gifts & Donations", None)
        return _make_custom_subcat(acct, "spending_categories", "cat_gifts", " › ".join(rest))

    if top == "Home & Utilities":
        if not rest:
            return _seeded_entry(acct, "spending_categories", "cat_bills", "bills", "Bills & Utilities", None)
        sub1 = rest[0]
        if sub1 in ("Council Rates", "Pool"):
            return _make_custom_subcat(acct, "spending_categories", "cat_housing", " › ".join(rest))
        if rest == ["Internet"]:
            return _seeded_entry(
                acct, "spending_categories", "cat_bills_internet", "bills_internet", "Internet", "cat_bills"
            )
        if rest == ["Mobile"]:
            return _seeded_entry(
                acct, "spending_categories", "cat_bills_phone", "bills_phone", "Phone", "cat_bills"
            )
        if rest == ["Media Subscriptions"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_bills_subscriptions",
                "bills_subscriptions",
                "Subscriptions",
                "cat_bills",
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_bills", " › ".join(rest))

    if top == "Personal & Health Care":
        if not rest:
            return _seeded_entry(
                acct, "spending_categories", "cat_health", "health", "Health & Wellness", None
            )
        if rest == ["Hair & Skincare"]:
            return _make_custom_subcat(acct, "spending_categories", "cat_personal", "Hair & Skincare")
        if rest == ["Chemist"]:
            return _seeded_entry(
                acct, "spending_categories", "cat_health_pharmacy", "health_pharmacy", "Pharmacy", "cat_health"
            )
        if rest == ["Dentist"]:
            return _seeded_entry(
                acct, "spending_categories", "cat_health_dental", "health_dental", "Dental", "cat_health"
            )
        if rest == ["Doctors & Specialists"]:
            return _seeded_entry(
                acct, "spending_categories", "cat_health_medical", "health_medical", "Medical", "cat_health"
            )
        if rest == ["Optical"]:
            return _seeded_entry(
                acct, "spending_categories", "cat_health_vision", "health_vision", "Vision", "cat_health"
            )
        if rest == ["Fitness & Wellness", "Gym"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_health_fitness",
                "health_fitness",
                "Gym & Fitness",
                "cat_health",
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_health", " › ".join(rest))

    if top == "Shopping & Lifestyle":
        if not rest:
            return _seeded_entry(acct, "spending_categories", "cat_shopping", "shopping", "Shopping", None)
        sub1 = rest[0]
        if sub1 == "Hobbies":
            if len(rest) == 1:
                return _seeded_entry(
                    acct,
                    "spending_categories",
                    "cat_entertainment_hobbies",
                    "entertainment_hobbies",
                    "Hobbies",
                    "cat_entertainment",
                )
            return _make_custom_subcat(acct, "spending_categories", "cat_entertainment", " › ".join(rest))
        if sub1 in ("Beauty & Pampering", "Cosmetic & Aesthetic Treatments"):
            return _make_custom_subcat(acct, "spending_categories", "cat_personal", " › ".join(rest))
        if rest == ["Accessories & Clothing"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_shopping_clothing",
                "shopping_clothing",
                "Clothing",
                "cat_shopping",
            )
        if rest == ["Electronics & Gadgets"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_shopping_electronics",
                "shopping_electronics",
                "Electronics",
                "cat_shopping",
            )
        if rest == ["Household Goods"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_shopping_home",
                "shopping_home",
                "Home Goods",
                "cat_shopping",
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_shopping", " › ".join(rest))

    if top == "Transport & Auto":
        if not rest:
            return _seeded_entry(
                acct, "spending_categories", "cat_transport", "transport", "Transportation", None
            )
        if rest == ["Fuel"]:
            return _seeded_entry(
                acct, "spending_categories", "cat_transport_gas", "transport_gas", "Gas & Fuel", "cat_transport"
            )
        if rest == ["Parking & Tolls"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_transport_parking",
                "transport_parking",
                "Parking",
                "cat_transport",
            )
        if rest == ["Personal Transport (Rental Car, Taxi, Uber)"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_transport_rideshare",
                "transport_rideshare",
                "Rideshare & Taxi",
                "cat_transport",
            )
        if rest == ["Public Transport (Bus, Ferry, Train)"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_transport_public",
                "transport_public",
                "Public Transit",
                "cat_transport",
            )
        if rest == ["Repairs & Maintenance"]:
            return _seeded_entry(
                acct,
                "spending_categories",
                "cat_transport_maintenance",
                "transport_maintenance",
                "Car Maintenance",
                "cat_transport",
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_transport", " › ".join(rest))

    if top == "Travel & Holidays":
        if not rest:
            return _seeded_entry(acct, "spending_categories", "cat_travel", "travel", "Travel", None)
        return _make_custom_subcat(acct, "spending_categories", "cat_travel", " › ".join(rest))

    if top == "Work & Education":
        if not rest:
            return _seeded_entry(
                acct, "spending_categories", "cat_education", "education", "Education", None
            )
        return _make_custom_subcat(acct, "spending_categories", "cat_education", " › ".join(rest))

    return _make_custom_subcat(acct, "spending_categories", "cat_other_expense", " › ".join(parts[1:]))


def _map_income_account(acct: GncAccount) -> CategoryMapEntry:
    parts = [p.strip() for p in acct.full_path.split(":") if p.strip()]
    if len(parts) <= 1:
        return _seeded_entry(
            acct, "income_sources", "cat_income_other", "income_other", "Other Income", None
        )

    top = parts[1]
    rest = parts[2:]

    if top == "Employment Income":
        if not rest:
            return _seeded_entry(
                acct, "income_sources", "cat_income_employment", "income_employment", "Employment", None
            )
        return _make_custom_subcat(acct, "income_sources", "cat_income_employment", " › ".join(rest))

    if top == "Government & Support Payments":
        if not rest:
            return CategoryMapEntry(
                gnc_guid=acct.guid,
                gnc_path=acct.full_path,
                taxonomy_id="income_sources",
                category_id="cat_income_government",
                category_key="income_government",
                category_name="Government & Support",
                parent_id=None,
                budget_group_id=None,
                is_seeded=False,
                color=ROOT_COLORS["cat_income_government"],
            )
        return _make_custom_subcat(acct, "income_sources", "cat_income_government", " › ".join(rest))

    if top == "Investments & Passive Income":
        if not rest:
            return _seeded_entry(
                acct,
                "income_sources",
                "cat_income_investment",
                "income_investment",
                "Investment Income",
                None,
            )
        if rest == ["Interest"]:
            return _seeded_entry(
                acct,
                "income_sources",
                "cat_income_interest",
                "income_interest",
                "Interest",
                "cat_income_investment",
            )
        if rest == ["Property"]:
            return _seeded_entry(
                acct,
                "income_sources",
                "cat_income_rental",
                "income_rental",
                "Rental Income",
                "cat_income_investment",
            )
        if rest == ["Shares", "Dividends"]:
            return _seeded_entry(
                acct,
                "income_sources",
                "cat_income_dividends",
                "income_dividends",
                "Dividends",
                "cat_income_investment",
            )
        return _make_custom_subcat(acct, "income_sources", "cat_income_investment", " › ".join(rest))

    if top == "Gifts, Windfalls & Other":
        if not rest:
            return _seeded_entry(
                acct, "income_sources", "cat_income_other", "income_other", "Other Income", None
            )
        return _make_custom_subcat(acct, "income_sources", "cat_income_other", " › ".join(rest))

    return _make_custom_subcat(acct, "income_sources", "cat_income_other", " › ".join(parts[1:]))


def _map_external_loan_property_category(acct: GncAccount) -> CategoryMapEntry:
    parts = [p.strip() for p in acct.full_path.split(":") if p.strip()]
    label = " › ".join(parts[1:]) if len(parts) > 1 else acct.name
    path_lower = acct.full_path.lower()
    if any(k in path_lower for k in ("investment", "rental", "property", "mortgage", "loan")):
        return _make_custom_subcat(
            acct, "spending_categories", "cat_investment_property", f"Principal & Capital › {label}"
        )
    if any(k in path_lower for k in ("vehicle", "car", "auto")):
        return _make_custom_subcat(
            acct, "spending_categories", "cat_transport", f"Loan & Asset › {label}"
        )
    return _make_custom_subcat(acct, "spending_categories", "cat_housing", f"Mortgage & Property › {label}")


def _map_external_savings_investment_category(acct: GncAccount) -> CategoryMapEntry:
    parts = [p.strip() for p in acct.full_path.split(":") if p.strip()]
    label = " › ".join(parts[1:]) if len(parts) > 1 else acct.name
    path_lower = acct.full_path.lower()
    if "trust" in path_lower:
        return _make_custom_subcat(
            acct, "spending_categories", "cat_investment_property", f"Trust Contributions › {label}"
        )
    if "superannuation" in path_lower or "super" in path_lower:
        return _make_custom_subcat(
            acct, "spending_categories", "cat_investment_property", f"Superannuation › {label}"
        )
    return _make_custom_subcat(
        acct, "spending_categories", "cat_investment_property", f"Investment Contributions › {label}"
    )


def _map_equity_category(acct: GncAccount) -> CategoryMapEntry:
    path = acct.full_path
    path_lower = path.lower()
    guid = acct.guid
    if any(k in path_lower for k in ("kids", "children")):
        subcat = path.split(":")[-1]
        cat_id = stable_id("cat", [guid, "family_pets", subcat])
        return CategoryMapEntry(
            gnc_guid=guid,
            gnc_path=path,
            taxonomy_id="spending_categories",
            category_id=cat_id,
            category_key=_slugify(subcat)[:40],
            category_name=f"Kids Bank Accounts › {subcat}",
            parent_id="cat_family_pets",
            budget_group_id="needs",
            color=ROOT_COLORS.get("cat_family_pets", "#EC4899"),
        )
    parts = [p.strip() for p in path.split(":") if p.strip()]
    rest = parts[1:] if len(parts) > 1 else parts
    subcat = " › ".join(rest) if rest else "Equity"
    cat_id = stable_id("cat", [guid, "other_expense", subcat])
    return CategoryMapEntry(
        gnc_guid=guid,
        gnc_path=path,
        taxonomy_id="spending_categories",
        category_id=cat_id,
        category_key=_slugify(subcat)[:40],
        category_name=subcat,
        parent_id="cat_other_expense",
        budget_group_id="other",
        color=ROOT_COLORS.get("cat_other_expense", "#6B7280"),
    )


def build_mapping_config(
    book: GncBook, existing_config: MappingConfig | None = None
) -> MappingConfig:
    tx_counts: Counter[str] = Counter()
    for trn in book.transactions:
        for sp in trn.splits:
            if sp.value != Decimal("0.00"):
                tx_counts[sp.account_guid] += 1

    custom_root_categories = [
        CategoryMapEntry(
            gnc_guid="root_family_pets",
            gnc_path="Expenses:Children, Family & Pets",
            taxonomy_id="spending_categories",
            category_id="cat_family_pets",
            category_key="family_pets",
            category_name="Children, Family & Pets",
            parent_id=None,
            budget_group_id="needs",
            is_seeded=False,
            color=ROOT_COLORS["cat_family_pets"],
            icon="Baby",
        ),
        CategoryMapEntry(
            gnc_guid="root_investment_property",
            gnc_path="Expenses:Financial & Insurance:Investment (Property)",
            taxonomy_id="spending_categories",
            category_id="cat_investment_property",
            category_key="investment_property",
            category_name="Investments",
            parent_id=None,
            budget_group_id="investment",
            is_seeded=False,
            color=ROOT_COLORS["cat_investment_property"],
            icon="Building2",
        ),
        CategoryMapEntry(
            gnc_guid="root_income_government",
            gnc_path="Income:Government & Support Payments",
            taxonomy_id="income_sources",
            category_id="cat_income_government",
            category_key="income_government",
            category_name="Government & Support",
            parent_id=None,
            budget_group_id="income",
            is_seeded=False,
            color=ROOT_COLORS["cat_income_government"],
            icon="Landmark",
        ),
    ]

    existing_cats = existing_config.categories if existing_config else {}
    existing_accounts = existing_config.accounts if existing_config else {}

    accounts_map: dict[str, AccountMapEntry] = {}
    categories_map: dict[str, CategoryMapEntry] = {}

    spending_rules, income_rules = load_standard_templates()

    for guid, acct in book.accounts.items():
        path = acct.full_path
        cnt = tx_counts[guid]

        # ---------------------------------------------------------------------
        # Account Role Mapping (Tier 3: User Config -> Tier 1: Generic Heuristics)
        # ---------------------------------------------------------------------
        if guid in existing_accounts and (
            getattr(existing_accounts[guid], "is_custom", False)
            or existing_accounts[guid].wf_account_id is not None
        ):
            # Tier 3: Preserve user overrides and existing resolved mapping
            prev_acc = existing_accounts[guid]
            prev_acc.tx_count = cnt
            accounts_map[guid] = prev_acc
        else:
            # Tier 1 Heuristics fallback
            role, wf_name, wf_type, wf_group = classify_gnc_account_tier1(acct)
            if role in (
                AccountRole.MIGRATE_CASH_ACTIVE,
                AccountRole.MIGRATE_CARD_ACTIVE,
                AccountRole.MIGRATE_CASH_ARCHIVED,
                AccountRole.MIGRATE_CARD_ARCHIVED,
            ):
                is_arch = 1 if "ARCHIVED" in role else 0
                accounts_map[guid] = AccountMapEntry(
                    gnc_guid=guid,
                    gnc_path=path,
                    role=role,
                    wf_account_id=deterministic_uuid("gnucash_account", guid),
                    wf_name=wf_name,
                    wf_account_type=wf_type,
                    wf_group=wf_group,
                    is_active=1,
                    is_archived=is_arch,
                    tx_count=cnt,
                )
            else:
                accounts_map[guid] = AccountMapEntry(
                    gnc_guid=guid, gnc_path=path, role=role, tx_count=cnt
                )

        # ---------------------------------------------------------------------
        # Category Mapping (Tier 3 -> Tier 2 Template Match -> Default Subcat)
        # ---------------------------------------------------------------------
        acct_role = accounts_map[guid].role
        if acct_role == AccountRole.EXPENSE:
            if guid in existing_cats:
                categories_map[guid] = existing_cats[guid]
            else:
                # Tier 2: Check template rules
                t2 = match_taxonomy_tier2(path, spending_rules)
                if t2 is not None:
                    parent_id, budget_grp, subcat_name = t2
                    cat_entry = _make_custom_subcat(acct, "spending_categories", parent_id, subcat_name)
                    cat_entry.budget_group_id = budget_grp
                    categories_map[guid] = cat_entry
                else:
                    categories_map[guid] = _map_expense_account(acct)
        elif acct_role == AccountRole.INCOME:
            if guid in existing_cats:
                categories_map[guid] = existing_cats[guid]
            else:
                t2 = match_taxonomy_tier2(path, income_rules)
                if t2 is not None:
                    parent_id, budget_grp, subcat_name = t2
                    cat_entry = _make_custom_subcat(acct, "income_sources", parent_id, subcat_name)
                    cat_entry.budget_group_id = budget_grp
                    categories_map[guid] = cat_entry
                else:
                    categories_map[guid] = _map_income_account(acct)
        elif acct_role == AccountRole.EQUITY:
            if guid in existing_cats:
                categories_map[guid] = existing_cats[guid]
            else:
                categories_map[guid] = _map_equity_category(acct)
        elif acct_role == AccountRole.EXTERNAL_SAVINGS_INVESTMENT:
            if guid in existing_cats:
                categories_map[guid] = existing_cats[guid]
            else:
                categories_map[guid] = _map_external_savings_investment_category(acct)
        elif acct_role == AccountRole.EXTERNAL_LOAN_PROPERTY:
            if guid in existing_cats:
                categories_map[guid] = existing_cats[guid]
            else:
                categories_map[guid] = _map_external_loan_property_category(acct)

    # Generate rules from business/store subcategories + frequent payees
    rules: list[RuleEntry] = []
    seen_patterns: set[str] = set()

    store_prefixes, generic_leaf_names = load_rule_mining_templates()

    priority = 100
    for guid, cat in sorted(categories_map.items(), key=lambda kv: kv[1].gnc_path):
        if any(cat.gnc_path.startswith(pref) for pref in store_prefixes):
            leaf_name = cat.gnc_path.split(":")[-1].strip()
            if leaf_name in generic_leaf_names or len(leaf_name) < 3:
                continue
            pat_key = leaf_name.lower()
            if pat_key in seen_patterns:
                continue
            seen_patterns.add(pat_key)
            rules.append(
                RuleEntry(
                    id=deterministic_uuid("gnucash_rule_store", cat.gnc_path),
                    name=f"Auto: {leaf_name}",
                    pattern=leaf_name,
                    match_type="contains",
                    taxonomy_id=cat.taxonomy_id,
                    category_id=cat.category_id,
                    activity_type="WITHDRAWAL",
                    priority=priority,
                )
            )
            priority += 1

    # Mine recurring payees from migrated transactions (>= 5 occurrences, >= 85% category consistency)
    migrated_guids = {
        g for g, a in accounts_map.items() if a.wf_account_id is not None
    }
    payee_cat_counts: dict[str, Counter[str]] = defaultdict(Counter)
    payee_display: dict[str, str] = {}

    for trn in book.transactions:
        desc = trn.description.strip()
        if len(desc) < 4:
            continue
        has_mig = any(s.account_guid in migrated_guids and s.value < 0 for s in trn.splits)
        if not has_mig:
            continue
        exp_splits = [
            s for s in trn.splits if s.account_guid in categories_map and s.value > 0
        ]
        if len(exp_splits) == 1:
            norm = desc.lower()
            payee_display[norm] = desc
            payee_cat_counts[norm][exp_splits[0].account_guid] += 1

    for norm_desc, counter in sorted(
        payee_cat_counts.items(), key=lambda kv: sum(kv[1].values()), reverse=True
    ):
        total = sum(counter.values())
        if total < 10:
            continue
        top_guid, top_cnt = counter.most_common(1)[0]
        if top_cnt / total < 0.85:
            continue
        if norm_desc in seen_patterns:
            continue
        if any(p in norm_desc for p in seen_patterns):
            continue
        seen_patterns.add(norm_desc)
        cat = categories_map[top_guid]
        display = payee_display[norm_desc]
        rules.append(
            RuleEntry(
                id=deterministic_uuid("gnucash_rule_payee", norm_desc),
                name=f"Payee: {display[:40]}",
                pattern=display,
                match_type="exact",
                taxonomy_id=cat.taxonomy_id,
                category_id=cat.category_id,
                activity_type="WITHDRAWAL",
                priority=priority,
            )
        )
        priority += 1
        if len(rules) >= 120:
            break

    return MappingConfig(
        version=existing_config.version if existing_config else 2,
        metadata=existing_config.metadata if existing_config else MappingMetadata(),
        accounts=accounts_map,
        categories=categories_map,
        custom_root_categories=custom_root_categories,
        rules=rules,
        alternative_assets=existing_config.alternative_assets if existing_config else [],
        transaction_overrides=existing_config.transaction_overrides if existing_config else {},
        budget_group_overrides=existing_config.budget_group_overrides if existing_config else {},
    )



def upgrade_mapping_config_if_needed(raw_data: dict) -> dict:
    """Upgrade legacy Schema v1 mapping dictionary to Schema v2 non-destructively."""
    data = dict(raw_data)
    if "version" not in data or data.get("version", 1) < 2:
        data["version"] = 2
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data.setdefault(
            "metadata",
            {
                "generated_at": now_iso,
                "updated_at": now_iso,
                "gnucash_book_id": None,
                "source_file": None,
                "currency": "AUD",
            },
        )
        data.setdefault("alternative_assets", [])
        data.setdefault("accounts", {})
        data.setdefault("categories", {})
        data.setdefault("custom_root_categories", [])
        data.setdefault("rules", [])
        data.setdefault("transaction_overrides", {})
        data.setdefault("budget_group_overrides", {})
    return data


def save_mapping_config(
    config: MappingConfig,
    path: Path,
    backup_dir: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Pre-save backup snapshot if existing file exists and backup_dir provided
    if backup_dir is not None and path.exists():
        mapping_backup_dir = backup_dir / "mappings"
        mapping_backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = mapping_backup_dir / f"mapping_{ts}.json"
        shutil.copy2(path, backup_file)
        from gnucash_to_wealthfolio.backups import prune_mapping_backups
        prune_mapping_backups(mapping_backup_dir, keep_latest=20)

    # 2. Serialize to payload
    meta_dict = asdict(config.metadata) if hasattr(config, "metadata") and isinstance(config.metadata, MappingMetadata) else (config.metadata if hasattr(config, "metadata") else asdict(MappingMetadata()))
    if isinstance(meta_dict, dict):
        meta_dict["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not meta_dict.get("generated_at"):
            meta_dict["generated_at"] = meta_dict["updated_at"]

    payload = {
        "version": getattr(config, "version", 2),
        "metadata": meta_dict,
        "accounts": {k: asdict(v) for k, v in config.accounts.items()},
        "categories": {k: asdict(v) for k, v in config.categories.items()},
        "custom_root_categories": [asdict(c) for c in config.custom_root_categories],
        "rules": [asdict(r) for r in config.rules],
        "alternative_assets": getattr(config, "alternative_assets", []),
        "transaction_overrides": getattr(config, "transaction_overrides", {}),
        "budget_group_overrides": getattr(config, "budget_group_overrides", {}),
    }

    # 3. Atomic file write via .tmp -> flush -> fsync -> os.replace
    tmp_path = path.with_suffix(".tmp")
    data_str = json.dumps(payload, indent=2)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(data_str)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)


def load_mapping_config(path: Path) -> MappingConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw = upgrade_mapping_config_if_needed(raw)

    meta_raw = raw.get("metadata", {})
    metadata = MappingMetadata(
        generated_at=meta_raw.get("generated_at", ""),
        updated_at=meta_raw.get("updated_at", ""),
        gnucash_book_id=meta_raw.get("gnucash_book_id"),
        source_file=meta_raw.get("source_file"),
        currency=meta_raw.get("currency", "AUD"),
    )

    return MappingConfig(
        version=raw.get("version", 2),
        metadata=metadata,
        accounts={k: AccountMapEntry(**v) for k, v in raw["accounts"].items()},
        categories={k: CategoryMapEntry(**v) for k, v in raw["categories"].items()},
        custom_root_categories=[CategoryMapEntry(**c) for c in raw["custom_root_categories"]],
        rules=[RuleEntry(**r) for r in raw["rules"]],
        alternative_assets=raw.get("alternative_assets", []),
        transaction_overrides=raw.get("transaction_overrides", {}),
        budget_group_overrides=raw.get("budget_group_overrides", {}),
    )



def update_budget_group_mapping(
    config: MappingConfig,
    category_id: str,
    budget_group: str,
    mapping_path: Path | None = None,
    backup_dir: Path | None = None,
) -> MappingConfig:
    """Assign or override the budget group for a parent or root category."""
    if not hasattr(config, "budget_group_overrides"):
        config.budget_group_overrides = {}
    config.budget_group_overrides[category_id] = budget_group.upper()

    # Also update in custom_root_categories if present
    for rc in config.custom_root_categories:
        if rc.category_id == category_id:
            rc.budget_group_id = budget_group.lower()

    if mapping_path is not None:
        save_mapping_config(config, mapping_path, backup_dir=backup_dir)
    return config


def update_category_mapping(
    config: MappingConfig,
    gnc_guid: str,
    parent_id: str | None,
    subcat_name: str,
    budget_group: str | None = None,
    mapping_path: Path | None = None,
    backup_dir: Path | None = None,
) -> MappingConfig:
    """Assign or reassign a GnuCash account to a Wealthfolio parent category and subcategory."""
    if gnc_guid not in config.categories:
        slug = _slugify(subcat_name)[:36].strip("_")
        short_hash = stable_id("gnc_cat", [gnc_guid])[:10]
        cat_id = f"cat_gnc_{slug}_{short_hash}" if parent_id else (parent_id or "cat_other_expense")
        config.categories[gnc_guid] = CategoryMapEntry(
            gnc_guid=gnc_guid,
            gnc_path=subcat_name,
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

    entry = config.categories[gnc_guid]
    entry.is_custom = True
    entry.parent_id = parent_id
    entry.category_name = subcat_name
    entry.color = ROOT_COLORS.get(parent_id or entry.category_id, "#808080")
    if budget_group:
        entry.budget_group_id = budget_group.lower()

    if parent_id is None:
        entry.category_id = parent_id or entry.category_id
        entry.category_key = entry.category_id.replace("cat_", "")
        entry.is_seeded = False if entry.category_id.startswith("cat_gnc_") else entry.is_seeded
    else:
        slug = _slugify(subcat_name)[:36].strip("_")
        short_hash = stable_id("gnc_cat", [entry.gnc_path])[:10]
        entry.category_id = f"cat_gnc_{slug}_{short_hash}"
        entry.category_key = f"gnc_{slug}_{short_hash}"
        entry.is_seeded = False

    if mapping_path is not None:
        save_mapping_config(config, mapping_path, backup_dir=backup_dir)
    return config
