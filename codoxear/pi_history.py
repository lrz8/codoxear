from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .session_log_paths import is_pi_subagent_session_log_path


def list_pi_history(sessions_dir: Path) -> list[dict[str, Any]]:
    root = sessions_dir.resolve()
    rows = []
    for path in root.rglob("*.jsonl"):
        if is_pi_subagent_session_log_path(path):
            continue
        try:
            relative = path.resolve().relative_to(root)
            with path.open(encoding="utf-8") as stream:
                header = json.loads(stream.readline())
            if not isinstance(header, dict) or header.get("type") != "session":
                continue
            if not isinstance(header.get("id"), str) or not header["id"] or not isinstance(header.get("cwd"), str) or not header["cwd"]:
                continue
            native = len(relative.parts) == 2 and relative.parts[0].startswith("--")
            rows.append({
                "history_id": hashlib.sha256(str(relative).encode()).hexdigest()[:24],
                "session_id": header["id"], "cwd": header["cwd"],
                "log_path": str(path), "updated_ts": path.stat().st_mtime,
                "timestamp": header.get("timestamp"), "agent_backend": "pi",
                "git_branch": "", "source_host": "" if native else relative.parts[0],
                "resumable": native and Path(header["cwd"]).is_dir(),
            })
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(rows, key=lambda row: row["updated_ts"], reverse=True)


def pi_history_log(sessions_dir: Path, history_id: str) -> Path | None:
    for row in list_pi_history(sessions_dir):
        if row["history_id"] == history_id:
            return Path(row["log_path"])
    return None
