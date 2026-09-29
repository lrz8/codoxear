from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from codoxear.session_snapshot import PendingRemap
from codoxear.session_snapshot import SessionSnapshotStore
from codoxear.session_snapshot import SnapshotCorrupt
from codoxear.session_snapshot import SnapshotEntry
from codoxear.session_snapshot import SnapshotState
from codoxear.session_snapshot import SnapshotWriteRefused
from codoxear.session_snapshot import snapshot_path


def _entry(session_id: str, *, thread_id: str | None = None, log_path: str | None = None, **overrides) -> SnapshotEntry:
    values = dict(
        session_id=session_id,
        thread_id=thread_id or f"thread-{session_id}",
        agent_backend="pi",
        log_path=log_path or f"/logs/{session_id}.jsonl",
        cwd="/repo",
        model_provider="dexgem",
        preferred_auth_method=None,
        model="kimi",
        reasoning_effort="high",
        service_tier=None,
        tmux=True,
        saved_at=0.0,
    )
    values.update(overrides)
    return SnapshotEntry(**values)


def _store(tmp_path: Path, *, clock: list[float] | None = None) -> SessionSnapshotStore:
    ticks = iter(clock or [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0])
    return SessionSnapshotStore(snapshot_path(tmp_path), now=lambda: next(ticks))


def test_missing_file_reads_as_empty_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.read()
    assert state == SnapshotState(saved_at=None, entries={}, pending_remaps=[])
    assert not store.exists()


def test_upsert_round_trips_entries_with_private_permissions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    changed = store.upsert([_entry("broker-1"), _entry("broker-2", tmux=False)])

    assert changed == ["broker-1", "broker-2"]
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert [row["session_id"] for row in on_disk["entries"]] == ["broker-1", "broker-2"]
    state = store.read()
    assert state.saved_at is not None
    assert state.entries["broker-2"].tmux is False
    assert state.entries["broker-1"].launch_fields() == _entry("broker-1").launch_fields()
    assert state.entries["broker-1"].saved_at > 0


def test_upsert_is_a_no_op_for_unchanged_launch_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1")])
    first = store.read()
    before = store.path.stat().st_mtime_ns

    assert store.upsert([_entry("broker-1", saved_at=999.0)]) == []
    assert store.path.stat().st_mtime_ns == before
    assert store.read() == first


def test_upsert_updates_changed_launch_fields_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1"), _entry("broker-2")])
    unchanged_saved_at = store.read().entries["broker-2"].saved_at

    assert store.upsert([_entry("broker-1", reasoning_effort="low"), _entry("broker-2")]) == ["broker-1"]
    state = store.read()
    assert state.entries["broker-1"].reasoning_effort == "low"
    assert state.entries["broker-2"].saved_at == unchanged_saved_at


def test_upsert_never_removes_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1"), _entry("broker-2")])

    store.upsert([_entry("broker-3")])
    assert set(store.read().entries) == {"broker-1", "broker-2", "broker-3"}

    assert store.upsert([]) == []
    assert set(store.read().entries) == {"broker-1", "broker-2", "broker-3"}


def test_upsert_of_a_resumed_session_supersedes_its_dead_incarnation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1", thread_id="t1", log_path="/logs/t1.jsonl"), _entry("broker-9")])

    same_thread = _entry("broker-2", thread_id="t1", log_path="/logs/t1-new.jsonl")
    assert store.upsert([same_thread]) == ["broker-2"]
    assert set(store.read().entries) == {"broker-2", "broker-9"}

    same_log = _entry("broker-3", thread_id="t1-renamed", log_path="/logs/t1-new.jsonl")
    assert store.upsert([same_log]) == ["broker-3"]
    assert set(store.read().entries) == {"broker-3", "broker-9"}

    other_backend = _entry("broker-4", thread_id="t1-renamed", log_path="/logs/t1-new.jsonl", agent_backend="codex")
    store.upsert([other_backend])
    assert set(store.read().entries) == {"broker-3", "broker-4", "broker-9"}


def test_empty_write_over_non_empty_snapshot_is_refused_unless_explicit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1")])
    previous = store.read()

    with pytest.raises(SnapshotWriteRefused):
        store._write(previous, SnapshotState(saved_at=1.0, entries={}, pending_remaps=[]), allow_empty=False)
    assert store.read() == previous

    assert store.remove(["broker-1"]) == ["broker-1"]
    assert store.read().entries == {}


def test_remove_only_touches_known_ids_and_keeps_pending_remaps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1"), _entry("broker-2")])
    remap = PendingRemap(old_session_id="broker-0", old_log_path="/logs/0.jsonl", thread_id="t0", broker_pid=42, launched_at=5.0)
    store.consume("broker-0", pending_remap=remap)
    saved_at = store.read().saved_at

    assert store.remove(["broker-2", "unknown"]) == ["broker-2"]
    assert store.remove(["unknown"]) == []
    state = store.read()
    assert set(state.entries) == {"broker-1"}
    assert state.pending_remaps == [remap]
    assert state.saved_at == saved_at


def test_consume_removes_entry_and_records_pending_remap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1"), _entry("broker-2")])
    remap = PendingRemap(old_session_id="broker-1", old_log_path="/logs/broker-1.jsonl", thread_id="thread-broker-1", broker_pid=None, launched_at=7.0)

    assert store.consume("broker-1", pending_remap=remap) is True
    state = store.read()
    assert set(state.entries) == {"broker-2"}
    assert state.pending_remaps == [remap]

    assert store.consume("broker-2", pending_remap=None) is True
    assert store.read().entries == {}
    assert store.consume("broker-2", pending_remap=None) is False

    assert store.clear_pending_remap("broker-1") is True
    assert store.read().pending_remaps == []
    assert store.clear_pending_remap("broker-1") is False


def test_consume_replaces_an_existing_pending_remap_for_the_same_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert([_entry("broker-1")])
    first = PendingRemap(old_session_id="broker-1", old_log_path="/l", thread_id="t", broker_pid=1, launched_at=1.0)
    second = PendingRemap(old_session_id="broker-1", old_log_path="/l", thread_id="t", broker_pid=2, launched_at=2.0)
    store.consume("broker-1", pending_remap=first)
    store.consume("broker-1", pending_remap=second)
    assert store.read().pending_remaps == [second]


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "entries": []}),
        json.dumps({"version": 1, "entries": {}}),
        json.dumps({"version": 1, "entries": [{"session_id": "x"}]}),
        json.dumps({"version": 1, "entries": [], "pending_remaps": [{"old_session_id": "x"}]}),
    ],
)
def test_corrupt_snapshot_raises_a_clear_error_and_is_not_overwritten_by_upsert(tmp_path: Path, raw: str) -> None:
    store = _store(tmp_path)
    store.path.write_text(raw, encoding="utf-8")

    with pytest.raises(SnapshotCorrupt):
        store.read()
    with pytest.raises(SnapshotCorrupt):
        store.upsert([_entry("broker-1")])
    assert store.path.read_text(encoding="utf-8") == raw


def test_entry_dict_round_trip_preserves_optional_fields() -> None:
    entry = _entry("broker-7", preferred_auth_method="apikey", service_tier="fast", agent_backend="codex", tmux=False, saved_at=12.5)
    assert SnapshotEntry.from_dict(entry.to_dict()) == entry
    assert SnapshotEntry.from_dict({**entry.to_dict(), "model": "  "}).model is None
    remap = PendingRemap(old_session_id="a", old_log_path="/l", thread_id="t", broker_pid=3, launched_at=1.0)
    assert PendingRemap.from_dict(remap.to_dict()) == remap
    assert PendingRemap.from_dict({**remap.to_dict(), "broker_pid": True}).broker_pid is None
