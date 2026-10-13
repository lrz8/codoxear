"""Behavioral coverage for the CJK autolink boundary in the markdown renderer.

marked's GFM extended autolink ends a URL candidate at the first whitespace or
``<`` and trims only ASCII trailing punctuation, so a bare URL followed directly
by CJK prose punctuation (no space) becomes one anchor carrying the prose —
``…/dexgem-strategy（私有，jackw1997 邀请已发）`` links every character up to the
next space. cmark-gfm/GitHub renders the same HTML, and CJK prose has no
inter-word spaces, so this is the common case for agent output rather than an
edge case. The renderer registers an inline tokenizer that ends the candidate at
the first non-ASCII punctuation or symbol and hands the truncated candidate back
to marked's own ``url`` tokenizer.

These tests execute the real vendored marked + app_markdown.js in a node VM and
pin the rendered HTML contract: the truncated anchors, and every path that must
stay byte-for-byte what marked already produced.
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


def test_reported_sentence_links_only_the_url_and_keeps_the_prose_as_text() -> None:
    markdown = "全部完成。仓库 https://github.com/yiwenlu66/dexgem-strategy（私有，jackw1997 邀请已发），两次提交已推送。"

    html = render_line(markdown)

    assert html == (
        '<p>全部完成。仓库 <a href="https://github.com/yiwenlu66/dexgem-strategy">'
        "https://github.com/yiwenlu66/dexgem-strategy</a>（私有，jackw1997 邀请已发），两次提交已推送。</p>"
    )


def test_fullwidth_prose_delimiters_end_the_autolink() -> None:
    for delimiter in ("。", "，", "、", "；", "：", "…", "——", "《标题》", "「引用」", "（注）", "）"):
        html = render_line(f"见 https://example.com{delimiter}尾")
        assert html == (
            f'<p>见 <a href="https://example.com">https://example.com</a>{delimiter}尾</p>'
        ), delimiter


def test_trailing_fullwidth_punctuation_after_a_path_is_excluded() -> None:
    assert render_line("https://example.com/x）") == (
        '<p><a href="https://example.com/x">https://example.com/x</a>）</p>'
    )
    assert render_line("https://example.com/docs?v=1（注）") == (
        '<p><a href="https://example.com/docs?v=1">https://example.com/docs?v=1</a>（注）</p>'
    )
    assert render_line("www.example.com（注）") == (
        '<p><a href="http://www.example.com">www.example.com</a>（注）</p>'
    )
    assert render_line("https://example.com/搜索（q）") == (
        '<p><a href="https://example.com/%E6%90%9C%E7%B4%A2">https://example.com/搜索</a>（q）</p>'
    )


def test_non_ascii_letters_and_digits_never_end_the_autolink() -> None:
    # CJK paths and query values are URL content: they must stay inside the link.
    assert render_line("https://zh.wikipedia.org/wiki/中文 路径") == (
        '<p><a href="https://zh.wikipedia.org/wiki/%E4%B8%AD%E6%96%87">'
        "https://zh.wikipedia.org/wiki/中文</a> 路径</p>"
    )
    assert render_line("https://www.baidu.com/s?wd=萧山人工智能") == (
        '<p><a href="https://www.baidu.com/s?wd=%E8%90%A7%E5%B1%B1%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD">'
        "https://www.baidu.com/s?wd=萧山人工智能</a></p>"
    )
    # A CJK punctuation character later in the run still ends it.
    assert render_line("https://example.com/搜索（q）").startswith(
        '<p><a href="https://example.com/%E6%90%9C%E7%B4%A2">'
    )


def test_ascii_url_punctuation_stays_governed_by_marked() -> None:
    assert render_line("see https://example.com.") == (
        '<p>see <a href="https://example.com">https://example.com</a>.</p>'
    )
    assert render_line("https://example.com/a(b)") == (
        '<p><a href="https://example.com/a(b)">https://example.com/a(b)</a></p>'
    )
    assert render_line("https://example.com/x%EF%BC%88y%EF%BC%89") == (
        '<p><a href="https://example.com/x%EF%BC%88y%EF%BC%89">https://example.com/x%EF%BC%88y%EF%BC%89</a></p>'
    )
    assert render_line("前 https://a.com/x 后（括号）") == (
        '<p>前 <a href="https://a.com/x">https://a.com/x</a> 后（括号）</p>'
    )


def test_email_autolink_and_explicit_destinations_keep_the_literal_destination() -> None:
    assert render_line("jackw1997@example.com（私有）") == (
        '<p><a href="mailto:jackw1997@example.com">jackw1997@example.com</a>（私有）</p>'
    )
    # An explicit destination is the author's statement, not a prose heuristic.
    assert render_line("[标签](https://example.com/a（b）)") == (
        '<p><a href="https://example.com/a%EF%BC%88b%EF%BC%89">标签</a></p>'
    )
    assert render_line("<https://example.com/a（b）>") == (
        '<p><a href="https://example.com/a%EF%BC%88b%EF%BC%89">https://example.com/a（b）</a></p>'
    )
    assert render_line("`https://example.com/a（b）`") == (
        "<p><code>https://example.com/a（b）</code></p>"
    )


def test_block_contexts_get_the_same_boundary() -> None:
    assert render_line("# 见 https://a.com。（x）") == (
        '<h1>见 <a href="https://a.com">https://a.com</a>。（x）</h1>'
    )
    assert render_line("- 见 https://a.com。（x）") == (
        '<ul>\n<li>见 <a href="https://a.com">https://a.com</a>。（x）</li>\n</ul>'
    )


def test_each_url_on_a_line_gets_its_own_boundary() -> None:
    assert render_line("https://a.com/x、https://b.com/y") == (
        '<p><a href="https://a.com/x">https://a.com/x</a>、<a href="https://b.com/y">https://b.com/y</a></p>'
    )
    assert render_line("见 https://example.com。私有说明：https://example.com/x（注）") == (
        '<p>见 <a href="https://example.com">https://example.com</a>。私有说明：'
        '<a href="https://example.com/x">https://example.com/x</a>（注）</p>'
    )


def test_leading_cjk_boundary_is_unchanged() -> None:
    # A URL directly preceded by a CJK character already links today (marked's
    # inline text rule stops before the scheme); the boundary fix must not lose it.
    assert render_line("仓库https://example.com/x（y）") == (
        '<p>仓库<a href="https://example.com/x">https://example.com/x</a>（y）</p>'
    )


def test_registration_is_idempotent_across_renders() -> None:
    markdown = "全部完成。仓库 https://github.com/yiwenlu66/dexgem-strategy（私有，jackw1997 邀请已发），两次提交已推送。"

    first, second = render_markdown_twice(markdown)

    assert first == second
    assert first.rstrip("\n") == (
        '<p>全部完成。仓库 <a href="https://github.com/yiwenlu66/dexgem-strategy">'
        "https://github.com/yiwenlu66/dexgem-strategy</a>（私有，jackw1997 邀请已发），两次提交已推送。</p>"
    )
