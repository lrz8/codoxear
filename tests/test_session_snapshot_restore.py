from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from codoxear.session_snapshot import SessionSnapshotStore
from codoxear.session_snapshot import SnapshotEntry
from codoxear.session_snapshot import snapshot_path
from codoxear.session_snapshot_restore import SnapshotRestoreDeps
from codoxear.session_snapshot_restore import SnapshotRestoreRequest
from codoxear.session_snapshot_restore import SnapshotRestoreUnavailable
from codoxear.session_snapshot_restore import parse_restore_request
from codoxear.session_snapshot_restore import restore_snapshot


class _LaunchError(RuntimeError):
    def __init__(self, message: str, record: dict) -> None:
        super().__init__(message)
        self.record = record


def _parse_launch_request(obj: dict) -> SimpleNamespace:
    if obj.get("reasoning_effort") == "bogus":
        raise ValueError("reasoning_effort must be one of low, medium, high")
    return SimpleNamespace(
        cwd=obj["cwd"],
        args=None,
        agent_backend=obj["agent_backend"],
        resume_session_id=obj["resume_session_id"],
        worktree_branch=None,
        model_provider=obj["model_provider"],
        preferred_auth_method=obj["preferred_auth_method"],
        model=obj["model"],
        reasoning_effort=obj["reasoning_effort"],
        service_tier=obj["service_tier"],
        create_in_tmux=obj["create_in_tmux"],
    )


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store = SessionSnapshotStore(snapshot_path(tmp_path), now=lambda: 10.0)
        self.live: dict[str, object] = {}
        self.spawns: list[dict] = []
        self.consumed: list[tuple[str, int | None, str | None]] = []
        self.tmux = True
        self.registry_by_pid: dict[int, str] = {}
        self.next_pid = 100
        self.spawn_error: BaseException | None = None
        self.spawn_gate: threading.Event | None = None
        self.deps = SnapshotRestoreDeps(
            read_snapshot=self.store.read,
            live_session_for_entry=lambda entry: self.live.get(entry.session_id),
            parse_launch_request=_parse_launch_request,
            spawn_web_session=self._spawn,
            tmux_available=lambda: self.tmux,
            session_id_for_broker_pid=lambda pid: self.registry_by_pid.get(pid),
            consume_restored=self._consume,
            launch_error=_LaunchError,
            lock=threading.Lock(),
        )

    def _spawn(self, **kwargs) -> dict:
        if self.spawn_gate is not None:
            self.spawn_gate.wait(5)
        self.spawns.append(kwargs)
        if self.spawn_error is not None:
            raise self.spawn_error
        pid = self.next_pid
        self.next_pid += 1
        return {"broker_pid": pid}

    def _consume(self, entry: SnapshotEntry, *, broker_pid: int | None, new_session_id: str | None) -> None:
        self.consumed.append((entry.session_id, broker_pid, new_session_id))
        self.store.consume(entry.session_id, pending_remap=None)

    def entry(self, session_id: str, *, thread_id: str | None = None, tmux: bool = True, log_exists: bool = True, **overrides) -> SnapshotEntry:
        log_path = self.tmp_path / f"{session_id}.jsonl"
        if log_exists:
            log_path.write_text("{}\n", encoding="utf-8")
        values = dict(
            session_id=session_id,
            thread_id=thread_id or f"thread-{session_id}",
            agent_backend="pi",
            log_path=str(log_path),
            cwd=str(self.tmp_path),
            model_provider="dexgem",
            preferred_auth_method=None,
            model="kimi",
            reasoning_effort="high",
            service_tier=None,
            tmux=tmux,
            saved_at=1.0,
        )
        values.update(overrides)
        entry = SnapshotEntry(**values)
        self.store.upsert([entry])
        return entry

    def restore(self, **kwargs) -> dict:
        return restore_snapshot(SnapshotRestoreRequest(**kwargs), deps=self.deps)


def test_launch_uses_recorded_settings_resume_row_and_saved_transport(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    entry = h.entry("broker-1", tmux=True)
    h.registry_by_pid[100] = "broker-100"

    out = h.restore()

    assert out == {
        "results": [{"session_id": "broker-1", "outcome": "launched", "new_session_id": "broker-100", "broker_pid": 100}],
        "restored": 1,
        "skipped": 0,
        "failed": 0,
    }
    assert len(h.spawns) == 1
    spawn = h.spawns[0]
    assert spawn["cwd"] == str(tmp_path)
    assert spawn["agent_backend"] == "pi"
    assert spawn["resume_session_id"] == "thread-broker-1"
    assert spawn["model_provider"] == "dexgem"
    assert spawn["model"] == "kimi"
    assert spawn["reasoning_effort"] == "high"
    assert spawn["create_in_tmux"] is True
    assert spawn["resume_row"] == {"session_id": "thread-broker-1", "log_path": entry.log_path, "cwd": str(tmp_path), "agent_backend": "pi"}
    assert h.consumed == [("broker-1", 100, "broker-100")]
    assert h.store.read().entries == {}


def test_live_entry_is_skipped_without_spawning(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.live["broker-1"] = SimpleNamespace(session_id="broker-1-live")

    out = h.restore()

    assert out["results"] == [{"session_id": "broker-1", "outcome": "skipped_live", "live_session_id": "broker-1-live"}]
    assert out["skipped"] == 1 and out["restored"] == 0
    assert h.spawns == []
    assert "broker-1" in h.store.read().entries


def test_missing_log_fails_and_keeps_entry(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1", log_exists=False)

    out = h.restore()

    assert out["failed"] == 1
    assert out["results"][0]["outcome"] == "failed"
    assert "session log missing" in out["results"][0]["error"]
    assert h.spawns == []
    assert "broker-1" in h.store.read().entries


def test_tmux_unavailable_fails_loudly_unless_transport_direct(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1", tmux=True)
    h.tmux = False

    out = h.restore()
    assert out["results"] == [{"session_id": "broker-1", "outcome": "failed", "error": "tmux unavailable"}]
    assert h.spawns == []

    out = h.restore(transport="direct")
    assert out["restored"] == 1
    assert h.spawns[0]["create_in_tmux"] is False


def test_direct_entries_do_not_need_tmux(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1", tmux=False)
    h.tmux = False

    out = h.restore()
    assert out["restored"] == 1
    assert h.spawns[0]["create_in_tmux"] is False


def test_invalid_recorded_settings_and_launch_errors_become_failed_outcomes(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1", reasoning_effort="bogus")
    h.entry("broker-2")
    h.entry("broker-3")

    def spawn(**kwargs):
        h.spawns.append(kwargs)
        if kwargs["resume_session_id"] == "thread-broker-2":
            raise ValueError("resume target is already live as broker-9")
        raise _LaunchError("broker exited early", {"stage": "broker_early_exit"})

    object.__setattr__(h.deps, "spawn_web_session", spawn)
    out = h.restore()

    by_id = {row["session_id"]: row for row in out["results"]}
    assert "reasoning_effort" in by_id["broker-1"]["error"]
    assert by_id["broker-2"]["error"] == "resume target is already live as broker-9"
    assert by_id["broker-3"]["error"] == "broker exited early"
    assert out == {**out, "restored": 0, "skipped": 0, "failed": 3}
    assert len(h.spawns) == 2
    assert set(h.store.read().entries) == {"broker-1", "broker-2", "broker-3"}


def test_outcomes_aggregate_per_entry_in_snapshot_order(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.entry("broker-2")
    h.entry("broker-3", log_exists=False)
    h.live["broker-2"] = SimpleNamespace(session_id="broker-2")

    out = h.restore()

    assert [row["outcome"] for row in out["results"]] == ["launched", "skipped_live", "failed"]
    assert (out["restored"], out["skipped"], out["failed"]) == (1, 1, 1)
    assert set(h.store.read().entries) == {"broker-2", "broker-3"}


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.entry("broker-2")
    h.live["broker-2"] = SimpleNamespace(session_id="broker-2")

    first = h.restore()
    assert (first["restored"], first["skipped"]) == (1, 1)
    second = h.restore()
    assert second["results"] == [{"session_id": "broker-2", "outcome": "skipped_live", "live_session_id": "broker-2"}]
    assert len(h.spawns) == 1


def test_include_restricts_to_listed_entries(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.entry("broker-2")

    out = h.restore(include=("broker-2", "unknown"))

    assert [row["session_id"] for row in out["results"]] == ["broker-2"]
    assert set(h.store.read().entries) == {"broker-1"}


def test_pending_launch_result_is_reported_without_new_session_id(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    object.__setattr__(h.deps, "spawn_web_session", lambda **kwargs: {"launch_id": "launch-1", "pending": True, "tmux_session": "codoxear"})

    out = h.restore()

    assert out["results"] == [{"session_id": "broker-1", "outcome": "launched", "pending": True, "launch_id": "launch-1"}]
    assert h.consumed == [("broker-1", None, None)]


def test_missing_or_corrupt_snapshot_is_unavailable(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    with pytest.raises(SnapshotRestoreUnavailable, match="no saved sessions"):
        h.restore()
    h.store.path.write_text("{oops", encoding="utf-8")
    with pytest.raises(SnapshotRestoreUnavailable, match="unreadable"):
        h.restore()


def test_concurrent_restores_never_spawn_the_same_entry_twice(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.spawn_gate = threading.Event()
    results: list[object] = []

    def run() -> None:
        try:
            results.append(h.restore())
        except SnapshotRestoreUnavailable as exc:
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    threads[0].start()
    threads[1].start()
    h.spawn_gate.set()
    for thread in threads:
        thread.join(5)

    assert len(h.spawns) == 1
    launched = [r for r in results if isinstance(r, dict)]
    unavailable = [r for r in results if isinstance(r, SnapshotRestoreUnavailable)]
    assert len(launched) == 1 and launched[0]["restored"] == 1
    assert len(unavailable) == 1


def test_parse_restore_request_defaults_and_validation() -> None:
    assert parse_restore_request(None) == SnapshotRestoreRequest(transport="as_saved", include=None)
    assert parse_restore_request({}) == SnapshotRestoreRequest(transport="as_saved", include=None)
    assert parse_restore_request({"transport": "direct", "include": [" a ", "b"]}) == SnapshotRestoreRequest(transport="direct", include=("a", "b"))
    with pytest.raises(ValueError, match="transport"):
        parse_restore_request({"transport": "tmux"})
    with pytest.raises(ValueError, match="include"):
        parse_restore_request({"include": ["", "b"]})
    with pytest.raises(ValueError, match="include"):
        parse_restore_request({"include": "a"})
