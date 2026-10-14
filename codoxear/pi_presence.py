"""Avoid starting a second writer for an unmanaged interactive Pi session."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def _processes(bucket: int) -> dict[int, tuple[int, bool, str]]:
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,comm=,args="], capture_output=True, text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError):
        return {}
    processes = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[3] if len(parts) > 3 else ""
        is_pi = Path(parts[2]).name == "pi" or command.strip() == "pi" or ("pi-coding-agent/" in command and "/cli.js" in command)
        cwd = ""
        if is_pi and sys.platform.startswith("linux"):
            try:
                cwd = str(Path(f"/proc/{pid}/cwd").resolve(strict=True))
            except OSError:
                pass
        processes[pid] = (parent, is_pi, cwd)
    if sys.platform == "darwin":
        targets = [str(pid) for pid, (_, is_pi, _) in processes.items() if is_pi]
        if targets:
            try:
                output = subprocess.run(["lsof", "-a", "-p", ",".join(targets), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=3).stdout
                pid = None
                for line in output.splitlines():
                    if line.startswith("p") and line[1:].isdigit():
                        pid = int(line[1:])
                    elif line.startswith("n") and pid in processes:
                        parent, is_pi, _ = processes[pid]
                        processes[pid] = (parent, is_pi, os.path.realpath(line[1:]))
            except (OSError, subprocess.SubprocessError):
                pass
    return processes


def unmanaged_presence(sessions: list[dict], *, config_dir: Path | None = None) -> tuple[set[str], set[str]]:
    processes = _processes(int(time.monotonic() // 5))
    managed = {int(row.get("pid") or row.get("codex_pid") or 0) for row in sessions}
    # Subagents belong to the managed parent; they are not independent writers
    # of the parent's transcript.
    for _ in range(8):
        children = {pid for pid, (parent, _, _) in processes.items() if parent in managed}
        if children <= managed:
            break
        managed.update(children)
    known_pids, session_ids = set(), set()
    root = config_dir or Path.home() / ".config/pi-sync"
    for file in (root / "active-sessions").glob("*.json"):
        try:
            if file.stat().st_size > 16 * 1024:
                continue
            data = json.loads(file.read_text())
            pid = int(data["pid"])
            if pid not in processes or not processes[pid][1]:
                continue
            known_pids.add(pid)
            if pid not in managed and isinstance(data.get("sessionId"), str):
                session_ids.add(data["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    unknown_cwds = {cwd for pid, (_, is_pi, cwd) in processes.items() if is_pi and cwd and pid not in managed and pid not in known_pids}
    return session_ids, unknown_cwds


def resume_blocked_reason(session_id: str, cwd: str, presence: tuple[set[str], set[str]]) -> str:
    session_ids, cwds = presence
    if session_id in session_ids:
        return "This conversation is open in a plain Pi terminal. Finish that terminal session, then resume it here or start it with piox."
    if os.path.realpath(cwd) in cwds:
        return "An unmanaged Pi terminal is open in this project. Reload pi-sync there so its active conversation can be identified safely."
    return ""
