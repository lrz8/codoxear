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
SNAPSHOT_ENTRY_PREFIX = SNAPSHOT_PATH + "/"


@dataclass(frozen=True)
class SnapshotRouteDeps:
    require_auth: Callable[[Any], bool]
    json_response: Callable[[Any, int, dict[str, Any]], None]
    read_body: Callable[[Any], bytes]


def public_snapshot_entry(entry: SnapshotEntry, *, annotation: dict[str, Any]) -> dict[str, Any]:
    """Frozen contract fields plus the coordinator's ``live``/``restoring`` annotation.

    ``live`` is true whenever the entry cannot be restored right now: its
    transcript is running, or a restore already launched a replacement that
    has not bound yet. The row counts ``!live`` entries as restorable.
    """
    return {**entry.to_dict(), **annotation, "live": bool(annotation.get("live"))}


def public_snapshot(state: SnapshotState, *, annotate: Callable[[SnapshotEntry], dict[str, Any]]) -> dict[str, Any]:
    return {
        "saved_at": state.saved_at,
        "entries": [public_snapshot_entry(entry, annotation=annotate(entry)) for entry in state.entries.values()],
    }


def snapshot_entry_identifier(path: str) -> str | None:
    """``/api/sessions/snapshot/<id>`` → ``<id>`` (registry session id or thread id), else None."""
    if not path.startswith(SNAPSHOT_ENTRY_PREFIX) or path == SNAPSHOT_RESTORE_PATH:
        return None
    identifier = path[len(SNAPSHOT_ENTRY_PREFIX):]
    if not identifier or "/" in identifier:
        return None
    return identifier


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


def handle_snapshot_delete_route(handler: Any, *, path: str, manager: Any, deps: SnapshotRouteDeps) -> bool:
    identifier = snapshot_entry_identifier(path)
    if identifier is None:
        return False
    if not _authorized(handler, deps):
        return True
    try:
        removed = manager.snapshot_dismiss(identifier)
    except SnapshotCorrupt as exc:
        deps.json_response(handler, 409, {"error": f"session snapshot is unreadable: {exc}"})
        return True
    deps.json_response(handler, 200, {"removed": bool(removed)})
    return True
