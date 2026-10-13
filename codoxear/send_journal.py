"""Durable write-ahead journal of confirmed sends and enqueues.

A confirmed send can be eaten by the backend (for example a modal dialog that
consumes the injected paste) and never reach the backend log. The journal keeps
the user's text on disk, written and fsync'd before the broker is called, so it
stays recoverable through ``GET /api/sessions/<id>/sent-history``.

One append-only JSONL file per session: ``<root>/<session_id>.jsonl``.

* submit row  ``{"v":1,"type":"submit","id","ts","source","text","display_text",
  "attachment_paths","queue_item_id"}`` — written before dispatch.
* outcome row ``{"v":1,"type":"outcome","id","ts","outcome","error"?}`` — appended
  after dispatch resolves; ``outcome`` is ``ack``, ``commit_unknown`` or ``error``.

A queued prompt is journaled once, at enqueue time, under its queue item id; its
later dispatch appends only an outcome row for that id.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SOURCES = ("send", "enqueue", "voice")
OUTCOMES = ("ack", "commit_unknown", "error")

MAX_SUBMITS = 200
MAX_BYTES = 2 * 1024 * 1024

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CURSOR_PREFIX = "j1:"

# Appends and compactions of every journal file go through one lock: writers are
# server threads of a single process, and writes are short.
_LOCK = threading.Lock()


class CursorError(ValueError):
    pass


def new_send_id() -> str:
    return f"send-{secrets.token_hex(8)}"


def _encode_cursor(entry_id: str) -> str:
    return base64.urlsafe_b64encode((_CURSOR_PREFIX + entry_id).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CursorError("invalid cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX) or len(raw) == len(_CURSOR_PREFIX):
        raise CursorError("invalid cursor")
    return raw[len(_CURSOR_PREFIX):]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse_rows(data: bytes) -> list[tuple[bytes, dict[str, Any]]]:
    """Return ``(raw_line, row)`` for every well-formed v1 row; torn lines are skipped."""
    rows: list[tuple[bytes, dict[str, Any]]] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("v") != 1 or not isinstance(row.get("id"), str):
            continue
        if row.get("type") not in ("submit", "outcome"):
            continue
        rows.append((line, row))
    return rows


def _compact(rows: list[tuple[bytes, dict[str, Any]]], *, keep_submits: int, keep_bytes: int) -> list[bytes]:
    """Keep the newest submits within both budgets (at least one), plus their outcomes."""
    submit_ids: list[str] = []
    submit_sizes: dict[str, int] = {}
    for line, row in rows:
        if row["type"] == "submit":
            submit_ids.append(row["id"])
            submit_sizes[row["id"]] = submit_sizes.get(row["id"], 0) + len(line) + 1
    kept: set[str] = set()
    total = 0
    for sid in reversed(submit_ids):
        if sid in kept:
            continue
        size = submit_sizes[sid]
        if kept and (len(kept) >= keep_submits or total + size > keep_bytes):
            break
        kept.add(sid)
        total += size
    return [line for line, row in rows if row["id"] in kept]


@dataclass(frozen=True)
class SentEntry:
    id: str
    ts: float
    text: str
    source: str
    outcome: str | None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "text": self.text, "source": self.source, "outcome": self.outcome}


class SendJournal:
    def __init__(
        self,
        root: Path,
        *,
        max_submits: int = MAX_SUBMITS,
        max_bytes: int = MAX_BYTES,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.max_submits = max_submits
        self.max_bytes = max_bytes
        self.now = now

    def path_for(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id) or session_id.endswith("."):
            raise ValueError("invalid session_id")
        return self.root / f"{session_id}.jsonl"

    def record_submit(
        self,
        session_id: str,
        *,
        source: str,
        text: str,
        display_text: str,
        attachment_paths: list[str] | None = None,
        queue_item_id: str | None = None,
        entry_id: str | None = None,
    ) -> str:
        if source not in SOURCES:
            raise ValueError(f"unknown journal source: {source}")
        entry_id = entry_id or new_send_id()
        self._append(
            session_id,
            {
                "v": 1,
                "type": "submit",
                "id": entry_id,
                "ts": self.now(),
                "source": source,
                "text": text,
                "display_text": display_text,
                "attachment_paths": list(attachment_paths or []),
                "queue_item_id": queue_item_id,
            },
        )
        return entry_id

    def record_outcome(self, session_id: str, entry_id: str, outcome: str, error: str | None = None) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown journal outcome: {outcome}")
        row: dict[str, Any] = {"v": 1, "type": "outcome", "id": entry_id, "ts": self.now(), "outcome": outcome}
        if error:
            row["error"] = error
        self._append(session_id, row)

    def remove(self, session_id: str) -> None:
        path = self.path_for(session_id)
        with _LOCK:
            try:
                path.unlink()
            except FileNotFoundError:
                return

    def history(self, session_id: str, *, cursor: str | None = None, limit: int = 20) -> tuple[list[SentEntry], str | None]:
        """Newest-first submits joined with their outcomes, plus the cursor for the next page.

        The cursor names the last entry returned. Compaction only drops the
        oldest rows, so a cursor whose entry is gone has nothing older left and
        yields an empty page.
        """
        after_id = _decode_cursor(cursor) if cursor else None
        path = self.path_for(session_id)
        with _LOCK:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                return [], None
        outcomes: dict[str, str] = {}
        submits: list[dict[str, Any]] = []
        for _line, row in _parse_rows(data):
            if row["type"] == "submit":
                submits.append(row)
            elif row.get("outcome") in OUTCOMES:
                outcomes[row["id"]] = row["outcome"]
        newest_first = list(reversed(submits))
        if after_id is not None:
            index = next((i for i, row in enumerate(newest_first) if row["id"] == after_id), None)
            newest_first = [] if index is None else newest_first[index + 1:]
        page = newest_first[: max(0, limit)]
        entries = [
            SentEntry(
                id=row["id"],
                ts=row["ts"] if isinstance(row.get("ts"), (int, float)) else 0.0,
                text=str(row["display_text"] if row.get("display_text") is not None else row.get("text") or ""),
                source=str(row.get("source") or "send"),
                outcome=outcomes.get(row["id"]),
            )
            for row in page
        ]
        more = len(newest_first) > len(page)
        return entries, (_encode_cursor(entries[-1].id) if entries and more else None)

    def _append(self, session_id: str, row: dict[str, Any]) -> None:
        path = self.path_for(session_id)
        line = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with _LOCK:
            created = not path.exists()
            self.root.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as f:
                # A crash mid-write can leave a torn final line; start on a fresh
                # line so the new row is not glued onto it.
                if f.tell() > 0:
                    with open(path, "rb") as probe:
                        probe.seek(-1, os.SEEK_END)
                        if probe.read(1) != b"\n":
                            f.write(b"\n")
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            if created:
                _fsync_dir(self.root)
            if row["type"] == "submit":
                self._compact_if_over_budget(path)

    def _compact_if_over_budget(self, path: Path) -> None:
        data = path.read_bytes()
        rows = _parse_rows(data)
        submits = sum(1 for _line, row in rows if row["type"] == "submit")
        if submits <= self.max_submits and len(data) <= self.max_bytes:
            return
        # Trim below the cap so the next sends do not each rewrite the file.
        kept = _compact(
            rows,
            keep_submits=max(1, self.max_submits * 3 // 4),
            keep_bytes=max(1, self.max_bytes * 3 // 4),
        )
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "wb") as f:
            f.write(b"".join(line + b"\n" for line in kept))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(self.root)
