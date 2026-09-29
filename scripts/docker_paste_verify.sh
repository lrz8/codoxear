#!/usr/bin/env bash
# Isolated browser verification for attachment paste feedback.
#
# Same isolation boundary as scripts/docker_verify.sh: the app runs from a Git
# archive of the requested commit inside Docker with a throwaway HOME, never
# from the editable checkout and never near the live deployment. The harness
# then drives the real UI through agent-browser: it dispatches a synthetic
# paste carrying a real PNG File into the composer and asserts, synchronously
# in the same task, that a pending chip with a thumbnail is already rendered
# (the "instant feedback" claim), and after settling that the chip swapped to
# the staged state with the thumbnail preserved.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/docker_paste_verify.sh [commit]

Behaviorally verifies attachment paste feedback in Docker. The app source is
exactly [commit] (default: HEAD). Artifacts are retained in a newly-created
/tmp/codoxear-paste-verify-results.* directory.
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
port="${CODOXEAR_VERIFY_PORT:-19644}"
password="${CODOXEAR_VERIFY_PASSWORD:-docker-verify-password}"
image="${CODOXEAR_VERIFY_IMAGE:-codoxear-verify:${short_commit}}"
container="codoxear-paste-verify-${short_commit}-${port}"
source_pi_agent="$HOME/.pi/agent"
root="$(mktemp -d /tmp/codoxear-paste-verify.XXXXXX)"
artifacts="$(mktemp -d /tmp/codoxear-paste-verify-results.XXXXXX)"
home_dir="$root/home"
browser_session="codoxear-paste-verify-${short_commit}-$$"
readonly root artifacts home_dir browser_session
readonly -a docker=(sudo docker)

if [[ "$port" == "8743" ]]; then
  echo "refusing live Codoxear port 8743" >&2
  exit 2
fi
if [[ ! -r "$source_pi_agent/models.json" || ! -r "$source_pi_agent/auth.json" || ! -r "$source_pi_agent/settings.json" ]]; then
  echo "Pi config is incomplete under $source_pi_agent" >&2
  exit 2
fi
if ! command -v agent-browser >/dev/null; then
  echo "agent-browser is required" >&2
  exit 2
fi

browser() {
  AGENT_BROWSER_SESSION="$browser_session" agent-browser "$@"
}

cleanup() {
  browser close >/dev/null 2>&1 || true
  "${docker[@]}" rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$root"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  "${docker[@]}" logs "$container" > "$artifacts/server.log" 2>&1 || true
  exit 1
}

umask 077
mkdir -p "$home_dir/.pi/agent" "$home_dir/.local/share"
for name in models.json models-store.json auth.json settings.json trust.json anthropic-context-management.json; do
  if [[ -r "$source_pi_agent/$name" ]]; then
    install -m 600 "$source_pi_agent/$name" "$home_dir/.pi/agent/$name"
  fi
done

if ! "${docker[@]}" image inspect "$image" >/dev/null 2>&1; then
  build_context="$root/build-context"
  mkdir -p "$build_context"
  git -C "$repo_root" archive --format=tar "$target_commit" | tar -x -C "$build_context"
  mkdir -p "$build_context/docker"
  install -m 644 "$repo_root/docker/verify.Dockerfile" "$build_context/docker/verify.Dockerfile"
  "${docker[@]}" build --file "$build_context/docker/verify.Dockerfile" --tag "$image" "$build_context" \
    > "$artifacts/docker-build.log" 2>&1 || fail "Docker image build failed; see $artifacts/docker-build.log"
fi

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
  --mount "type=bind,src=$home_dir,dst=/home/tester" \
  --workdir /workspace \
  "$image" \
  bash -c '
    set -eu
    cd /workspace
    export PYTHONPATH=/workspace
    pi --offline --no-extensions --no-skills --no-prompt-templates --no-context-files \
      --session-id docker-verify-session -p "Initialize isolated verification session." \
      >"$HOME/pi-bootstrap.log" 2>&1 || true
    session_log="$(find "$HOME/.pi/agent/sessions" -type f -name "*docker-verify-session.jsonl" -print -quit)"
    test -n "$session_log"
    CODEX_WEB_AGENT_BACKEND=pi CODEX_WEB_OWNER=web PI_BIN=pi PI_OFFLINE=1 \
      codoxear-broker --cwd /workspace -- \
        --offline --no-extensions --no-skills --no-prompt-templates --no-context-files \
        --session "$session_log" >"$HOME/sessiond.log" 2>&1 &
    for _ in $(seq 1 120); do
      meta="$(find "$HOME/.local/share/codoxear/socks" -maxdepth 1 -type f -name "*.json" -print -quit 2>/dev/null || true)"
      if test -n "$meta" && grep -Eq "\\\"session_id\\\"[[:space:]]*:[[:space:]]*\\\"[^\\\"]+\\\"" "$meta"; then
        break
      fi
      sleep 1
    done
    test -n "${meta:-}" && grep -Eq "\\\"session_id\\\"[[:space:]]*:[[:space:]]*\\\"[^\\\"]+\\\"" "$meta"
    exec python3 -m codoxear.server
  ' > "$artifacts/container-id.txt" || fail "container start failed"

for _ in $(seq 1 120); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/api/me" 2>/dev/null || true)"
  if [[ "$code" == "401" ]]; then
    break
  fi
  sleep 1
done
[[ "$code" == "401" ]] || fail "container server did not become reachable"

browser open "http://127.0.0.1:${port}/" > "$artifacts/browser-open.txt" 2>&1 || fail "browser could not open app"
browser fill '#pw' "$password" > "$artifacts/browser-login-fill.txt" 2>&1 || fail "login password field was not available"
browser click '#loginBtn' > "$artifacts/browser-login-click.txt" 2>&1 || fail "login button was not available"
browser wait 3500 > /dev/null 2>&1 || fail "browser did not settle after login"

# Select the sole session card so the composer binds to it.
browser eval '(() => {
  const card = document.querySelector("#sessions > .session");
  if (!card) return { clicked: false };
  card.click();
  return { clicked: true };
})()' --json > "$artifacts/browser-select.json" 2>&1 || fail "session card selection failed"
browser wait 1500 > /dev/null 2>&1 || true

# Dispatch a real paste carrying a PNG File and capture the tray state in the
# same task: the pending chip must already be rendered before any await.
browser eval '(() => {
  const ta = document.querySelector("textarea");
  if (!ta) return { ok: false, reason: "no textarea" };
  const canvas = document.createElement("canvas");
  canvas.width = 8;
  canvas.height = 8;
  const g = canvas.getContext("2d");
  g.fillStyle = "#c96f4a";
  g.fillRect(0, 0, 8, 8);
  const b64 = canvas.toDataURL("image/png").split(",")[1];
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const file = new File([bytes], "paste-test.png", { type: "image/png" });
  const dt = new DataTransfer();
  dt.items.add(file);
  ta.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  const tray = document.querySelector("#stagedAttachments");
  const chip = tray ? tray.querySelector(".stagedAttachmentChip") : null;
  const phase = chip ? chip.querySelector(".stagedAttachmentPhase") : null;
  const badge = document.querySelector("#attachBadge");
  return {
    ok: true,
    trayDisplay: tray ? tray.style.display : null,
    pending: chip ? chip.getAttribute("data-pending") : null,
    thumb: chip ? Boolean(chip.querySelector("img.stagedAttachmentThumb")) : null,
    phase: phase ? phase.textContent : null,
    badgeText: badge ? badge.textContent : null,
    badgeDisplay: badge ? badge.style.display : null,
  };
})()' --json > "$artifacts/pending-report.json" 2>&1 || fail "paste dispatch evaluation failed"
browser screenshot --full "$artifacts/pending.png" > /dev/null 2>&1 || true

browser wait 3000 > /dev/null 2>&1 || true

browser eval '(() => {
  const tray = document.querySelector("#stagedAttachments");
  const chip = tray ? tray.querySelector(".stagedAttachmentChip") : null;
  const meta = chip ? chip.querySelector(".stagedAttachmentMeta") : null;
  const badge = document.querySelector("#attachBadge");
  return {
    trayDisplay: tray ? tray.style.display : null,
    pending: chip ? chip.getAttribute("data-pending") : null,
    fresh: chip ? chip.getAttribute("data-fresh") : null,
    thumb: chip ? Boolean(chip.querySelector("img.stagedAttachmentThumb")) : null,
    thumbSrcPrefix: chip && chip.querySelector("img.stagedAttachmentThumb")
      ? String(chip.querySelector("img.stagedAttachmentThumb").src).slice(0, 5) : null,
    meta: meta ? meta.textContent : null,
    badgeText: badge ? badge.textContent : null,
  };
})()' --json > "$artifacts/staged-report.json" 2>&1 || fail "staged-state evaluation failed"
browser screenshot --full "$artifacts/staged.png" > /dev/null 2>&1 || true

python3 - "$artifacts/pending-report.json" "$artifacts/staged-report.json" "$artifacts/report.json" <<'PY'
import json
import sys

def load(path):
    raw = json.load(open(path, encoding="utf-8"))
    # agent-browser --json wraps as {success, data: {origin, result}, error}.
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        inner = raw["data"]
        if "result" in inner:
            return inner["result"]
        return inner
    return raw

pending = load(sys.argv[1])
staged = load(sys.argv[2])

checks = {
    "pending chip rendered synchronously on paste": pending.get("pending") == "1",
    "pending chip carries a thumbnail": pending.get("thumb") is True,
    "pending chip shows the uploading phase": pending.get("phase") == "uploading",
    "tray visible during upload": pending.get("trayDisplay") == "flex",
    "badge counts the in-flight upload": pending.get("badgeText") == "1",
    "chip swapped to staged state": staged.get("pending") in (None, ""),
    "staged chip keeps the local thumbnail": staged.get("thumb") is True and staged.get("thumbSrcPrefix") == "blob:",
    "staged chip shows the size meta": bool(staged.get("meta")),
    "badge counts the staged attachment": staged.get("badgeText") == "1",
}
report = {"pending": pending, "staged": staged, "checks": checks}
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2)
failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
if failed:
    sys.exit(1)
PY

echo "paste verification artifacts: $artifacts"
