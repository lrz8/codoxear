# OPS — immutable evidence trail

## 2026-09-29 — planning phase
- Read broker_metadata.py, session_resume.py, session_launch_plan.py,
  session_runtime.py, session_launcher.py, unattended.py, draft_store.py,
  queue_store.py, tmux_runtime.py, app_shell.js, app_sessions.js, app.css.
  Findings recorded in EPISTEMIC.md.
- Plan written to SESSION_SNAPSHOT_RESTORE_PLAN.md; iterated with user over
  3 review rounds (auto-save upsert-only semantics; queues/drafts/unattended
  in scope via resume-stable keys; tmux fail-loud; one-click contextual row
  UI; clean-exit vs crash distinction via sidecar unlink).
- UI mock rendered with real app.css + themes via local http.server (pid
  3002541, killed after) + agent-browser headless; screenshot
  /tmp/restore_mock.png shown to user; design approved ("ok, implement").
- Plan committed: 3c41b446.

## 2026-09-29 — implementation dispatch
- Backend executor dispatched async (run 64cdb528-629e-457a-93dd-3ca0140bc8e3,
  model gpt-6-astra, worktree). Owns codoxear/**/*.py + python tests.
- Frontend executor dispatched async (run f6b3c8da-b43a-470d-8562-b636f4215716,
  model gpt-5.6-terra, worktree). Owns codoxear/static/** + JS tests.
- API contract frozen in both prompts (see PROMPT.md).
- Both instructed: no AGENTS.md edits; behavioral tests only; commit in
  worktree.

## 2026-09-29 — dispatch failure + relaunch
- Both initial executor runs (64cdb528 backend/gpt-6-astra, f6b3c8da
  frontend/gpt-5.6-terra) failed ~20s after launch with identical provider
  error: 400 session_history_handoff_required on dexgem-responses. No
  worktrees or artifacts produced; failure is infrastructure, not task.
- Action: switched provider family, relaunched fresh (not resumed — the error
  explicitly rejects provider-state resume).
  - Backend: run ac600e72-0c4b-475d-b4d9-fe53bcbfae4a, claude-fable-5-1.
  - Frontend: run 2d79f824-3ee8-4c27-8237-483d3c330921, claude-fable-5.
  Same task specs, same frozen API contract.

## 2026-09-29 — frontend complete
- Run 2d79f824 completed. Worktree /tmp/codoxear-snapshot-frontend, branch
  snapshot-restore-frontend, commits e9c8e3e9 (impl) + 23499413 (tests).
- 8 VM tests + 4 parsed-cascade CSS tests + wiring contract test; wiring
  guard passes; browser-verified against fake API (screenshots
  /tmp/snapshot-ui-fake/*.png).
- Deviations accepted after diff review: row is sibling before #sessions
  (innerHTML wipe constraint — pinned above scroll area, spacing preserved
  via sibling selector); feed refetches only on liveness-key change (avoids
  doubling poll traffic); ::after 44px hit lane added.
- Main-agent diff review of e9c8e3e9: module respects widget rule +
  state-authority (own store, own render, documented feed reconciliation);
  wiring is a clean 11-key select contract; CSS token-only. No issues found.

## 2026-09-29 — backend complete (main checkout, branch main)
- Commits on main: ae34ba21 (store/coordinator/restore/routes + unit tests),
  a295dab0 (launch plan trusted resume_row), 9439657d (server wiring, hooks,
  manager/trigger tests). Full suite: 2048 passed.
- Docker e2e (/tmp/codoxear-snapshot-e2e.sh, verify.Dockerfile, --init):
  auto-save within debounce → force save → restore-while-live skipped_live →
  SIGKILL broker → entry kept live=false (lost tombstone) → restore launched
  new_session_id broker-123 → queue item + draft present on broker-123, old
  tombstone empty, snapshot entry re-keyed to broker-123, pending_remaps [] →
  SIGTERM pi child (broker teardown unlinks) → entries [] → restore 409.
- Anomaly (pre-existing): pid_alive() is zombie-blind (os.kill(pid,0)). When a
  crashed broker's parent never reaps it (first harness runs: broker was a
  child of the server via bash exec), prune takes the "definitely stale +
  broker alive" branch → session pruned as dead → clear_deleted_session_state
  wipes queue/draft (snapshot entry survives; restore works but has nothing
  to remap). Production parents (tmux pane shell, server Popen reaper, init
  after server restart) reap, so the lost-tombstone path applies there.

## 2026-09-29 — backend complete, merged, docs
- Backend run ac600e72 completed; committed directly to main (did not use a
  worktree): ae34ba21 (store+runtime+orchestrator+routes+51 tests),
  a295dab0 (trusted resume_row), 9439657d (server wiring).
- Key correction to my model: registry session id = sock stem (broker-<pid>),
  NOT the backend thread id. Queue/draft keys therefore always change on
  restore; remap is the common path. Entries carry session_id + thread_id +
  log_path; upsert supersedes stale entries on the same transcript.
- Backend agent ran its own Docker e2e (real broker+Pi): auto-save, skip-live,
  SIGKILL keeps entry, restore launched with queue+draft remap, clean SIGTERM
  removes entries, 409 on empty. 
- Anomaly recorded in ISSUES_TRACKER (#21b): pid_alive zombie-blindness can
  prune a crashed broker as dead and wipe queue/draft when the broker's
  parent never reaps it.
- Merged frontend branch snapshot-restore-frontend into main: fd7b0e49.
  Clean (disjoint files).
- Full suite on merged tree: 2060 passed, 112 subtests. Wiring contract test
  included.
- AGENTS.md: added "Session snapshot (save/restore-all)" bullet under
  frontend/runtime state.
- Dispatched critic (opus-5-5, run 0fba8c77) for design-level audit and
  executor (fable-5, run 9a79f680) for combined-stack Docker e2e with the
  real frontend.

## 2026-09-29 — combined-stack Docker e2e (real server + rebuilt bundle + real broker + real Pi + real browser)
- Script: /tmp/codoxear-snapshot-ui-e2e.sh (git archive of fd7b0e49 + esbuild-rebuilt
  bundle in the build context; verify.Dockerfile; --init; port 19647; agent-browser
  headless). Final run artifacts: /tmp/codoxear-snapshot-ui-results.PGagnc
  (04-restore-row-visible.png is the row screenshot; 05b/05c the post-restore state).
- The committed dist/app.bundle.js at fd7b0e49 does NOT contain the restore row
  (bundle is a derived artifact rebuilt by deploy.sh); docker_verify.sh builds from
  the git archive without rebuilding, so it cannot see this feature.
- All 7 steps PASS: row hidden while healthy → UI-queued item + UI draft → SIGKILL
  broker → "Restore 1 saved session" row → click → toast "Restored 1 session", new
  session broker-208 with old transcript, draft in composer, queue 1 badge + item in
  viewer, row hidden → session B auto-saved then removed on clean exit → all-live
  restore 200 {restored 0, skipped 1} → empty snapshot restore 409
  "no saved sessions to restore". Browser page errors [] / console [].
- Scenario lesson: enqueue on an idle, ready session promotes the head immediately
  (session_queue.enqueue, require_idle_grace=False) — the first UI-queued item was
  sent to offline Pi in run 1. Queue two items; the second is held (then idle grace).
- Anomaly (harness + pre-existing, not snapshot): a broker started with
  CODEX_WEB_OWNER=web but no server launch id whose Pi exits with a non-final log
  tail yields 3 "failed" cards: broker's own agent_exit_after_log_bind record + two
  session_pruned_after_log_bind records minted in the same ms by concurrent prune
  passes (each with a fresh launch_id). A real web-launched session (restored
  broker-208, SIGTERM'd Pi, status 143) produced exactly one record.

## 2026-09-29 — combined-stack Docker e2e PASS
- Run 9a79f680 completed: real server + esbuild-rebuilt bundle + real broker
  + real Pi + headless Chromium against the merged tree (fd7b0e49).
  Script /tmp/codoxear-snapshot-ui-e2e.sh; artifacts
  /tmp/codoxear-snapshot-ui-results.PGagnc/.
- All 7 steps pass: row hidden while healthy; SIGKILL → row "Restore 1 saved
  session" with lost tombstone; click → new broker same thread, transcript
  marker ALPHA-7731 visible, queue item + draft present on NEW session id,
  snapshot re-keyed, pending_remaps empty; SIGTERM clean exit → entry removed;
  all-live restore → 200 all skipped_live; drained → 409.
- Browser errors/console: none.
- Main agent visually verified 04-restore-row-visible.png (row + lost card +
  draft) and 05b (resumed transcript + restored draft on new session).
- Findings: (1) committed dist bundle lacks the feature — deploy.sh rebuilds,
  but docker_verify.sh builds from git archive without rebuilding (known
  pattern, docker_draft_verify.sh rebuilds the same way); (2) queue head
  auto-promotes when idle — shaped the scenario, not a bug; (3) PRE-EXISTING
  race: two concurrent prune_dead_sessions passes mint duplicate
  launch-failure records for one cleanly-exited session.

## 2026-09-29 — critic audit (run 0fba8c77, opus-5-5)
- Verdict: store/locking/supersede sound; lifecycle rules unsafe in two core
  scenarios. 13 findings, repros in /tmp/snapreview/.
- HIGH 1: host reboot loses queues (dead broker never registers after
  restart → queue sweep drops its queue before restore). My e2e kept the
  server up, so it never saw this.
- HIGH 2: broker teardown unlinks sock+sidecar unconditionally → agent crash
  (OOM, exit 134) looks like a clean exit and deletes the entry. My "clean
  exit ⇔ agent exited" premise was false.
- MEDIUM: pid-reuse collision on broker-<pid> keys; consume-at-launch leaves
  transcript unprotected until log bind; stuck entries can't be dismissed
  after restart; restore liveness gap can double-bind a transcript.
- Fix design (mine, in the dispatch specs): rekey entries by
  (backend, thread_id); registry id becomes an attribute; remap fires at
  upsert-supersede instead of consume-at-launch; restoring marker with 10-min
  expiry replaces pending_remaps if coverage allows; broker unlinks sidecar
  only on exit_code==0; queue sweep exempts snapshot-referenced ids;
  dismiss endpoint; cwd-missing pre-check; flush retry; per-entry error
  isolation; extend remap to alias/UI-state/dependency refs.
- Fix executors dispatched: backend 0cb864ba (fable-5-1), frontend
  7f58d097 (fable-5, feed cadence + full failure toast).

## 2026-09-29 — frontend fix round complete
- Run 7f58d097 completed on main: 8028b2fb (60s cadence safety net evaluated
  inside the catalog subscription; answered-404 closes the window, transport
  loss retries; injected clock) + 2042003c (toast joins ALL failure reasons
  with " ; ").
- VM suite + CSS + wiring guard: 239 passed.
- Follow-up noted: AGENTS.md snapshot bullet now stale re feed trigger
  (key change OR >60s cadence) — main agent to fix in docs pass.

## 2026-09-29 — backend fix run killed mid-refactor
- Run 0cb864ba (fable-5-1) died on a spurious provider content-block ("ToS
  reverse-engineering" false positive) at 52 turns, mid-rewrite, on MAIN (no
  worktree). Tree left uncommitted and broken (PendingRemap removed from
  session_snapshot.py while tests still import it; 10 source files touched).
- Decision: fresh completion run rather than resume (the block lives in the
  old provider session). Dispatched 3c1af81c (fable-5, proven reliable this
  session) to assess/keep/complete the partial state against the full fix
  spec, then commit atomically.
- Completion attempt 3c1af81c (fable-5) failed with broken tool routing
  ("Tool not found" for every tool); agent honestly reported zero changes.
  Tree unchanged. Retried as 86f75b62 with kimi-k3.

## 2026-09-29 — fix round complete (kimi-k3, run 86f75b62)
- Killed agent's partial production rewrite was coherent; kept near-verbatim.
  Completion: ported 5 test files, extracted broker gate to
  _unlink_clean_exit_evidence, finished pending_remaps removal
  (remap-at-supersede proven restart-safe).
- Commits: 52dde455 (broker exit-0 unlink gate), 0fe379e8 (transcript-keyed
  entries, restoring marks, remap-at-supersede, all 10 findings).
- Final design improvement beyond my spec: entries keyed (backend,
  thread_id) + start_ts incarnation pair; THREE supersede rules incl.
  resume-target rule for harnesses continuing transcripts under new ids.
- Full suite: 2091 passed (from 2060).
- AGENTS.md snapshot bullet rewritten to final design; committed 5018d684
  (with ISSUES_TRACKER updates).
- Final e2e dispatched (a74d8337, fable-5): reprise core flow + REBOOT
  scenario (server restart between kill and restore — queue/draft must
  survive) + agent-crash-keeps-entry + restoring-mark double-click + dismiss
  endpoint.
- Final e2e attempt a74d8337 (fable-5) failed at dispatch with the same
  broken tool routing as 3c1af81c (all tools "Tool not found"; zero actions
  taken, nothing fabricated). Retried as d9beed71 with kimi-k3.
- Infra failure taxonomy for user issue report (evidence gathered):
  (1) dexgem-responses 400 session_history_handoff_required on fresh launches;
  (2) dexgem-messages fable-5-1 mid-run ToS content-block false positive;
  (3) dexgem-messages fable-5 ×2: model emitted Claude Code tool schema
  (Read/Bash/ToolSearch with file_path) instead of pi's declared tools →
  dispatcher "Tool Read not found". Proof: responseId
  msg_011CfXXSV4dSjG6QPzxWBsPt in session 04f074ea. Indicates gateway
  dropped/rewrote tool declarations or routed to a CC-shaped serving config.

## 2026-09-29 — final e2e PASS + zombie fix
- Run d9beed71 (kimi-k3): all 5 scenarios PASS at 5018d684 in Docker
  (--init container, rebuilt bundle, headless Chromium). Artifacts
  /tmp/codoxear-snapshot-final-results.qES1Wz/ and s2clean/s2redo dirs.
  S1 core flow; S2 REBOOT (server restart between SIGKILL and restore —
  queue/draft survive, remap lands on new session); S3 agent SIGKILL →
  broker nonzero exit → sidecar kept, lost tombstone, entry kept; S4
  restoring mark → second restore = skipped_restoring, one broker; S5
  dismiss endpoint.
- E2e surfaced live evidence for 21b: remap moved queue/draft onto a
  replacement broker that then crashed; zombie-blind prune dead-branch wiped
  the just-moved state. Fixed at root: process_runtime.pid_alive now treats
  /proc state Z as dead (c026f2b5), behavioral fork test. Suite 2092 green.
- ISSUES_TRACKER 21b CLOSED (25f915ce). 21c (prune double-record race)
  remains NOT-CLOSED, out of scope.
