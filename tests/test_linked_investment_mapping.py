import json
import pytest
from pathlib import Path
from gnucash_to_wealthfolio.models import AccountRole
from gnucash_to_wealthfolio.mapper import (
    AccountMapEntry,
    MappingConfig,
    load_mapping_config,
    save_mapping_config,
)


def test_linked_investment_account_role_and_fields():
    assert hasattr(AccountRole, "LINKED_INVESTMENT_ACCOUNT")
    entry = AccountMapEntry(
        gnc_guid="guid123",
        gnc_path="Assets:Investments:Portfolio",
        role=AccountRole.LINKED_INVESTMENT_ACCOUNT,
        linked_wf_account_id="wf-acc-456",
        linked_wf_name="External Portfolio",
        suppress_asset_creation=True,
    )
    assert entry.role == AccountRole.LINKED_INVESTMENT_ACCOUNT
    assert entry.linked_wf_account_id == "wf-acc-456"
    assert entry.linked_wf_name == "External Portfolio"
    assert entry.suppress_asset_creation is True


def test_linked_investment_account_round_trip(tmp_path: Path):
    entry = AccountMapEntry(
        gnc_guid="guid123",
        gnc_path="Assets:Investments:Portfolio",
        role=AccountRole.LINKED_INVESTMENT_ACCOUNT,
        linked_wf_account_id="wf-acc-456",
        linked_wf_name="External Portfolio",
        suppress_asset_creation=True,
    )
    cfg = MappingConfig(
        accounts={"guid123": entry}
    )
    out_file = tmp_path / "mapping.json"
    save_mapping_config(cfg, out_file)

    loaded = load_mapping_config(out_file)
    assert "guid123" in loaded.accounts
    loaded_entry = loaded.accounts["guid123"]
    assert loaded_entry.role == AccountRole.LINKED_INVESTMENT_ACCOUNT
    assert loaded_entry.linked_wf_account_id == "wf-acc-456"
    assert loaded_entry.linked_wf_name == "External Portfolio"
    assert loaded_entry.suppress_asset_creation is True
