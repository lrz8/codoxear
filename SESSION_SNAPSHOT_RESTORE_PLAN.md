# Plan: Save / restore all codoxear sessions

Feature: snapshot every live session's launch-relevant config (backend, cwd,
model, effort, provider, session/log id, tmux transport) and later respawn all
of them as new sessions bound to the same transcripts via each harness's
resume mechanism.

Explicitly accepted limitation (per request): restore creates **new** sessions
with a resume flag. Background commands, in-flight turn state, scrollback, and
harness-private runtime state are lost. Backend-specific resume quirks are
expected.

Resolved decisions (user-confirmed):
- Queues, drafts, and unattended config are **in scope** (mechanism below —
  they turn out to be already resume-stable; the work is verification +
  key remap, not snapshot payload).
- Auto-save is wanted; it must be **upsert-only** so a null live-session list
  can never overwrite a meaningful snapshot.
- tmux unavailable at restore → fail that entry loudly.
- No per-entry checkboxes; the restore UI shows exactly what will happen and
  then exactly what happened.

## What already exists (reuse, do not rebuild)

- **Sidecar metadata** (`broker_metadata.py::_broker_sidecar_meta`) already
  persists every field the snapshot needs: `agent_backend`, `session_id`,
  `log_path`, `cwd`, `model_provider`, `preferred_auth_method`, `model`,
  `reasoning_effort`, `service_tier`, `live_run_settings`, `transport`,
  `tmux_session`, `tmux_window`, `broker_pid`, `codex_pid`, `owner`.
- **Live-settings authority** (`session_runtime.py::session_run_settings_from_meta`)
  already resolves the *current* model/effort with the correct precedence:
  `live_run_settings` (bridge-reported, wins) > sidecar launch values > log
  replay. The snapshot must reuse this reconciliation, not re-derive it.
- **Resume launch path**: `POST /api/sessions` accepts `resume_session_id`
  together with model/effort/provider and `create_in_tmux`.
  `session_launch_plan.py::prepare_launch_plan` validates the resume target,
  rejects resuming an already-live session, and appends per-backend resume
  args (`agent_backend/*.py::build_resume_args`: codex `resume <id>`, pi
  `--session <log_path>`, cc `--resume <id>`).
- **tmux launch from web**: `create_in_tmux` + server-configured tmux session
  name (`CODEX_WEB_TMUX_SESSION`, default `codoxear`) with fresh window names
  (`session_launcher.py::_launch_tmux_broker`). Restore into tmux is a flag,
  not new machinery.
- **App-dir persistence pattern**: JSON stores under
  `~/.local/share/codoxear/` (queue_store, session_files, etc.) — atomic
  write + 0600 is the established convention.

## Design decisions

### 1. Snapshot file

- Location: `~/.local/share/codoxear/session_snapshot.json` (app dir, 0600,
  atomic tmp+rename write).
- Single latest snapshot; no named/multi snapshots in v1. Overwritten on each
  save.
- Shape:
  ```json
  {
    "version": 1,
    "saved_at": 1736000000.0,
    "entries": [
      {
        "agent_backend": "pi",
        "session_id": "...",
        "log_path": "/home/.../sessions/....jsonl",
        "cwd": "/abs/path",
        "model_provider": "...", "preferred_auth_method": null,
        "model": "...", "reasoning_effort": "...",
        "service_tier": null,
        "tmux": true
      }
    ]
  }
  ```
- Model/effort values are stored in **raw authoritative form**
  (`live_run_settings` when present, else sidecar launch values), not
  display-normalized strings. Launch-time validation/normalization in
  `launch_config.py` then owns the request form, avoiding a display→request
  round-trip mismatch (identified risk: Pi/CC display effort tokens differ
  from request tokens).
- `tmux` is a boolean ("was in tmux"), not the original session/window name.
  Window names are nonce-suffixed at launch and the tmux session name is
  server config, so the original names are neither restorable nor meaningful.

### 2. Save (manual + auto, upsert-only)

The snapshot is a **map keyed by session id**, never a replacement dump of
the current live list. This is the core safety property: an empty live-session
list performs no upserts and writes nothing, so a fresh server instance can
never overwrite a meaningful snapshot with null. Belt-and-braces: the writer
refuses to persist an empty snapshot over a non-empty one.

Triggers (event-driven, debounced ~2s, coalesced):
1. **New live session discovered** → upsert entry.
2. **Config change on a live session** — model/effort/provider change in the
   sidecar `live_run_settings` (the listing path already polls this) → upsert.
3. **Manual "Save all sessions"** → forces the same upsert for all live
   sessions immediately (`POST /api/sessions/snapshot`).
3. **Session delete in the UI** → remove the entry.
4. **Clean broker exit** (Ctrl+D / `/exit` in the TUI → backend exits → the
   broker's teardown unlinks its sock + sidecar JSON, `broker.py`) → remove
   the entry. This is how intentional terminal closes are distinguished from
   crashes: a clean exit erases its own evidence, while kill -9 / power loss
   leaves a stale sidecar with a dead pid, which keeps the entry. Residual
   ambiguity (a deliberate `kill -9` looks like a crash) is unresolvable in
   principle; the escape hatch is session delete in the UI. Sessions closed
   intentionally remain available via the existing New session → resume
   picker.

Session death without clean teardown is **not** a removal trigger —
involuntary death is exactly what the snapshot protects against. The snapshot
therefore always holds "live sessions plus not-yet-restored dead ones".

Entry lifecycle: created/updated while live → persists through death →
consumed on successful restore (removed; the new live session re-upserts
itself on discovery — an update-in-place when resume preserves the id) → or
removed by explicit session delete. Stale entries whose logs are gone surface
as restore failures and are cleared by deleting the lost session row.

Busy sessions are saved (liveness is pid-based).

### 3. Restore

- `POST /api/sessions/snapshot/restore` — no body; restores every
  restorable entry. No per-entry selection (the UI is one click).
  `GET /api/sessions/snapshot` remains only to supply the button's entry
  count and disabled state.
- Per entry, in order:
  1. **Skip if live**: session id live in the registry, or its `log_path` is
     claimed by a live sidecar → outcome `skipped_live`. This makes restore
     idempotent: re-running restores only what is still missing.
  2. **Validate the resume target directly**: `log_path` exists and its
     session meta id matches `session_id`. Do *not* reuse
     `list_resume_candidates_for_cwd` (it scans up to 1000 logs per entry,
     keyed by cwd — wrong shape for a trusted snapshot that already carries
     `log_path`; Pi needs `log_path` in the resume row anyway).
     Implementation: add an optional trusted `resume_row` to
     `LaunchPlanRequest`/`prepare_launch_plan` that bypasses the candidate
     scan but keeps the live-target guard.
  3. **Launch** through the existing `spawn_web_session` path with the
     recorded backend/cwd/model/effort/provider/auth/service_tier and
     `create_in_tmux = entry.tmux`.
  4. **tmux unavailable at restore time**: fail that entry with a clear error
     (`tmux_unavailable`), do not silently fall back to a direct spawn.
     A request-level `transport: "as_saved" | "direct"` option (default
     `as_saved`) lets the user explicitly demote.
- Sequential launches (N is small; tmux launches already synchronize on
  broker metadata). Each entry's outcome is collected; the response is
  `{results: [{session_id, outcome, new_session_id?, error?}]}` with outcomes
  `launched | skipped_live | failed`. HTTP 200 with per-entry detail — bulk
  restore is partially-successful by design.
- Failure surface: missing/corrupt snapshot → 409/400 with reason; missing
  log file or id mismatch → per-entry `failed` with reason. No silent skips.

### 4. Queues, drafts, unattended (in scope — mostly already solved)

These stores already persist in the app dir keyed on **resume-stable
identity**, so they survive broker death without riding in the snapshot:

- Queues (`queue_store.py`) and drafts (`draft_store.py`): keyed by session
  id.
- Unattended (`unattended.py::unattended_config_key`): explicitly "the
  resume-stable storage key" — `thread:<id>` when a thread id exists,
  `log:<path>` otherwise.

All three backends preserve session identity across resume (Pi `--session`
reopens the same file; codex `resume <id>` and claude `--resume <id>`
continue the same thread), so in the common case the keys survive unchanged
and nothing needs to happen. The feature's work here:

1. **Verify per backend** (in Docker) that resume preserves the thread id;
   codex is the uncertain one (new rollout file — same thread id expected).
2. **Key remap**: for any backend where resume changes the identity, remap
   old → new key in the queue, draft, and unattended stores after the new
   sidecar is known.
3. **Mid-send queue items**: left to the existing orphan-recovery /
   commit-unknown machinery (`mark_orphan_recovery_items`), which already
   handles broker death.
4. Restored sessions with unattended enabled resume injecting — that is the
   intent of carrying the config over.

### 5. UI (one contextual row, one click)

- **Placement: a contextual restore row at the top of the session list**,
  rendered only when the snapshot holds entries that are not currently
  live — `⟲ Restore 3 saved sessions`. Rationale: the sidebar footer is a
  deliberate one-line 44px band that already carries Help / Settings /
  Log out at ~95px each; a fourth variable-length label would break it.
  The restore affordance only matters when sessions are missing, so it
  lives where the absence is, including above the "No sessions yet" empty
  state after a reboot. It self-removes as restores drain the snapshot.
  (Fallback if permanent chrome is preferred: icon-only button with count
  badge in the sidebar header actions row, reusing the queue-badge
  pattern.)
- **No Save button.** Auto-save upserts on discovery and config change, so
  the snapshot is always current within the debounce window; a visible Save
  adds no capability. The `POST /api/sessions/snapshot` endpoint exists but
  is trigger-driven. `GET /api/sessions/snapshot` supplies the row's
  count/visibility (entries minus currently-live ones), refreshed with the
  session list.
- **Click → restore runs immediately.** No dialog, no preview, no confirm —
  restore is idempotent and skips live sessions, so an accidental click is
  harmless. Restored sessions appear as sidebar cards on the next refresh
  (the list itself is the success evidence), followed by one toast:
  `Restored 5 · skipped 1 (running) · failed 1 — tmux unavailable`.
  Failure reasons are one line each in the toast; full detail lands in the
  existing diagnostics surface.
- Keyboard: the row is an activatable control and participates in hint
  mode like other controls.
- Follows existing constraints: per-controller `select()` wiring through
  `app_wiring.js` (must pass `scripts/check_wiring.py`), store-driven
  rendering, no new media-query behavior.

### 6. Explicitly out of scope (v1)

- Restoring original tmux window names / pane layout.
- Snapshot history, named snapshots.
- Aliases and sidebar order (display preferences, not session config).
- Restoring sessions whose broker is alive (they are skipped, not
  restarted).
- Auto-restore on server start (surprise process spawns; restore stays an
  explicit user action).

## New/changed code map

| File | Change |
|---|---|
| `codoxear/session_snapshot.py` (new) | Upsert/remove entries from discovery+sidecars; validate; atomic store load/save; server-side projected-action annotation for the restore dialog |
| `codoxear/session_snapshot_routes.py` (new) or `session_routes.py` | `POST /api/sessions/snapshot`, `GET /api/sessions/snapshot`, `POST /api/sessions/snapshot/restore` |
| `codoxear/session_launch_plan.py` | Optional trusted `resume_row` bypassing the cwd candidate scan (live guard kept) |
| Auto-save hook | Upsert triggers on session discovery + `live_run_settings` change; entry removal on session delete and successful restore |
| Queue/draft/unattended | Key remap old→new on restore where backend identity changed |
| `codoxear/static/app_session_snapshot.js` (new) | Sidebar controls, restore dialog (projected actions → outcomes) |
| `app_wiring.js` / composition | Wire the controller via `select()` contracts |
| `tests/` | Behavioral tests (below) |
| `AGENTS.md` | Document the snapshot contract + reconciliation rule |

## Verification plan (behavioral, per testing policy)

- **Unit (Python, direct calls)**: upsert semantics (death never removes,
  empty never overwrites, delete removes); snapshot build from fabricated
  sidecar dir (live vs dead pid filtering, live_run_settings precedence);
  store round-trip/atomicity; restore orchestrator with a fake launcher —
  `skipped_live`, missing-log failure, tmux-unavailable failure, per-entry
  outcome aggregation, snapshot-entry consumption on success, queue/draft/
  unattended key remap; `resume_row` trust path in `prepare_launch_plan`.
- **Docker end-to-end** (`scripts/docker_verify.sh` pattern, never the live
  deployment): start server, spawn one Pi and one Codex session (one in
  tmux) with queued items, a draft, and unattended config; save snapshot;
  kill the broker processes; restore; assert new sessions appear bound to
  the resumed transcripts with the recorded model/effort/transport **and
  that queue, draft, and unattended config carried over** (this is where
  per-backend thread-id preservation is proven).
- **UI**: agent-browser in Docker — restore row visibility/count against
  live sessions, one-click restore, sidebar cards appearing, outcome toast,
  row disappearing when the snapshot drains.

## Open questions

None blocking. (Auto-save trigger, clean-exit vs crash distinction, tmux
failure, queues/drafts/unattended scope, and one-click UI resolved with the
user.)
