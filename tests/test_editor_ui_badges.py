import pytest
from gnucash_to_wealthfolio.editor_server import _render_transaction_badge, _render_account_role_pill


def test_editor_ui_badges_rendering():
    tx_meta = {"matched_existing_wf_activity_id": "act-999"}
    badge_html = _render_transaction_badge(tx_meta)
    assert "In Wealthfolio" in badge_html
    assert "badge-matched" in badge_html

    badge_none = _render_transaction_badge(None)
    assert badge_none == ""

    badge_empty = _render_transaction_badge({})
    assert badge_empty == ""

    role_pill = _render_account_role_pill("LINKED_INVESTMENT_ACCOUNT", linked_name="Main Portfolio")
    assert "Linked Portfolio" in role_pill
    assert "Main Portfolio" in role_pill
    assert "badge-linked-portfolio" in role_pill

    role_pill_no_name = _render_account_role_pill("LINKED_INVESTMENT_ACCOUNT")
    assert "Linked Portfolio" in role_pill_no_name
    assert "badge-linked-portfolio" in role_pill_no_name

    default_role_pill = _render_account_role_pill("MIGRATE_BANK")
    assert "MIGRATE_BANK" in default_role_pill
