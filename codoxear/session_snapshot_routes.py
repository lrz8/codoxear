from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from .session_snapshot import SnapshotCorrupt
from .session_snapshot import SnapshotEntry
from .session_snapshot import SnapshotState
from .session_snapshot_restore import SnapshotRestoreUnavailable
from .session_snapshot_restore import parse_restore_request


SNAPSHOT_PATH = "/api/sessions/snapshot"
SNAPSHOT_RESTORE_PATH = "/api/sessions/snapshot/restore"


@dataclass(frozen=True)
class SnapshotRouteDeps:
    require_auth: Callable[[Any], bool]
    json_response: Callable[[Any, int, dict[str, Any]], None]
    read_body: Callable[[Any], bytes]


def public_snapshot_entry(entry: SnapshotEntry, *, live: bool) -> dict[str, Any]:
    return {**entry.to_dict(), "live": bool(live)}


def public_snapshot(state: SnapshotState, *, is_live: Callable[[SnapshotEntry], bool]) -> dict[str, Any]:
    return {
        "saved_at": state.saved_at,
        "entries": [public_snapshot_entry(entry, live=is_live(entry)) for entry in state.entries.values()],
    }


def _optional_json_object(raw: bytes) -> dict[str, Any]:
    """Restore accepts no body ("everything, as saved") or a JSON object of options."""
    text = raw.decode("utf-8", errors="strict") if raw else ""
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid json body") from exc
    if not isinstance(obj, dict):
        raise ValueError("invalid json body (expected object)")
    return obj


def _authorized(handler: Any, deps: SnapshotRouteDeps) -> bool:
    if deps.require_auth(handler):
        return True
    handler._unauthorized()
    return False


def handle_snapshot_get_route(handler: Any, *, path: str, manager: Any, deps: SnapshotRouteDeps) -> bool:
    if path != SNAPSHOT_PATH:
        return False
    if not _authorized(handler, deps):
        return True
    try:
        deps.json_response(handler, 200, manager.snapshot_get())
    except SnapshotCorrupt as exc:
        deps.json_response(handler, 409, {"error": f"session snapshot is unreadable: {exc}"})
    return True


def handle_snapshot_post_route(handler: Any, *, path: str, manager: Any, deps: SnapshotRouteDeps) -> bool:
    if path == SNAPSHOT_PATH:
        if not _authorized(handler, deps):
            return True
        try:
            deps.json_response(handler, 200, manager.snapshot_save())
        except SnapshotCorrupt as exc:
            deps.json_response(handler, 409, {"error": f"session snapshot is unreadable: {exc}"})
        return True
    if path != SNAPSHOT_RESTORE_PATH:
        return False
    if not _authorized(handler, deps):
        return True
    try:
        request = parse_restore_request(_optional_json_object(deps.read_body(handler)))
    except ValueError as exc:
        deps.json_response(handler, 400, {"error": str(exc)})
        return True
    try:
        deps.json_response(handler, 200, manager.snapshot_restore(request))
    except SnapshotRestoreUnavailable as exc:
        deps.json_response(handler, 409, {"error": str(exc)})
    return True
