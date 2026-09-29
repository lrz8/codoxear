"""Behavioral coverage for GFM-strict strikethrough in the markdown renderer.

Marked v15's del rule accepts a run of one or two tildes per side, so two
unrelated approximation/range tildes in agent output (``100~300`` and
``~50``) pair into a strikethrough span that deletes the prose between them.
GFM defines strikethrough as exactly ``~~``; the renderer overrides the del
tokenizer so single tildes stay literal text. These tests execute the real
vendored marked + app_markdown.js in a node VM and pin the HTML contract.
"""

from __future__ import annotations

from test_markdown_edge_cases import render_markdown
from test_markdown_cjk_emphasis import render_markdown_twice


def render_line(markdown: str) -> str:
    """Single trailing newline emitted by marked is not part of the contract."""
    return render_markdown(markdown).rstrip("\n")


def test_reported_approximation_sentence_keeps_everything_literal_except_bold() -> None:
    markdown = "头部正常转动约 100~300°/s。取 200°/s × 15ms = 3°。FOV 120° 对应 1920 像素宽（约 16px/度），即同一帧的顶部和底部差了 **~50 像素**。"

    html = render_line(markdown)

    assert "<del>" not in html
    assert "100~300°/s" in html
    assert "<strong>~50 像素</strong>" in html


def test_single_tilde_span_renders_as_literal_text() -> None:
    assert render_line("a ~not deleted~ b") == "<p>a ~not deleted~ b</p>"


def test_double_tilde_strikethrough_still_renders() -> None:
    assert render_line("~~strike~~ and ~~another~~") == "<p><del>strike</del> and <del>another</del></p>"
    assert render_line("~~「删除」~~测试") == "<p><del>「删除」</del>测试</p>"


def test_single_and_double_tildes_in_one_line_do_not_interfere() -> None:
    assert render_line("范围 1~10，删除 ~~old~~，约 ~3ms") == "<p>范围 1~10，删除 <del>old</del>，约 ~3ms</p>"


def test_strikethrough_override_registration_is_idempotent() -> None:
    first, second = render_markdown_twice("约 ~5% 且 ~~gone~~")

    assert first == second
    assert "<del>gone</del>" in second
    assert "<del>" not in first.replace("<del>gone</del>", "")
    assert "~5%" in second
