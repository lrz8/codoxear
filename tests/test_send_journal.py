import io
import json
import threading
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from codoxear.message_routes import MessageRouteDeps
from codoxear.message_routes import handle_messages_get_route
from codoxear.send_journal import SendJournal
from codoxear.server import Session
from codoxear.server import SessionCommitUnknownError
from codoxear.server import SessionManager
from codoxear.server import _match_session_route
from codoxear.session_manager_factories import CleanupFactoryDeps
from codoxear.session_manager_factories import cleanup_coordinator_for_manager
from codoxear.session_errors import SessionInjectionError


def _rows(journal: SendJournal, sid: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in journal.path_for(sid).read_text(encoding="utf-8").splitlines()]


def _make_session(sid: str) -> Session:
    return Session(
        session_id=sid,
        thread_id="t1",
        broker_pid=1,
        codex_pid=1,
        agent_backend="codex",
        owned=False,
        start_ts=0.0,
        cwd="/tmp",
        log_path=None,
        sock_path=Path(f"/tmp/{sid}.sock"),
        sync_send_supported=True,
        key_write_errors_supported=True,
    )


class TestSendJournalStore(unittest.TestCase):
    def test_submit_and_outcome_round_trip_with_exact_rows(self) -> None:
        with TemporaryDirectory() as td:
            clock = iter([10.0, 11.0])
            journal = SendJournal(Path(td) / "send_journal", now=lambda: next(clock))
            entry_id = journal.record_submit(
                "s1", source="send", text="Attachment 1: /u/a.txt\nhello", display_text="hello", attachment_paths=["/u/a.txt"]
            )
            journal.record_outcome("s1", entry_id, "ack")

            submit, outcome = _rows(journal, "s1")
            self.assertEqual(
                submit,
                {
                    "v": 1,
                    "type": "submit",
                    "id": entry_id,
                    "ts": 10.0,
                    "source": "send",
                    "text": "Attachment 1: /u/a.txt\nhello",
                    "display_text": "hello",
                    "attachment_paths": ["/u/a.txt"],
                    "queue_item_id": None,
                },
            )
            self.assertEqual(outcome, {"v": 1, "type": "outcome", "id": entry_id, "ts": 11.0, "outcome": "ack"})

            entries, next_cursor = journal.history("s1")
            self.assertIsNone(next_cursor)
            self.assertEqual([e.to_json() for e in entries], [{"id": entry_id, "ts": 10.0, "text": "hello", "source": "send", "outcome": "ack"}])

    def test_orphan_outcomes_and_torn_lines_are_ignored_and_appends_recover(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td))
            journal.record_outcome("s1", "send-orphan", "ack")
            first = journal.record_submit("s1", source="send", text="one", display_text="one")
            with open(journal.path_for("s1"), "ab") as f:
                f.write(b'{"v":1,"type":"submit","id":"torn')  # crash mid-write
            second = journal.record_submit("s1", source="send", text="two", display_text="two")

            entries, _ = journal.history("s1")
            self.assertEqual([e.id for e in entries], [second, first])
            self.assertEqual([e.outcome for e in entries], [None, None])

    def test_rotation_drops_oldest_submits_with_their_outcomes(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td), max_submits=8)
            ids = []
            for i in range(9):
                entry_id = journal.record_submit("s1", source="send", text=f"m{i}", display_text=f"m{i}")
                journal.record_outcome("s1", entry_id, "ack")
                ids.append(entry_id)

            rows = _rows(journal, "s1")
            submits = [r for r in rows if r["type"] == "submit"]
            self.assertLessEqual(len(submits), 8)
            self.assertEqual([r["display_text"] for r in submits], [f"m{i}" for i in range(9 - len(submits), 9)])
            self.assertEqual(submits[-1]["id"], ids[-1])
            kept = {r["id"] for r in submits}
            self.assertTrue(all(r["id"] in kept for r in rows))
            self.assertFalse(journal.path_for("s1").with_suffix(".jsonl.tmp").exists())
            entries, _ = journal.history("s1", limit=100)
            self.assertEqual(entries[0].text, "m8")
            self.assertEqual(entries[0].outcome, "ack")

    def test_rotation_by_bytes_keeps_the_newest_submit_even_when_oversized(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td), max_submits=200, max_bytes=1000)
            journal.record_submit("s1", source="send", text="a" * 300, display_text="a" * 300)
            journal.record_submit("s1", source="send", text="b" * 300, display_text="b" * 300)
            journal.record_submit("s1", source="send", text="c" * 5000, display_text="c" * 5000)

            entries, _ = journal.history("s1", limit=100)
            self.assertEqual([e.text[0] for e in entries], ["c"])

    def test_remove_deletes_the_session_journal(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td))
            journal.record_submit("s1", source="send", text="x", display_text="x")
            journal.record_submit("s2", source="send", text="y", display_text="y")
            journal.remove("s1")
            journal.remove("never-written")
            self.assertFalse(journal.path_for("s1").exists())
            self.assertTrue(journal.path_for("s2").exists())

    def test_path_traversal_session_ids_are_rejected(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td) / "j")
            for bad in ["", "..", "../x", "a/b", ".hidden", "a\\b"]:
                with self.assertRaises(ValueError, msg=bad):
                    journal.record_submit(bad, source="send", text="x", display_text="x")

    def test_history_pages_newest_first_and_cursor_survives_new_appends_and_rotation(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td), max_submits=8)
            for i in range(5):
                journal.record_submit("s1", source="send", text=f"m{i}", display_text=f"m{i}")
            page1, cursor = journal.history("s1", limit=2)
            self.assertEqual([e.text for e in page1], ["m4", "m3"])
            self.assertIsNotNone(cursor)
            journal.record_submit("s1", source="send", text="m5", display_text="m5")
            page2, cursor2 = journal.history("s1", cursor=cursor, limit=2)
            self.assertEqual([e.text for e in page2], ["m2", "m1"])
            page3, cursor3 = journal.history("s1", cursor=cursor2, limit=2)
            self.assertEqual([e.text for e in page3], ["m0"])
            self.assertIsNone(cursor3)
            # Rotation drops the cursor's entry together with everything older.
            for i in range(6, 12):
                journal.record_submit("s1", source="send", text=f"m{i}", display_text=f"m{i}")
            self.assertEqual(journal.history("s1", cursor=cursor2, limit=2), ([], None))


class TestSendJournalSendPath(unittest.TestCase):
    def _mgr(self) -> SessionManager:
        mgr = SessionManager.__new__(SessionManager)
        mgr._lock = threading.Lock()
        mgr._sessions = {}
        mgr._queues = {}
        mgr._pending_attachment_ids = set()
        mgr._staged_attachments = {}
        mgr._commit_unknown_sends = {}
        mgr._save_queues = lambda: None
        mgr._save_pending_attachments = lambda: None
        mgr._save_staged_attachments = lambda: None
        mgr._save_commit_unknown_sends = lambda: None
        mgr._record_prelog_user_message = lambda *_a, **_k: None  # type: ignore[method-assign]
        mgr.get_state = lambda _sid: {"busy": False, "queue_len": 0}  # type: ignore[method-assign]
        return mgr

    def _journal(self, mgr: SessionManager) -> SendJournal:
        return mgr._send_coordinator_for_manager().journal

    def test_confirmed_send_journals_submit_before_dispatch_then_ack(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        journal = self._journal(mgr)
        seen_at_dispatch: list[list[str]] = []

        def sock_call(_sock: Path, req: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            seen_at_dispatch.append([r["type"] for r in _rows(journal, sid)])
            return {"queued": False, "queue_len": 0}

        mgr._sock_call = sock_call  # type: ignore[method-assign]
        SessionManager.send(mgr, sid, "exact  prompt\nwith lines")

        self.assertEqual(seen_at_dispatch, [["submit"]])
        submit, outcome = _rows(journal, sid)
        self.assertEqual((submit["source"], submit["text"], submit["display_text"]), ("send", "exact  prompt\nwith lines", "exact  prompt\nwith lines"))
        self.assertEqual(submit["attachment_paths"], [])
        self.assertIsNone(submit["queue_item_id"])
        self.assertEqual((outcome["id"], outcome["outcome"]), (submit["id"], "ack"))

    def test_attachment_send_journals_committed_text_and_display_text_separately(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        mgr._sessions[sid].pending_attachment = True
        mgr._pending_attachment_ids.add(sid)
        mgr._staged_attachments[sid] = [
            {"id": "a1", "display_name": "one.txt", "filename": "1_one.txt", "path": "/tmp/uploads/s1/1_one.txt", "size": 3, "created_ts": 1.0},
        ]
        mgr.clear_staged_attachments = lambda *_a, **_k: {"ok": True}  # type: ignore[method-assign]
        mgr._sock_call = lambda *_a, **_k: {"queued": False, "queue_len": 0}  # type: ignore[method-assign]

        SessionManager.send(mgr, sid, "use this", allow_pending_attachment=True)

        submit, outcome = _rows(self._journal(mgr), sid)
        self.assertEqual(submit["text"], "Attachment 1: /tmp/uploads/s1/1_one.txt\nuse this")
        self.assertEqual(submit["display_text"], "use this")
        self.assertEqual(submit["attachment_paths"], ["/tmp/uploads/s1/1_one.txt"])
        self.assertEqual(outcome["outcome"], "ack")

    def test_broker_call_raising_leaves_submit_and_error_outcome(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)

        def sock_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("broker exploded")

        mgr._sock_call = sock_call  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "broker exploded"):
            SessionManager.send(mgr, sid, "lost prompt")

        submit, outcome = _rows(self._journal(mgr), sid)
        self.assertEqual(submit["text"], "lost prompt")
        self.assertEqual((outcome["id"], outcome["outcome"], outcome["error"]), (submit["id"], "error", "broker exploded"))

    def test_injection_error_response_records_error_outcome(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        mgr._sock_call = lambda *_a, **_k: {"error": "write failed"}  # type: ignore[method-assign]
        with self.assertRaisesRegex(SessionInjectionError, "write failed"):
            SessionManager.send(mgr, sid, "p")
        self.assertEqual([r.get("outcome") for r in _rows(self._journal(mgr), sid)], [None, "error"])

    def test_commit_unknown_records_exactly_one_commit_unknown_outcome(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        mgr._sock_call = lambda *_a, **_k: {"commit_unknown": True, "error": "ack lost"}  # type: ignore[method-assign]
        with self.assertRaises(SessionCommitUnknownError):
            SessionManager.send(mgr, sid, "maybe sent")

        rows = _rows(self._journal(mgr), sid)
        self.assertEqual([r["type"] for r in rows], ["submit", "outcome"])
        self.assertEqual(rows[1]["outcome"], "commit_unknown")
        self.assertEqual(rows[0]["text"], "maybe sent")

    def test_journal_write_failure_never_blocks_send_and_attaches_warning(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        coordinator = mgr._send_coordinator_for_manager()
        stderr = io.StringIO()

        class BrokenJournal:
            def record_submit(self, *_a: Any, **_k: Any) -> str:
                raise OSError(28, "No space left on device")

            def record_outcome(self, *_a: Any, **_k: Any) -> None:
                raise AssertionError("no outcome row without a submit row")

        from dataclasses import replace

        mgr._coordinators = replace(mgr._coordinator_graph(), send=replace(coordinator, journal=BrokenJournal(), stderr=stderr))
        dispatched: list[dict[str, Any]] = []
        mgr._sock_call = lambda _s, req, **_k: dispatched.append(req) or {"queued": False, "queue_len": 0}  # type: ignore[method-assign]

        response = SessionManager.send(mgr, sid, "still goes out")

        self.assertEqual([req["text"] for req in dispatched], ["still goes out"])
        self.assertIn("No space left on device", response["send_journal_error"])
        self.assertIn("send journal submit", stderr.getvalue())

    def test_enqueue_journals_once_and_dispatch_appends_only_the_linked_outcome(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        mgr._sock_call = lambda *_a, **_k: {"queued": False, "queue_len": 0}  # type: ignore[method-assign]

        response = SessionManager.enqueue(mgr, sid, "queued then sent")

        self.assertFalse(response.get("queued"))
        submit, outcome = _rows(self._journal(mgr), sid)
        self.assertEqual((submit["type"], submit["source"], submit["text"]), ("submit", "enqueue", "queued then sent"))
        self.assertEqual(submit["id"], submit["queue_item_id"])
        self.assertTrue(submit["id"].startswith("queue-"))
        self.assertEqual((outcome["type"], outcome["id"], outcome["outcome"]), ("outcome", submit["id"], "ack"))

    def test_enqueue_behind_a_busy_session_journals_submit_without_outcome(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        mgr._queues[sid] = [{"id": "queue-head", "text": "ahead", "created_ts": 1.0}]
        mgr._sock_call = lambda *_a, **_k: self.fail("a queued prompt must not dispatch behind the head")  # type: ignore[method-assign]

        response = SessionManager.enqueue(mgr, sid, "second in line")

        self.assertTrue(response["queued"])
        (submit,) = _rows(self._journal(mgr), sid)
        self.assertEqual((submit["source"], submit["text"], submit["id"]), ("enqueue", "second in line", response["item"]["id"]))
        entries, _ = self._journal(mgr).history(sid)
        self.assertEqual([(e.text, e.source, e.outcome) for e in entries], [("second in line", "enqueue", None)])

    def test_explicit_delete_removes_journal_but_dead_session_pruning_keeps_it(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        mgr._aliases = {}
        mgr._sidebar_meta = {}
        mgr._unattended = {}
        mgr._files = {}
        mgr._input_locks = {}
        mgr._save_aliases = lambda: None
        mgr._save_sidebar_meta = lambda: None
        mgr._save_unattended = lambda: None
        mgr._save_files = lambda: None
        journal = self._journal(mgr)
        journal.record_submit(sid, source="send", text="x", display_text="x")

        SessionManager._clear_deleted_session_state(mgr, sid)
        self.assertTrue(journal.path_for(sid).exists())

        SessionManager._clear_deleted_session_state(mgr, sid, clear_recovery=True)
        self.assertFalse(journal.path_for(sid).exists())

    def test_journal_remove_failure_does_not_abort_session_cleanup(self) -> None:
        sid = "s1"
        mgr = self._mgr()
        mgr._sessions[sid] = _make_session(sid)
        mgr._aliases = {}
        mgr._sidebar_meta = {}
        mgr._unattended = {}
        mgr._files = {}
        mgr._input_locks = {}
        mgr._save_aliases = lambda: None
        mgr._save_sidebar_meta = lambda: None
        mgr._save_unattended = lambda: None
        mgr._save_files = lambda: None
        stderr = io.StringIO()

        class BrokenJournal:
            def remove(self, _sid: str) -> None:
                raise PermissionError(13, "denied")

        cleanup = cleanup_coordinator_for_manager(
            mgr, CleanupFactoryDeps(unlink_quiet=lambda _p: None, send_journal=BrokenJournal(), stderr=stderr)
        )
        cleanup.clear_deleted_session_state(sid, clear_recovery=True)

        self.assertIn("send journal remove failed for s1", stderr.getvalue())


class TestSentHistoryEndpoint(unittest.TestCase):
    class Handler:
        def __init__(self) -> None:
            self.unauthorized = False

        def _unauthorized(self) -> None:
            self.unauthorized = True

    class Manager:
        def __init__(self, journal: SendJournal) -> None:
            self.journal = journal

        def sent_history(self, session_id: str, *, cursor: str | None, limit: int) -> Any:
            return self.journal.history(session_id, cursor=cursor, limit=limit)

    def _get(self, journal: SendJournal, session_id: str, query: str = "", *, auth: bool = True) -> tuple[bool, list[tuple[int, Any]], Any]:
        responses: list[tuple[int, Any]] = []
        deps = MessageRouteDeps(
            require_auth=lambda _h: auth,
            set_auth_cookie=lambda _h: None,
            json_response=lambda _h, status, obj: responses.append((status, obj)),
            launch_attempt_transcript_for_session_id=lambda *_a, **_k: None,
            transcript_export_max_bytes=1,
            transcript_search_max_line_bytes=1,
            decode_message_cursor=lambda *_a, **_k: 0,
            encode_message_cursor=lambda *_a, **_k: "",
            record_metric=lambda *_a: None,
            message_runtime_snapshot=lambda *_a, **_k: ({}, False, 0, None),
        )
        handler = self.Handler()
        handled = handle_messages_get_route(
            handler,
            path=f"/api/sessions/{session_id}/sent-history",
            query=query,
            manager=self.Manager(journal),
            deps=deps,
            match_session_route=_match_session_route,
        )
        return handled, responses, handler

    def test_pages_newest_first_with_opaque_cursor(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td))
            for i in range(5):
                entry_id = journal.record_submit("s1", source="enqueue" if i == 1 else "send", text=f"wire {i}", display_text=f"typed {i}")
                journal.record_outcome("s1", entry_id, "commit_unknown" if i == 4 else "ack")

            handled, responses, _ = self._get(journal, "s1", "limit=2")
            self.assertTrue(handled)
            (status, body), = responses
            self.assertEqual(status, 200)
            self.assertEqual([e["text"] for e in body["entries"]], ["typed 4", "typed 3"])
            self.assertEqual(set(body["entries"][0]), {"id", "ts", "text", "source", "outcome"})
            self.assertEqual(body["entries"][0]["outcome"], "commit_unknown")
            self.assertIsInstance(body["next_cursor"], str)

            _, responses, _ = self._get(journal, "s1", "limit=2&cursor=" + urllib.parse.quote(body["next_cursor"]))
            body2 = responses[0][1]
            self.assertEqual([(e["text"], e["source"]) for e in body2["entries"]], [("typed 2", "send"), ("typed 1", "enqueue")])

            _, responses, _ = self._get(journal, "s1", "limit=2&cursor=" + urllib.parse.quote(body2["next_cursor"]))
            body3 = responses[0][1]
            self.assertEqual([e["text"] for e in body3["entries"]], ["typed 0"])
            self.assertIsNone(body3["next_cursor"])

    def test_limit_defaults_to_twenty_and_clamps_to_one_hundred(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td), max_submits=1000, max_bytes=10 * 1024 * 1024)
            for i in range(130):
                journal.record_submit("s1", source="send", text=f"m{i}", display_text=f"m{i}")

            self.assertEqual(len(self._get(journal, "s1")[1][0][1]["entries"]), 20)
            self.assertEqual(len(self._get(journal, "s1", "limit=500")[1][0][1]["entries"]), 100)
            self.assertEqual(len(self._get(journal, "s1", "limit=0")[1][0][1]["entries"]), 1)
            status, body = self._get(journal, "s1", "limit=abc")[1][0]
            self.assertEqual(status, 400)

    def test_bad_cursor_and_bad_session_id_are_400_and_unknown_session_is_empty(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td))
            self.assertEqual(self._get(journal, "s1", "cursor=!!!")[1][0][0], 400)
            self.assertEqual(self._get(journal, "..", "")[1][0][0], 400)
            self.assertEqual(self._get(journal, "nobody")[1], [(200, {"entries": [], "next_cursor": None})])

    def test_unauthenticated_request_is_rejected_without_reading(self) -> None:
        with TemporaryDirectory() as td:
            journal = SendJournal(Path(td))
            journal.record_submit("s1", source="send", text="secret", display_text="secret")
            handled, responses, handler = self._get(journal, "s1", auth=False)
            self.assertTrue(handled)
            self.assertEqual(responses, [])
            self.assertTrue(handler.unauthorized)
