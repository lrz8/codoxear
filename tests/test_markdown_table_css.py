"""Markdown table borders must round with the family radius token.

``.md table`` carries ``border-radius: var(--radius-card)`` so clay and slate
render rounded tables while paper stays square. In the collapsed-border model
that radius is ignored for the table's own border: the border paints square
while ``overflow: hidden`` still clips the header fill to the rounded border
box, so the header looks rounded inside a square boundary. The separate model
(``border-collapse: separate; border-spacing: 0``) applies the radius to the
border and the background clipping alike, making the two agree.

The model switch must not double any internal rule: cells draw only bottom
borders, and the last body row drops its bottom border so it cannot stack
against the table's own bottom border.

The stylesheet is parsed and cascaded per selector; nothing here inspects raw
source text.
"""

from __future__ import annotations

from css_tokens import APP_CSS, base_tokens, declarations, parse_stylesheet, resolve, selector_text, top_level_rules


def cascaded(selector: str) -> dict[str, str]:
    """Declarations from every top-level rule whose selector list names ``selector``."""
    result: dict[str, str] = {}
    for rule in top_level_rules(parse_stylesheet(APP_CSS)):
        if selector in [part.strip() for part in selector_text(rule).split(",")]:
            result.update(declarations(rule))
    assert result, f"no rule for {selector}"
    return result


def test_table_uses_the_radius_honoring_border_model() -> None:
    style = cascaded(".md table")
    assert style["border-collapse"] == "separate"
    assert style["border-spacing"] == "0"


def test_table_keeps_family_radius_and_clipping() -> None:
    tokens = base_tokens()
    style = cascaded(".md table")
    # The radius must keep resolving through --radius-card so every family
    # retunes table corners as one unit; overflow:hidden clips the header
    # fill to the same curve the border draws.
    assert resolve(style["border-radius"], tokens) == resolve(tokens["--radius-card"], tokens)
    assert style["overflow"] == "hidden"


def test_row_seams_stay_single_under_zero_spacing() -> None:
    tokens = base_tokens()
    cells = cascaded(".md td")
    assert resolve(cells["border-bottom"], tokens) == f"1px solid {resolve(tokens['--hairline'], tokens)}"
    # With separate borders the last row's bottom border would sit flush
    # against the table's own bottom border and paint a 2px seam.
    last_row = cascaded(".md tbody tr:last-child td")
    assert last_row["border-bottom"] == "0"
