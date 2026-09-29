# Epistemic model — session snapshot/restore (FINAL)

## Shipped design (all verified, see OPS)
- Snapshot at <app dir>/session_snapshot.json, keyed by transcript identity
  (agent_backend, thread_id); registry id (broker-<pid> sock stem) is a
  mutable attribute; (session_id, start_ts) names one broker incarnation
  (pid-reuse protection).
- Upsert-only writes; three supersede rules (same transcript new registry id
  → remap fires + restoring mark clears; same incarnation new transcript →
  /new moved on; new thread declaring stored thread as resume target).
  Empty-over-nonempty upsert refused.
- Auto-save: discovery signature diff → debounced flush (bounded retry).
  Removal only: UI delete, DELETE dismiss endpoint, clean broker exit
  (broker unlinks sock+sidecar ONLY on agent exit 0), restore finding the
  transcript permanently gone.
- Restore: trusted resume_row (log-derived, live guard kept), restoring marks
  (10-min expiry, skipped_restoring), per-entry error isolation, tmux-missing
  fails loudly, cwd-missing pre-check. Remap covers queue, draft, unattended,
  alias, UI state, dependency refs — at supersede, restart-safe.
- Queue sweep exempts snapshot-referenced ids (reboot survival).
- UI: contextual dashed row in own lead host above #sessions (never inside —
  innerHTML wipe); feed = liveness key change OR 60s cadence, evaluated only
  on catalog generations.
- pid_alive treats zombies as dead (c026f2b5) — was the root cause of state
  wipes after broker crashes.

## Residual accepted risks
- Server restart loses the clean-exit signal for exits while down (entry
  persists; user dismisses).
- Codex resume thread-id preservation verified only via Pi e2e; codex/cc
  resume paths are mechanism-tested, not Docker-verified with real backends.
- Recovery-flagged queues move to the restored session and block its queue
  until reviewed (safe direction).
- 21c prune double-record race remains open (pre-existing, cosmetic).
