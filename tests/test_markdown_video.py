"""Behavioral coverage for markdown video rewriting.

The production post-processing runs against a browser ``template`` element, so
these tests drive the exported ``rewriteMarkedVideos`` against a minimal fake
DOM and assert the rewritten element state. ``previewVideoUrlForRef`` (the
production ``resolveVideoSrc`` wiring for chat and file preview) is exercised
as the real resolver, so the local-path → blob-URL routing and the
browser-safe container gate are covered end to end.
"""

from __future__ import annotations

from frontend_module_loader import module_path

import json
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_MARKDOWN_JS = module_path("app_markdown.js")


def run_video_probe(program: str) -> dict[str, object]:
    source = APP_MARKDOWN_JS.read_text(encoding="utf-8")
    js = textwrap.dedent(
        f"""
        const vm = require("vm");
        const source = {json.dumps(source)};
        const ctx = {{
          URL,
          location: {{ origin: "http://localhost", href: "http://localhost/" }},
          window: {{ CodoxearUrls: {{ resolveAppUrl: (path) => path }} }},
        }};
        vm.createContext(ctx);
        vm.runInContext(source, ctx);
        const api = ctx.window.CodoxearMarkdown;

        function makeSource(src) {{
          const attrs = new Map();
          if (src != null) attrs.set("src", src);
          return {{
            tagName: "SOURCE",
            removed: false,
            getAttribute(name) {{ return attrs.has(name) ? attrs.get(name) : null; }},
            setAttribute(name, value) {{ attrs.set(name, String(value)); }},
            removeAttribute(name) {{ attrs.delete(name); }},
            hasAttribute(name) {{ return attrs.has(name); }},
            remove() {{ this.removed = true; }},
          }};
        }}

        function makeVideo({{ src = null, sources = [], poster = null, textContent = "" }} = {{}}) {{
          const attrs = new Map();
          if (src != null) attrs.set("src", src);
          if (poster != null) attrs.set("poster", poster);
          return {{
            tagName: "VIDEO",
            attrs,
            sources,
            replacedBy: null,
            textContent,
            getAttribute(name) {{ return attrs.has(name) ? attrs.get(name) : null; }},
            setAttribute(name, value) {{ attrs.set(name, String(value)); }},
            removeAttribute(name) {{ attrs.delete(name); }},
            hasAttribute(name) {{ return attrs.has(name); }},
            querySelectorAll(selector) {{ return selector === "source" ? this.sources.filter((s) => !s.removed) : []; }},
            replaceWith(node) {{ this.replacedBy = node; }},
          }};
        }}

        const doc = {{ createTextNode: (text) => ({{ nodeType: 3, nodeValue: String(text) }}) }};
        const chatOptions = {{
          resolveVideoSrc: (raw, ref) => api.previewVideoUrlForRef(raw, ref, {{ sessionId: "s" }}),
          resolveImageSrc: (raw, ref) => (ref && ref.path ? `/img?path=${{encodeURIComponent(ref.path)}}` : null),
        }};
        function rewrite(videos, options = chatOptions) {{
          api.rewriteMarkedVideos({{ querySelectorAll: (sel) => (sel === "video" ? videos : []) }}, doc, options);
        }}
        {program}
        """
    )
    proc = subprocess.run(["node", "-e", js], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return json.loads(proc.stdout)


def test_local_mp4_video_routes_through_session_blob_with_player_defaults() -> None:
    result = run_video_probe(
        """
        const video = makeVideo({ src: "/abs/clips/demo.mp4", sources: [makeSource("/abs/clips/demo.mp4")] });
        rewrite([video]);
        process.stdout.write(JSON.stringify({
          src: video.getAttribute("src"),
          controls: video.getAttribute("controls"),
          preload: video.getAttribute("preload"),
          playsinline: video.getAttribute("playsinline"),
          sourcesLeft: video.querySelectorAll("source").length,
          replaced: video.replacedBy !== null,
        }));
        """
    )

    assert result == {
        "src": "/api/sessions/s/file/blob?path=%2Fabs%2Fclips%2Fdemo.mp4",
        "controls": "",
        "preload": "metadata",
        "playsinline": "",
        "sourcesLeft": 0,
        "replaced": False,
    }


def test_browser_unsafe_container_collapses_to_text_for_file_ref_upgrade() -> None:
    result = run_video_probe(
        """
        const video = makeVideo({ src: "/abs/clips/demo.mkv" });
        rewrite([video]);
        process.stdout.write(JSON.stringify({
          replaced: video.replacedBy !== null,
          nodeType: video.replacedBy && video.replacedBy.nodeType,
          text: video.replacedBy && video.replacedBy.nodeValue,
        }));
        """
    )

    assert result == {"replaced": True, "nodeType": 3, "text": "/abs/clips/demo.mkv"}


def test_source_children_keep_first_playable_and_drop_unplayable() -> None:
    result = run_video_probe(
        """
        const mkv = makeSource("/abs/clips/demo.mkv");
        const webm = makeSource("/abs/clips/demo.webm");
        const video = makeVideo({ sources: [mkv, webm] });
        rewrite([video]);
        process.stdout.write(JSON.stringify({
          ownSrc: video.getAttribute("src"),
          mkvRemoved: mkv.removed,
          webmSrc: webm.getAttribute("src"),
          replaced: video.replacedBy !== null,
        }));
        """
    )

    assert result == {
        "ownSrc": None,
        "mkvRemoved": True,
        "webmSrc": "/api/sessions/s/file/blob?path=%2Fabs%2Fclips%2Fdemo.webm",
        "replaced": False,
    }


def test_remote_https_video_passes_through_and_javascript_src_drops_to_text() -> None:
    result = run_video_probe(
        """
        const remote = makeVideo({ src: "https://media.example.test/clip.mp4" });
        const evil = makeVideo({ src: "javascript:alert(1)" });
        rewrite([remote, evil]);
        process.stdout.write(JSON.stringify({
          remoteSrc: remote.getAttribute("src"),
          remoteControls: remote.getAttribute("controls"),
          remoteReplaced: remote.replacedBy !== null,
          evilReplaced: evil.replacedBy !== null,
          evilText: evil.replacedBy && evil.replacedBy.nodeValue,
        }));
        """
    )

    assert result == {
        "remoteSrc": "https://media.example.test/clip.mp4",
        "remoteControls": "",
        "remoteReplaced": False,
        "evilReplaced": True,
        "evilText": "javascript:alert(1)",
    }


def test_poster_routes_through_image_resolver_and_existing_controls_win() -> None:
    result = run_video_probe(
        """
        const video = makeVideo({ src: "/abs/clips/demo.mp4", poster: "/abs/clips/poster.png" });
        video.setAttribute("controls", "");
        video.setAttribute("preload", "none");
        rewrite([video]);
        process.stdout.write(JSON.stringify({
          poster: video.getAttribute("poster"),
          preload: video.getAttribute("preload"),
        }));
        """
    )

    assert result == {"poster": "/img?path=%2Fabs%2Fclips%2Fposter.png", "preload": "none"}


def test_preview_video_url_gates_extensions_and_session_scope() -> None:
    result = run_video_probe(
        """
        process.stdout.write(JSON.stringify({
          mp4Session: api.previewVideoUrlForRef("/a/v.mp4", { path: "/a/v.mp4", line: null }, { sessionId: "s" }),
          m4vSession: api.previewVideoUrlForRef("/a/v.m4v", { path: "/a/v.m4v", line: null }, { sessionId: "s" }),
          ogvSession: api.previewVideoUrlForRef("/a/v.ogv", { path: "/a/v.ogv", line: null }, { sessionId: "s" }),
          mkvSession: api.previewVideoUrlForRef("/a/v.mkv", { path: "/a/v.mkv", line: null }, { sessionId: "s" }),
          movSession: api.previewVideoUrlForRef("/a/v.mov", { path: "/a/v.mov", line: null }, { sessionId: "s" }),
          mp4AbsoluteNoSession: api.previewVideoUrlForRef("/a/v.mp4", { path: "/a/v.mp4", line: null }, {}),
          mp4RelativeNoSession: api.previewVideoUrlForRef("clips/v.mp4", { path: "clips/v.mp4", line: null }, {}),
          remoteOk: api.previewVideoUrlForRef("https://x.test/v.mp4", null, {}),
          remoteBad: api.previewVideoUrlForRef("data:video/mp4;base64,AAAA", null, {}),
        }));
        """
    )

    assert result == {
        "mp4Session": "/api/sessions/s/file/blob?path=%2Fa%2Fv.mp4",
        "m4vSession": "/api/sessions/s/file/blob?path=%2Fa%2Fv.m4v",
        "ogvSession": "/api/sessions/s/file/blob?path=%2Fa%2Fv.ogv",
        "mkvSession": None,
        "movSession": None,
        "mp4AbsoluteNoSession": "/api/files/blob?path=%2Fa%2Fv.mp4",
        "mp4RelativeNoSession": None,
        "remoteOk": "https://x.test/v.mp4",
        "remoteBad": None,
    }
