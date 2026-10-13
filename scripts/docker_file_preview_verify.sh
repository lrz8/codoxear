#!/usr/bin/env bash
# Docker-isolated real-browser verifier for default file previews. It builds the
# requested committed revision from a Git archive, creates a container-only Pi
# session rooted at a small Git fixture, and records every browser command and
# raw result under a fresh artifact directory.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/docker_file_preview_verify.sh [commit]

Behaviorally verifies markdown/HTML default previews, source toggling, plain
source files, and changed-file diffs using agent-browser against Docker.
Artifacts (raw browser command output, JSON assertions, screenshots, and
container logs) are retained in /tmp/codoxear-file-preview-verify.*.

Environment:
  CODOXEAR_FILE_PREVIEW_VERIFY_PORT       Loopback port (default: 19643; never 8743)
  CODOXEAR_FILE_PREVIEW_VERIFY_IMAGE      Image tag override
  CODOXEAR_FILE_PREVIEW_VERIFY_PASSWORD   Login password override
USAGE
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_ref="${1:-HEAD}"
target_commit="$(git -C "$repo_root" rev-parse --verify "${target_ref}^{commit}")" || {
  echo "unable to resolve commit: $target_ref" >&2
  exit 2
}
short_commit="$(git -C "$repo_root" rev-parse --short=12 "$target_commit")"
port="${CODOXEAR_FILE_PREVIEW_VERIFY_PORT:-19643}"
password="${CODOXEAR_FILE_PREVIEW_VERIFY_PASSWORD:-docker-file-preview-password}"
image="${CODOXEAR_FILE_PREVIEW_VERIFY_IMAGE:-codoxear-file-preview-verify:${short_commit}}"
container="codoxear-file-preview-verify-${short_commit}-${port}"
source_pi_agent="$HOME/.pi/agent"
root="$(mktemp -d /tmp/codoxear-file-preview-verify-build.XXXXXX)"
artifacts="$(mktemp -d /tmp/codoxear-file-preview-verify.XXXXXX)"
home_dir="$root/home"
browser_session="codoxear-file-preview-verify-${short_commit}-$$"
readonly repo_root target_commit short_commit port password image container source_pi_agent root artifacts home_dir browser_session
readonly -a docker=(sudo docker)

if [[ "$port" == "8743" ]]; then
  echo "refusing live Codoxear port 8743" >&2
  exit 2
fi
if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
  echo "CODOXEAR_FILE_PREVIEW_VERIFY_PORT must be a TCP port, got: $port" >&2
  exit 2
fi
if [[ -n "${CODEXEAR_APP_DIR:-}${CODEX_WEB_APP_DIR:-}" ]]; then
  echo "refusing host app-dir override; unset CODEXEAR_APP_DIR and CODEX_WEB_APP_DIR" >&2
  exit 2
fi
if [[ ! -r "$source_pi_agent/models.json" || ! -r "$source_pi_agent/auth.json" || ! -r "$source_pi_agent/settings.json" ]]; then
  echo "Pi config is incomplete under $source_pi_agent (models.json, auth.json, and settings.json are required)" >&2
  exit 2
fi
if ! command -v agent-browser >/dev/null; then
  echo "agent-browser is required" >&2
  exit 2
fi

browser() { AGENT_BROWSER_SESSION="$browser_session" agent-browser "$@"; }
browser_step() { # artifact stem, followed by agent-browser command and args
  local stem="$1"
  shift
  printf '$ agent-browser' > "$artifacts/${stem}.txt"
  printf ' %q' "$@" >> "$artifacts/${stem}.txt"
  printf '\n' >> "$artifacts/${stem}.txt"
  browser "$@" >> "$artifacts/${stem}.txt" 2>&1
}

capture_container_diagnostics() {
  "${docker[@]}" logs "$container" > "$artifacts/container.log" 2>&1 || true
  "${docker[@]}" exec "$container" sh -lc '
    printf "HOME=%s\\n" "$HOME"
    printf "APP_DIR=%s\\n" "$(python3 -c "from codoxear.util import default_app_dir; print(default_app_dir())")"
    printf -- "--- fixture ---\\n"
    find /workspace/demo -maxdepth 3 -type f -printf "%y %p\\n" 2>/dev/null || true
    printf -- "--- git status ---\\n"
    git -C /workspace/demo status --short 2>/dev/null || true
    printf -- "--- socks ---\\n"
    find "$HOME/.local/share/codoxear/socks" -maxdepth 1 -type f -name "*.json" -exec sh -c '\''echo --- "$1"; cat "$1"'\'' _ {} \; 2>/dev/null || true
    printf -- "--- session logs ---\\n"
    find "$HOME/.pi/agent/sessions" -type f -name "*.jsonl" -printf "%p\\n" 2>/dev/null || true
  ' > "$artifacts/container-isolation.txt" 2>&1 || true
  "${docker[@]}" exec "$container" sh -lc 'cat "$HOME/pi-bootstrap.log" "$HOME/broker.log"' > "$artifacts/pi-and-broker.log" 2>&1 || true
}

cleanup() {
  capture_container_diagnostics
  browser close >/dev/null 2>&1 || true
  "${docker[@]}" rm -f "$container" >/dev/null 2>&1 || true
  sudo /bin/rm -rf "$root"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

copy_pi_config() {
  umask 077
  mkdir -p "$home_dir/.pi/agent" "$home_dir/.local/share"
  local name
  for name in models.json models-store.json auth.json settings.json trust.json anthropic-context-management.json; do
    [[ -r "$source_pi_agent/$name" ]] && install -m 600 "$source_pi_agent/$name" "$home_dir/.pi/agent/$name"
  done
}

wait_for_server() {
  local status
  for _ in $(seq 1 120); do
    status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/api/me" 2>/dev/null || true)"
    [[ "$status" == "401" ]] && return 0
    sleep 1
  done
  return 1
}

wait_for_pi_session() {
  local sessions="$artifacts/api-sessions.json" status
  curl -fsS -c "$root/cookies.txt" -H 'Content-Type: application/json' \
    --data "$(python3 -c 'import json,sys; print(json.dumps({"password":sys.argv[1]}))' "$password")" \
    "http://127.0.0.1:${port}/api/login" > "$artifacts/api-login.json"
  for _ in $(seq 1 120); do
    status="$(curl -sS -b "$root/cookies.txt" -o "$sessions" -w '%{http_code}' "http://127.0.0.1:${port}/api/sessions" 2>/dev/null || true)"
    if [[ "$status" == "200" ]] && python3 - "$sessions" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1], encoding="utf-8")).get("sessions", [])
raise SystemExit(0 if any(isinstance(row, dict) and row.get("agent_backend") == "pi" and row.get("session_id") for row in rows) else 1)
PY
    then return 0; fi
    sleep 1
  done
  return 1
}

copy_pi_config

# Build exactly the target commit. The current verifier Dockerfile is copied
# into the disposable context; no editable checkout files enter the image.
build_context="$root/build-context"
mkdir -p "$build_context/docker"
git -C "$repo_root" archive --format=tar "$target_commit" | tar -x -C "$build_context"
install -m 644 "$repo_root/docker/verify.Dockerfile" "$build_context/docker/verify.Dockerfile"
"${docker[@]}" build --file "$build_context/docker/verify.Dockerfile" --tag "$image" "$build_context" \
  > "$artifacts/docker-build.log" 2>&1 || fail "Docker image build failed; see $artifacts/docker-build.log"

"${docker[@]}" rm -f "$container" >/dev/null 2>&1 || true
"${docker[@]}" run --detach \
  --name "$container" \
  --publish "127.0.0.1:${port}:${port}" \
  --env HOME=/home/tester \
  --env CODEX_WEB_PASSWORD="$password" \
  --env CODEX_WEB_HOST=0.0.0.0 \
  --env CODEX_WEB_PORT="$port" \
  --env CODEX_WEB_DEFAULT_AGENT_BACKEND=pi \
  --env PYTHONPATH=/workspace \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --user root \
  --mount "type=bind,src=$home_dir,dst=/home/tester" \
  --workdir /workspace \
  "$image" bash -c '
    set -eu
    cd /workspace
    export PYTHONPATH=/workspace
    fixture=/workspace/demo
    mkdir -p "$fixture/img" "$fixture/src"
    cd "$fixture"
    git init -q
    git config user.email verify@example.test
    git config user.name "Codoxear verifier"
    cat > notes.md <<'"'"'EOF'"'"'
# Notes Heading

- alpha
- beta
EOF
    cat > report.html <<'"'"'EOF'"'"'
<!doctype html><html><head><link rel="stylesheet" href="style.css"></head><body><h1 id="title">HTML Report Heading</h1><img id="fixture-image" src="img/pic.png" alt="fixture image"></body></html>
EOF
    printf "body { background-color: #101820; color: #f5f5f5; }\n" > style.css
    printf "print(\"fixture\")\n" > src/main.py
    printf "# Fixture README\n" > README.md
    python3 - <<'"'"'PY'"'"'
import struct, zlib
width = height = 24
row = b"\0" + bytes((0x44, 0x99, 0xdd, 0xff)) * width
raw = row * height
chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
open("img/pic.png", "wb").write(png)
PY
    git add notes.md report.html style.css img/pic.png src/main.py README.md
    git commit -qm fixture
    printf "Changed after commit.\n" >> README.md
    cd /workspace
    pi --offline --no-extensions --no-skills --no-prompt-templates --no-context-files \
      --session-id docker-file-preview-verify -p "Initialize isolated file preview verification session." \
      >"$HOME/pi-bootstrap.log" 2>&1 || true
    session_log="$(find "$HOME/.pi/agent/sessions" -type f -name "*docker-file-preview-verify.jsonl" -print -quit)"
    test -n "$session_log"
    CODEX_WEB_AGENT_BACKEND=pi CODEX_WEB_OWNER=web PI_BIN=pi PI_OFFLINE=1 \
      codoxear-broker --cwd "$fixture" -- \
        --offline --no-extensions --no-skills --no-prompt-templates --no-context-files \
        --session "$session_log" >"$HOME/broker.log" 2>&1 &
    for _ in $(seq 1 120); do
      meta="$(find "$HOME/.local/share/codoxear/socks" -maxdepth 1 -type f -name "*.json" -print -quit 2>/dev/null || true)"
      if test -n "$meta" && grep -Eq "\\\"session_id\\\"[[:space:]]*:[[:space:]]*\\\"[^\\\"]+\\\"" "$meta"; then break; fi
      sleep 1
    done
    test -n "${meta:-}" && grep -Eq "\\\"session_id\\\"[[:space:]]*:[[:space:]]*\\\"[^\\\"]+\\\"" "$meta"
    exec python3 -m codoxear.server
  ' > "$artifacts/container-id.txt" || fail "container start failed"

wait_for_server || fail "isolated server did not become reachable"
wait_for_pi_session || fail "offline Pi session did not appear"
session_id="$(python3 - "$artifacts/api-sessions.json" <<'PY'
import json, sys
for row in json.load(open(sys.argv[1], encoding="utf-8")).get("sessions", []):
    if isinstance(row, dict) and row.get("agent_backend") == "pi" and isinstance(row.get("session_id"), str):
        print(row["session_id"])
        break
else: raise SystemExit("no Pi session")
PY
)"
printf '%s\n' "$session_id" > "$artifacts/session-id.txt"
card_selector="#sessions .session[data-session-id=\"${session_id}\"]"

# All interaction below goes through agent-browser; every command's raw output
# is kept in the artifact directory before assertions consume it.
app_url="http://127.0.0.1:${port}/"
browser_step 01-open open "$app_url" || fail "browser could not open app"
browser_step 02-errors-clear errors --clear --json || fail "cannot clear browser errors"
browser_step 03-console-clear console --clear --json || fail "cannot clear browser console"
browser_step 04-login-fill fill '#pw' "$password" || fail "login field missing"
browser_step 05-login-click click '#loginBtn' || fail "login button missing"
browser_step 06-login-wait wait 3500 || fail "login did not settle"
browser_step 07-select-session click "$card_selector" || fail "Pi session card missing"
browser_step 08-select-wait wait 1600 || fail "session selection did not settle"
# Baseline before a document is opened: Resource Timing makes any 4xx URL
# explicit instead of inferring it from console wording.
browser_step 08a-errors-after-login errors --json || fail "cannot collect login checkpoint errors"
browser_step 08b-resource-failures-after-login eval 'performance.getEntriesByType("resource").filter((e) => (e.responseStatus || 0) >= 400).map((e) => [e.name, e.responseStatus, e.initiatorType])' --json || fail "cannot collect login checkpoint resource failures"

# The user row is sufficient: Pi is offline, so a failed assistant turn does
# not weaken this transcript-link test.
browser_step 09-send-reference-fill fill '#msg' 'see notes.md and report.html and notes.md:3' || fail "composer missing"
browser_step 10-send-reference-click click '#sendBtn' || fail "send button missing"
browser_step 11-send-wait wait 3000 || fail "user file-reference row did not settle"
browser_step 12-transcript-links eval '(() => Array.from(document.querySelectorAll("a.inlineFileLink")).map((a) => ({text:a.textContent, path:a.dataset.filePath, line:a.dataset.fileLine || null})))()' --json || fail "cannot inspect transcript links"

# A: Transcript markdown link opens the rendered markdown default.
browser_step 13-click-notes click 'a.inlineFileLink[data-file-path$="/notes.md"]:not([data-file-line])' || fail "notes.md transcript link missing"
browser_step 14-notes-wait wait 1500 || fail "notes preview did not settle"
browser_step 15-markdown-preview eval '(() => { const v=(n)=>!!n&&getComputedStyle(n).display!=="none"&&getComputedStyle(n).visibility!=="hidden"&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0; const viewer=document.querySelector("#fileViewer"); const p=document.querySelector(".fileMarkdownPreview"); const monaco=Array.from(document.querySelectorAll(".monaco-editor")).some(v); const b=document.querySelector("#fileModePreviewBtn"); return {viewerOpen:v(viewer), previewVisible:v(p), heading:p?.textContent.includes("Notes Heading")||false, monacoVisible:monaco, previewActive:b?.classList.contains("active")||false}; })()' --json || fail "cannot inspect markdown preview"
browser_step 16-markdown-preview-screenshot screenshot --full "$artifacts/markdown-preview.png" || fail "markdown screenshot failed"
# This checkpoint isolates the one-document scenario: it is collected before
# the source toggle or any file switch can cancel an in-flight load.
browser_step 16a-errors-after-markdown errors --json || fail "cannot collect markdown checkpoint errors"
browser_step 16b-console-after-markdown console --json || fail "cannot collect markdown checkpoint console"
browser_step 16c-resource-failures-after-markdown eval 'performance.getEntriesByType("resource").filter((e) => (e.responseStatus || 0) >= 400).map((e) => [e.name, e.responseStatus, e.initiatorType])' --json || fail "cannot collect markdown checkpoint resource failures"

# B: the preview control toggles source and returns to preview.
browser_step 17-toggle-source click '#fileModePreviewBtn' || fail "preview toggle unavailable"
browser_step 18-source-wait wait 1200 || fail "source toggle did not settle"
browser_step 19-markdown-source eval '(() => { const v=(n)=>!!n&&getComputedStyle(n).display!=="none"&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0; const p=document.querySelector(".fileMarkdownPreview"); const monaco=Array.from(document.querySelectorAll(".monaco-editor")).some(v); const b=document.querySelector("#fileModePreviewBtn"); return {previewNodePresent:!!p, monacoVisible:monaco, previewActive:b?.classList.contains("active")||false}; })()' --json || fail "cannot inspect markdown source"
browser_step 20-source-screenshot screenshot --full "$artifacts/markdown-source.png" || fail "source screenshot failed"
browser_step 21-toggle-preview click '#fileModePreviewBtn' || fail "preview toggle unavailable on source"
browser_step 22-preview-return-wait wait 1200 || fail "preview return did not settle"
browser_step 23-markdown-preview-return eval '(() => { const v=(n)=>!!n&&getComputedStyle(n).display!=="none"&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0; const p=document.querySelector(".fileMarkdownPreview"); const b=document.querySelector("#fileModePreviewBtn"); return {previewVisible:v(p), heading:p?.textContent.includes("Notes Heading")||false, previewActive:b?.classList.contains("active")||false}; })()' --json || fail "cannot inspect returned markdown preview"

# C: HTML preview comes from a sandboxed same-origin frame whose own relative
# image and stylesheet have loaded.
browser_step 24-close-notes click '#fileCloseBtn' || fail "cannot close markdown viewer"
browser_step 25-click-report click 'a.inlineFileLink[data-file-path$="/report.html"]' || fail "report.html transcript link missing"
browser_step 26-report-wait wait 2000 || fail "HTML preview did not settle"
browser_step 27-html-preview eval '(() => { const v=(n)=>!!n&&getComputedStyle(n).display!=="none"&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0; const viewer=document.querySelector("#fileViewer"); const f=document.querySelector("iframe.fileHtmlPreview"); const d=f?.contentDocument; const img=d?.querySelector("#fixture-image"); const status=document.querySelector("#fileStatus"); return {viewerOpen:v(viewer), frameExists:!!f, frameVisible:v(f), src:f?.src||null, documentUrl:d?.URL||null, bodyText:d?.body?.innerText||null, sandbox:f?.getAttribute("sandbox")||null, hasAllowScripts:(f?.getAttribute("sandbox")||"").split(/\\s+/).includes("allow-scripts"), statusLine:status?.textContent||null, heading:d?.querySelector("#title")?.textContent||null, imageNaturalWidth:img?.naturalWidth ?? null, backgroundColor:d?.body ? getComputedStyle(d.body).backgroundColor : null}; })()' --json || fail "cannot inspect HTML frame"
# Fetching the same frame URL inside Chromium records the response headers
# without probing the host/server API outside the real browser flow.
browser_step 27a-errors-after-html errors --json || fail "cannot collect HTML checkpoint errors"
browser_step 27b-resource-failures-after-html eval 'performance.getEntriesByType("resource").filter((e) => (e.responseStatus || 0) >= 400).map((e) => [e.name, e.responseStatus, e.initiatorType])' --json || fail "cannot collect HTML checkpoint resource failures"
# Fetch the same frame URL only after the resource checkpoint, so this
# header-evidence request does not add an entry to the checkpoint list.
browser_step 27c-html-response-headers eval '(async () => { const frame=document.querySelector("iframe.fileHtmlPreview"); const response=await fetch(frame?.src || "", {credentials:"same-origin"}); return {url:response.url, status:response.status, contentType:response.headers.get("content-type"), contentSecurityPolicy:response.headers.get("content-security-policy")}; })()' --json || fail "cannot inspect HTML frame response headers"
browser_step 28-html-preview-screenshot screenshot --full "$artifacts/html-preview.png" || fail "HTML screenshot failed"

# D: a plain source file reached through the actual picker stays in Monaco.
browser_step 29-close-html click '#fileCloseBtn' || fail "cannot close HTML viewer"
browser_step 30-open-picker click '#fileBtn' || fail "file picker button missing"
browser_step 31-search-main fill '#filePickerInput' 'main.py' || fail "picker input missing"
browser_step 32-main-options eval '(() => Array.from(document.querySelectorAll("#filePickerMenu .fileMenuItem")).map((n)=>({id:n.id,text:n.innerText,changed:!!n.querySelector(".fileMenuStat.changed")})))()' --json || fail "cannot inspect main.py picker row"
browser_step 33-open-main click '#filePickerOption-1' || fail "src/main.py picker row missing"
browser_step 34-main-wait wait 1500 || fail "main.py did not settle"
browser_step 35-plain-source eval '(() => { const v=(n)=>!!n&&getComputedStyle(n).display!=="none"&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0; const monaco=Array.from(document.querySelectorAll(".monaco-editor")).some(v); const preview=document.querySelector(".fileMarkdownPreview, iframe.fileHtmlPreview"); const b=document.querySelector("#fileModePreviewBtn"); return {monacoVisible:monaco, previewPresent:!!preview, previewVisible:v(preview), previewButtonHidden:!v(b), previewActive:b?.classList.contains("active")||false}; })()' --json || fail "cannot inspect plain source"

# E: README.md is changed in fixture Git and must win as a diff over preview.
browser_step 36-search-readme fill '#filePickerInput' 'README.md' || fail "cannot search README"
browser_step 37-readme-options eval '(() => ({sections:Array.from(document.querySelectorAll("#filePickerMenu .fileMenuSection")).map(n=>n.textContent), options:Array.from(document.querySelectorAll("#filePickerMenu .fileMenuItem")).map(n=>({id:n.id,text:n.innerText,changed:!!n.querySelector(".fileMenuStat.changed")}))}))()' --json || fail "cannot inspect README picker row"
browser_step 38-open-readme click '#filePickerOption-1' || fail "changed README picker row missing"
browser_step 39-readme-wait wait 1600 || fail "README diff did not settle"
browser_step 40-changed-diff eval '(() => { const v=(n)=>!!n&&getComputedStyle(n).display!=="none"&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0; const d=document.querySelector(".monaco-diff-editor"); const b=document.querySelector("#fileModeDiffBtn"); return {diffVisible:v(d), diffActive:b?.classList.contains("active")||false}; })()' --json || fail "cannot inspect changed-file diff"
browser_step 41-changed-diff-screenshot screenshot --full "$artifacts/changed-readme-diff.png" || fail "changed diff screenshot failed"

# G (optional): the parsed line-targeted transcript reference must force source.
browser_step 42-close-readme click '#fileCloseBtn' || fail "cannot close README viewer"
if browser_step 43-click-line-reference click 'a.inlineFileLink[data-file-path$="/notes.md"][data-file-line="3"]'; then
  browser_step 44-line-reference-wait wait 1400 || fail "line reference did not settle"
  browser_step 45-line-reference eval '(() => { const v=(n)=>!!n&&getComputedStyle(n).display!=="none"&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0; return {parsed:true, monacoVisible:Array.from(document.querySelectorAll(".monaco-editor")).some(v), previewPresent:!!document.querySelector(".fileMarkdownPreview"), previewActive:document.querySelector("#fileModePreviewBtn")?.classList.contains("active")||false}; })()' --json || fail "cannot inspect line reference"
else
  printf '{"parsed":false,"reason":"line-targeted transcript reference did not become a clickable inlineFileLink"}\n' > "$artifacts/45-line-reference.txt"
fi

browser_step 46-errors errors --json || fail "cannot collect browser errors"
browser_step 47-console console --json || fail "cannot collect browser console"
browser_step 48-final-snapshot snapshot -i -c --depth 12 || fail "cannot capture final browser snapshot"

python3 - "$artifacts" "$target_commit" <<'PY' > "$artifacts/report.json"
import json, re, sys
from pathlib import Path

artifacts = Path(sys.argv[1])
def browser_result(stem):
    path = artifacts / f"{stem}.txt"
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    marker = "\n{\"success\":"
    start = raw.find(marker)
    if start < 0: return {"_raw_error": raw}
    try: return json.loads(raw[start + 1:]).get("data", {}).get("result")
    except Exception as exc: return {"_raw_error": str(exc), "raw": raw}
def browser_payload(stem):
    path = artifacts / f"{stem}.txt"
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    marker = "\n{\"success\":"
    start = raw.find(marker)
    if start < 0: return {"_raw_error": raw}
    try: return json.loads(raw[start + 1:])
    except Exception as exc: return {"_raw_error": str(exc), "raw": raw}

links = browser_result("12-transcript-links")
md = browser_result("15-markdown-preview")
source = browser_result("19-markdown-source")
returned = browser_result("23-markdown-preview-return")
html = browser_result("27-html-preview")
html_headers = browser_result("27c-html-response-headers")
login_errors = browser_payload("08a-errors-after-login").get("data", {}).get("errors", [])
login_resource_failures = browser_result("08b-resource-failures-after-login")
early_errors = browser_payload("16a-errors-after-markdown").get("data", {}).get("errors", [])
early_console = browser_payload("16b-console-after-markdown").get("data", {}).get("messages", [])
markdown_resource_failures = browser_result("16c-resource-failures-after-markdown")
html_errors = browser_payload("27a-errors-after-html").get("data", {}).get("errors", [])
html_resource_failures = browser_result("27b-resource-failures-after-html")
main_options = browser_result("32-main-options")
plain = browser_result("35-plain-source")
readme_options = browser_result("37-readme-options")
diff = browser_result("40-changed-diff")
line = browser_result("45-line-reference")
errors = browser_payload("46-errors").get("data", {}).get("errors", [])
console = browser_payload("47-console").get("data", {}).get("messages", [])
fatal_console = [x for x in console if isinstance(x, dict) and re.search(r"\\b(?:ReferenceError|TypeError|SyntaxError)\\b|\\bCodoxear\\b.*\\bfailed to load\\b", str(x.get("text") or ""), re.I)]

checks = {
  "A_transcript_markdown_preview": bool(isinstance(links, list) and any(str(x.get("path") or "").endswith("/notes.md") for x in links if isinstance(x, dict)) and md.get("viewerOpen") is True and md.get("previewVisible") is True and md.get("heading") is True and md.get("monacoVisible") is False and md.get("previewActive") is True),
  "B_toggle_source_and_back": source.get("previewNodePresent") is False and source.get("monacoVisible") is True and source.get("previewActive") is False and returned.get("previewVisible") is True and returned.get("heading") is True and returned.get("previewActive") is True,
  "C_transcript_html_preview": html.get("viewerOpen") is True and html.get("frameExists") is True and html.get("frameVisible") is True and html.get("sandbox") == "allow-same-origin" and html.get("hasAllowScripts") is False and isinstance(html.get("statusLine"), str) and "preview (scripts blocked)" in html["statusLine"] and html.get("heading") == "HTML Report Heading" and isinstance(html.get("imageNaturalWidth"), int) and html["imageNaturalWidth"] > 0 and html.get("backgroundColor") == "rgb(16, 24, 32)" and html_headers.get("status") == 200 and html_headers.get("contentType") == "text/html; charset=utf-8" and html_headers.get("contentSecurityPolicy") == "sandbox allow-same-origin",
  "D_picker_plain_source": bool(isinstance(main_options, list) and any("main.py" in str(x.get("text")) for x in main_options if isinstance(x, dict)) and plain.get("monacoVisible") is True and plain.get("previewPresent") is False and plain.get("previewButtonHidden") is True and plain.get("previewActive") is False),
  "E_picker_changed_markdown_diff": bool(isinstance(readme_options, dict) and any(isinstance(x, dict) and "README.md" in str(x.get("text")) and x.get("changed") for x in readme_options.get("options", [])) and diff.get("diffVisible") is True and diff.get("diffActive") is True),
  "F_no_js_errors": errors == [] and not fatal_console,
  "G_line_targeted_reference_source": line.get("parsed") is True and line.get("monacoVisible") is True and line.get("previewPresent") is False and line.get("previewActive") is False,
}
report = {
  "commit": sys.argv[2], "pass": all(checks[k] for k in checks if k != "G_line_targeted_reference_source"),
  "checks": checks,
  "observed": {"transcriptLinks":links, "markdownPreview":md, "errorCheckpoints":{"afterLogin":{"pageErrors":login_errors, "errorsEmpty":not login_errors, "resourceFailures":login_resource_failures}, "afterMarkdownPreview":{"pageErrors":early_errors, "errorsEmpty":not early_errors, "resourceFailures":markdown_resource_failures, "console":early_console}, "afterHtmlPreview":{"pageErrors":html_errors, "errorsEmpty":not html_errors, "resourceFailures":html_resource_failures}, "final":{"pageErrors":errors, "errorsEmpty":not errors, "fatalConsole":fatal_console}}, "markdownSource":source, "markdownPreviewReturn":returned, "htmlPreview":html, "htmlResponseHeaders":html_headers, "mainOptions":main_options, "plainSource":plain, "readmeOptions":readme_options, "changedDiff":diff, "lineReference":line, "pageErrors":errors, "fatalConsole":fatal_console, "console":console},
  "artifacts": {"markdownPreviewScreenshot":str(artifacts / "markdown-preview.png"), "markdownSourceScreenshot":str(artifacts / "markdown-source.png"), "htmlPreviewScreenshot":str(artifacts / "html-preview.png"), "changedDiffScreenshot":str(artifacts / "changed-readme-diff.png"), "containerLog":str(artifacts / "container.log")}
}
print(json.dumps(report, indent=2))
PY

cat "$artifacts/report.json"
if ! python3 - "$artifacts/report.json" <<'PY'
import json, sys
raise SystemExit(0 if json.load(open(sys.argv[1], encoding="utf-8")).get("pass") else 1)
PY
then
  echo "FAIL: file preview verification failed; artifacts=$artifacts" >&2
  exit 1
fi
printf 'PASS: file previews verified at %s; artifacts=%s\n' "$short_commit" "$artifacts"
