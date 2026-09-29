from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from codoxear.session_lifecycle import SessionLifecycleCoordinator
from codoxear.session_model import Session
from codoxear.session_prune import SessionPruneCoordinator
from codoxear.session_snapshot import SessionSnapshotStore
from codoxear.session_snapshot import snapshot_path
from codoxear.session_snapshot_runtime import SessionSnapshotCoordinator
from codoxear.sidecar_metadata import read_metadata


def _coordinator(tmp_path: Path, sessions: dict[str, Session], alive: set[int], lock: threading.Lock) -> SessionSnapshotCoordinator:
    return SessionSnapshotCoordinator(
        store=SessionSnapshotStore(snapshot_path(tmp_path), now=lambda: 1.0),
        lock=lock,
        sessions=lambda: sessions,
        session_store=lambda: SimpleNamespace(queues={}, drafts={}, unattended={}),
        save_queues=lambda: None,
        save_drafts=lambda: None,
        save_unattended=lambda: None,
        read_sidecar_metadata=lambda meta_path: read_metadata(meta_path, sock=meta_path.with_suffix(".sock")),
        snapshot_run_settings=lambda meta, log_path, backend: (meta.get("model_provider"), None, meta.get("model"), meta.get("reasoning_effort")),
        live_session_for_resume_target=lambda _resume_id, _row: None,
        pid_alive=lambda pid: int(pid) in alive,
        now=lambda: 1.0,
        timer_factory=lambda _delay, fn: SimpleNamespace(start=fn, cancel=lambda: None),
    )


def _live_session(tmp_path: Path, session_id: str, *, broker_pid: int) -> Session:
    log_path = tmp_path / f"{session_id}.jsonl"
    log_path.write_text("{}\n", encoding="utf-8")
    sock = tmp_path / f"{session_id}.sock"
    sock.touch()
    sock.with_suffix(".json").write_text(
        json.dumps({"session_id": f"thread-{session_id}", "broker_pid": broker_pid, "codex_pid": broker_pid, "cwd": "/repo", "log_path": str(log_path), "agent_backend": "pi", "model": "kimi", "reasoning_effort": "high", "model_provider": "dexgem"}),
        encoding="utf-8",
    )
    return Session(
        session_id=session_id,
        thread_id=f"thread-{session_id}",
        broker_pid=broker_pid,
        codex_pid=broker_pid,
        agent_backend="pi",
        owned=True,
        start_ts=1.0,
        cwd="/repo",
        log_path=log_path,
        sock_path=sock,
    )


def _prune(sessions: dict[str, Session], *, alive: set[int], lock: threading.Lock, coordinator: SessionSnapshotCoordinator, sock_call) -> SessionPruneCoordinator:
    return SessionPruneCoordinator(
        lock=lock,
        sessions=lambda: sessions,
        sock_call=sock_call,
        broker_busy_queue_from_state=lambda _state: (False, 0),
        broker_interrupted_idle_from_state=lambda _state: False,
        sock_error_definitely_stale=lambda error: isinstance(error, ConnectionRefusedError),
        pid_alive=lambda pid: int(pid) in alive,
        latest_launch_attempt=lambda _launch_id: None,
        submitted_user_messages=lambda _record: [],
        launch_failure_tail=lambda _record: "",
        which_tmux=lambda _name: None,
        tmux_pane_snapshot=lambda *_args, **_kwargs: {},
        clean_optional_text=lambda value: value if isinstance(value, str) else None,
        record_launch_attempt=lambda _record: None,
        clear_deleted_session_state=lambda _session_id: None,
        unlink_quiet=lambda path: path.unlink(missing_ok=True),
        session_sock_vanished=coordinator.session_sock_vanished,
    )


def test_prune_drops_entry_when_broker_unlinked_its_socket_but_keeps_crashed_sessions(tmp_path: Path) -> None:
    lock = threading.Lock()
    sessions: dict[str, Session] = {}
    alive: set[int] = set()
    coordinator = _coordinator(tmp_path, sessions, alive, lock)
    clean = _live_session(tmp_path, "broker-1", broker_pid=1)
    crashed = _live_session(tmp_path, "broker-2", broker_pid=2)
    sessions.update({clean.session_id: clean, crashed.session_id: crashed})
    alive.update({1, 2})
    coordinator.save_now()
    assert set(coordinator.read().entries) == {"broker-1", "broker-2"}

    # Clean exit: the broker removed its own sock + sidecar.
    clean.sock_path.unlink()
    clean.sock_path.with_suffix(".json").unlink()
    alive.discard(1)
    # Crash: files remain, control socket refuses, broker pid dead.
    alive.discard(2)

    def sock_call(sock_path, _req, **_kwargs):
        raise ConnectionRefusedError("stale")

    _prune(sessions, alive=alive, lock=lock, coordinator=coordinator, sock_call=sock_call).prune_dead_sessions()

    assert set(coordinator.read().entries) == {"broker-2"}
    assert "broker-1" not in sessions
    assert sessions["broker-2"].lost is True


def test_prune_of_a_dead_broker_with_a_surviving_socket_file_keeps_the_entry(tmp_path: Path) -> None:
    lock = threading.Lock()
    sessions: dict[str, Session] = {}
    alive: set[int] = set()
    coordinator = _coordinator(tmp_path, sessions, alive, lock)
    session = _live_session(tmp_path, "broker-3", broker_pid=3)
    sessions[session.session_id] = session
    alive.add(3)
    coordinator.save_now()
    alive.discard(3)

    def sock_call(sock_path, _req, **_kwargs):
        raise TimeoutError("hung")

    _prune(sessions, alive=alive, lock=lock, coordinator=coordinator, sock_call=sock_call).prune_dead_sessions()

    assert "broker-3" not in sessions
    assert set(coordinator.read().entries) == {"broker-3"}


def test_delete_removes_entry_for_live_and_recovery_rows(tmp_path: Path) -> None:
    lock = threading.Lock()
    sessions: dict[str, Session] = {}
    alive: set[int] = set()
    coordinator = _coordinator(tmp_path, sessions, alive, lock)
    session = _live_session(tmp_path, "broker-4", broker_pid=4)
    sessions[session.session_id] = session
    alive.add(4)
    coordinator.save_now()
    coordinator.store.upsert([coordinator.entry_for_session(_live_session(tmp_path, "broker-5", broker_pid=5))])
    assert set(coordinator.read().entries) == {"broker-4", "broker-5"}

    lifecycle = SessionLifecycleCoordinator(
        lock=lock,
        sessions=lambda: sessions,
        sock_call=lambda _sock, _req, **_kwargs: {"ok": True},
        process_group_alive=lambda _pid: False,
        pid_alive=lambda pid: int(pid) in alive,
        terminate_process_group=lambda *_a, **_kw: True,
        terminate_process=lambda *_a, **_kw: True,
        unlink_quiet=lambda path: path.unlink(missing_ok=True),
        commit_unknown_sends=lambda: {"broker-5": {"created_ts": 1.0}},
        queue_has_recovery_items_locked=lambda _sid: False,
        clear_deleted_session_state=lambda *_a, **_kw: None,
        read_launch_attempts=lambda: [],
        launch_attempt_row=lambda _record: None,
        hide_session=lambda _sid: None,
        session_deleted=coordinator.session_deleted,
    )

    assert lifecycle.delete_session("broker-4") is True
    assert set(coordinator.read().entries) == {"broker-5"}
    assert lifecycle.delete_session("broker-5") is True
    assert coordinator.read().entries == {}
    assert lifecycle.delete_session("unknown") is False
