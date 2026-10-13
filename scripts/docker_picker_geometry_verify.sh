#!/usr/bin/env bash
# Docker-isolated behavioral verifier for caret-anchored composer completion.
# Builds exactly the requested Git commit into a throwaway container HOME and
# drives it from the host's agent-browser on a loopback port which is never 8743.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/docker_picker_geometry_verify.sh [commit]

Writes report.json, browser geometry probes, screenshots, and diagnostics to a
new /tmp/codoxear-picker-verify.* directory.

Environment:
  CODOXEAR_PICKER_VERIFY_PORT       Loopback port (default: 19665; never 8743)
  CODOXEAR_PICKER_VERIFY_IMAGE      Image tag (default: codoxear-verify:<commit>)
  CODOXEAR_PICKER_VERIFY_PASSWORD   Login password (default: docker-picker-password)
USAGE
}
case "${1:-}" in -h|--help|help) usage; exit 0;; esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_ref="${1:-HEAD}"
target_commit="$(git -C "$repo_root" rev-parse --verify "${target_ref}^{commit}")" || { echo "unable to resolve commit: $target_ref" >&2; exit 2; }
short_commit="$(git -C "$repo_root" rev-parse --short=12 "$target_commit")"
port="${CODOXEAR_PICKER_VERIFY_PORT:-19665}"
password="${CODOXEAR_PICKER_VERIFY_PASSWORD:-docker-picker-password}"
image="${CODOXEAR_PICKER_VERIFY_IMAGE:-codoxear-verify:${short_commit}}"
container="codoxear-picker-verify-${short_commit}-${port}"
source_pi_agent="$HOME/.pi/agent"
root="$(mktemp -d /tmp/codoxear-picker-verify-build.XXXXXX)"
artifacts="$(mktemp -d /tmp/codoxear-picker-verify.XXXXXX)"
home_dir="$root/home"
browser_session="codoxear-picker-verify-${short_commit}-$$"
readonly root artifacts home_dir browser_session
readonly -a docker=(sudo docker)

if [[ "$port" == "8743" ]]; then echo "refusing live Codoxear port 8743" >&2; exit 2; fi
if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || ((port > 65535)); then echo "invalid loopback port: $port" >&2; exit 2; fi
if [[ -n "${CODEXEAR_APP_DIR:-}${CODEX_WEB_APP_DIR:-}" ]]; then echo "refusing host app-dir override" >&2; exit 2; fi
if [[ ! -r "$source_pi_agent/models.json" || ! -r "$source_pi_agent/auth.json" || ! -r "$source_pi_agent/settings.json" ]]; then
  echo "Pi config is incomplete under $source_pi_agent" >&2; exit 2
fi
command -v agent-browser >/dev/null || { echo "agent-browser is required" >&2; exit 2; }

browser() { AGENT_BROWSER_SESSION="$browser_session" agent-browser "$@"; }
capture_container_diagnostics() {
  "${docker[@]}" logs "$container" > "$artifacts/server.log" 2>&1 || true
  "${docker[@]}" exec "$container" sh -lc '
    printf "HOME=%s\\n" "$HOME"
    python3 -c "from codoxear.util import default_app_dir; print(default_app_dir())"
    find "$HOME/.pi/agent/sessions" -type f -name "*.jsonl" -printf "%p\\n" 2>/dev/null
    find "$HOME/.local/share/codoxear/socks" -maxdepth 1 -type f -name "*.json" -printf "%p\\n" 2>/dev/null
  ' > "$artifacts/isolation-and-session.txt" 2>&1 || true
  "${docker[@]}" exec "$container" sh -lc 'cat "$HOME"/pi-bootstrap-*.log "$HOME"/sessiond-*.log' > "$artifacts/sessiond.log" 2>&1 || true
}
cleanup() {
  browser close >/dev/null 2>&1 || true
  "${docker[@]}" rm -f "$container" >/dev/null 2>&1 || true
  /bin/rm -rf "$root"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; capture_container_diagnostics; exit 1; }
copy_pi_config() {
  umask 077
  mkdir -p "$home_dir/.pi/agent" "$home_dir/.local/share"
  local name
  for name in models.json models-store.json auth.json settings.json trust.json anthropic-context-management.json; do
    [[ -r "$source_pi_agent/$name" ]] && install -m 600 "$source_pi_agent/$name" "$home_dir/.pi/agent/$name"
  done
}
wait_for_server() {
  local code
  for _ in $(seq 1 120); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/api/me" 2>/dev/null || true)"
    [[ "$code" == 401 ]] && return 0
    sleep 1
  done
  return 1
}
wait_for_pi_sessions() {
  local cookie="$root/cookies.txt" sessions="$artifacts/sessions.json"
  curl -fsS -c "$cookie" -H 'Content-Type: application/json' --data "{\"password\":\"${password}\"}" "http://127.0.0.1:${port}/api/login" > "$artifacts/login.json"
  for _ in $(seq 1 120); do
    if [[ "$(curl -sS -b "$cookie" -o "$sessions" -w '%{http_code}' "http://127.0.0.1:${port}/api/sessions" 2>/dev/null || true)" == 200 ]] && \
      python3 - "$sessions" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding='utf-8'))
raise SystemExit(0 if sum(isinstance(s, dict) and s.get('agent_backend') == 'pi' for s in payload.get('sessions', [])) >= 2 else 1)
PY
    then return 0; fi
    sleep 1
  done
  return 1
}

copy_pi_config
build_context="$root/build-context"
mkdir -p "$build_context/docker"
git -C "$repo_root" archive --format=tar "$target_commit" | tar -x -C "$build_context"
install -m 644 "$repo_root/docker/verify.Dockerfile" "$build_context/docker/verify.Dockerfile"
# app.bundle.js is generated release material and deliberately stays untouched
# in the checkout. Rebuild it only in this immutable Git-archive context so the
# browser exercises the requested source commit, exactly as deploy.sh does.
npx --yes esbuild "$build_context/codoxear/static/app.js" --bundle --minify \
  --outfile="$build_context/codoxear/static/dist/app.bundle.js" --format=esm \
  > "$artifacts/esbuild.log" 2>&1 || fail "esbuild bundle rebuild failed; see $artifacts/esbuild.log"
# Rebuild even when the tag already exists: the tag is keyed to the application
# commit while this verifier also supplies the freshly generated bundle above.
"${docker[@]}" build --file "$build_context/docker/verify.Dockerfile" --tag "$image" "$build_context" > "$artifacts/docker-build.log" 2>&1 || fail "Docker build failed"
printf 'built image %s with regenerated browser bundle\n' "$image" > "$artifacts/docker-image.txt"

"${docker[@]}" rm -f "$container" >/dev/null 2>&1 || true
"${docker[@]}" run --detach --name "$container" --publish "127.0.0.1:${port}:${port}" \
  --env HOME=/home/tester --env CODEX_WEB_PASSWORD="$password" --env CODEX_WEB_HOST=0.0.0.0 --env CODEX_WEB_PORT="$port" \
  --env CODEX_WEB_DEFAULT_AGENT_BACKEND=pi --env PYTHONPATH=/workspace --env PYTHONDONTWRITEBYTECODE=1 \
  --mount "type=bind,src=$home_dir,dst=/home/tester" --workdir /workspace "$image" bash -c '
    set -eu
    cd /workspace
    export PYTHONPATH=/workspace
    for sid in docker-picker-a docker-picker-b; do
      pi --offline --no-extensions --no-skills --no-prompt-templates --no-context-files --session-id "$sid" -p "Initialize isolated picker geometry verification session." >"$HOME/pi-bootstrap-$sid.log" 2>&1 || true
      session_log="$(find "$HOME/.pi/agent/sessions" -type f -name "*${sid}.jsonl" -print -quit)"
      test -n "$session_log"
      CODEX_WEB_AGENT_BACKEND=pi CODEX_WEB_OWNER=web PI_BIN=pi PI_OFFLINE=1 codoxear-broker --cwd /workspace -- --offline --no-extensions --no-skills --no-prompt-templates --no-context-files --session "$session_log" >"$HOME/sessiond-$sid.log" 2>&1 &
    done
    for _ in $(seq 1 120); do
      n=0
      for meta in "$HOME"/.local/share/codoxear/socks/*.json; do test -f "$meta" && grep -q session_id "$meta" && n=$((n + 1)); done
      test "$n" -ge 2 && break
      sleep 1
    done
    exec python3 -m codoxear.server
  ' > "$artifacts/container-id.txt" || fail "container start failed"

wait_for_server || fail "server readiness check failed"
wait_for_pi_sessions || fail "two Pi sessions never appeared"

PROBE='(() => {
  const q = (s) => document.querySelector(s);
  const r = (node) => { if (!node) return null; const x = node.getBoundingClientRect(); return {left:x.left,right:x.right,top:x.top,bottom:x.bottom,width:x.width,height:x.height}; };
  const composer = q(".composer"), textarea = q("#msg"), picker = q(".modelPicker"), form = composer && q(".composer form"), wrap = textarea && textarea.parentElement, topbar = q(".topbar");
  const cs = textarea ? getComputedStyle(textarea) : null;
  const lineHeight = cs ? parseFloat(cs.lineHeight) : null;
  const paddingTop = cs ? parseFloat(cs.paddingTop) : null;
  const borderTop = cs ? parseFloat(cs.borderTopWidth) : null;
  const caret = textarea ? textarea.selectionStart : null;
  const before = textarea ? textarea.value.slice(0, caret) : "";
  const line = before.split("\n").length - 1;
  const tr = r(textarea);
  // All multiline probe drafts deliberately have short, non-wrapping lines.
  const expectedCaretTop = tr && Number.isFinite(lineHeight) ? tr.top + borderTop + paddingTop + line * lineHeight - textarea.scrollTop : null;
  const activeId = textarea && textarea.getAttribute("aria-activedescendant");
  const active = activeId && document.getElementById(activeId);
  return {
    viewport:{width:innerWidth,height:innerHeight}, height:composer && composer.dataset.height,
    composer:r(composer), form:r(form), textarea:tr, wrap:r(wrap), topbar:r(topbar), picker:r(picker),
    pickerDisplay:picker && getComputedStyle(picker).display, pickerVisibility:picker && getComputedStyle(picker).visibility,
    pickerDir:picker && picker.dataset.pickerDir || null, inlineTop:picker && picker.style.top, inlineLeft:picker && picker.style.left,
    inlineMaxHeight:picker && picker.style.maxHeight, value:textarea && textarea.value, caret, line, expectedCaretTop,
    scrollTop:textarea && textarea.scrollTop, scrollHeight:textarea && textarea.scrollHeight, clientHeight:textarea && textarea.clientHeight,
    activeId, activeText:active && active.textContent, pickerOptions:picker ? [...picker.querySelectorAll(".modelPickerOption")].map((n) => n.textContent) : []
  };
})()'
probe() { browser eval "$PROBE" --json > "$artifacts/$1.json" 2>&1 || fail "probe $1 failed"; }
shot() { browser screenshot "$artifacts/$1.png" > /dev/null 2>&1 || fail "screenshot $1 failed"; }
settle() { browser wait "${1:-500}" > /dev/null 2>&1; }
# agent-browser eval executes in the page, not a shell; pass the payload through
# a JSON string to avoid clipboard or shell-dependent text entry for long drafts.
set_draft_json() {
  local value="$1" encoded
  encoded="$(printf '%s' "$value" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  browser eval "(() => { const t=document.getElementById('msg'); const v=$encoded; t.focus(); t.value=v; t.setSelectionRange(v.length,v.length); t.dispatchEvent(new Event('input',{bubbles:true,inputType:'insertText'})); return v.length; })()" --json >/dev/null 2>&1 || fail "could not set draft"
}
wait_for_picker() {
  for _ in $(seq 1 30); do
    if browser eval '(() => { const p=document.querySelector(".modelPicker"); return Boolean(p && getComputedStyle(p).display === "block" && p.getBoundingClientRect().height > 0); })()' --json > /dev/null 2>&1; then
      local visible
      visible="$(browser eval '(() => { const p=document.querySelector(".modelPicker"); return Boolean(p && getComputedStyle(p).display === "block" && p.getBoundingClientRect().height > 0); })()' --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["result"])' 2>/dev/null || true)"
      [[ "$visible" == True ]] && return 0
    fi
    sleep 0.15
  done
  return 1
}
# The caret-anchored picker floats over the composer in tall/full modes and can
# cover the grabber strip, which fails Playwright's hit-test. Dismiss an open
# picker with its normal Escape action without clearing the overflowing draft:
# normal mode hides the grabber again when that draft is cleared.
click_expand() {
  local picker_open picker_closed
  picker_open="$(browser eval '(() => { const p=document.querySelector(".modelPicker"); return Boolean(p && getComputedStyle(p).display === "block"); })()' --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["result"])' 2>/dev/null || true)"
  if [[ "$picker_open" == True ]]; then
    browser focus '#msg' >/dev/null 2>&1 || fail "could not focus composer to close picker"
    browser press Escape >/dev/null 2>&1 || fail "could not close picker before expand"
    settle
  fi
  picker_closed="$(browser eval '(() => { const p=document.querySelector(".modelPicker"); return Boolean(p && getComputedStyle(p).display === "none"); })()' --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["result"])' 2>/dev/null || true)"
  [[ "$picker_closed" == True ]] || fail "picker did not close before expand"
  browser click '#expandBtn' >/dev/null 2>&1 || fail "expand click failed"
  settle
}
selected_session_ids() {
  browser eval '(() => [...document.querySelectorAll("#sessions .session[data-session-id]")].map((n) => n.dataset.sessionId))()' --json > "$artifacts/session-ids.json" 2>&1 || fail "could not enumerate sessions"
  python3 - "$artifacts/session-ids.json" <<'PY'
import json, sys
ids=json.load(open(sys.argv[1],encoding='utf-8'))['data']['result']
if len(ids)<2: raise SystemExit('needed two session cards, got %r' % ids)
print(ids[0], ids[1])
PY
}
select_session() { browser click "#sessions .session[data-session-id=\"$1\"]" >/dev/null 2>&1 || fail "session selection failed"; settle 1200; }
open_slash_picker() {
  browser fill '#msg' '' >/dev/null 2>&1 || fail "could not clear composer"
  browser focus '#msg' >/dev/null 2>&1 || fail "could not focus composer"
  browser type '#msg' '/' >/dev/null 2>&1 || fail "could not type slash"
  wait_for_picker || fail "slash picker did not open"
  settle
}
assert_height() {
  local wanted="$1" label="$2"
  probe "$label"
  python3 - "$artifacts/$label.json" "$wanted" <<'PY'
import json,sys
p=json.load(open(sys.argv[1]))['data']['result']
if p['height'] != sys.argv[2]: raise SystemExit('height=%r, wanted=%r' % (p['height'], sys.argv[2]))
PY
}

# This height leaves enough room above the first tall-mode line for the menu,
# so the required prefer-up branch is observable before the full-mode flip-down.
browser set viewport 1280 1000 >/dev/null 2>&1 || true
browser set media light >/dev/null 2>&1 || true
browser open "http://127.0.0.1:${port}/" > "$artifacts/browser-open.txt" 2>&1 || fail "browser open failed"
browser errors --clear --json > "$artifacts/browser-errors-before.json" 2>&1 || fail "could not clear browser errors"
browser console --clear --json > "$artifacts/browser-console-before.json" 2>&1 || fail "could not clear browser console"
browser fill '#pw' "$password" >/dev/null 2>&1 || fail "login password unavailable"
browser click '#loginBtn' >/dev/null 2>&1 || fail "login button unavailable"
browser wait 3500 >/dev/null 2>&1 || fail "login did not settle"
read -r SID_A SID_B < <(selected_session_ids)
printf '%s\n%s\n' "$SID_A" "$SID_B" > "$artifacts/session-order.txt"
select_session "$SID_A"

# 1. Normal remains the original above-the-composer positioning with no inline top.
open_slash_picker
probe normal
shot 01-normal

# Getting from normal to tall needs a draft which overflows the normal 180px cap,
# because the grabber is intentionally hidden until then.
long_draft="$(for i in $(seq 1 20); do printf 'grow line %02d for the height grabber\n' "$i"; done)"
set_draft_json "${long_draft%$'\n'}"
settle
click_expand
assert_height tall tall-height
open_slash_picker
probe tall
shot 02-tall

# 3. Tall -> full. The command picker remains anchored to the first caret line,
# which in full mode must flip down because the topbar leaves no room above it.
click_expand
assert_height full full-height
open_slash_picker
probe full
shot 03-full

# 4. Return through normal into tall and use a mid-draft path trigger. Slash/model
# completion is deliberately start-only; ./ is the supported completion trigger in
# the final line of an ordinary multiline draft.
click_expand
assert_height normal back-normal
multiline="$(for i in $(seq 1 14); do printf 'short line %02d\n' "$i"; done)"$'\ncheck '
set_draft_json "$multiline"
settle
click_expand
assert_height tall multiline-tall-height
set_draft_json "${multiline}./"
wait_for_picker || fail "multiline path picker did not open"
settle 900
probe multiline-path

# 5. Full mode, select /model by keyboard from slash completion. The selected
# command is expected to open the model picker without requiring a second edit.
click_expand
assert_height full model-full-height
open_slash_picker
probe model-command-open
model_index="$(python3 - "$artifacts/model-command-open.json" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))['data']['result']
for i, text in enumerate(p['pickerOptions']):
    if text.strip().startswith('/model'):
        print(i); break
else: raise SystemExit('no /model option: %r' % p['pickerOptions'])
PY
)" || fail "could not find /model command"
for _ in $(seq 1 "$model_index"); do browser press ArrowDown >/dev/null 2>&1 || fail "could not navigate command picker"; done
browser press Enter >/dev/null 2>&1 || fail "could not accept /model command"
settle 600
probe model-picker

# 6. A path completion in full mode starts mid-line. Its left edge must be at the
# token start rather than the input wrap's left edge. Pick a directory entry with
# the keyboard and ensure the open picker is recomputed after drill-down.
set_draft_json $'prefix line\ncheck ./'
wait_for_picker || fail "full path picker did not open"
settle 900
probe path-before-directory
path_dir_index="$(python3 - "$artifacts/path-before-directory.json" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))['data']['result']
for i, text in enumerate(p['pickerOptions']):
    if text.rstrip().endswith('/'):
        print(i); break
else: raise SystemExit('no directory result: %r' % p['pickerOptions'])
PY
)" || fail "no directory completion could be selected"
for _ in $(seq 1 "$path_dir_index"); do browser press ArrowDown >/dev/null 2>&1 || fail "could not navigate path picker"; done
browser press Enter >/dev/null 2>&1 || fail "could not accept directory completion"
wait_for_picker || fail "path picker closed after directory drill-down"
settle 700
probe path-after-directory

# 7. A tall textarea's picker stays logically open but hides if its caret leaves
# the visible scrollport, and becomes visible again at the scroll bottom.
click_expand
assert_height normal scroll-normal-height
scroll_draft="$(for i in $(seq 1 34); do printf 'scroll line %02d\n' "$i"; done)"$'\ncheck '
set_draft_json "$scroll_draft"
settle
click_expand
assert_height tall scroll-tall-height
set_draft_json "${scroll_draft}./"
wait_for_picker || fail "scroll path picker did not open"
settle 800
browser eval '(() => { const t=document.getElementById("msg"); t.scrollTop=0; t.dispatchEvent(new Event("scroll")); return t.scrollTop; })()' --json >/dev/null 2>&1 || fail "could not scroll textarea to top"
settle
probe scroll-hidden
shot 04-scroll-hidden
browser eval '(() => { const t=document.getElementById("msg"); t.scrollTop=t.scrollHeight; t.dispatchEvent(new Event("scroll")); return t.scrollTop; })()' --json >/dev/null 2>&1 || fail "could not scroll textarea to bottom"
settle
probe scroll-visible-again

# 8. Keyboard model: move active option, accept it, then reopen on the next slash.
browser fill '#msg' '' >/dev/null 2>&1 || fail "could not clear before keyboard regression"
browser focus '#msg' >/dev/null 2>&1 || fail "could not focus before keyboard regression"
browser type '#msg' '/' >/dev/null 2>&1 || fail "could not type keyboard slash"
wait_for_picker || fail "keyboard picker did not open"
probe keyboard-initial
browser press ArrowDown >/dev/null 2>&1 || fail "ArrowDown failed"
probe keyboard-down
browser press ArrowUp >/dev/null 2>&1 || fail "ArrowUp failed"
probe keyboard-up
# Select /new (rather than /model, whose acceptance intentionally opens the
# next model-picker stage tested above) to prove ordinary command acceptance
# closes the command menu and leaves the command in the composer.
browser press ArrowDown >/dev/null 2>&1 || fail "could not select ordinary command"
browser press Enter >/dev/null 2>&1 || fail "Enter acceptance failed"
settle
probe keyboard-accepted
browser fill '#msg' '' >/dev/null 2>&1 || fail "could not clear after keyboard acceptance"
browser type '#msg' '/' >/dev/null 2>&1 || fail "could not reopen slash picker"
wait_for_picker || fail "picker did not reopen"
probe keyboard-reopened

browser errors --json > "$artifacts/browser-errors.json" 2>&1 || true
browser console --json > "$artifacts/browser-console.json" 2>&1 || true
capture_container_diagnostics

python3 - "$artifacts" <<'PY'
import json, sys
from pathlib import Path

artifacts=Path(sys.argv[1])
def load(name):
    payload=json.loads((artifacts/f'{name}.json').read_text(encoding='utf-8'))
    if payload.get('success') is not True: raise SystemExit(f'unsuccessful {name}: {payload!r}')
    return payload['data']['result']

names=['normal','tall-height','tall','full-height','full','back-normal','multiline-tall-height','multiline-path','model-full-height','model-command-open','model-picker','path-before-directory','path-after-directory','scroll-normal-height','scroll-tall-height','scroll-hidden','scroll-visible-again','keyboard-initial','keyboard-down','keyboard-up','keyboard-accepted','keyboard-reopened']
p={n:load(n) for n in names}
def on_screen(x):
    m=x['picker']; t=x['topbar']; v=x['viewport']
    return bool(m and t and m['top'] >= t['bottom']-1 and m['bottom'] <= v['height']+1 and m['left'] >= -1 and m['right'] <= v['width']+1)
def adjacent(x, tolerance=10):
    m=x['picker']; caret=x['expectedCaretTop']
    if not m or caret is None: return False
    if x['pickerDir']=='up': return abs(m['bottom']-(caret-6)) <= tolerance
    return abs(m['top']-(caret+20+6)) <= tolerance
normal,tall,full,multi,model,path_before,path_after,hidden,shown=(p[k] for k in ['normal','tall','full','multiline-path','model-picker','path-before-directory','path-after-directory','scroll-hidden','scroll-visible-again'])
checks={
 'normal_display_block': normal['pickerDisplay']=='block',
 'normal_preserves_above_form_gap': bool(normal['picker'] and normal['form']) and abs(normal['picker']['bottom']-(normal['form']['top']-6)) <= 8,
 'normal_fully_in_viewport': on_screen(normal),
 'normal_has_no_inline_top': normal['inlineTop']=='',
 'tall_height_mode': p['tall-height']['height']=='tall',
 'tall_prefers_up': tall['pickerDir']=='up',
 'tall_adjacent_to_caret': adjacent(tall),
 'tall_is_on_screen': on_screen(tall),
 'tall_has_inline_top': bool(tall['inlineTop']),
 'full_height_mode': p['full-height']['height']=='full',
 'full_adjacent_to_caret': adjacent(full),
 'full_is_on_screen_below_topbar': on_screen(full),
 'multiline_path_picker_open': multi['pickerDisplay']=='block',
 'multiline_path_is_above_last_caret_line': multi['pickerDir']=='up' and adjacent(multi),
 'multiline_path_is_not_above_composer': bool(multi['picker'] and multi['composer']) and multi['picker']['bottom'] > multi['composer']['top']+100,
 'model_picker_open_after_selecting_command': model['pickerDisplay']=='block' and str(model['activeId'] or '').startswith('model-picker-option-'),
 'model_picker_is_adjacent_and_on_screen': model['pickerDisplay']=='block' and adjacent(model) and on_screen(model),
 'path_picker_open': path_before['pickerDisplay']=='block',
 'path_token_has_midline_horizontal_anchor': bool(path_before['picker'] and path_before['wrap']) and path_before['picker']['left'] > path_before['wrap']['left']+25,
 'path_picker_fits_input_wrap_horizontally': bool(path_before['picker'] and path_before['wrap']) and path_before['picker']['left'] >= path_before['wrap']['left']-1 and path_before['picker']['right'] <= path_before['wrap']['right']+1,
 'path_directory_acceptance_keeps_picker_open': path_after['pickerDisplay']=='block' and path_after['value'] != path_before['value'],
 'path_directory_picker_repositioned_on_screen': bool(path_after['inlineTop'] and path_after['inlineLeft']) and on_screen(path_after),
 'scroll_hide_keeps_picker_open': hidden['pickerDisplay']=='block' and hidden['pickerVisibility']=='hidden',
 'scroll_bottom_restores_picker_visibility': shown['pickerDisplay']=='block' and shown['pickerVisibility']!='hidden',
 'keyboard_down_changes_active_option': p['keyboard-initial']['activeId'] != p['keyboard-down']['activeId'],
 'keyboard_up_restores_active_option': p['keyboard-initial']['activeId'] == p['keyboard-up']['activeId'],
 'keyboard_enter_accepts_and_closes': p['keyboard-accepted']['pickerDisplay']=='none' and str(p['keyboard-accepted']['value']).startswith('/new '),
 'keyboard_picker_reopens': p['keyboard-reopened']['pickerDisplay']=='block',
}
errors=json.loads((artifacts/'browser-errors.json').read_text(encoding='utf-8')).get('data',{}).get('errors',[])
checks['no_page_errors']=errors==[]
summary={'pass':all(checks.values()),'checks':checks,'probes':p,'pageErrors':errors}
(artifacts/'report.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'pass':summary['pass'],'checks':checks},indent=2))
if not summary['pass']: raise SystemExit(1)
PY

printf 'PASS: commit=%s artifacts=%s\n' "$target_commit" "$artifacts"
