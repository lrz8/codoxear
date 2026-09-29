"""Computed CSS contract for the sidebar session-restore row.

The row is the dashed ghost control above the session list
(``app_session_snapshot.js``). The cascade must express its appearance through
tokens only (so every theme family retunes it without a mirror rule), keep the
lead host out of the scrolling list while preserving the list's horizontal
inset, and reach the 44px touch target through a ``::after`` hit lane rather
than a taller visual. These checks parse the stylesheet and resolve tokens
instead of matching source text.
"""

from __future__ import annotations

import re

from css_tokens import APP_CSS, base_tokens, declarations, parse_stylesheet, resolve, selector_text, top_level_rules


COLOR_LITERAL_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def cascaded(selector: str) -> dict[str, str]:
    style: dict[str, str] = {}
    for rule in top_level_rules(parse_stylesheet(APP_CSS)):
        selectors = [part.strip() for part in selector_text(rule).split(",")]
        if selector in selectors:
            style.update(declarations(rule))
    assert style, f"no rule for {selector}"
    return style


def test_restore_row_is_a_token_only_dashed_ghost_control() -> None:
    tokens = base_tokens()
    row = cascaded(".sessionRestore")
    assert row["display"] == "flex"
    assert row["align-items"] == "center"
    assert row["width"] == "100%"
    assert row["text-align"] == "left"
    assert row["cursor"] == "pointer"
    assert row["background"] == "transparent"
    assert row["border"] == "1px dashed var(--border)"
    assert row["border-radius"] == "var(--radius-card)"
    assert row["color"] == "var(--muted)"
    assert row["font-size"] == "var(--font-md)"
    assert row["gap"] == "var(--space-3)"
    assert row["margin-bottom"] == "var(--space-3)"
    assert row["padding"] == "var(--space-3) var(--space-4)"
    for value in row.values():
        assert not COLOR_LITERAL_RE.search(value), f"color literal leaked into the row: {value}"
    # Paper light (the base sheet) resolves the geometry to square corners and
    # the approved 8px/10px inset; families retune the same tokens.
    assert resolve(row["border-radius"], tokens) == "0"
    assert resolve(row["padding"], tokens) == "8px 10px"
    assert resolve(row["gap"], tokens) == "8px"
    assert resolve(row["font-size"], tokens) == "13px"
    assert resolve(row["color"], tokens) == tokens["--muted"]


def test_restore_row_hover_and_disabled_states_stay_on_tokens() -> None:
    hover = cascaded(".sessionRestore:hover")
    assert hover["background"] == "var(--surface-subtle)"
    assert hover["color"] == "var(--ink)"
    disabled = cascaded(".sessionRestore:disabled")
    assert disabled["background"] == "transparent"
    assert disabled["color"] == "var(--muted)"
    assert disabled["cursor"] == "default"
    # The disabled treatment also wins over hover while a restore is in flight.
    assert cascaded(".sessionRestore:disabled:hover") == disabled


def test_restore_row_reaches_the_touch_target_through_a_hit_lane() -> None:
    row = cascaded(".sessionRestore")
    assert row["position"] == "relative"
    lane = cascaded(".sessionRestore::after")
    assert lane["content"] == '""'
    assert lane["position"] == "absolute"
    assert lane["inset"] == "-6px 0"
    icon = cascaded(".sessionRestore .icon")
    assert icon["flex"] == "0 0 auto"


def test_lead_host_carries_the_list_inset_and_hides_cleanly() -> None:
    tokens = base_tokens()
    lead = cascaded(".sessionRestoreLead")
    assert lead["flex"] == "0 0 auto"
    assert lead["padding"] == "var(--space-3) var(--space-3) 0"
    sessions = cascaded(".sidebar .sessions")
    # Same horizontal inset as the list it sits above.
    assert resolve(lead["padding"], tokens).split()[1] == resolve(sessions["padding"], tokens).split()[1]
    assert cascaded(".sessionRestoreLead[hidden]")["display"] == "none"
    # With the lead visible the list drops its own top padding, so the row's
    # margin is the only gap to the first group header (as in the mock).
    assert cascaded(".sessionRestoreLead:not([hidden]) + .sessions")["padding-top"] == "0"
