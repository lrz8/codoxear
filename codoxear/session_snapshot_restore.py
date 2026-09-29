"""Restore-all orchestrator: respawn snapshot entries through the web launch path.

Sequential, per-entry, partially-successful by design. Outcomes:

* ``skipped_restoring`` — a previous restore launched a replacement for this
  entry and it has not bound its transcript yet (unexpired restoring mark).
* ``skipped_live`` — the entry's transcript is currently live (same broker
  incarnation, bound thread, declared resume target, held log, or a live
  broker in the entry's own sidecar); restoring would bind a second session
  to the same transcript.
* ``failed`` — the recorded working directory is gone (never recreated), the
  transcript log is gone (the entry is dropped: it can never be restored),
  tmux was requested but is unavailable (no silent fallback to a direct
  spawn), the recorded settings no longer validate, or the launch itself
  failed. Any unexpected error is also a per-entry failure: one entry never
  prevents later entries from being attempted.
* ``launched`` — the replacement broker started. The entry is *not*
  consumed: it is stamped ``restoring`` and stays under its old registry id
  until the replacement's own discovery upsert supersedes it, which is the
  moment queue/draft/alias/... follow the new registry id.

Re-running is idempotent: restoring and live entries are skipped. Concurrent
calls serialize on one lock so two restores can never both spawn the same
entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Callable

from .session_snapshot import SnapshotCorrupt
from .session_snapshot import SnapshotEntry
from .session_snapshot import SnapshotState


RESTORE_TRANSPORTS = ("as_saved", "direct")


class SnapshotRestoreUnavailable(ValueError):
    """No usable snapshot to restore from (missing or corrupt)."""


@dataclass(frozen=True)
class SnapshotRestoreRequest:
    transport: str = "as_saved"
    include: tuple[str, ...] | None = None


def parse_restore_request(obj: dict[str, Any] | None) -> SnapshotRestoreRequest:
    body = obj or {}
    transport_raw = body.get("transport", "as_saved")
    if not isinstance(transport_raw, str) or transport_raw not in RESTORE_TRANSPORTS:
        raise ValueError(f"transport must be one of {', '.join(RESTORE_TRANSPORTS)}")
    include_raw = body.get("include")
    include: tuple[str, ...] | None
    if include_raw is None:
        include = None
    elif isinstance(include_raw, list) and all(isinstance(item, str) and item.strip() for item in include_raw):
        include = tuple(item.strip() for item in include_raw)
    else:
        raise ValueError("include must be a list of non-empty session or thread ids")
    return SnapshotRestoreRequest(transport=transport_raw, include=include)


@dataclass(frozen=True)
class SnapshotRestoreDeps:
    read_snapshot: Callable[[], SnapshotState]
    live_session_id_for_entry: Callable[[SnapshotEntry], str | None]
    restoring_active: Callable[[SnapshotEntry], bool]
    parse_launch_request: Callable[[dict[str, Any]], Any]
    spawn_web_session: Callable[..., dict[str, Any]]
    tmux_available: Callable[[], bool]
    session_id_for_broker_pid: Callable[[int], str | None]
    mark_restoring: Callable[..., bool]
    drop_entry: Callable[[SnapshotEntry], bool]
    launch_error: type[BaseException]
    lock: threading.Lock


def _launch_request_body(entry: SnapshotEntry, *, create_in_tmux: bool) -> dict[str, Any]:
    return {
        "cwd": entry.cwd,
        "agent_backend": entry.agent_backend,
        "resume_session_id": entry.thread_id,
        "model_provider": entry.model_provider,
        "preferred_auth_method": entry.preferred_auth_method,
        "model": entry.model,
        "reasoning_effort": entry.reasoning_effort,
        "service_tier": entry.service_tier,
        "create_in_tmux": create_in_tmux,
    }


def _row(entry: SnapshotEntry, outcome: str, **extra: Any) -> dict[str, Any]:
    return {"session_id": entry.session_id, "thread_id": entry.thread_id, "outcome": outcome, **extra}


def _failed(entry: SnapshotEntry, error: str) -> dict[str, Any]:
    return _row(entry, "failed", error=error)


def _restore_entry(entry: SnapshotEntry, *, transport: str, deps: SnapshotRestoreDeps) -> dict[str, Any]:
    if deps.restoring_active(entry):
        return _row(entry, "skipped_restoring")
    live_id = deps.live_session_id_for_entry(entry)
    if live_id is not None:
        return _row(entry, "skipped_live", live_session_id=live_id)
    log_path = Path(entry.log_path).expanduser()
    if not log_path.is_file():
        deps.drop_entry(entry)
        return _failed(entry, f"session log missing: {log_path}; entry dropped")
    cwd = Path(entry.cwd).expanduser()
    if not cwd.is_dir():
        return _failed(entry, f"cwd missing: {cwd}")
    create_in_tmux = bool(entry.tmux) and transport == "as_saved"
    if create_in_tmux and not deps.tmux_available():
        return _failed(entry, "tmux unavailable")
    try:
        launch_req = deps.parse_launch_request(_launch_request_body(entry, create_in_tmux=create_in_tmux))
    except ValueError as exc:
        return _failed(entry, f"recorded launch settings are no longer valid: {exc}")
    try:
        result = deps.spawn_web_session(
            cwd=launch_req.cwd,
            args=launch_req.args,
            agent_backend=launch_req.agent_backend,
            resume_session_id=launch_req.resume_session_id,
            worktree_branch=launch_req.worktree_branch,
            model_provider=launch_req.model_provider,
            preferred_auth_method=launch_req.preferred_auth_method,
            model=launch_req.model,
            reasoning_effort=launch_req.reasoning_effort,
            service_tier=launch_req.service_tier,
            create_in_tmux=launch_req.create_in_tmux,
            resume_row={
                "session_id": entry.thread_id,
                "log_path": entry.log_path,
                "cwd": entry.cwd,
                "agent_backend": entry.agent_backend,
            },
        )
    except ValueError as exc:
        return _failed(entry, str(exc))
    except deps.launch_error as exc:
        return _failed(entry, str(exc))
    broker_pid_raw = result.get("broker_pid") if isinstance(result, dict) else None
    broker_pid = int(broker_pid_raw) if isinstance(broker_pid_raw, int) and not isinstance(broker_pid_raw, bool) else None
    deps.mark_restoring(entry, broker_pid=broker_pid)
    new_session_id = deps.session_id_for_broker_pid(broker_pid) if broker_pid is not None else None
    out = _row(entry, "launched")
    if new_session_id is not None:
        out["new_session_id"] = new_session_id
    if broker_pid is not None:
        out["broker_pid"] = broker_pid
    if isinstance(result, dict):
        if result.get("pending") is True:
            out["pending"] = True
        if isinstance(result.get("launch_id"), str):
            out["launch_id"] = result["launch_id"]
    return out


def _restore_entry_isolated(entry: SnapshotEntry, *, transport: str, deps: SnapshotRestoreDeps) -> dict[str, Any]:
    try:
        return _restore_entry(entry, transport=transport, deps=deps)
    except Exception as exc:  # noqa: BLE001 - one entry's surprise must not abort the batch
        return _failed(entry, f"unexpected error: {type(exc).__name__}: {exc}")


def restore_snapshot(request: SnapshotRestoreRequest, *, deps: SnapshotRestoreDeps) -> dict[str, Any]:
    with deps.lock:
        try:
            state = deps.read_snapshot()
        except SnapshotCorrupt as exc:
            raise SnapshotRestoreUnavailable(f"session snapshot is unreadable: {exc}") from exc
        if not state.entries:
            raise SnapshotRestoreUnavailable("no saved sessions to restore")
        selected = list(state.entries.values())
        if request.include is not None:
            wanted = set(request.include)
            selected = [entry for entry in selected if entry.session_id in wanted or entry.thread_id in wanted]
        results = [_restore_entry_isolated(entry, transport=request.transport, deps=deps) for entry in selected]
    counts = {"launched": 0, "skipped_live": 0, "skipped_restoring": 0, "failed": 0}
    for row in results:
        counts[str(row["outcome"])] += 1
    return {
        "results": results,
        "restored": counts["launched"],
        "skipped": counts["skipped_live"] + counts["skipped_restoring"],
        "skipped_restoring": counts["skipped_restoring"],
        "failed": counts["failed"],
    }
