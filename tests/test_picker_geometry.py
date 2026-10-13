"""Execute caret-picker placement decisions in the frontend Node VM."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from frontend_module_loader import module_path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = module_path("app_picker_geometry.js").read_text(encoding="utf-8")


def run_js(body: str) -> None:
    script = f"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const ctx = {{ window: {{}}, console }};
vm.createContext(ctx);
vm.runInContext({json.dumps(SOURCE)}, ctx);
const decide = ctx.window.CodoxearPickerGeometry.decidePickerGeometry;
{body}
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_prefers_above_when_that_side_fits() -> None:
    run_js("""
const result = decide({
  caretTop: 300, caretBottom: 320, tokenLeft: 120,
  menuNaturalHeight: 120, menuWidth: 160,
  wrapLeft: 100, wrapRight: 500, minTop: 60, maxBottom: 700,
  textareaTop: 100, textareaBottom: 600, gap: 6, minHeight: 96,
});
assert.deepEqual(JSON.parse(JSON.stringify(result)), {
  top: 174, left: 120, maxHeight: 120, dir: 'up', visible: true,
});
""")


def test_flips_below_when_above_cannot_fit_and_below_is_larger() -> None:
    run_js("""
const result = decide({
  caretTop: 140, caretBottom: 160, tokenLeft: 120,
  menuNaturalHeight: 200, menuWidth: 160,
  wrapLeft: 100, wrapRight: 500, minTop: 60, maxBottom: 700,
  textareaTop: 100, textareaBottom: 600, gap: 6, minHeight: 96,
});
assert.deepEqual(JSON.parse(JSON.stringify(result)), {
  top: 166, left: 120, maxHeight: 200, dir: 'down', visible: true,
});
""")


def test_shrinks_max_height_when_neither_side_fits_natural_height() -> None:
    run_js("""
const result = decide({
  caretTop: 300, caretBottom: 320, tokenLeft: 120,
  menuNaturalHeight: 400, menuWidth: 160,
  wrapLeft: 100, wrapRight: 500, minTop: 60, maxBottom: 450,
  textareaTop: 100, textareaBottom: 400, gap: 6, minHeight: 96,
});
assert.deepEqual(JSON.parse(JSON.stringify(result)), {
  top: 60, left: 120, maxHeight: 234, dir: 'up', visible: true,
});
""")


def test_hides_when_the_caret_line_is_scrolled_out_of_textarea_view() -> None:
    run_js("""
const result = decide({
  caretTop: 80, caretBottom: 100, tokenLeft: 120,
  menuNaturalHeight: 120, menuWidth: 160,
  wrapLeft: 100, wrapRight: 500, minTop: 60, maxBottom: 700,
  textareaTop: 150, textareaBottom: 500, gap: 6, minHeight: 96,
});
assert.equal(result.visible, false);
""")


def test_clamps_token_anchor_to_both_input_wrap_edges() -> None:
    run_js("""
const common = {
  caretTop: 300, caretBottom: 320, menuNaturalHeight: 120, menuWidth: 120,
  wrapLeft: 100, wrapRight: 400, minTop: 60, maxBottom: 700,
  textareaTop: 100, textareaBottom: 600, gap: 6, minHeight: 96,
};
assert.equal(decide({ ...common, tokenLeft: 40 }).left, 100);
assert.equal(decide({ ...common, tokenLeft: 360 }).left, 280);
""")


def test_clamps_to_topbar_boundary_when_full_mode_space_is_exhausted() -> None:
    run_js("""
const result = decide({
  caretTop: 150, caretBottom: 170, tokenLeft: 120,
  menuNaturalHeight: 200, menuWidth: 160,
  wrapLeft: 100, wrapRight: 500, minTop: 100, maxBottom: 215,
  textareaTop: 100, textareaBottom: 200, gap: 6, minHeight: 96,
});
assert.deepEqual(JSON.parse(JSON.stringify(result)), {
  top: 100, left: 120, maxHeight: 96, dir: 'up', visible: true,
});
""")
