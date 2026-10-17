from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class GncAccount:
    guid: str
    name: str
    full_path: str
    acct_type: str
    parent_guid: str | None
    commodity: str
    description: str = ""
    placeholder: bool = False
    hidden: bool = False


@dataclass(frozen=True)
class GncSplit:
    guid: str
    account_guid: str
    value: Decimal
    quantity: Decimal
    memo: str = ""
    reconciled_state: str = "n"


@dataclass(frozen=True)
class GncTransaction:
    guid: str
    currency: str
    date_posted: str
    date_entered: str
    description: str
    splits: list[GncSplit] = field(default_factory=list)


@dataclass
class GncBook:
    accounts: dict[str, GncAccount]
    transactions: list[GncTransaction]


@dataclass
class MappingMetadata:
    generated_at: str = ""
    updated_at: str = ""
    gnucash_book_id: str | None = None
    source_file: str | None = None
    currency: str = "AUD"


@dataclass
class AlternativeAssetSpec:
    gnc_path: str
    kind: str
    name: str
    display_code: str
    sub_type: str
    purchase_price: str | None = None
    purchase_date: str | None = None
    min_quote_date: str | None = None
    address: str | None = None
    original_amount: str | None = None
    origination_date: str | None = None
    linked_gnc_path: str | None = None


class AccountRole:
    MIGRATE_CASH_ACTIVE = "MIGRATE_CASH_ACTIVE"
    MIGRATE_CARD_ACTIVE = "MIGRATE_CARD_ACTIVE"
    MIGRATE_LOAN_ACTIVE = "MIGRATE_LOAN_ACTIVE"
    MIGRATE_CASH_ARCHIVED = "MIGRATE_CASH_ARCHIVED"
    MIGRATE_CARD_ARCHIVED = "MIGRATE_CARD_ARCHIVED"
    MIGRATE_LOAN_ARCHIVED = "MIGRATE_LOAN_ARCHIVED"
    EXTERNAL_SAVINGS_INVESTMENT = "EXTERNAL_SAVINGS_INVESTMENT"
    EXTERNAL_LOAN_PROPERTY = "EXTERNAL_LOAN_PROPERTY"
    EQUITY = "EQUITY"
    EXPENSE = "EXPENSE"
    INCOME = "INCOME"
    IGNORE = "IGNORE"

