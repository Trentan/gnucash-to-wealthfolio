import gzip
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import BinaryIO

from gnucash_to_wealthfolio.models import GncAccount, GncBook, GncSplit, GncTransaction

NS = {
    "gnc": "http://www.gnucash.org/XML/gnc",
    "act": "http://www.gnucash.org/XML/act",
    "trn": "http://www.gnucash.org/XML/trn",
    "split": "http://www.gnucash.org/XML/split",
    "ts": "http://www.gnucash.org/XML/ts",
    "cmdty": "http://www.gnucash.org/XML/cmdty",
    "slot": "http://www.gnucash.org/XML/slot",
}

TWO_PLACES = Decimal("0.01")


def parse_rational(val_str: str | None) -> Decimal:
    """Parse a GnuCash rational string (e.g. '-12345/100') into a 2-decimal Decimal."""
    if not val_str:
        return Decimal("0.00")
    s = val_str.strip()
    if "/" in s:
        num_str, denom_str = s.split("/", 1)
        denom = Decimal(denom_str)
        if denom == 0:
            return Decimal("0.00")
        return (Decimal(num_str) / denom).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return Decimal(s).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _open_gnucash_stream(path: Path) -> BinaryIO:
    with open(path, "rb") as probe:
        magic_16 = probe.read(16)
    if magic_16.startswith(b"SQLite format 3\x00"):
        raise ValueError(
            f"'{path.name}' is a GnuCash SQLite3 database. This migrator requires GnuCash XML format.\n"
            "Please open GnuCash -> File -> Save As... and select 'XML' as the format."
        )
    if magic_16.startswith(b"\x1f\x8b"):
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return open(path, "rb")


def parse_gnucash_file(path: Path) -> GncBook:
    """Parse a gzipped or plain XML GnuCash file into a GncBook."""
    with _open_gnucash_stream(path) as stream:
        tree = ET.parse(stream)
    root = tree.getroot()
    book_el = root.find("gnc:book", NS)
    if book_el is None:
        book_el = root

    raw_accounts: dict[str, dict[str, object]] = {}
    for acct_el in book_el.findall("gnc:account", NS):
        guid = acct_el.findtext("act:id", default="", namespaces=NS)
        name = acct_el.findtext("act:name", default="", namespaces=NS)
        acct_type = acct_el.findtext("act:type", default="", namespaces=NS)
        parent_guid = acct_el.findtext("act:parent", default=None, namespaces=NS)
        cmdty = acct_el.find("act:commodity", NS)
        commodity = cmdty.findtext("cmdty:id", default="AUD", namespaces=NS) if cmdty is not None else "AUD"
        description = acct_el.findtext("act:description", default="", namespaces=NS) or ""

        placeholder = False
        hidden = False
        slots_el = acct_el.find("act:slots", NS)
        if slots_el is not None:
            for slot_el in slots_el.findall("slot", NS):
                k = slot_el.findtext("slot:key", default="", namespaces=NS)
                v = slot_el.findtext("slot:value", default="", namespaces=NS)
                if k == "placeholder" and v.lower() == "true":
                    placeholder = True
                elif k == "hidden" and v.lower() == "true":
                    hidden = True

        raw_accounts[guid] = {
            "guid": guid,
            "name": name,
            "acct_type": acct_type,
            "parent_guid": parent_guid,
            "commodity": commodity,
            "description": description,
            "placeholder": placeholder,
            "hidden": hidden,
        }

    def build_full_path(guid: str) -> str:
        parts: list[str] = []
        curr: str | None = guid
        seen: set[str] = set()
        while curr and curr in raw_accounts and curr not in seen:
            seen.add(curr)
            info = raw_accounts[curr]
            if info["acct_type"] == "ROOT":
                break
            parts.append(str(info["name"]))
            curr = info["parent_guid"]  # type: ignore[assignment]
        return ":".join(reversed(parts))

    accounts: dict[str, GncAccount] = {}
    for guid, info in raw_accounts.items():
        accounts[guid] = GncAccount(
            guid=guid,
            name=str(info["name"]),
            full_path=build_full_path(guid),
            acct_type=str(info["acct_type"]),
            parent_guid=info["parent_guid"],  # type: ignore[arg-type]
            commodity=str(info["commodity"]),
            description=str(info["description"]),
            placeholder=bool(info["placeholder"]),
            hidden=bool(info["hidden"]),
        )

    transactions: list[GncTransaction] = []
    for trn_el in book_el.findall("gnc:transaction", NS):
        t_guid = trn_el.findtext("trn:id", default="", namespaces=NS)
        cmdty = trn_el.find("trn:currency", NS)
        currency = cmdty.findtext("cmdty:id", default="AUD", namespaces=NS) if cmdty is not None else "AUD"
        dp_raw = trn_el.findtext("trn:date-posted/ts:date", default="", namespaces=NS)
        de_raw = trn_el.findtext("trn:date-entered/ts:date", default="", namespaces=NS)
        date_posted = dp_raw[:10] if dp_raw else "1970-01-01"
        date_entered = de_raw[:10] if de_raw else date_posted
        desc = (trn_el.findtext("trn:description", default="", namespaces=NS) or "").strip()

        splits: list[GncSplit] = []
        splits_el = trn_el.find("trn:splits", NS)
        if splits_el is not None:
            for sp_el in splits_el.findall("trn:split", NS):
                s_guid = sp_el.findtext("split:id", default="", namespaces=NS)
                s_acct = sp_el.findtext("split:account", default="", namespaces=NS)
                s_val = parse_rational(sp_el.findtext("split:value", default="0/100", namespaces=NS))
                s_qty = parse_rational(sp_el.findtext("split:quantity", default="0/100", namespaces=NS))
                s_memo = (sp_el.findtext("split:memo", default="", namespaces=NS) or "").strip()
                s_rec = sp_el.findtext("split:reconciled-state", default="n", namespaces=NS) or "n"
                splits.append(
                    GncSplit(
                        guid=s_guid,
                        account_guid=s_acct,
                        value=s_val,
                        quantity=s_qty,
                        memo=s_memo,
                        reconciled_state=s_rec,
                    )
                )

        transactions.append(
            GncTransaction(
                guid=t_guid,
                currency=currency,
                date_posted=date_posted,
                date_entered=date_entered,
                description=desc,
                splits=splits,
            )
        )

    return GncBook(accounts=accounts, transactions=transactions)
