from __future__ import annotations

import hashlib
import json
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any

from .session_log_paths import is_pi_subagent_session_log_path


def _display_text(value: Any, limit: int = 160) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


@lru_cache(maxsize=4096)
def _session_metadata(path: str, modified_ns: int, size: int) -> dict[str, Any]:
    """Cache discovery metadata without caching transcript bodies or credentials."""
    result: dict[str, Any] = {"name": "", "preview": "", "provenance": None}
    with Path(path).open("rb") as stream:
        header = stream.readline(64 * 1024 + 1)
        if not header.endswith(b"\n"):
            raise ValueError("incomplete or oversized session header")
        result["header"] = json.loads(header)
        discarding = False
        while chunk := stream.readline(256 * 1024 + 1):
            complete = chunk.endswith(b"\n")
            if discarding or len(chunk) > 256 * 1024:
                discarding = not complete
                continue
            if not complete:
                continue
            try:
                row = json.loads(chunk)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") == "session_info":
                result["name"] = _display_text(row.get("name")) or result["name"]
            elif row.get("type") == "custom" and row.get("customType") in {
                "pi-sync.session-handoff", "pi-sync.session-fork",
            }:
                data = row.get("data")
                if isinstance(data, dict):
                    result["provenance"] = {
                        key: data[key] for key in (
                            "sourceHostname", "sourceSessionId", "sourceConversationId", "transfer",
                        ) if isinstance(data.get(key), str)
                    }
            elif row.get("type") == "message" and not result["preview"]:
                message = row.get("message")
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                text = content if isinstance(content, str) else " ".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                ) if isinstance(content, list) else ""
                if not text.lstrip().startswith("# Synced session text handoff"):
                    result["preview"] = _display_text(text)
    return result


def list_pi_history(sessions_dir: Path, *, local_hostname: str | None = None) -> list[dict[str, Any]]:
    root = sessions_dir.resolve()
    host = local_hostname or socket.gethostname()
    rows = []
    for path in root.rglob("*.jsonl"):
        if is_pi_subagent_session_log_path(path):
            continue
        try:
            relative = path.resolve().relative_to(root)
            stat = path.stat()
            metadata = _session_metadata(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
            header = metadata["header"]
            if not isinstance(header, dict) or header.get("type") != "session":
                continue
            if not isinstance(header.get("id"), str) or not header["id"] or not isinstance(header.get("cwd"), str) or not header["cwd"]:
                continue
            native = len(relative.parts) == 2 and relative.parts[0].startswith("--")
            owner = host if native else relative.parts[0]
            provenance = metadata["provenance"]
            identity = f"{owner}/{header['id']}"
            conversation = identity
            if provenance and provenance.get("sourceHostname") and provenance.get("sourceSessionId"):
                conversation = provenance.get("sourceConversationId") or f"{provenance['sourceHostname']}/{provenance['sourceSessionId']}"
            kind = ("handoff" if provenance.get("transfer") == "sanitized-text" else "fork") if provenance else ("native" if native else "mirror")
            rows.append({
                "history_id": hashlib.sha256(str(relative).encode()).hexdigest()[:24],
                "session_id": header["id"], "cwd": header["cwd"],
                "log_path": str(path), "updated_ts": stat.st_mtime,
                "timestamp": header.get("timestamp"), "agent_backend": "pi",
                "git_branch": "", "source_host": "" if native else relative.parts[0],
                "resumable": native and Path(header["cwd"]).is_dir(),
                "owner_host": owner, "conversation_id": conversation, "branch_id": identity,
                "display_name": metadata["name"] or metadata["preview"] or "Untitled conversation",
                "first_user_message": metadata["preview"], "history_kind": kind,
                "provenance": provenance, "size_bytes": stat.st_size,
                "resume_unavailable_reason": "" if native and Path(header["cwd"]).is_dir() else (
                    "Original machine required" if not native else "Project directory is unavailable"
                ),
            })
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(rows, key=lambda row: row["updated_ts"], reverse=True)


def pi_history_log(sessions_dir: Path, history_id: str) -> Path | None:
    for row in list_pi_history(sessions_dir):
        if row["history_id"] == history_id:
            return Path(row["log_path"])
    return None
