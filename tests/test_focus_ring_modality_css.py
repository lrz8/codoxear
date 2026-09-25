"""Cascade coverage for the input-modality focus ring.

``app.css`` keys keyboard-modality focus rings on
``html[data-input-modality]`` (one writer: ``createInputModalityController``).
These tests parse the real stylesheet, run a small specificity-aware cascade
over the parsed rules, and assert computed outline values per element state —
never source strings.
"""

from __future__ import annotations

import re

from css_tokens import APP_CSS, declarations, parse_stylesheet, selector_text, top_level_rules

MODALITY = "keyboard"


class Element:
    """A minimal element shape the matcher below understands."""

    def __init__(self, tag: str, *, classes: frozenset[str] = frozenset(), attrs: dict[str, str] | None = None,
                 focus: bool = False, focus_visible: bool = False) -> None:
        self.tag = tag
        self.classes = classes
        self.attrs = attrs or {}
        self.focus = focus
        self.focus_visible = focus_visible


COMPOUND_TOKEN = re.compile(
    r"(?P<where>:where\((?P<where_body>[^()]*(?:\([^()]*\)[^()]*)*)\))"
    r"|(?P<tag>[A-Za-z][A-Za-z0-9-]*)"
    r"|(?P<class>\.[A-Za-z0-9_-]+)"
    r"|(?P<attr>\[(?P<attr_name>[A-Za-z-]+)(?:=(?P<attr_value>\"[^\"]*\"))?])"
    r"|(?P<pseudo>:[A-Za-z-]+)"
)


def _split_compounds(selector: str) -> list[str]:
    """Split on descendant combinators outside brackets/parens."""
    compounds: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(selector):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == " " and depth == 0:
            piece = selector[start:index].strip()
            if piece:
                compounds.append(piece)
            start = index + 1
    piece = selector[start:].strip()
    if piece:
        compounds.append(piece)
    return compounds


def _compound_specificity(compound: str) -> tuple[int, int, int]:
    ids = classes = elements = 0
    for match in COMPOUND_TOKEN.finditer(compound):
        if match.group("where") is not None:
            continue  # :where() contributes zero specificity
        if match.group("tag") is not None:
            elements += 1
        elif match.group("class") is not None or match.group("attr") is not None:
            classes += 1
        elif match.group("pseudo") is not None:
            classes += 1
    return ids, classes, elements


def specificity(selector: str) -> tuple[int, int, int]:
    total = [0, 0, 0]
    for compound in _split_compounds(selector):
        for index, count in enumerate(_compound_specificity(compound)):
            total[index] += count
    return tuple(total)  # type: ignore[return-value]


def _compound_matches(compound: str, element: Element) -> bool | None:
    """Whether a compound matches; ``None`` when the matcher cannot decide."""
    for match in COMPOUND_TOKEN.finditer(compound):
        if match.group("where") is not None:
            raise AssertionError(f":where() must be a standalone compound, got: {compound}")
        if match.group("tag") is not None and match.group("tag") != element.tag:
            return False
        if match.group("class") is not None and match.group("class")[1:] not in element.classes:
            return False
        if match.group("attr") is not None:
            name = match.group("attr_name")
            expected = match.group("attr_value")
            if name not in element.attrs:
                return False
            if expected is not None and element.attrs[name] != expected.strip('"'):
                return False
        if match.group("pseudo") is not None:
            pseudo = match.group("pseudo")
            if pseudo == ":focus":
                return element.focus
            if pseudo == ":focus-visible":
                return element.focus_visible
            if pseudo == ":hover":
                return False  # the cascade tests model no hover state
            return None  # unsupported pseudo-class: the matcher cannot decide
    return True


def _selector_matches(selector: str, chain: list[Element]) -> bool | None:
    """Match right-to-left; ``None`` when some part cannot be decided."""
    compounds = _split_compounds(selector)
    last = _compound_matches(compounds[-1], chain[-1])
    if last is not True:
        return last
    ancestor_index = len(chain) - 2
    for compound in reversed(compounds[:-1]):
        if compound.startswith(":where(") and compound.endswith(")"):
            body = compound[len(":where(") : -1]
            inner = _split_compounds(body)
            matched = False
            index = ancestor_index
            while index >= 0:
                if index - len(inner) + 1 < 0:
                    break
                if all(_compound_matches(part, chain[index - offset]) is True for offset, part in enumerate(inner)):
                    matched = True
                    ancestor_index = index - len(inner)
                    break
                index -= 1
            if not matched:
                return False
            continue
        while ancestor_index >= 0 and _compound_matches(compound, chain[ancestor_index]) is not True:
            ancestor_index -= 1
        if ancestor_index < 0:
            return False
        ancestor_index -= 1
    return True


def computed(chain: list[Element], *properties: str) -> dict[str, str | None]:
    """Resolve the winning declaration per property over the real stylesheet.

    A property no matching rule declares maps to ``None`` (the initial value),
    which the tests keep distinct from an explicit ``outline: none`` rule.
    """
    winners: dict[str, tuple[tuple[int, int, int], int, str]] = {}
    rules = top_level_rules(parse_stylesheet(APP_CSS))
    for order, rule in enumerate(rules):
        for part in selector_text(rule).split(","):
            part = part.strip()
            if not part:
                continue
            rule_declarations = declarations(rule)
            if not any(name in rule_declarations for name in properties):
                continue
            matched = _selector_matches(part, chain)
            if matched is None:
                # Undecidable selectors (e.g. :not(...), ::after) must not
                # silently hide a declaration this cascade computes.
                raise AssertionError(f"cascade matcher cannot evaluate selector: {part}")
            if matched is not True:
                continue
            for name, value in rule_declarations.items():
                if name not in properties:
                    continue
                key = specificity(part)
                current = winners.get(name)
                if current is None or key >= current[0]:
                    winners[name] = (key, order, value)
    return {name: winners[name][2] if name in winners else None for name in properties}


def html_with_modality(modality: str | None) -> Element:
    attrs = {} if modality is None else {"data-input-modality": modality}
    return Element("html", attrs=attrs)


# --- The rule itself -------------------------------------------------------

def test_modality_rule_exists_at_top_level_with_base_ring_values() -> None:
    rules = top_level_rules(parse_stylesheet(APP_CSS))
    base = next(rule for rule in rules if selector_text(rule) == "button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible")
    base_declarations = declarations(base)
    expected_selector = ", ".join(
        f':where(html[data-input-modality="{MODALITY}"]) {tag}:focus' for tag in ("button", "input", "textarea", "select")
    )
    modality_rules = [rule for rule in rules if selector_text(rule) == expected_selector]
    assert len(modality_rules) == 1, "one top-level keyboard-modality ring rule must exist, next to the base rule"
    assert declarations(modality_rules[0]) == {
        "outline": base_declarations["outline"],
        "outline-offset": base_declarations["outline-offset"],
    }
    assert base_declarations["outline"] == "2px solid var(--focus-ring)"
    assert base_declarations["outline-offset"] == "1px"


def test_modality_rule_does_not_outrank_the_base_focus_rule() -> None:
    assert specificity(f':where(html[data-input-modality="{MODALITY}"]) button:focus') == specificity("button:focus-visible")


# --- Computed rings per element state --------------------------------------

def test_script_focused_button_rings_only_under_keyboard_modality() -> None:
    # Script focus after a pointer interaction: the :focus-visible heuristic
    # does not match, and no rule may paint a ring (pointer-opened dialog).
    pointer_chain = [html_with_modality("pointer"), Element("button", focus=True, focus_visible=False)]
    assert computed(pointer_chain, "outline", "outline-offset") == {"outline": None, "outline-offset": None}
    absent_chain = [html_with_modality(None), Element("button", focus=True, focus_visible=False)]
    assert computed(absent_chain, "outline") == {"outline": None}

    # Keyboard modality: plain :focus rings even without :focus-visible —
    # the deterministic script-focus ring the fix introduces.
    keyboard_chain = [html_with_modality(MODALITY), Element("button", focus=True, focus_visible=False)]
    assert computed(keyboard_chain, "outline", "outline-offset") == {
        "outline": "2px solid var(--focus-ring)",
        "outline-offset": "1px",
    }

    # Tab focus (heuristic ring) paints the same values it always did.
    tab_chain = [html_with_modality(MODALITY), Element("button", focus=True, focus_visible=True)]
    assert computed(tab_chain, "outline", "outline-offset") == {
        "outline": "2px solid var(--focus-ring)",
        "outline-offset": "1px",
    }


def test_composer_textarea_keeps_its_no_ring_treatment() -> None:
    # The composer band is the input surface: focus there is self-evident and
    # must stay ring-free even under keyboard modality.
    chain = [
        html_with_modality(MODALITY),
        Element("div", classes=frozenset({"composer"})),
        Element("textarea", focus=True, focus_visible=True),
    ]
    assert computed(chain, "outline") == {"outline": "none"}


def test_tabindex_minus_one_targets_keep_their_no_ring_treatment() -> None:
    chain = [html_with_modality(MODALITY), Element("button", attrs={"tabindex": "-1"}, focus=True, focus_visible=True)]
    assert computed(chain, "outline") == {"outline": "none"}


def test_text_input_baseline_and_generic_textarea_parity_is_preserved() -> None:
    # input[type="text"] baseline (outline: none from the shared control rule,
    # equal specificity and later in source order) must keep winning — the
    # modality rule must not change which controls ring, only when.
    text_input = [html_with_modality(MODALITY), Element("input", attrs={"type": "text"}, focus=True, focus_visible=True)]
    assert computed(text_input, "outline") == {"outline": "none"}
    # A generic focused textarea rings under keyboard modality, as it already
    # did through the base :focus-visible rule.
    textarea = [html_with_modality(MODALITY), Element("textarea", focus=True, focus_visible=True)]
    assert computed(textarea, "outline") == {"outline": "2px solid var(--focus-ring)"}


def test_per_surface_ring_variants_keep_winning_over_the_modality_rule() -> None:
    # .filePlainEditTextarea paints its inset ring (outline-offset: -2px).
    chain = [
        html_with_modality(MODALITY),
        Element("textarea", classes=frozenset({"filePlainEditTextarea"}), focus=True, focus_visible=True),
    ]
    assert computed(chain, "outline", "outline-offset") == {
        "outline": "2px solid var(--focus-ring)",
        "outline-offset": "-2px",
    }
