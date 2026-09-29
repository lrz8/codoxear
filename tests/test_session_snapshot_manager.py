"""Manager-level wiring of the session snapshot: discovery trigger, GET/save projection, restore, remap."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from codoxear import server
from codoxear.session_discovery import DiscoveryRegistration
from codoxear.session_discovery import DiscoveryResult
from codoxear.session_snapshot import SessionSnapshotStore
from codoxear.session_snapshot_restore import SnapshotRestoreRequest
from codoxear.session_store import SessionStore


class _FakeVoicePushCoordinator:
    def __init__(self, *args, **kwargs) -> None:
        pass


def _build_manager(tmp_path: Path) -> server.SessionManager:
    with (
        patch.object(server, "SESSION_SNAPSHOT_PATH", tmp_path / "session_snapshot.json"),
        patch.object(server, "SESSION_SNAPSHOT_DEBOUNCE_SECONDS", 0.01),
        patch.object(SessionStore, "load_persistent_state", lambda self: None),
        patch.object(server.SessionManager, "_backfill_recent_cwds_from_logs", lambda self: None),
        patch.object(server.SessionManager, "_discover_existing", lambda self, force=True: None),
        patch.object(server, "VoicePushCoordinator", _FakeVoicePushCoordinator),
        patch("threading.Thread.start", lambda self: None),
    ):
        manager = server.SessionManager()
    # Discovery against the host sock dir must never run from a test; the
    # tests below feed DiscoveryResult values directly.
    manager._discover_existing = lambda force=True: None
    manager._store.queue_store.path = tmp_path / "queues.json"
    manager._store.draft_store.path = tmp_path / "drafts.json"
    manager._store.unattended_store.path = tmp_path / "unattended.json"
    return manager


def _registration(tmp_path: Path, session_id: str, *, broker_pid: int, thread_id: str, effort: str = "high") -> DiscoveryRegistration:
    log_path = tmp_path / f"{thread_id}.jsonl"
    log_path.write_text(json.dumps({"type": "session", "id": thread_id, "cwd": str(tmp_path)}) + "\n", encoding="utf-8")
    sock = tmp_path / f"{session_id}.sock"
    sock.touch()
    sock.with_suffix(".json").write_text(
        json.dumps(
            {
                "session_id": thread_id,
                "broker_pid": broker_pid,
                "codex_pid": broker_pid,
                "cwd": str(tmp_path),
                "log_path": str(log_path),
                "agent_backend": "pi",
                "model_provider": "dexgem",
                "model": "kimi",
                "reasoning_effort": "medium",
                "transport": "tmux",
                "live_run_settings": {"model_provider": "dexgem", "model": "kimi", "reasoning_effort": effort},
            }
        ),
        encoding="utf-8",
    )
    return DiscoveryRegistration(
        session_id=session_id,
        thread_id=thread_id,
        broker_pid=broker_pid,
        codex_pid=broker_pid,
        agent_backend="pi",
        owned=True,
        transport="tmux",
        start_ts=1.0,
        cwd=str(tmp_path),
        log_path=log_path,
        sock_path=sock,
        busy=False,
        queue_len=0,
        token=None,
        meta_log_off=0,
        model_provider="dexgem",
        preferred_auth_method=None,
        model="kimi",
        reasoning_effort=effort,
        service_tier=None,
        tmux_session="codoxear",
        tmux_window=None,
        launch_id=None,
        spawn_nonce=None,
        resume_session_id=None,
        sync_send_supported=True,
        key_write_errors_supported=True,
        interrupted_idle=False,
    )


def _wait_for(predicate, *, timeout_s: float = 3.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_discovery_auto_saves_live_sessions_with_raw_live_settings(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    store = SessionSnapshotStore(tmp_path / "session_snapshot.json")
    live_pid = os.getpid()

    manager._apply_discovery_result(DiscoveryResult(registrations=[_registration(tmp_path, "broker-a", broker_pid=live_pid, thread_id="thread-a", effort="xhigh")]))

    _wait_for(lambda: store.exists())
    entry = store.read().entries["broker-a"]
    assert entry.thread_id == "thread-a"
    assert entry.reasoning_effort == "xhigh"
    assert entry.model == "kimi"
    assert entry.tmux is True

    payload = manager.snapshot_get()
    assert payload["entries"][0]["session_id"] == "broker-a"
    assert payload["entries"][0]["live"] is True


def test_snapshot_get_marks_dead_sessions_not_live_and_save_forces_upsert(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    dead_pid = 2_147_483_600
    manager._apply_discovery_result(DiscoveryResult(registrations=[_registration(tmp_path, "broker-b", broker_pid=dead_pid, thread_id="thread-b")]))
    # The dead broker never gets upserted by the flush (liveness is pid based)...
    time.sleep(0.05)
    assert manager.snapshot_get()["entries"] == []

    # ...but a session that was live at save time is captured immediately.
    live = _registration(tmp_path, "broker-c", broker_pid=os.getpid(), thread_id="thread-c")
    manager._apply_discovery_result(DiscoveryResult(registrations=[live]))
    payload = manager.snapshot_save()
    assert [row["session_id"] for row in payload["entries"]] == ["broker-c"]
    assert payload["entries"][0]["live"] is True

    manager._sessions["broker-c"].broker_pid = dead_pid
    manager._sessions["broker-c"].codex_pid = dead_pid
    assert manager.snapshot_get()["entries"][0]["live"] is False


def test_restore_spawns_through_web_launch_consumes_entry_and_remaps_stores(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    dead_pid = 2_147_483_600
    registration = _registration(tmp_path, "broker-d", broker_pid=os.getpid(), thread_id="thread-d")
    manager._apply_discovery_result(DiscoveryResult(registrations=[registration]))
    manager.snapshot_save()
    session = manager._sessions["broker-d"]
    session.broker_pid = dead_pid
    session.codex_pid = dead_pid
    manager._queues["broker-d"] = [{"id": "q1", "text": "queued", "created_ts": 1.0}]
    manager._drafts["broker-d"] = {"text": "draft", "updated_ts": 1.0}

    spawns: list[dict] = []

    def spawn(self, **kwargs):
        spawns.append(kwargs)
        replacement = _registration(tmp_path, "broker-e", broker_pid=os.getpid(), thread_id="thread-d")
        self._apply_discovery_result(DiscoveryResult(registrations=[replacement]))
        return {"broker_pid": os.getpid()}

    with (
        patch.object(server.SessionManager, "spawn_web_session", spawn),
        patch.object(server, "_tmux_available", lambda: True),
    ):
        out = manager.snapshot_restore(SnapshotRestoreRequest())

    assert out["restored"] == 1
    assert out["results"][0]["new_session_id"] == "broker-e"
    assert spawns[0]["resume_session_id"] == "thread-d"
    assert spawns[0]["create_in_tmux"] is True
    assert spawns[0]["resume_row"]["log_path"] == str(tmp_path / "thread-d.jsonl")
    assert manager._queues == {"broker-e": [{"id": "q1", "text": "queued", "created_ts": 1.0}]}
    assert manager._drafts == {"broker-e": {"text": "draft", "updated_ts": 1.0}}
    assert json.loads((tmp_path / "queues.json").read_text(encoding="utf-8")).keys() == {"broker-e"}

    state = SessionSnapshotStore(tmp_path / "session_snapshot.json").read()
    assert "broker-d" not in state.entries
    assert state.pending_remaps == []
    _wait_for(lambda: "broker-e" in SessionSnapshotStore(tmp_path / "session_snapshot.json").read().entries)


def test_restore_fails_loudly_when_saved_tmux_transport_is_unavailable(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    registration = _registration(tmp_path, "broker-f", broker_pid=os.getpid(), thread_id="thread-f")
    manager._apply_discovery_result(DiscoveryResult(registrations=[registration]))
    manager.snapshot_save()
    manager._sessions.clear()

    with (
        patch.object(server.SessionManager, "spawn_web_session", lambda self, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn"))),
        patch.object(server, "_tmux_available", lambda: False),
    ):
        out = manager.snapshot_restore(SnapshotRestoreRequest())

    assert out["results"] == [{"session_id": "broker-f", "outcome": "failed", "error": "tmux unavailable"}]
    assert "broker-f" in SessionSnapshotStore(tmp_path / "session_snapshot.json").read().entries
