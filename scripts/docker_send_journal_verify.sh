#!/usr/bin/env bash
# Docker-isolated behavioral verifier for the send journal, Pi `--approve`
# trust suppression, and composer ArrowUp recall.
#
# Builds the application from a Git archive of [commit] (default HEAD), but
# rebuilds the browser bundle inside the throwaway build context (tracked
# dist/app.bundle.js can lag source). Pi runs against a fake provider
# (unreachable loopback baseUrl) so no real credentials or network are used;
# the failed turn is still persisted in the Pi session log. The Pi settings
# deliberately omit defaultProjectTrust so the trust dialog is NOT pre-empted
# by user config.
#
# MODE=full      (default) trust suppression + journal + recall + argv.
# MODE=baseline  trust scenario only, expecting the dialog to swallow the send
#                (negative control against a pre-fix commit).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_commit="$(git -C "$repo_root" rev-parse --verify "${1:-HEAD}^{commit}")"
short_commit="$(git -C "$repo_root" rev-parse --short=12 "$target_commit")"
mode="${MODE:-full}"
port="${CODOXEAR_SJV_PORT:-19683}"
password="docker-sjv-password"
image="codoxear-sjv:${short_commit}"
container="codoxear-sjv-${short_commit}-${port}"
root="$(mktemp -d /tmp/codoxear-sjv.XXXXXX)"
artifacts="${CODOXEAR_SJV_ARTIFACTS:-$(mktemp -d /tmp/codoxear-sjv-results.XXXXXX)}"
home_dir="$root/home"
readonly -a docker=(sudo docker)

[[ "$port" != "8743" ]] || { echo "refusing live port 8743" >&2; exit 2; }
[[ -z "${CODEXEAR_APP_DIR:-}${CODEX_WEB_APP_DIR:-}" ]] || { echo "refusing app-dir override" >&2; exit 2; }
command -v agent-browser >/dev/null || { echo "agent-browser required" >&2; exit 2; }

cleanup() {
  "${docker[@]}" logs "$container" > "$artifacts/container.log" 2>&1 || true
  "${docker[@]}" exec "$container" sh -lc 'tail -n 120 "$HOME/server.log"' > "$artifacts/server.log" 2>&1 || true
  AGENT_BROWSER_SESSION="sjv-$$" agent-browser close >/dev/null 2>&1 || true
  "${docker[@]}" rm -f "$container" >/dev/null 2>&1 || true
  /bin/rm -rf "$root" || sudo /bin/rm -rf "$root"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# Fake Pi provider + settings WITHOUT any trust default.
mkdir -p "$home_dir/.pi/agent" "$home_dir/proj/.agents/skills/trivial"
cat > "$home_dir/.pi/agent/models.json" <<'JSON'
{"providers":{"fake":{"baseUrl":"http://127.0.0.1:9/v1","apiKey":"fake-key","api":"openai-completions",
 "models":[{"id":"fake-1","reasoning":false,"contextWindow":100000,"maxTokens":4096,"input":["text"]}]}}}
JSON
cat > "$home_dir/.pi/agent/settings.json" <<'JSON'
{"defaultProvider":"fake","defaultModel":"fake-1","retry":{"enabled":false},"quietStartup":true}
JSON
cat > "$home_dir/proj/.agents/skills/trivial/SKILL.md" <<'MD'
---
name: trivial
description: A trivial skill that exists only to trigger Pi's project-trust prompt.
---
Say hello.
MD
# Pi rewrites its process title to just "pi", erasing argv from /proc/<pid>/cmdline,
# so a logging shim (via PI_BIN) records the exact argv codoxear hands to Pi.
mkdir -p "$home_dir/bin"
cat > "$home_dir/bin/pi-shim" <<'SH'
#!/bin/sh
printf '%s\t%s\n' "$(date +%s.%N)" "$*" >> "$HOME/pi-argv.log"
exec /usr/local/bin/pi "$@"
SH
chmod +x "$home_dir/bin/pi-shim"
chmod -R a+rwX "$home_dir"

# Build context from the commit; bundle rebuilt inside the context only.
ctx="$root/ctx"; mkdir -p "$ctx"
git -C "$repo_root" archive --format=tar "$target_commit" | tar -x -C "$ctx"
sed 's/git libatomic1/git libatomic1 tmux/' "$repo_root/docker/verify.Dockerfile" > "$ctx/docker/verify.Dockerfile"
if [[ -f "$ctx/codoxear/static/app.js" ]]; then
  npx --yes esbuild "$ctx/codoxear/static/app.js" --bundle --minify \
    --outfile="$ctx/codoxear/static/dist/app.bundle.js" --format=esm > "$artifacts/esbuild.log" 2>&1 \
    || fail "esbuild failed; see $artifacts/esbuild.log"
fi
"${docker[@]}" build -f "$ctx/docker/verify.Dockerfile" -t "$image" "$ctx" > "$artifacts/docker-build.log" 2>&1 \
  || fail "docker build failed; see $artifacts/docker-build.log"

"${docker[@]}" rm -f "$container" >/dev/null 2>&1 || true
"${docker[@]}" run --detach --name "$container" \
  --publish "127.0.0.1:${port}:${port}" \
  --env HOME=/home/tester --env CODEX_WEB_PASSWORD="$password" \
  --env CODEX_WEB_HOST=0.0.0.0 --env CODEX_WEB_PORT="$port" \
  --env CODEX_WEB_DEFAULT_AGENT_BACKEND=pi --env PYTHONPATH=/workspace \
  --env PYTHONDONTWRITEBYTECODE=1 --env PI_OFFLINE=1 --env PI_BIN=/home/tester/bin/pi-shim --env TERM=xterm-256color \
  --mount "type=bind,src=$home_dir,dst=/home/tester" --workdir /workspace \
  "$image" bash -c 'cd /workspace; python3 -m codoxear.server >"$HOME/server.log" 2>&1 & exec sleep infinity' \
  > "$artifacts/container-id.txt" || fail "container start failed"

python3 "$repo_root/scripts/docker_send_journal_verify.py" \
  --port "$port" --password "$password" --container "$container" \
  --artifacts "$artifacts" --mode "$mode" --commit "$short_commit" \
  --browser-session "sjv-$$"
