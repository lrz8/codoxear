"""Auto-save triggers, liveness checks, and identity remap for the session snapshot.

Trigger model (all server-side, none in a request handler's critical path):

* ``observe_registrations`` — called after every discovery pass with the
  live registrations. A registration's launch-relevant signature (thread,
  backend, cwd, log, tmux transport, provider/auth/model/effort/tier) is
  diffed against the last one seen for that registry id; a change marks the
  id dirty and arms one coalescing debounce timer. The flush re-reads each
  dirty session's sidecar and reconciles its *raw* launch settings through
  ``snapshot_run_settings`` (live bridge values > sidecar launch values >
  log replay) before upserting.
* ``session_sock_vanished`` — the prune coordinator's "registered session
  whose control socket file is gone" branch. A broker unlinks its socket and
  sidecar only in its own teardown, so a vanished socket is the clean-exit
  signal; a crash leaves the files behind and produces a lost tombstone
  instead, which keeps the entry.
* ``session_deleted`` — explicit UI/API delete.
* ``consume_restored`` — a restore launched a replacement; the entry is
  removed and its queue/draft/unattended keys are remapped to the new
  registry id, immediately when the new id is already known, otherwise when
  a later discovery proves continuity (``resolve_pending_remaps``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
import threading
from typing import Any, Callable, Iterable, MutableMapping, Protocol

from .session_discovery import DiscoveryRegistration
from .session_model import Session
from .session_snapshot import PendingRemap
from .session_snapshot import SessionSnapshotStore
from .session_snapshot import SnapshotCorrupt
from .session_snapshot import SnapshotEntry
from .session_snapshot import SnapshotState
from .session_snapshot import SnapshotWriteRefused


class _TimerLike(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


def default_timer_factory(delay_seconds: float, fn: Callable[[], None]) -> _TimerLike:
    timer = threading.Timer(delay_seconds, fn)
    timer.daemon = True
    return timer


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    out = value.strip()
    return out or None


def registration_signature(registration: DiscoveryRegistration) -> tuple[Any, ...]:
    return (
        registration.thread_id,
        registration.agent_backend,
        registration.cwd,
        str(registration.log_path) if registration.log_path is not None else None,
        registration.transport == "tmux",
        registration.model_provider,
        registration.preferred_auth_method,
        registration.model,
        registration.reasoning_effort,
        registration.service_tier,
    )


@dataclass(frozen=True)
class RemapResult:
    queues_moved: bool
    draft_moved: bool
    unattended_moved: bool


@dataclass
class SessionSnapshotCoordinator:
    store: SessionSnapshotStore
    lock: Any
    sessions: Callable[[], MutableMapping[str, Session]]
    session_store: Callable[[], Any]
    save_queues: Callable[[], None]
    save_drafts: Callable[[], None]
    save_unattended: Callable[[], None]
    read_sidecar_metadata: Callable[[Path], dict[str, Any]]
    snapshot_run_settings: Callable[[dict[str, Any], Path | None, str], tuple[str | None, str | None, str | None, str | None]]
    live_session_for_resume_target: Callable[[str, dict[str, Any]], Session | None]
    pid_alive: Callable[[int], bool]
    now: Callable[[], float]
    debounce_seconds: float = 2.0
    timer_factory: Callable[[float, Callable[[], None]], _TimerLike] = default_timer_factory
    stderr: Any = sys.stderr
    _state_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)
    _signatures: dict[str, tuple[Any, ...]] = field(init=False, default_factory=dict, repr=False)
    _dirty: set[str] = field(init=False, default_factory=set, repr=False)
    _timer: _TimerLike | None = field(init=False, default=None, repr=False)
    _pending_remaps: list[PendingRemap] | None = field(init=False, default=None, repr=False)
    # Serializes restore runs: two concurrent restores must not both spawn one entry.
    restore_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------ #
    # Discovery trigger
    # ------------------------------------------------------------------ #

    def observe_registrations(self, registrations: Iterable[DiscoveryRegistration]) -> None:
        rows = list(registrations)
        with self._state_lock:
            for registration in rows:
                if registration.log_path is None:
                    # Nothing to resume yet; the signature changes once the
                    # transcript binds, which marks the session dirty then.
                    continue
                signature = registration_signature(registration)
                if self._signatures.get(registration.session_id) != signature:
                    self._signatures[registration.session_id] = signature
                    self._dirty.add(registration.session_id)
            if self._dirty:
                self._schedule_flush_locked()
        self.resolve_pending_remaps(rows)

    def _schedule_flush_locked(self) -> None:
        if self._timer is not None:
            return
        timer = self.timer_factory(self.debounce_seconds, self._flush)
        self._timer = timer
        timer.start()

    def _flush(self) -> None:
        with self._state_lock:
            dirty = set(self._dirty)
            self._dirty.clear()
            self._timer = None
        if not dirty:
            return
        entries = self._entries_for_session_ids(dirty)
        self._upsert(entries)

    def _upsert(self, entries: list[SnapshotEntry]) -> list[str]:
        if not entries:
            return []
        try:
            return self.store.upsert(entries)
        except (SnapshotCorrupt, SnapshotWriteRefused, OSError) as exc:
            self._log(f"session snapshot upsert failed: {type(exc).__name__}: {exc}")
            return []

    def _log(self, message: str) -> None:
        try:
            self.stderr.write(f"error: {message}\n")
            self.stderr.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Entry construction
    # ------------------------------------------------------------------ #

    def _live_sessions(self) -> list[Session]:
        with self.lock:
            values = list(self.sessions().values())
        return [s for s in values if isinstance(s, Session) and not s.lost and self.pid_alive(int(s.broker_pid))]

    def _entries_for_sessions(self, sessions: Iterable[Session]) -> list[SnapshotEntry]:
        out: list[SnapshotEntry] = []
        for session in sessions:
            entry = self.entry_for_session(session)
            if entry is not None:
                out.append(entry)
        return out

    def _entries_for_session_ids(self, session_ids: set[str]) -> list[SnapshotEntry]:
        return self._entries_for_sessions(s for s in self._live_sessions() if s.session_id in session_ids)

    def entry_for_session(self, session: Session) -> SnapshotEntry | None:
        """Build the snapshot entry for a live session, or None when it is not restorable yet.

        A session without a bound log has nothing to resume; it becomes an
        entry once discovery binds its transcript.
        """
        if session.log_path is None:
            return None
        meta_path = session.sock_path.with_suffix(".json")
        try:
            meta = self.read_sidecar_metadata(meta_path)
        except (OSError, ValueError) as exc:
            self._log(f"session snapshot: cannot read sidecar {meta_path}: {type(exc).__name__}: {exc}")
            return None
        thread_id = _clean_text(meta.get("session_id")) or _clean_text(session.thread_id)
        if thread_id is None:
            return None
        backend = session.agent_backend
        model_provider, preferred_auth_method, model, reasoning_effort = self.snapshot_run_settings(meta, session.log_path, backend)
        return SnapshotEntry(
            session_id=session.session_id,
            thread_id=thread_id,
            agent_backend=backend,
            log_path=str(session.log_path),
            cwd=str(session.cwd),
            model_provider=model_provider,
            preferred_auth_method=preferred_auth_method,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=_clean_text(meta.get("service_tier")) if backend == "codex" else None,
            tmux=session.transport == "tmux",
            saved_at=self.now(),
        )

    # ------------------------------------------------------------------ #
    # Manual save / read
    # ------------------------------------------------------------------ #

    def save_now(self) -> SnapshotState:
        """Force-upsert every live session immediately (manual save)."""
        with self._state_lock:
            timer = self._timer
            self._timer = None
            self._dirty.clear()
        if timer is not None:
            timer.cancel()
        self.store.upsert(self._entries_for_sessions(self._live_sessions()))
        return self.store.read()

    def read(self) -> SnapshotState:
        return self.store.read()

    def live_session_for_entry(self, entry: SnapshotEntry) -> Session | None:
        with self.lock:
            current = self.sessions().get(entry.session_id)
        if current is not None and (self.pid_alive(int(current.broker_pid)) or self.pid_alive(int(current.codex_pid))):
            return current
        return self.live_session_for_resume_target(entry.thread_id, {"session_id": entry.thread_id, "log_path": entry.log_path})

    # ------------------------------------------------------------------ #
    # Removal triggers
    # ------------------------------------------------------------------ #

    def session_sock_vanished(self, session: Session) -> None:
        self._forget(session.session_id, reason="clean broker exit")

    def session_deleted(self, session_id: str) -> None:
        self._forget(session_id, reason="session delete")
        self._drop_pending_remap(session_id)

    def _forget(self, session_id: str, *, reason: str) -> None:
        with self._state_lock:
            self._signatures.pop(session_id, None)
            self._dirty.discard(session_id)
        try:
            self.store.remove([session_id])
        except (SnapshotCorrupt, OSError) as exc:
            self._log(f"session snapshot removal on {reason} failed for {session_id}: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # Restore consumption and identity remap
    # ------------------------------------------------------------------ #

    def session_id_for_broker_pid(self, broker_pid: int) -> str | None:
        with self.lock:
            values = list(self.sessions().values())
        for session in values:
            if int(session.broker_pid) == int(broker_pid):
                return session.session_id
        return None

    def consume_restored(self, entry: SnapshotEntry, *, broker_pid: int | None, new_session_id: str | None) -> None:
        if new_session_id is not None:
            self.store.consume(entry.session_id, pending_remap=None)
            self._drop_pending_remap(entry.session_id, persist=False)
            if new_session_id != entry.session_id:
                self.remap_session_identity(old_session_id=entry.session_id, new_session_id=new_session_id, thread_id=entry.thread_id)
            return
        remap = PendingRemap(
            old_session_id=entry.session_id,
            old_log_path=entry.log_path,
            thread_id=entry.thread_id,
            broker_pid=broker_pid,
            launched_at=self.now(),
        )
        self.store.consume(entry.session_id, pending_remap=remap)
        with self._state_lock:
            pending = self._load_pending_locked()
            pending[:] = [p for p in pending if p.old_session_id != remap.old_session_id]
            pending.append(remap)

    def _load_pending_locked(self) -> list[PendingRemap]:
        if self._pending_remaps is None:
            try:
                self._pending_remaps = list(self.store.read().pending_remaps)
            except SnapshotCorrupt as exc:
                self._log(f"session snapshot: cannot load pending remaps: {exc}")
                self._pending_remaps = []
        return self._pending_remaps

    def _drop_pending_remap(self, old_session_id: str, *, persist: bool = True) -> None:
        with self._state_lock:
            pending = self._load_pending_locked()
            pending[:] = [p for p in pending if p.old_session_id != old_session_id]
        if persist:
            try:
                self.store.clear_pending_remap(old_session_id)
            except (SnapshotCorrupt, OSError) as exc:
                self._log(f"session snapshot: cannot clear pending remap for {old_session_id}: {type(exc).__name__}: {exc}")

    def resolve_pending_remaps(self, registrations: Iterable[DiscoveryRegistration]) -> list[tuple[str, str]]:
        """Remap stores for pending records whose replacement is now registered.

        A registration continues a consumed entry's transcript when it is the
        launched broker (pid), declares that thread as its resume target, or
        has bound that thread id.
        """
        with self._state_lock:
            pending = list(self._load_pending_locked())
        if not pending:
            return []
        rows = list(registrations)
        resolved: list[tuple[str, str]] = []
        for remap in pending:
            match = next(
                (
                    r
                    for r in rows
                    if (remap.broker_pid is not None and int(r.broker_pid) == remap.broker_pid)
                    or r.resume_session_id == remap.thread_id
                    or r.thread_id == remap.thread_id
                ),
                None,
            )
            if match is None:
                continue
            if match.session_id != remap.old_session_id:
                self.remap_session_identity(old_session_id=remap.old_session_id, new_session_id=match.session_id, thread_id=remap.thread_id)
            self._drop_pending_remap(remap.old_session_id)
            resolved.append((remap.old_session_id, match.session_id))
        return resolved

    def remap_session_identity(self, *, old_session_id: str, new_session_id: str, thread_id: str) -> RemapResult:
        """Move queue items, the draft, and legacy unattended config from the old registry id to the new one.

        Unattended config is normally already keyed ``thread:<id>`` (resume
        stable); only a legacy session-id key needs to move, and it moves to
        the thread-scoped key the new session reads.
        """
        queues_moved = draft_moved = unattended_moved = False
        with self.lock:
            store = self.session_store()
            queues = store.queues
            old_queue = queues.pop(old_session_id, None)
            if isinstance(old_queue, list) and old_queue:
                existing = queues.get(new_session_id)
                queues[new_session_id] = list(old_queue) + (list(existing) if isinstance(existing, list) else [])
                queues_moved = True
            drafts = store.drafts
            old_draft = drafts.pop(old_session_id, None)
            if isinstance(old_draft, dict) and new_session_id not in drafts:
                drafts[new_session_id] = old_draft
                draft_moved = True
            unattended = store.unattended
            legacy = unattended.pop(old_session_id, None)
            scoped_key = f"thread:{thread_id}"
            if isinstance(legacy, dict) and scoped_key not in unattended:
                unattended[scoped_key] = legacy
                unattended_moved = True
        if queues_moved:
            self.save_queues()
        if draft_moved:
            self.save_drafts()
        if unattended_moved:
            self.save_unattended()
        return RemapResult(queues_moved=queues_moved, draft_moved=draft_moved, unattended_moved=unattended_moved)
