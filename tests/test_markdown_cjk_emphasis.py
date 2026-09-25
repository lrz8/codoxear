"""Behavioral coverage for CJK-aware emphasis rescue in the markdown renderer.

CommonMark delimiter flanking treats CJK corner brackets and fullwidth
punctuation as Unicode punctuation, so marked pairs the wrong ``**`` delimiters
in prose like ``首推是**「欲与天公试比高」**：``. The renderer registers an
inline rescue tokenizer for delimiter runs adjacent to CJK punctuation; these
tests execute the real vendored marked + app_markdown.js in a node VM and pin
the rendered HTML contract (rescued spans and untouched built-in paths alike).
"""

from __future__ import annotations
from test_markdown_edge_cases import render_markdown

import json
import subprocess
import textwrap
from pathlib import Path

from frontend_module_loader import module_path


ROOT = Path(__file__).resolve().parents[1]
APP_MARKDOWN_JS = module_path("app_markdown.js")
MARKED_JS = ROOT / "codoxear" / "static" / "vendor" / "marked.min.js"


def render_line(markdown: str) -> str:
    """Single trailing newline emitted by marked is not part of the contract."""
    return render_markdown(markdown).rstrip("\n")


def render_markdown_twice(markdown: str) -> tuple[str, str]:
    """Render the same input twice in one process (registration idempotency)."""
    app_source = APP_MARKDOWN_JS.read_text(encoding="utf-8")
    marked_source = MARKED_JS.read_text(encoding="utf-8")
    program = textwrap.dedent(
        f"""
        const vm = require("vm");
        const context = {{
          URL,
          location: {{ origin: "http://localhost", href: "http://localhost/" }},
          window: {{ CodoxearUrls: {{ resolveAppUrl: (path) => path }} }},
        }};
        vm.createContext(context);
        vm.runInContext({json.dumps(marked_source)}, context);
        context.window.marked = context.marked;
        vm.runInContext({json.dumps(app_source)}, context);
        const first = context.window.CodoxearMarkdown.mdToHtml({json.dumps(markdown)});
        const second = context.window.CodoxearMarkdown.mdToHtml({json.dumps(markdown)});
        process.stdout.write(JSON.stringify([first, second]));
        """
    )
    proc = subprocess.run(
        ["node", "-e", program],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return tuple(json.loads(proc.stdout))


def test_reported_two_title_line_bold_each_quoted_phrase_only() -> None:
    markdown = "首推是**「欲与天公试比高」**：它把竞争的坐标系。次推**「会当水击三千里」**，如果更想要动感。"

    html = render_markdown(markdown)

    assert "<strong>「欲与天公试比高」</strong>" in html
    assert "<strong>「会当水击三千里」</strong>" in html
    # The CommonMark mis-parse bolds the prose BETWEEN the quoted titles.
    assert "<strong>：它把竞争的坐标系。次推</strong>" not in html
    assert "**" not in html


def test_mirror_case_closer_before_letter_bold_only_the_phrase() -> None:
    html = render_line("**「敢教日月换新天」**很霸气")

    assert html == "<p><strong>「敢教日月换新天」</strong>很霸气</p>"


def test_single_star_cjk_emphasis_rescued() -> None:
    html = render_line("*「斜体」*测试")

    assert html == "<p><em>「斜体」</em>测试</p>"


def test_bold_adjacent_to_cjk_letters_only_keeps_builtin_path() -> None:
    # CJK ideographs are letters, not punctuation: built-in flanking already
    # opens and closes these, and the rescue must stay byte-identical.
    html = render_line("这是**加粗**文本")

    assert html == "<p>这是<strong>加粗</strong>文本</p>"


def test_cjk_bold_surrounded_by_brackets_keeps_builtin_result() -> None:
    html = render_line("「**bold**」")

    assert html == "<p>「<strong>bold</strong>」</p>"


def test_triple_delimiter_cjk_rescues_as_em_strong_like_the_ascii_twin() -> None:
    html = render_line("***「both」***混排")

    assert html == "<p><em><strong>「both」</strong></em>混排</p>"


def test_link_inside_cjk_bold_renders_inside_strong() -> None:
    html = render_line("**「看[文档](https://example.com)」**")

    assert html == '<p><strong>「看<a href="https://example.com">文档</a>」</strong></p>'


def test_star_inside_link_label_is_not_taken_as_the_closer() -> None:
    html = render_line("**「看[a*b](url)」**外")

    assert html == '<p><strong>「看<a href="url">a*b</a>」</strong>外</p>'


def test_consecutive_cjk_bold_spans_stay_separate() -> None:
    html = render_line("**「中文」**English**「更多」**")

    assert html == "<p><strong>「中文」</strong>English<strong>「更多」</strong></p>"


def test_ascii_emphasis_regressions_keep_builtin_output() -> None:
    assert render_line("**bold** and **another**") == "<p><strong>bold</strong> and <strong>another</strong></p>"
    # Intraword `*` is legal CommonMark emphasis; the vendored marked renders
    # it, and the rescue extension must not change that (underscore is the
    # intraword-literal delimiter, covered below).
    assert render_line("2*3*4") == "<p>2<em>3</em>4</p>"
    assert render_line("a_b_c") == "<p>a_b_c</p>"
    assert render_line("foo_bar_baz") == "<p>foo_bar_baz</p>"


def test_strikethrough_behavior_unchanged_including_cjk_brackets() -> None:
    # marked's del rule has no flanking rules, so CJK punctuation never breaks
    # it — the rescue extension intentionally does not cover `~~`.
    assert render_line("~~strike~~ and ~~another~~") == "<p><del>strike</del> and <del>another</del></p>"
    assert render_line("~~「删除」~~测试") == "<p><del>「删除」</del>测试</p>"


def test_code_span_with_cjk_bold_markers_stays_literal() -> None:
    html = render_line("`**「not bold」**`")

    assert html == "<p><code>**「not bold」**</code></p>"


def test_math_placeholders_survive_alongside_cjk_bold() -> None:
    html = render_line("数学 $a*b$ 与 **「粗」**")

    assert "<strong>「粗」</strong>" in html
    assert "md-math-fallback" in html
    assert "**" not in html


def test_rescue_registration_is_idempotent_across_repeated_renders() -> None:
    first, second = render_markdown_twice("首推**「一次」**：再次**「二次」**，完")

    assert first == second
    assert "<strong>「一次」</strong>" in second
    assert "<strong>「二次」</strong>" in second
