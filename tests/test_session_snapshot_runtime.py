from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from codoxear.session_discovery import DiscoveryRegistration
from codoxear.session_model import Session
from codoxear.session_snapshot import PendingRemap
from codoxear.session_snapshot import SessionSnapshotStore
from codoxear.session_snapshot import SnapshotEntry
from codoxear.session_snapshot import snapshot_path
from codoxear.session_snapshot_runtime import SessionSnapshotCoordinator
from codoxear.sidecar_metadata import read_metadata


class _FakeTimer:
    def __init__(self, delay: float, fn) -> None:
        self.delay = delay
        self.fn = fn
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fn()


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.sock_dir = tmp_path / "socks"
        self.sock_dir.mkdir(exist_ok=True)
        self.sessions: dict[str, Session] = {}
        self.alive: set[int] = set()
        self.timers: list[_FakeTimer] = []
        self.saved: list[str] = []
        self.session_store = SimpleNamespace(queues={}, drafts={}, unattended={})
        self.stderr = io.StringIO()
        self.store = SessionSnapshotStore(snapshot_path(tmp_path), now=lambda: 50.0)
        self.lock = threading.Lock()

        def timer_factory(delay, fn):
            timer = _FakeTimer(delay, fn)
            self.timers.append(timer)
            return timer

        def snapshot_run_settings(meta, log_path, agent_backend):
            live = meta.get("live_run_settings") or {}
            return (
                live.get("model_provider") or meta.get("model_provider"),
                meta.get("preferred_auth_method"),
                live.get("model") or meta.get("model"),
                live.get("reasoning_effort") or meta.get("reasoning_effort"),
            )

        self.coordinator = SessionSnapshotCoordinator(
            store=self.store,
            lock=self.lock,
            sessions=lambda: self.sessions,
            session_store=lambda: self.session_store,
            save_queues=lambda: self.saved.append("queues"),
            save_drafts=lambda: self.saved.append("drafts"),
            save_unattended=lambda: self.saved.append("unattended"),
            read_sidecar_metadata=lambda meta_path: read_metadata(meta_path, sock=meta_path.with_suffix(".sock")),
            snapshot_run_settings=snapshot_run_settings,
            live_session_for_resume_target=self._live_session_for_resume_target,
            pid_alive=lambda pid: int(pid) in self.alive,
            now=lambda: 50.0,
            debounce_seconds=2.0,
            timer_factory=timer_factory,
            stderr=self.stderr,
        )

    def _live_session_for_resume_target(self, resume_id: str, row: dict) -> Session | None:
        for session in self.sessions.values():
            same = session.thread_id == resume_id or (session.log_path is not None and str(session.log_path) == row.get("log_path"))
            if same and (session.broker_pid in self.alive or session.codex_pid in self.alive):
                return session
        return None

    def add_live_session(
        self,
        session_id: str,
        *,
        broker_pid: int,
        thread_id: str = "thread-1",
        log_path: Path | None = None,
        transport: str | None = "tmux",
        meta: dict | None = None,
    ) -> tuple[Session, DiscoveryRegistration]:
        if log_path is None:
            log_path = self.tmp_path / f"{thread_id}.jsonl"
            log_path.write_text("{}\n", encoding="utf-8")
        sock = self.sock_dir / f"{session_id}.sock"
        sock.touch()
        sidecar = {
            "session_id": thread_id,
            "broker_pid": broker_pid,
            "codex_pid": broker_pid + 1,
            "cwd": "/repo",
            "log_path": str(log_path),
            "agent_backend": "pi",
            "model_provider": "dexgem",
            "model": "kimi",
            "reasoning_effort": "high",
            "service_tier": None,
            "transport": transport,
            "resume_session_id": None,
        }
        sidecar.update(meta or {})
        sock.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")
        session = Session(
            session_id=session_id,
            thread_id=thread_id,
            broker_pid=broker_pid,
            codex_pid=broker_pid + 1,
            agent_backend="pi",
            owned=True,
            start_ts=1.0,
            cwd="/repo",
            log_path=log_path,
            sock_path=sock,
            transport=transport,
            resume_session_id=sidecar.get("resume_session_id"),
        )
        self.sessions[session_id] = session
        self.alive.add(broker_pid)
        return session, self.registration(session, sidecar)

    @staticmethod
    def registration(session: Session, sidecar: dict) -> DiscoveryRegistration:
        live = sidecar.get("live_run_settings") or {}
        return DiscoveryRegistration(
            session_id=session.session_id,
            thread_id=session.thread_id,
            broker_pid=session.broker_pid,
            codex_pid=session.codex_pid,
            agent_backend=session.agent_backend,
            owned=True,
            transport=session.transport,
            start_ts=1.0,
            cwd=session.cwd,
            log_path=session.log_path,
            sock_path=session.sock_path,
            busy=False,
            queue_len=0,
            token=None,
            meta_log_off=0,
            model_provider=live.get("model_provider") or sidecar.get("model_provider"),
            preferred_auth_method=None,
            model=live.get("model") or sidecar.get("model"),
            reasoning_effort=live.get("reasoning_effort") or sidecar.get("reasoning_effort"),
            service_tier=None,
            tmux_session="codoxear" if session.transport == "tmux" else None,
            tmux_window=None,
            launch_id=None,
            spawn_nonce=None,
            resume_session_id=sidecar.get("resume_session_id"),
            sync_send_supported=True,
            key_write_errors_supported=True,
            interrupted_idle=False,
        )


def test_discovery_upserts_new_live_session_after_one_coalesced_debounce(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    _session, registration = h.add_live_session("broker-10", broker_pid=10)

    h.coordinator.observe_registrations([registration])
    h.coordinator.observe_registrations([registration])
    h.coordinator.observe_registrations([registration])

    assert len(h.timers) == 1
    assert h.timers[0].delay == 2.0
    assert h.timers[0].started is True
    assert not h.store.exists()

    h.timers[0].fire()
    state = h.store.read()
    entry = state.entries["broker-10"]
    assert entry.thread_id == "thread-1"
    assert entry.agent_backend == "pi"
    assert entry.cwd == "/repo"
    assert entry.model == "kimi"
    assert entry.reasoning_effort == "high"
    assert entry.model_provider == "dexgem"
    assert entry.tmux is True
    assert entry.log_path == str(_session.log_path)

    # Unchanged registrations arm no further timer.
    h.coordinator.observe_registrations([registration])
    assert len(h.timers) == 1


def test_settings_change_upserts_raw_live_values(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, registration = h.add_live_session("broker-10", broker_pid=10)
    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()
    assert h.store.read().entries["broker-10"].reasoning_effort == "high"

    sidecar = json.loads(session.sock_path.with_suffix(".json").read_text(encoding="utf-8"))
    sidecar["live_run_settings"] = {"model_provider": "dexgem", "model": "kimi", "reasoning_effort": "xhigh"}
    session.sock_path.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")
    changed = h.registration(session, sidecar)

    h.coordinator.observe_registrations([changed])
    assert len(h.timers) == 2
    h.timers[-1].fire()
    assert h.store.read().entries["broker-10"].reasoning_effort == "xhigh"


def test_session_without_bound_log_is_not_snapshotted(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, registration = h.add_live_session("broker-10", broker_pid=10)
    session.log_path = None
    unbound = DiscoveryRegistration(**{**registration.__dict__, "log_path": None})

    h.coordinator.observe_registrations([unbound])
    assert h.timers == []
    assert h.coordinator.entry_for_session(session) is None


def test_crashed_session_absent_from_discovery_keeps_its_entry(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    _session, registration = h.add_live_session("broker-10", broker_pid=10)
    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()

    h.alive.discard(10)
    h.coordinator.observe_registrations([])
    assert "broker-10" in h.store.read().entries
    assert len(h.timers) == 1


def test_clean_broker_exit_and_delete_remove_entries(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session_a, reg_a = h.add_live_session("broker-10", broker_pid=10, thread_id="t-a")
    _session_b, reg_b = h.add_live_session("broker-11", broker_pid=11, thread_id="t-b")
    h.coordinator.observe_registrations([reg_a, reg_b])
    h.timers[-1].fire()
    assert set(h.store.read().entries) == {"broker-10", "broker-11"}

    h.coordinator.session_sock_vanished(session_a)
    assert set(h.store.read().entries) == {"broker-11"}

    h.coordinator.session_deleted("broker-11")
    assert h.store.read().entries == {}

    # Forgetting drops the signature so a same-id session is re-upserted.
    h.coordinator.observe_registrations([reg_b])
    assert len(h.timers) == 2


def test_save_now_writes_immediately_and_cancels_pending_timer(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    _session, registration = h.add_live_session("broker-10", broker_pid=10)
    h.coordinator.observe_registrations([registration])
    timer = h.timers[-1]

    state = h.coordinator.save_now()
    assert timer.cancelled is True
    assert set(state.entries) == {"broker-10"}
    assert set(h.store.read().entries) == {"broker-10"}
    timer.fire()
    assert set(h.store.read().entries) == {"broker-10"}


def test_save_now_skips_lost_and_dead_sessions(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    live, _ = h.add_live_session("broker-10", broker_pid=10, thread_id="t-live")
    lost, _ = h.add_live_session("broker-11", broker_pid=11, thread_id="t-lost")
    dead, _ = h.add_live_session("broker-12", broker_pid=12, thread_id="t-dead")
    lost.lost = True
    h.alive.discard(12)

    state = h.coordinator.save_now()
    assert set(state.entries) == {"broker-10"}


def test_live_session_for_entry_matches_registry_id_thread_or_log(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, _ = h.add_live_session("broker-10", broker_pid=10, thread_id="t-1")
    entry = h.coordinator.entry_for_session(session)
    assert entry is not None

    assert h.coordinator.live_session_for_entry(entry) is session
    by_thread = SnapshotEntry(**{**entry.__dict__, "session_id": "broker-old", "log_path": "/elsewhere.jsonl"})
    assert h.coordinator.live_session_for_entry(by_thread) is session
    by_log = SnapshotEntry(**{**entry.__dict__, "session_id": "broker-old", "thread_id": "other"})
    assert h.coordinator.live_session_for_entry(by_log) is session

    h.alive.discard(10)
    assert h.coordinator.live_session_for_entry(entry) is None


def _seed_stores(h: _Harness, old: str) -> None:
    h.session_store.queues[old] = [{"id": "q1", "text": "queued", "created_ts": 1.0}]
    h.session_store.drafts[old] = {"text": "draft text", "updated_ts": 2.0}
    h.session_store.unattended[old] = {"enabled": True, "request": "continue", "cooldown_minutes": 5, "remaining_injections": 3}


def test_consume_restored_with_known_new_id_remaps_stores_immediately(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, registration = h.add_live_session("broker-10", broker_pid=10, thread_id="t-1")
    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()
    entry = h.store.read().entries["broker-10"]
    _seed_stores(h, "broker-10")
    h.session_store.queues["broker-20"] = [{"id": "q2", "text": "later", "created_ts": 3.0}]

    h.coordinator.consume_restored(entry, broker_pid=20, new_session_id="broker-20")

    state = h.store.read()
    assert state.entries == {}
    assert state.pending_remaps == []
    assert [item["id"] for item in h.session_store.queues["broker-20"]] == ["q1", "q2"]
    assert "broker-10" not in h.session_store.queues
    assert h.session_store.drafts == {"broker-20": {"text": "draft text", "updated_ts": 2.0}}
    assert h.session_store.unattended == {"thread:t-1": {"enabled": True, "request": "continue", "cooldown_minutes": 5, "remaining_injections": 3}}
    assert h.saved == ["queues", "drafts", "unattended"]


def test_consume_restored_with_identity_preserved_moves_nothing(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, registration = h.add_live_session("broker-10", broker_pid=10, thread_id="t-1")
    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()
    entry = h.store.read().entries["broker-10"]
    _seed_stores(h, "broker-10")

    h.coordinator.consume_restored(entry, broker_pid=10, new_session_id="broker-10")

    assert h.store.read().entries == {}
    assert set(h.session_store.queues) == {"broker-10"}
    assert set(h.session_store.drafts) == {"broker-10"}
    assert set(h.session_store.unattended) == {"broker-10"}
    assert h.saved == []


def test_pending_remap_resolves_when_discovery_registers_the_replacement(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, registration = h.add_live_session("broker-10", broker_pid=10, thread_id="t-1")
    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()
    entry = h.store.read().entries["broker-10"]
    _seed_stores(h, "broker-10")

    h.coordinator.consume_restored(entry, broker_pid=30, new_session_id=None)
    state = h.store.read()
    assert state.entries == {}
    assert state.pending_remaps == [PendingRemap(old_session_id="broker-10", old_log_path=entry.log_path, thread_id="t-1", broker_pid=30, launched_at=50.0)]
    assert set(h.session_store.queues) == {"broker-10"}

    # An unrelated registration proves nothing.
    _other, unrelated = h.add_live_session("broker-40", broker_pid=40, thread_id="t-other")
    h.coordinator.observe_registrations([unrelated])
    assert h.store.read().pending_remaps != []

    # The launched broker (pid 30) registers, pre-log-bind, declaring its resume target.
    h.alive.discard(10)
    _new, replacement = h.add_live_session("broker-30", broker_pid=30, thread_id="broker-30", meta={"resume_session_id": "t-1", "session_id": None})
    resolved = h.coordinator.resolve_pending_remaps([replacement])

    assert resolved == [("broker-10", "broker-30")]
    assert h.store.read().pending_remaps == []
    assert set(h.session_store.queues) == {"broker-30"}
    assert set(h.session_store.drafts) == {"broker-30"}
    assert set(h.session_store.unattended) == {"thread:t-1"}


def test_pending_remap_survives_a_fresh_coordinator_and_matches_by_bound_thread(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, registration = h.add_live_session("broker-10", broker_pid=10, thread_id="t-1")
    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()
    entry = h.store.read().entries["broker-10"]
    _seed_stores(h, "broker-10")
    h.coordinator.consume_restored(entry, broker_pid=None, new_session_id=None)

    # New server process: the coordinator state is gone, the file remains.
    h2 = _Harness(tmp_path)
    h2.session_store = h.session_store
    h2.coordinator.session_store = lambda: h.session_store
    _new, replacement = h2.add_live_session("broker-31", broker_pid=31, thread_id="t-1")
    h2.coordinator.observe_registrations([replacement])

    assert h2.store.read().pending_remaps == []
    assert set(h.session_store.queues) == {"broker-31"}


def test_delete_of_the_old_session_clears_its_pending_remap(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    session, registration = h.add_live_session("broker-10", broker_pid=10, thread_id="t-1")
    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()
    entry = h.store.read().entries["broker-10"]
    h.coordinator.consume_restored(entry, broker_pid=None, new_session_id=None)
    assert h.store.read().pending_remaps != []

    h.coordinator.session_deleted("broker-10")
    assert h.store.read().pending_remaps == []


def test_corrupt_snapshot_is_reported_not_overwritten_by_auto_save(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.store.path.write_text("{broken", encoding="utf-8")
    _session, registration = h.add_live_session("broker-10", broker_pid=10)

    h.coordinator.observe_registrations([registration])
    h.timers[-1].fire()

    assert h.store.path.read_text(encoding="utf-8") == "{broken"
    assert "session snapshot upsert failed" in h.stderr.getvalue()
