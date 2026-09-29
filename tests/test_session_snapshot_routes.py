from __future__ import annotations

import json

from codoxear.session_snapshot import SnapshotCorrupt
from codoxear.session_snapshot import SnapshotEntry
from codoxear.session_snapshot import SnapshotState
from codoxear.session_snapshot_restore import SnapshotRestoreRequest
from codoxear.session_snapshot_restore import SnapshotRestoreUnavailable
from codoxear.session_snapshot_routes import SnapshotRouteDeps
from codoxear.session_snapshot_routes import handle_snapshot_delete_route
from codoxear.session_snapshot_routes import handle_snapshot_get_route
from codoxear.session_snapshot_routes import handle_snapshot_post_route
from codoxear.session_snapshot_routes import public_snapshot
from codoxear.session_snapshot_routes import snapshot_entry_identifier


class _Handler:
    def __init__(self, body: bytes = b"") -> None:
        self.body = body
        self.unauthorized = False

    def _unauthorized(self) -> None:
        self.unauthorized = True


class _Manager:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.get_error: BaseException | None = None
        self.restore_error: BaseException | None = None
        self.dismiss_error: BaseException | None = None
        self.dismissable: set[str] = set()

    def snapshot_get(self) -> dict:
        self.calls.append("get")
        if self.get_error is not None:
            raise self.get_error
        return {"saved_at": 1.0, "entries": [{"session_id": "broker-1", "live": False}]}

    def snapshot_save(self) -> dict:
        self.calls.append("save")
        return {"saved_at": 2.0, "entries": []}

    def snapshot_restore(self, request: SnapshotRestoreRequest) -> dict:
        self.calls.append(request)
        if self.restore_error is not None:
            raise self.restore_error
        return {"results": [], "restored": 0, "skipped": 0, "skipped_restoring": 0, "failed": 0}

    def snapshot_dismiss(self, identifier: str) -> bool:
        self.calls.append(("dismiss", identifier))
        if self.dismiss_error is not None:
            raise self.dismiss_error
        return identifier in self.dismissable


def _deps(*, authed: bool = True):
    responses: list[tuple[int, dict]] = []
    deps = SnapshotRouteDeps(
        require_auth=lambda _handler: authed,
        json_response=lambda _handler, status, payload: responses.append((status, payload)),
        read_body=lambda handler: handler.body,
    )
    return deps, responses


def test_get_snapshot_returns_manager_projection() -> None:
    deps, responses = _deps()
    manager = _Manager()
    handler = _Handler()

    assert handle_snapshot_get_route(handler, path="/api/sessions/snapshot", manager=manager, deps=deps) is True
    assert responses == [(200, {"saved_at": 1.0, "entries": [{"session_id": "broker-1", "live": False}]})]
    assert manager.calls == ["get"]


def test_snapshot_routes_ignore_other_paths_and_require_auth() -> None:
    deps, responses = _deps()
    manager = _Manager()
    assert handle_snapshot_get_route(_Handler(), path="/api/sessions", manager=manager, deps=deps) is False
    assert handle_snapshot_post_route(_Handler(), path="/api/sessions/s1/read", manager=manager, deps=deps) is False
    assert handle_snapshot_get_route(_Handler(), path="/api/sessions/snapshot/restore", manager=manager, deps=deps) is False
    assert manager.calls == []

    deps, responses = _deps(authed=False)
    for route, path in ((handle_snapshot_get_route, "/api/sessions/snapshot"), (handle_snapshot_post_route, "/api/sessions/snapshot"), (handle_snapshot_post_route, "/api/sessions/snapshot/restore")):
        handler = _Handler()
        assert route(handler, path=path, manager=manager, deps=deps) is True
        assert handler.unauthorized is True
    assert manager.calls == []
    assert responses == []


def test_corrupt_snapshot_is_a_409_on_get_and_save() -> None:
    deps, responses = _deps()
    manager = _Manager()
    manager.get_error = SnapshotCorrupt("snapshot is not valid JSON")

    handle_snapshot_get_route(_Handler(), path="/api/sessions/snapshot", manager=manager, deps=deps)
    assert responses[-1][0] == 409
    assert "not valid JSON" in responses[-1][1]["error"]


def test_post_snapshot_forces_save() -> None:
    deps, responses = _deps()
    manager = _Manager()

    assert handle_snapshot_post_route(_Handler(), path="/api/sessions/snapshot", manager=manager, deps=deps) is True
    assert responses == [(200, {"saved_at": 2.0, "entries": []})]
    assert manager.calls == ["save"]


def test_restore_accepts_empty_body_and_option_body() -> None:
    deps, responses = _deps()
    manager = _Manager()

    handle_snapshot_post_route(_Handler(b""), path="/api/sessions/snapshot/restore", manager=manager, deps=deps)
    handle_snapshot_post_route(_Handler(json.dumps({"transport": "direct", "include": ["broker-1"]}).encode()), path="/api/sessions/snapshot/restore", manager=manager, deps=deps)

    assert manager.calls == [
        SnapshotRestoreRequest(transport="as_saved", include=None),
        SnapshotRestoreRequest(transport="direct", include=("broker-1",)),
    ]
    assert [status for status, _ in responses] == [200, 200]
    assert responses[0][1] == {"results": [], "restored": 0, "skipped": 0, "skipped_restoring": 0, "failed": 0}


def test_restore_rejects_bad_bodies_and_reports_unavailable_snapshot() -> None:
    deps, responses = _deps()
    manager = _Manager()

    handle_snapshot_post_route(_Handler(b"{nope"), path="/api/sessions/snapshot/restore", manager=manager, deps=deps)
    handle_snapshot_post_route(_Handler(b"[]"), path="/api/sessions/snapshot/restore", manager=manager, deps=deps)
    handle_snapshot_post_route(_Handler(b'{"transport": "tmux"}'), path="/api/sessions/snapshot/restore", manager=manager, deps=deps)
    assert [status for status, _ in responses] == [400, 400, 400]
    assert manager.calls == []

    manager.restore_error = SnapshotRestoreUnavailable("no saved sessions to restore")
    handle_snapshot_post_route(_Handler(b""), path="/api/sessions/snapshot/restore", manager=manager, deps=deps)
    assert responses[-1] == (409, {"error": "no saved sessions to restore"})


def test_public_snapshot_annotates_live_and_keeps_contract_fields() -> None:
    entry = SnapshotEntry(
        session_id="broker-1",
        thread_id="t1",
        agent_backend="pi",
        log_path="/l.jsonl",
        cwd="/repo",
        model_provider="dexgem",
        preferred_auth_method=None,
        model="kimi",
        reasoning_effort="high",
        service_tier=None,
        tmux=True,
        saved_at=3.0,
    )
    state = SnapshotState(saved_at=4.0, entries={("pi", "t1"): entry})

    payload = public_snapshot(
        state,
        annotate=lambda e: {"live": True, "live_session_id": "broker-9", "restoring": None} if e.session_id == "broker-1" else {"live": False, "live_session_id": None, "restoring": None},
    )

    assert payload["saved_at"] == 4.0
    row = payload["entries"][0]
    assert {"session_id", "thread_id", "agent_backend", "cwd", "model", "reasoning_effort", "tmux", "live"} <= set(row)
    assert row["live"] is True
    assert row["live_session_id"] == "broker-9"
    assert row["session_id"] == "broker-1"
    assert row["tmux"] is True


def test_snapshot_entry_identifier_parses_only_single_entry_paths() -> None:
    assert snapshot_entry_identifier("/api/sessions/snapshot/broker-1") == "broker-1"
    assert snapshot_entry_identifier("/api/sessions/snapshot/thread-abc") == "thread-abc"
    assert snapshot_entry_identifier("/api/sessions/snapshot") is None
    assert snapshot_entry_identifier("/api/sessions/snapshot/") is None
    assert snapshot_entry_identifier("/api/sessions/snapshot/restore") is None
    assert snapshot_entry_identifier("/api/sessions/snapshot/a/b") is None
    assert snapshot_entry_identifier("/api/sessions/s1/delete") is None


def test_delete_dismisses_an_entry_by_registry_id_or_thread_id() -> None:
    deps, responses = _deps()
    manager = _Manager()
    manager.dismissable = {"broker-1"}

    assert handle_snapshot_delete_route(_Handler(), path="/api/sessions/snapshot/broker-1", manager=manager, deps=deps) is True
    assert responses[-1] == (200, {"removed": True})
    assert handle_snapshot_delete_route(_Handler(), path="/api/sessions/snapshot/broker-gone", manager=manager, deps=deps) is True
    assert responses[-1] == (200, {"removed": False})
    assert manager.calls == [("dismiss", "broker-1"), ("dismiss", "broker-gone")]


def test_delete_ignores_non_entry_paths_and_requires_auth() -> None:
    deps, responses = _deps()
    manager = _Manager()
    for path in ("/api/sessions/snapshot", "/api/sessions/snapshot/restore", "/api/sessions/snapshot/", "/api/sessions/s1/files"):
        assert handle_snapshot_delete_route(_Handler(), path=path, manager=manager, deps=deps) is False
    assert manager.calls == []

    deps, _responses = _deps(authed=False)
    handler = _Handler()
    assert handle_snapshot_delete_route(handler, path="/api/sessions/snapshot/broker-1", manager=manager, deps=deps) is True
    assert handler.unauthorized is True
    assert manager.calls == []


def test_delete_reports_a_corrupt_snapshot_as_409() -> None:
    deps, responses = _deps()
    manager = _Manager()
    manager.dismiss_error = SnapshotCorrupt("snapshot is not valid JSON")

    assert handle_snapshot_delete_route(_Handler(), path="/api/sessions/snapshot/broker-1", manager=manager, deps=deps) is True
    assert responses[-1][0] == 409
    assert "not valid JSON" in responses[-1][1]["error"]
