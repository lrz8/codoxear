"""Restore: per-entry outcomes, restoring marks, cwd/log pre-checks, error isolation."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from codoxear.session_snapshot import RESTORING_TTL_SECONDS
from codoxear.session_snapshot import RestoringMark
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
        self.store = SessionSnapshotStore(snapshot_path(tmp_path), now=lambda: self.clock[0])
        self.clock = [10.0]
        self.live_by_thread: dict[str, str] = {}
        self.spawns: list[dict] = []
        self.tmux = True
        self.alive: set[int] = set()
        self.registry_by_pid: dict[int, str] = {}
        self.next_pid = 100
        self.spawn_error: BaseException | None = None
        self.spawn_gate: threading.Event | None = None
        self.deps = SnapshotRestoreDeps(
            read_snapshot=self.store.read,
            live_session_id_for_entry=lambda entry: self.live_by_thread.get(entry.thread_id),
            restoring_active=self._restoring_active,
            parse_launch_request=_parse_launch_request,
            spawn_web_session=self._spawn,
            tmux_available=lambda: self.tmux,
            session_id_for_broker_pid=lambda pid: self.registry_by_pid.get(pid),
            mark_restoring=self._mark_restoring,
            drop_entry=lambda entry: bool(self.store.remove([entry.key])),
            launch_error=_LaunchError,
            lock=threading.Lock(),
        )

    def _restoring_active(self, entry: SnapshotEntry) -> bool:
        mark = entry.restoring
        if mark is None or not mark.active(self.clock[0]):
            return False
        return mark.broker_pid is None or mark.broker_pid in self.alive

    def _mark_restoring(self, entry: SnapshotEntry, *, broker_pid: int | None) -> bool:
        return self.store.mark_restoring(
            entry.key,
            expected_session_id=entry.session_id,
            mark=RestoringMark(broker_pid=broker_pid, launched_at=self.clock[0]),
        )

    def _spawn(self, **kwargs) -> dict:
        if self.spawn_gate is not None:
            self.spawn_gate.wait(5)
        self.spawns.append(kwargs)
        if self.spawn_error is not None:
            raise self.spawn_error
        pid = self.next_pid
        self.next_pid += 1
        self.alive.add(pid)
        return {"broker_pid": pid}

    def entry(self, session_id: str, *, thread_id: str | None = None, tmux: bool = True, log_exists: bool = True, cwd: str | None = None, **overrides) -> SnapshotEntry:
        log_path = self.tmp_path / f"{session_id}.jsonl"
        if log_exists:
            log_path.write_text("{}\n", encoding="utf-8")
        values = dict(
            session_id=session_id,
            thread_id=thread_id or f"thread-{session_id}",
            agent_backend="pi",
            log_path=str(log_path),
            cwd=cwd or str(self.tmp_path),
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


def test_launch_uses_recorded_settings_and_stamps_a_restoring_mark(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    entry = h.entry("broker-1", tmux=True)
    h.registry_by_pid[100] = "broker-100"

    out = h.restore()

    assert out == {
        "results": [{"session_id": "broker-1", "thread_id": "thread-broker-1", "outcome": "launched", "new_session_id": "broker-100", "broker_pid": 100}],
        "restored": 1,
        "skipped": 0,
        "skipped_restoring": 0,
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

    # No consume-at-launch: the entry stays, stamped restoring with the new broker pid.
    stored = h.store.read().entries[("pi", "thread-broker-1")]
    assert stored.restoring == RestoringMark(broker_pid=100, launched_at=10.0)


def test_live_entry_is_skipped_without_spawning(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.live_by_thread["thread-broker-1"] = "broker-1-live"

    out = h.restore()

    assert out["results"] == [{"session_id": "broker-1", "thread_id": "thread-broker-1", "outcome": "skipped_live", "live_session_id": "broker-1-live"}]
    assert out["skipped"] == 1 and out["restored"] == 0
    assert h.spawns == []
    assert ("pi", "thread-broker-1") in h.store.read().entries


def test_a_restoring_marked_entry_is_skipped_until_the_mark_expires(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    entry = h.entry("broker-1")

    first = h.restore()
    assert first["restored"] == 1

    # Re-running while the replacement has not bound: skipped, never spawned twice.
    second = h.restore()
    assert second["results"] == [{"session_id": "broker-1", "thread_id": "thread-broker-1", "outcome": "skipped_restoring"}]
    assert (second["restored"], second["skipped"], second["skipped_restoring"]) == (0, 1, 1)
    assert len(h.spawns) == 1

    # The launched broker dying in flight releases the entry immediately.
    h.alive.discard(100)
    third = h.restore()
    assert third["restored"] == 1
    assert len(h.spawns) == 2

    # An expired mark also releases the entry.
    h.clock[0] += RESTORING_TTL_SECONDS
    fourth = h.restore()
    assert fourth["restored"] == 1
    assert len(h.spawns) == 3
    assert h.store.read().entries[entry.key].restoring is not None


def test_missing_log_fails_and_drops_the_entry_in_the_same_run(tmp_path: Path) -> None:
    """A permanently gone transcript can never be restored; the entry must not pile up."""
    h = _Harness(tmp_path)
    h.entry("broker-1", log_exists=False)

    out = h.restore()

    assert out["failed"] == 1
    row = out["results"][0]
    assert row["outcome"] == "failed"
    assert "session log missing" in row["error"]
    assert "entry dropped" in row["error"]
    assert h.spawns == []
    assert h.store.read().entries == {}


def test_missing_cwd_fails_before_any_launch_and_keeps_the_entry(tmp_path: Path) -> None:
    """Restore must never recreate a deleted working directory."""
    h = _Harness(tmp_path)
    h.entry("broker-1", cwd=str(tmp_path / "removed-worktree"))

    out = h.restore()

    assert out["results"] == [{"session_id": "broker-1", "thread_id": "thread-broker-1", "outcome": "failed", "error": f"cwd missing: {tmp_path / 'removed-worktree'}"}]
    assert h.spawns == []
    assert not (tmp_path / "removed-worktree").exists()
    assert ("pi", "thread-broker-1") in h.store.read().entries


def test_tmux_unavailable_fails_loudly_unless_transport_direct(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1", tmux=True)
    h.tmux = False

    out = h.restore()
    assert out["results"] == [{"session_id": "broker-1", "thread_id": "thread-broker-1", "outcome": "failed", "error": "tmux unavailable"}]
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


def test_invalid_settings_launch_errors_and_surprises_are_per_entry_failures(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1", reasoning_effort="bogus")
    h.entry("broker-2")
    h.entry("broker-3")
    h.entry("broker-4")

    def spawn(**kwargs):
        h.spawns.append(kwargs)
        target = kwargs["resume_session_id"]
        if target == "thread-broker-2":
            raise ValueError("resume target is already live as broker-9")
        if target == "thread-broker-3":
            raise _LaunchError("broker exited early", {"stage": "broker_early_exit"})
        if target == "thread-broker-4":
            raise RuntimeError("queue persistence exploded")
        return {"broker_pid": 100}

    object.__setattr__(h.deps, "spawn_web_session", spawn)
    h.entry("broker-5")
    out = h.restore()

    by_id = {row["session_id"]: row for row in out["results"]}
    assert "reasoning_effort" in by_id["broker-1"]["error"]
    assert by_id["broker-2"]["error"] == "resume target is already live as broker-9"
    assert by_id["broker-3"]["error"] == "broker exited early"
    assert by_id["broker-4"]["error"] == "unexpected error: RuntimeError: queue persistence exploded"
    assert by_id["broker-5"]["outcome"] == "launched"
    assert (out["restored"], out["skipped"], out["failed"]) == (1, 0, 4)
    # One entry's surprise never aborts the batch: every entry was attempted.
    assert len(h.spawns) == 4
    # Failures keep their entries (except a dropped missing-log case).
    assert set(h.store.read().entries) == {("pi", f"thread-broker-{i}") for i in range(1, 6)}


def test_outcomes_aggregate_per_entry_in_snapshot_order(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.entry("broker-2")
    h.entry("broker-3", log_exists=False)
    h.live_by_thread["thread-broker-2"] = "broker-2"

    out = h.restore()

    assert [row["outcome"] for row in out["results"]] == ["launched", "skipped_live", "failed"]
    assert (out["restored"], out["skipped"], out["failed"]) == (1, 1, 1)
    # The live entry and the launched (restoring-marked) entry stay; the dropped one is gone.
    assert set(h.store.read().entries) == {("pi", "thread-broker-1"), ("pi", "thread-broker-2")}


def test_include_accepts_registry_ids_or_thread_ids(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    h.entry("broker-2", thread_id="t-custom")
    h.entry("broker-3")

    out = h.restore(include=("t-custom", "broker-3", "unknown"))

    assert [row["session_id"] for row in out["results"]] == ["broker-2", "broker-3"]
    assert h.store.read().entries[("pi", "thread-broker-1")].restoring is None


def test_pending_launch_result_is_reported_without_new_session_id(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.entry("broker-1")
    object.__setattr__(h.deps, "spawn_web_session", lambda **kwargs: {"launch_id": "launch-1", "pending": True, "tmux_session": "codoxear"})

    out = h.restore()

    assert out["results"] == [{"session_id": "broker-1", "thread_id": "thread-broker-1", "outcome": "launched", "pending": True, "launch_id": "launch-1"}]
    stored = h.store.read().entries[("pi", "thread-broker-1")]
    assert stored.restoring == RestoringMark(broker_pid=None, launched_at=10.0)


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
        results.append(h.restore())

    threads = [threading.Thread(target=run) for _ in range(2)]
    threads[0].start()
    threads[1].start()
    h.spawn_gate.set()
    for thread in threads:
        thread.join(5)

    assert len(h.spawns) == 1
    launched = [r for r in results if r["restored"] == 1]
    skipped = [r for r in results if r["skipped_restoring"] == 1]
    assert len(launched) == 1 and len(skipped) == 1


def test_parse_restore_request_defaults_and_validation() -> None:
    assert parse_restore_request(None) == SnapshotRestoreRequest(transport="as_saved", include=None)
    assert parse_restore_request({}) == SnapshotRestoreRequest(transport="as_saved", include=None)
    assert parse_restore_request({"transport": "direct", "include": [" a ", "b"]}) == SnapshotRestoreRequest(transport="direct", include=("a", "b"))
    with pytest.raises(ValueError, match="transport"):
        parse_restore_request({"transport": "tmux"})
    with pytest.raises(ValueError, match="session or thread ids"):
        parse_restore_request({"include": ["", "b"]})
    with pytest.raises(ValueError, match="session or thread ids"):
        parse_restore_request({"include": "a"})
