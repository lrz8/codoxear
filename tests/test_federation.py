import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from codoxear.federation import PeerRegistry, merge_history, rewrite_payload


class Worker(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200 if self.path == "/api/login" and data.get("password") == "private-fixture" else 403)
        self.send_header("Set-Cookie", "codoxear_auth=worker-cookie; HttpOnly")
        self.end_headers()

    def do_GET(self):
        if self.headers.get("Cookie") != "codoxear_auth=worker-cookie":
            self.send_response(401)
            self.end_headers()
            return
        if self.server.unavailable:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({
            "version": 1, "hostname": self.server.hostname,
            "sessions": [{"session_id": "original", "broker_pid": 100, "agent_backend": "pi"}],
            "history": [{"session_id": "original", "history_id": "history", "branch_id": "Mac/original",
                         "conversation_id": "Mac/original", "owner_host": "Mac", "resumable": True,
                         "active_session_id": "original", "updated_ts": 1}],
        }).encode())


class FederationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
        self.worker.unavailable = False
        self.worker.hostname = "Mac"
        self.thread = threading.Thread(target=self.worker.serve_forever, daemon=True)
        self.thread.start()
        password = self.root / "password"
        password.write_text("private-fixture")
        config = self.root / "peers.json"
        config.write_text(json.dumps({"peers": [{"id": "mac", "name": "Mac", "hostname": "Mac",
                                                 "url": f"http://127.0.0.1:{self.worker.server_port}",
                                                 "password_file": str(password)}]}))
        self.registry = PeerRegistry(config, self.root / "cache.json")

    def tearDown(self):
        self.registry.executor.shutdown(wait=True)
        self.worker.shutdown()
        self.worker.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def test_worker_login_and_scoped_identities_without_exposing_credentials(self):
        sessions, history, devices = self.registry.snapshot()
        self.assertEqual(sessions[0]["session_id"], "mac~original")
        self.assertEqual(history[0]["active_session_id"], "mac~original")
        self.assertEqual(history[0]["history_id"], "mac~history")
        self.assertTrue(devices[0]["online"])
        self.assertIsNone(self.registry.resolve("unknown~original"))
        self.assertNotIn("private-fixture", (self.root / "cache.json").read_text())
        self.assertNotIn("worker-cookie", (self.root / "cache.json").read_text())

    def test_offline_device_retains_catalog_but_disables_resume(self):
        self.registry.snapshot()
        self.worker.unavailable = True
        self.registry._refresh(self.registry.peers["mac"])
        _, history, devices = self.registry.snapshot(refresh=False)
        self.assertFalse(devices[0]["online"])
        self.assertFalse(history[0]["resumable"])
        self.assertEqual(history[0]["resume_unavailable_reason"], "Original device is offline")
        self.assertEqual(len(history), 1)

    def test_wrong_worker_identity_does_not_replace_cached_history(self):
        self.registry.snapshot()
        self.worker.hostname = "unexpected"
        self.registry._refresh(self.registry.peers["mac"])
        _, history, devices = self.registry.snapshot(refresh=False)
        self.assertFalse(devices[0]["online"])
        self.assertEqual(history[0]["owner_host"], "Mac")

    def test_replica_group_prefers_online_original_and_retains_local_copy(self):
        local = {"session_id": "original", "branch_id": "Mac/original", "history_id": "copy", "updated_ts": 3, "resumable": False}
        remote = {**local, "history_id": "mac~history", "session_id": "mac~original", "host_id": "mac", "host_online": True, "resumable": True}
        fork = {"session_id": "branch", "branch_id": "Vultr/branch", "history_id": "fork", "conversation_id": "Mac/original", "updated_ts": 4}
        rows = merge_history([local, fork], [remote])
        self.assertEqual(len(rows), 2)
        original = next(row for row in rows if row["branch_id"] == "Mac/original")
        self.assertEqual(original["session_id"], "mac~original")
        self.assertEqual(original["local_history_id"], "copy")
        self.assertEqual(original["replica_count"], 2)

    def test_routing_rewrite_preserves_transcript_and_tool_arguments(self):
        data = {"session_id": "original", "messages": [{"text": "original", "arguments": {"session_id": "original"}}]}
        rewritten = rewrite_payload(data, "mac", "original")
        self.assertEqual(rewritten["session_id"], "mac~original")
        self.assertEqual(rewritten["messages"], data["messages"])

    def test_replica_identity_survives_a_device_rename(self):
        sid = "01234567-89ab-4cde-8f01-23456789abcd"
        old = {"session_id": sid, "branch_id": "OldMac/" + sid, "conversation_id": "OldMac/" + sid,
               "history_id": "old-copy", "resumable": False}
        current = {**old, "session_id": "mac~" + sid, "branch_id": "NewMac/" + sid,
                   "host_id": "mac", "host_online": True, "resumable": True}
        rows = merge_history([old], [current])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conversation_id"], sid)
        self.assertEqual(rows[0]["session_id"], "mac~" + sid)
