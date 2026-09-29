"""Persistent snapshot of live session launch configuration.

The snapshot is the durable record used to respawn sessions after broker
death (reboot, crash, server outage). It is a map keyed by the registry
session id (the broker sidecar's sock stem, e.g. ``broker-<pid>``) — the id
the UI, queue store, and draft store all key on. Each entry also carries the
backend ``thread_id`` (the transcript id the harness resumes) and the bound
``log_path``; those two fields are the transcript's *continuity identity*:
a resumed session gets a fresh registry id (new broker pid) but keeps its
thread id and log, so an upsert for the new id supersedes any stale entry
that still points at the same transcript.

Write policy is upsert-only: discovery adds/updates entries for live
sessions and never removes them. Removal happens only through explicit
events (UI delete, detected clean broker exit, successful restore
consumption). As belt-and-braces against a null live list wiping a
meaningful snapshot, an upsert write that would leave zero entries over a
non-empty on-disk snapshot is refused; only the explicit removal paths may
persist an empty entry list.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable
import uuid


SNAPSHOT_VERSION = 1
SNAPSHOT_FILENAME = "session_snapshot.json"


class SnapshotCorrupt(ValueError):
    """The on-disk snapshot cannot be parsed into the documented shape."""


class SnapshotWriteRefused(RuntimeError):
    """An upsert write would have persisted an empty entry list over a non-empty snapshot."""


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    out = value.strip()
    return out or None


def _required_text(raw: dict[str, Any], key: str) -> str:
    out = _clean_text(raw.get(key))
    if out is None:
        raise SnapshotCorrupt(f"snapshot entry is missing {key}")
    return out


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class SnapshotEntry:
    session_id: str
    thread_id: str
    agent_backend: str
    log_path: str
    cwd: str
    model_provider: str | None
    preferred_auth_method: str | None
    model: str | None
    reasoning_effort: str | None
    service_tier: str | None
    tmux: bool
    saved_at: float

    def launch_fields(self) -> tuple[Any, ...]:
        """Everything that decides how the session is respawned; ``saved_at`` excluded."""
        return (
            self.session_id,
            self.thread_id,
            self.agent_backend,
            self.log_path,
            self.cwd,
            self.model_provider,
            self.preferred_auth_method,
            self.model,
            self.reasoning_effort,
            self.service_tier,
            self.tmux,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "agent_backend": self.agent_backend,
            "log_path": self.log_path,
            "cwd": self.cwd,
            "model_provider": self.model_provider,
            "preferred_auth_method": self.preferred_auth_method,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "tmux": bool(self.tmux),
            "saved_at": float(self.saved_at),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SnapshotEntry":
        if not isinstance(raw, dict):
            raise SnapshotCorrupt("snapshot entry must be an object")
        saved_at = _float_or_none(raw.get("saved_at"))
        return cls(
            session_id=_required_text(raw, "session_id"),
            thread_id=_required_text(raw, "thread_id"),
            agent_backend=_required_text(raw, "agent_backend"),
            log_path=_required_text(raw, "log_path"),
            cwd=_required_text(raw, "cwd"),
            model_provider=_clean_text(raw.get("model_provider")),
            preferred_auth_method=_clean_text(raw.get("preferred_auth_method")),
            model=_clean_text(raw.get("model")),
            reasoning_effort=_clean_text(raw.get("reasoning_effort")),
            service_tier=_clean_text(raw.get("service_tier")),
            tmux=bool(raw.get("tmux")),
            saved_at=saved_at if saved_at is not None else 0.0,
        )


@dataclass(frozen=True)
class PendingRemap:
    """A restore launched a replacement whose registry id was not yet known.

    Resolved when discovery registers a session that proves continuity:
    the launched broker pid, or a sidecar whose ``resume_session_id``/thread
    id equals the consumed entry's thread id.
    """

    old_session_id: str
    old_log_path: str
    thread_id: str
    broker_pid: int | None
    launched_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_session_id": self.old_session_id,
            "old_log_path": self.old_log_path,
            "thread_id": self.thread_id,
            "broker_pid": self.broker_pid,
            "launched_at": float(self.launched_at),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PendingRemap":
        if not isinstance(raw, dict):
            raise SnapshotCorrupt("pending remap must be an object")
        broker_pid = raw.get("broker_pid")
        launched_at = _float_or_none(raw.get("launched_at"))
        return cls(
            old_session_id=_required_text(raw, "old_session_id"),
            old_log_path=_required_text(raw, "old_log_path"),
            thread_id=_required_text(raw, "thread_id"),
            broker_pid=int(broker_pid) if isinstance(broker_pid, int) and not isinstance(broker_pid, bool) and broker_pid > 0 else None,
            launched_at=launched_at if launched_at is not None else 0.0,
        )


@dataclass(frozen=True)
class SnapshotState:
    saved_at: float | None
    entries: dict[str, SnapshotEntry]
    pending_remaps: list[PendingRemap]

    @classmethod
    def empty(cls) -> "SnapshotState":
        return cls(saved_at=None, entries={}, pending_remaps=[])


def snapshot_path(app_dir: Path) -> Path:
    return app_dir / SNAPSHOT_FILENAME


def _same_transcript(a: SnapshotEntry, b: SnapshotEntry) -> bool:
    if a.agent_backend != b.agent_backend:
        return False
    if a.thread_id == b.thread_id:
        return True
    try:
        return Path(a.log_path).resolve() == Path(b.log_path).resolve()
    except OSError:
        return a.log_path == b.log_path


def _parse_state(obj: Any) -> SnapshotState:
    if not isinstance(obj, dict):
        raise SnapshotCorrupt("snapshot must be a JSON object")
    version = obj.get("version")
    if version != SNAPSHOT_VERSION:
        raise SnapshotCorrupt(f"unsupported snapshot version {version!r}")
    raw_entries = obj.get("entries")
    if not isinstance(raw_entries, list):
        raise SnapshotCorrupt("snapshot entries must be a list")
    entries: dict[str, SnapshotEntry] = {}
    for raw in raw_entries:
        entry = SnapshotEntry.from_dict(raw)
        entries[entry.session_id] = entry
    raw_remaps = obj.get("pending_remaps", [])
    if not isinstance(raw_remaps, list):
        raise SnapshotCorrupt("snapshot pending_remaps must be a list")
    pending = [PendingRemap.from_dict(raw) for raw in raw_remaps]
    return SnapshotState(saved_at=_float_or_none(obj.get("saved_at")), entries=entries, pending_remaps=pending)


def _serialize_state(state: SnapshotState) -> dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "saved_at": state.saved_at,
        "entries": [entry.to_dict() for entry in state.entries.values()],
        "pending_remaps": [remap.to_dict() for remap in state.pending_remaps],
    }


def _write_private_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    data = (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


class SessionSnapshotStore:
    """Read-modify-write store for the snapshot file. All mutations serialize on one lock."""

    def __init__(self, path: Path, *, now: Callable[[], float] | None = None) -> None:
        self.path = path
        self._now = now or time.time
        self._lock = threading.Lock()

    def read(self) -> SnapshotState:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SnapshotState.empty()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SnapshotCorrupt(f"snapshot is not valid JSON: {exc}") from exc
        return _parse_state(obj)

    def exists(self) -> bool:
        return self.path.exists()

    def _write(self, previous: SnapshotState, state: SnapshotState, *, allow_empty: bool) -> None:
        if not state.entries and previous.entries and not allow_empty:
            raise SnapshotWriteRefused("refusing to persist an empty session snapshot over a non-empty one")
        _write_private_json(self.path, _serialize_state(state))

    def upsert(self, entries: Iterable[SnapshotEntry]) -> list[str]:
        """Add or update entries; returns ids whose launch fields changed.

        An entry for a new registry id that points at a transcript already
        held under another id replaces that stale entry (same thread id or
        same log path): the live session is the continuation of the dead one.
        """
        incoming = list(entries)
        with self._lock:
            previous = self.read()
            merged = dict(previous.entries)
            changed: list[str] = []
            for entry in incoming:
                current = merged.get(entry.session_id)
                superseded = [
                    sid
                    for sid, other in merged.items()
                    if sid != entry.session_id and _same_transcript(entry, other)
                ]
                for sid in superseded:
                    merged.pop(sid, None)
                if current is not None and current.launch_fields() == entry.launch_fields() and not superseded:
                    continue
                merged[entry.session_id] = replace(entry, saved_at=self._now())
                changed.append(entry.session_id)
            if not changed:
                return []
            state = SnapshotState(saved_at=self._now(), entries=merged, pending_remaps=list(previous.pending_remaps))
            self._write(previous, state, allow_empty=False)
            return changed

    def remove(self, session_ids: Iterable[str]) -> list[str]:
        ids = [sid for sid in session_ids if isinstance(sid, str) and sid]
        with self._lock:
            previous = self.read()
            removed = [sid for sid in ids if sid in previous.entries]
            if not removed:
                return []
            merged = {sid: entry for sid, entry in previous.entries.items() if sid not in removed}
            state = SnapshotState(saved_at=previous.saved_at, entries=merged, pending_remaps=list(previous.pending_remaps))
            self._write(previous, state, allow_empty=True)
            return removed

    def consume(self, session_id: str, *, pending_remap: PendingRemap | None) -> bool:
        """Remove a restored entry and optionally record its identity remap."""
        with self._lock:
            previous = self.read()
            if session_id not in previous.entries and pending_remap is None:
                return False
            merged = {sid: entry for sid, entry in previous.entries.items() if sid != session_id}
            remaps = [remap for remap in previous.pending_remaps if remap.old_session_id != session_id]
            if pending_remap is not None:
                remaps.append(pending_remap)
            state = SnapshotState(saved_at=previous.saved_at, entries=merged, pending_remaps=remaps)
            self._write(previous, state, allow_empty=True)
            return True

    def clear_pending_remap(self, old_session_id: str) -> bool:
        with self._lock:
            previous = self.read()
            remaps = [remap for remap in previous.pending_remaps if remap.old_session_id != old_session_id]
            if len(remaps) == len(previous.pending_remaps):
                return False
            state = SnapshotState(saved_at=previous.saved_at, entries=dict(previous.entries), pending_remaps=remaps)
            self._write(previous, state, allow_empty=True)
            return True
