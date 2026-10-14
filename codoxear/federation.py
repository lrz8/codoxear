"""A private gateway to existing Codoxear brokers on the owner's machines.

Only configured peers are reachable. Credentials stay on the gateway; browser
requests retain the gateway's authentication and never navigate to a worker.
"""
from __future__ import annotations

import copy
import http.cookies
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PEER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_MAX_JSON = 16 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Peer:
    id: str
    name: str
    url: str
    hostname: str
    password_file: Path
    cookie: str = ""
    auth_lock: threading.Lock = field(default_factory=threading.Lock)

    def _request(self, route: str, *, method: str = "GET", data: bytes | None = None,
                 content_type: str = "application/json", timeout: float = 10, range_header: str | None = None):
        # Peer traffic must bypass shell HTTP proxies (including Clash).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        headers = {
            "Cookie": self.cookie, "Content-Type": content_type, "X-Codoxear-Local-Only": "1",
        }
        if range_header:
            headers["Range"] = range_header
        request = urllib.request.Request(self.url + route, data=data, method=method, headers=headers)
        return opener.open(request, timeout=timeout)

    def login(self):
        password = self.password_file.read_text().strip()
        with self._request("/api/login", method="POST", data=json.dumps({"password": password}).encode()) as response:
            cookies = http.cookies.SimpleCookie()
            for header in response.headers.get_all("Set-Cookie", []):
                cookies.load(header)
            self.cookie = "; ".join(f"{name}={value.value}" for name, value in cookies.items())
        if not self.cookie:
            raise ValueError("Worker login did not return a session cookie")

    def open(self, route: str, **kwargs):
        with self.auth_lock:
            if not self.cookie:
                self.login()
        try:
            return self._request(route, **kwargs)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            error.close()
            with self.auth_lock:
                self.login()
            return self._request(route, **kwargs)

    def get_json(self, route: str) -> dict[str, Any]:
        with self.open(route, timeout=4) as response:
            raw = response.read(_MAX_JSON + 1)
        if len(raw) > _MAX_JSON:
            raise ValueError("Worker metadata exceeds the response limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid worker response")
        return value


def qualify(peer_id: str, session_id: str) -> str:
    return f"{peer_id}~{session_id}"


def rewrite_payload(value: Any, peer_id: str, source_id: str | None = None, *, route_ids: bool = True) -> Any:
    """Rewrite routing identities, never transcript text or provider state."""
    if isinstance(value, list):
        return [rewrite_payload(item, peer_id, source_id, route_ids=False) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if route_ids and key in {"session_id", "existing_session_id", "launch_id"} and isinstance(item, str) and (
            source_id is None or item == source_id or key != "session_id"
        ):
            result[key] = qualify(peer_id, item)
        elif key in {"url", "href", "preview_url", "download_url"} and isinstance(item, str) and item.startswith("/api/sessions/"):
            parts = item.split("/", 4)
            parts[3] = qualify(peer_id, parts[3])
            result[key] = "/".join(parts)
        elif isinstance(item, (dict, list)):
            result[key] = rewrite_payload(item, peer_id, source_id, route_ids=False)
        else:
            result[key] = item
    return result


class PeerRegistry:
    def __init__(self, config_path: Path, cache_path: Path):
        self.config_path, self.cache_path = config_path, cache_path
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="codoxear-peer")
        self.peers: dict[str, Peer] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.jobs = {}
        self.last_attempt: dict[str, float] = {}
        self.signature = None
        try:
            saved = json.loads(cache_path.read_text())
            if isinstance(saved, dict):
                self.snapshots = saved
                for snapshot in self.snapshots.values():
                    snapshot["error"] = "Waiting for device"
        except (OSError, ValueError, TypeError):
            pass

    def reload(self):
        try:
            stat = self.config_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        with self.lock:
            if signature == self.signature:
                return
            data = json.loads(self.config_path.read_text()) if signature else {}
            peers = {}
            for row in data.get("peers", []):
                peer_id = row["id"]
                url = str(row["url"]).rstrip("/")
                parsed = urllib.parse.urlsplit(url)
                if not _PEER_ID.fullmatch(peer_id) or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
                    raise ValueError("Invalid Codoxear peer configuration")
                peers[peer_id] = Peer(peer_id, str(row.get("name") or peer_id), url,
                                      str(row["hostname"]), Path(row["password_file"]).expanduser())
            self.peers, self.signature = peers, signature

    def resolve(self, qualified_id: str) -> tuple[Peer, str] | None:
        self.reload()
        peer_id, separator, session_id = qualified_id.partition("~")
        if not separator or not session_id:
            return None
        with self.lock:
            peer = self.peers.get(peer_id)
        return (peer, session_id) if peer else None

    def _refresh(self, peer: Peer):
        try:
            snapshot = peer.get_json("/api/peer-snapshot")
            if snapshot.get("version") != 1 or snapshot.get("hostname") != peer.hostname or not isinstance(snapshot.get("sessions"), list) or not isinstance(snapshot.get("history"), list):
                raise ValueError("Worker identity or metadata does not match configuration")
            snapshot.update(last_success=time.time(), error="")
        except Exception as error:
            with self.lock:
                snapshot = copy.deepcopy(self.snapshots.get(peer.id, {"sessions": [], "history": []}))
            snapshot["error"] = f"Device unavailable ({type(error).__name__})"
        with self.lock:
            self.snapshots[peer.id] = snapshot
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            with temporary.open("w") as stream:
                os.chmod(temporary, 0o600)
                json.dump(self.snapshots, stream)
            temporary.replace(self.cache_path)

    def snapshot(self, *, refresh: bool = True) -> tuple[list[dict], list[dict], list[dict]]:
        self.reload()
        initial_jobs = []
        with self.lock:
            if refresh:
                for peer in self.peers.values():
                    previous = self.jobs.get(peer.id)
                    if previous and not previous.done():
                        continue
                    if time.monotonic() - self.last_attempt.get(peer.id, -100) < 5:
                        continue
                    self.last_attempt[peer.id] = time.monotonic()
                    self.jobs[peer.id] = self.executor.submit(self._refresh, peer)
                    if peer.id not in self.snapshots:
                        initial_jobs.append(self.jobs[peer.id])
        if initial_jobs:
            wait(initial_jobs, timeout=2)
        sessions, history, devices = [], [], []
        with self.lock:
            for peer in self.peers.values():
                snapshot = self.snapshots.get(peer.id, {})
                online = not snapshot.get("error") and time.time() - snapshot.get("last_success", 0) < 30
                devices.append({"id": peer.id, "name": peer.name, "hostname": peer.hostname, "online": online,
                                "last_seen": snapshot.get("last_success"), "error": snapshot.get("error", "Connecting")})
                for row in snapshot.get("sessions", []):
                    if not isinstance(row, dict) or not isinstance(row.get("session_id"), str):
                        continue
                    item = rewrite_payload(row, peer.id, row["session_id"])
                    item.update(host_id=peer.id, host_name=peer.name, host_online=online)
                    if not online:
                        item["busy"] = False
                    sessions.append(item)
                for row in snapshot.get("history", []):
                    if not isinstance(row, dict) or not isinstance(row.get("session_id"), str):
                        continue
                    item = copy.deepcopy(row)
                    item["session_id"] = qualify(peer.id, row["session_id"])
                    item["history_id"] = qualify(peer.id, row["history_id"])
                    if item.get("active_session_id"):
                        item["active_session_id"] = qualify(peer.id, item["active_session_id"])
                    item.update(host_id=peer.id, host_name=peer.name, host_online=online, source_host=peer.hostname,
                                resumable=bool(row.get("resumable") and online), last_seen=snapshot.get("last_success"))
                    if not online:
                        item["resume_unavailable_reason"] = "Original device is offline"
                    history.append(item)
        return sessions, history, devices


_registry: PeerRegistry | None = None
_registry_lock = threading.Lock()


def peers() -> PeerRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = PeerRegistry(
                Path(os.environ.get("CODOXEAR_PEERS_FILE", str(Path.home() / ".config/codoxear/peers.json"))),
                Path.home() / ".local/share/codoxear/peer-cache.json",
            )
        return _registry


def merge_history(local: list[dict], remote: list[dict]) -> list[dict]:
    """Coalesce replicas of the same branch; retain genuine forks as separate branches."""
    groups: dict[str, list[dict]] = {}
    for row in local + remote:
        # Pi UUIDs survive device renames and mirror path changes. Native forks
        # receive new UUIDs, so they remain separate branches.
        identity = row["session_id"].split("~", 1)[-1]
        try:
            identity = str(uuid.UUID(identity))
        except ValueError:
            identity = row.get("branch_id") or row["session_id"]
        groups.setdefault(identity, []).append(row)
    result = []
    for copies in groups.values():
        copies.sort(key=lambda row: (bool(row.get("resumable")), bool(row.get("host_online")), row.get("updated_ts", 0)), reverse=True)
        chosen = dict(copies[0])
        try:
            chosen["conversation_id"] = str(uuid.UUID(chosen.get("conversation_id", "").rsplit("/", 1)[-1]))
        except ValueError:
            pass
        chosen["replica_count"] = len(copies)
        local_copy = next((row for row in copies if not row.get("host_id")), None)
        if local_copy:
            chosen["local_history_id"] = local_copy["history_id"]
            origin = next((row for row in copies if row.get("host_id")), None)
            if origin and not chosen.get("resumable") and origin.get("host_online") is False:
                chosen.update(host_id=origin["host_id"], host_name=origin.get("host_name"), host_online=False,
                              last_seen=origin.get("last_seen"), resume_unavailable_reason="Original device is offline; saved copy available")
        result.append(chosen)
    return sorted(result, key=lambda row: row.get("updated_ts", 0), reverse=True)


def proxy_session(handler, *, path: str, query: str, deps) -> bool:
    match = re.match(r"^/api/(sessions|pi_history)/([^/]+)(/.*)?$", path)
    if not match or "~" not in urllib.parse.unquote(match[2]):
        return False
    if not deps.require_auth(handler):
        handler._unauthorized()
        return True
    target = peers().resolve(urllib.parse.unquote(match[2]))
    if target is None:
        deps.json_response(handler, 404, {"error": "Unknown device"})
        return True
    peer, session_id = target
    route = f"/api/{match[1]}/{urllib.parse.quote(session_id, safe='')}{match[3] or ''}"
    if query:
        route += "?" + query
    body = None
    if handler.command == "POST":
        body = handler.deps.read_body(handler, limit=64 * 1024 * 1024)
    try:
        response = peer.open(route, method=handler.command, data=body,
                             content_type=handler.headers.get("Content-Type", "application/json"), timeout=30,
                             range_header=handler.headers.get("Range"))
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, ValueError):
        deps.json_response(handler, 503, {
            "error": f"{peer.name} is unavailable. Check the conversation before retrying a send.",
            "commit_unknown": handler.command == "POST" and path.endswith(("/send", "/enqueue")),
        })
        return True
    started = False
    try:
        with response:
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            if "application/json" in content_type:
                raw = response.read(_MAX_JSON + 1)
                if len(raw) > _MAX_JSON:
                    deps.json_response(handler, 502, {"error": "Worker response exceeds limit"})
                else:
                    deps.json_response(handler, response.status, rewrite_payload(json.loads(raw), peer.id, session_id))
                return True
            handler.send_response(response.status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            for name in ("Content-Disposition", "Content-Security-Policy", "X-Content-Type-Options", "Content-Range", "Accept-Ranges"):
                if value := response.headers.get(name):
                    handler.send_header(name, value)
            handler.end_headers()
            started = True
            handler.close_connection = True
            if "text/event-stream" in content_type:
                for line in response:
                    if line.startswith(b"data:"):
                        try:
                            value = json.loads(line[5:])
                            line = b"data: " + json.dumps(rewrite_payload(value, peer.id, session_id)).encode() + b"\n"
                        except ValueError:
                            pass
                    handler.wfile.write(line)
                    handler.wfile.flush()
            else:
                while chunk := response.read(64 * 1024):
                    handler.wfile.write(chunk)
    except (OSError, ValueError, http.client.HTTPException):
        if not started:
            deps.json_response(handler, 502, {
                "error": f"Connection to {peer.name} was interrupted. Check the conversation before retrying.",
                "commit_unknown": handler.command == "POST" and path.endswith(("/send", "/enqueue")),
            })
        else:
            handler.close_connection = True
    return True
