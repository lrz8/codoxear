"""Manager-level wiring of the session snapshot: discovery trigger, GET/save projection, restore, remap."""

from __future__ import annotations

import contextlib
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


# Every persisted-state path the manager can write, redirected into tmp_path.
# The manager's store is reused only while ``store.paths`` equals the server's
# path constants (rebuilt otherwise), so the constants must stay patched for
# the manager's whole lifetime — hence a context manager, not
# construction-time patching.
_STORE_PATH_CONSTANTS = (
    "QUEUE_PATH",
    "DRAFT_PATH",
    "UNATTENDED_PATH",
    "ALIAS_PATH",
    "SIDEBAR_META_PATH",
    "HIDDEN_SESSIONS_PATH",
    "FILE_HISTORY_PATH",
    "PENDING_ATTACHMENTS_PATH",
    "STAGED_ATTACHMENTS_PATH",
    "COMMIT_UNKNOWN_SENDS_PATH",
    "RECENT_CWD_PATH",
    "SESSION_UNREAD_PATH",
)


@contextlib.contextmanager
def _build_manager(tmp_path: Path):
    # Path constants stay patched for the manager's whole lifetime; the
    # construction-only patches (notably Thread.start, which would otherwise
    # neuter the debounce Timer) wrap just the constructor.
    patchers = [
        patch.object(server, "SESSION_SNAPSHOT_PATH", tmp_path / "session_snapshot.json"),
        patch.object(server, "SESSION_SNAPSHOT_DEBOUNCE_SECONDS", 0.01),
        patch.object(server, "UPLOAD_DIR", tmp_path / "uploads"),
    ]
    patchers += [patch.object(server, name, tmp_path / f"{name.lower()}.json") for name in _STORE_PATH_CONSTANTS]
    with contextlib.ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)
        with (
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
        yield manager


def _registration(tmp_path: Path, session_id: str, *, broker_pid: int, thread_id: str, effort: str = "high", start_ts: float = 1.0) -> DiscoveryRegistration:
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
                "start_ts": start_ts,
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
        start_ts=start_ts,
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


def _read_store(tmp_path: Path) -> SessionSnapshotStore:
    return SessionSnapshotStore(tmp_path / "session_snapshot.json")


def test_discovery_auto_saves_live_sessions_with_raw_live_settings(tmp_path: Path) -> None:
    with _build_manager(tmp_path) as manager:
        store = _read_store(tmp_path)
        live_pid = os.getpid()

        manager._apply_discovery_result(DiscoveryResult(registrations=[_registration(tmp_path, "broker-a", broker_pid=live_pid, thread_id="thread-a", effort="xhigh")]))

        _wait_for(lambda: store.exists())
        entry = store.read().entries[("pi", "thread-a")]
        assert entry.session_id == "broker-a"
        assert entry.thread_id == "thread-a"
        assert entry.reasoning_effort == "xhigh"
        assert entry.model == "kimi"
        assert entry.tmux is True

        payload = manager.snapshot_get()
        assert payload["entries"][0]["session_id"] == "broker-a"
        assert payload["entries"][0]["thread_id"] == "thread-a"
        assert payload["entries"][0]["live"] is True


def test_snapshot_get_marks_dead_sessions_not_live_and_save_forces_upsert(tmp_path: Path) -> None:
    with _build_manager(tmp_path) as manager:
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


def test_restore_marks_restoring_then_supersede_remaps_stores(tmp_path: Path) -> None:
    """Restore never consumes at launch; the replacement's upsert supersedes the entry and remaps."""
    with _build_manager(tmp_path) as manager:
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
            replacement = _registration(tmp_path, "broker-e", broker_pid=os.getpid(), thread_id="thread-d", start_ts=2.0)
            self._apply_discovery_result(DiscoveryResult(registrations=[replacement]))
            return {"broker_pid": os.getpid()}

        with (
            patch.object(server.SessionManager, "spawn_web_session", spawn),
            patch.object(server, "_tmux_available", lambda: True),
        ):
            out = manager.snapshot_restore(SnapshotRestoreRequest())

        assert out["restored"] == 1
        assert out["results"][0]["new_session_id"] == "broker-e"
        assert out["results"][0]["thread_id"] == "thread-d"
        assert spawns[0]["resume_session_id"] == "thread-d"
        assert spawns[0]["create_in_tmux"] is True
        assert spawns[0]["resume_row"]["log_path"] == str(tmp_path / "thread-d.jsonl")

        # The entry is not consumed; it is stamped restoring until the replacement's upsert lands.
        entry = _read_store(tmp_path).read().entries[("pi", "thread-d")]
        assert entry.session_id == "broker-d"
        assert entry.restoring is not None

        # The supersede remap moves queue + draft to the new registry id and persists them.
        _wait_for(lambda: "broker-e" in manager._queues)
        assert manager._queues == {"broker-e": [{"id": "q1", "text": "queued", "created_ts": 1.0}]}
        assert manager._drafts == {"broker-e": {"text": "draft", "updated_ts": 1.0}}
        assert json.loads((tmp_path / "queue_path.json").read_text(encoding="utf-8")).keys() == {"broker-e"}

        state = _read_store(tmp_path).read()
        assert state.entries[("pi", "thread-d")].session_id == "broker-e"
        assert state.entries[("pi", "thread-d")].restoring is None


def test_queue_survives_a_server_restart_between_broker_death_and_restore(tmp_path: Path) -> None:
    """Repro of the critic's restart scenario: the sweep must not drop a snapshot-protected queue."""
    with _build_manager(tmp_path) as m1:
        m1._apply_discovery_result(DiscoveryResult(registrations=[_registration(tmp_path, "broker-d", broker_pid=os.getpid(), thread_id="thread-d")]))
        m1.snapshot_save()
        m1._queues["broker-d"] = [{"id": "q1", "text": "queued", "created_ts": 1.0}]
        m1._drafts["broker-d"] = {"text": "draft", "updated_ts": 1.0}
        m1._save_queues()
        m1._save_drafts()

    # Host reboot / server restart: the broker is dead and never re-registers.
    with _build_manager(tmp_path) as m2:
        m2._queues.update(json.loads((tmp_path / "queue_path.json").read_text(encoding="utf-8")))
        m2._drafts.update(json.loads((tmp_path / "draft_path.json").read_text(encoding="utf-8")))
        m2._prune_dead_sessions = lambda: None

        m2._queue_sweep()
        assert m2._queues == {"broker-d": [{"id": "q1", "text": "queued", "created_ts": 1.0}]}
        assert m2._drafts == {"broker-d": {"text": "draft", "updated_ts": 1.0}}

        def spawn(self, **kwargs):
            self._apply_discovery_result(
                DiscoveryResult(registrations=[_registration(tmp_path, "broker-e", broker_pid=os.getpid(), thread_id="thread-d", start_ts=2.0)])
            )
            return {"broker_pid": os.getpid()}

        with (
            patch.object(server.SessionManager, "spawn_web_session", spawn),
            patch.object(server, "_tmux_available", lambda: True),
        ):
            out = m2.snapshot_restore(SnapshotRestoreRequest())

        assert out["restored"] == 1
        _wait_for(lambda: "broker-e" in m2._queues)
        assert m2._queues["broker-e"] == [{"id": "q1", "text": "queued", "created_ts": 1.0}]
        assert "broker-d" not in m2._queues
        assert m2._drafts == {"broker-e": {"text": "draft", "updated_ts": 1.0}}


def test_unbound_replacement_keeps_the_entry_until_it_binds(tmp_path: Path) -> None:
    """Repro of the collision scenario: a launched-but-unbound replacement must not strand the transcript."""
    with _build_manager(tmp_path) as manager:
        dead_pid = 2_147_483_600
        manager._apply_discovery_result(DiscoveryResult(registrations=[_registration(tmp_path, "broker-g", broker_pid=os.getpid(), thread_id="thread-g")]))
        manager.snapshot_save()
        manager._sessions["broker-g"].broker_pid = dead_pid
        manager._sessions["broker-g"].codex_pid = dead_pid

        def spawn_unbound(self, **kwargs):
            # The replacement starts but has not bound its transcript yet: no registration.
            return {"broker_pid": os.getpid()}

        with (
            patch.object(server.SessionManager, "spawn_web_session", spawn_unbound),
            patch.object(server, "_tmux_available", lambda: True),
        ):
            out = manager.snapshot_restore(SnapshotRestoreRequest())

        assert out["restored"] == 1
        state = _read_store(tmp_path).read()
        assert ("pi", "thread-g") in state.entries
        assert state.entries[("pi", "thread-g")].restoring is not None

        # A second restore while the mark is active does not double-launch.
        with (
            patch.object(server.SessionManager, "spawn_web_session", lambda self, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn"))),
            patch.object(server, "_tmux_available", lambda: True),
        ):
            again = manager.snapshot_restore(SnapshotRestoreRequest())
        assert again["results"][0]["outcome"] == "skipped_restoring"

        # When the replacement finally binds, its upsert supersedes the entry.
        manager._apply_discovery_result(
            DiscoveryResult(registrations=[_registration(tmp_path, "broker-h", broker_pid=os.getpid(), thread_id="thread-g", start_ts=2.0)])
        )
        _wait_for(lambda: _read_store(tmp_path).read().entries[("pi", "thread-g")].session_id == "broker-h")


def test_restore_of_a_permanently_missing_log_drops_the_stuck_entry(tmp_path: Path) -> None:
    """Repro of the stuck-entry scenario: missing transcript log drops the entry; dismiss clears the rest."""
    with _build_manager(tmp_path) as manager:
        dead_pid = 2_147_483_600
        manager._apply_discovery_result(DiscoveryResult(registrations=[_registration(tmp_path, "broker-f", broker_pid=os.getpid(), thread_id="thread-f")]))
        manager.snapshot_save()
        manager._sessions.clear()
        (tmp_path / "thread-f.jsonl").unlink()

        with patch.object(server, "_tmux_available", lambda: True):
            out = manager.snapshot_restore(SnapshotRestoreRequest())

        assert out["failed"] == 1
        assert "session log missing" in out["results"][0]["error"]
        assert "entry dropped" in out["results"][0]["error"]
        assert _read_store(tmp_path).read().entries == {}

        # A dismissible entry the user no longer wants (accepted by registry id or thread id).
        manager._apply_discovery_result(DiscoveryResult(registrations=[_registration(tmp_path, "broker-i", broker_pid=os.getpid(), thread_id="thread-i")]))
        manager.snapshot_save()
        manager._sessions["broker-i"].broker_pid = dead_pid
        manager._sessions["broker-i"].codex_pid = dead_pid
        assert manager.snapshot_dismiss("thread-i") is True
        assert _read_store(tmp_path).read().entries == {}
        assert manager.snapshot_dismiss("thread-i") is False


def test_restore_fails_loudly_when_saved_tmux_transport_is_unavailable(tmp_path: Path) -> None:
    with _build_manager(tmp_path) as manager:
        registration = _registration(tmp_path, "broker-f", broker_pid=os.getpid(), thread_id="thread-f")
        manager._apply_discovery_result(DiscoveryResult(registrations=[registration]))
        manager.snapshot_save()
        manager._sessions.clear()

        with (
            patch.object(server.SessionManager, "spawn_web_session", lambda self, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn"))),
            patch.object(server, "_tmux_available", lambda: False),
        ):
            out = manager.snapshot_restore(SnapshotRestoreRequest())

        assert out["results"] == [{"session_id": "broker-f", "thread_id": "thread-f", "outcome": "failed", "error": "tmux unavailable"}]
        assert ("pi", "thread-f") in _read_store(tmp_path).read().entries
