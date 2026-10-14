"""Exercise Pi's real history picker using an isolated Docker fixture."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time

from codoxear.broker_launch import _ensure_pi_session_arg, _pi_session_dir_name


def main() -> None:
    if not Path("/.dockerenv").exists():
        raise SystemExit("Run this verification in Docker only.")
    pi = shutil.which("pi")
    if not pi:
        raise SystemExit("The Docker fixture needs the real Pi CLI.")
    with tempfile.TemporaryDirectory(prefix="pi-resume-picker-") as temporary:
        root = Path(temporary)
        cwd, agent = root / "project", root / "agent"
        cwd.mkdir()
        folder = agent / "sessions" / _pi_session_dir_name(str(cwd))
        folder.mkdir(parents=True)
        title = "pi-resume-regression-history"
        session = folder / "2026-10-14T00-00-00-000Z_00000000-0000-4000-8000-000000000001.jsonl"
        rows = [
            {"type": "session", "version": 3, "id": "00000000-0000-4000-8000-000000000001", "timestamp": "2026-10-14T00:00:00.000Z", "cwd": str(cwd)},
            {"type": "message", "id": "user1", "parentId": None, "timestamp": "2026-10-14T00:00:01.000Z", "message": {"role": "user", "content": [{"type": "text", "text": title}], "timestamp": 1791936001000}},
            {"type": "session_info", "id": "name1", "parentId": "user1", "timestamp": "2026-10-14T00:00:02.000Z", "name": title},
        ]
        original = "".join(json.dumps(row) + "\n" for row in rows)
        session.write_text(original)
        args = _ensure_pi_session_arg(args=["-r", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--approve"], cwd=str(cwd), sessions_dir=agent / "sessions", agent_backend="pi")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 120, 0, 0))
        process = subprocess.Popen([pi, *args], cwd=cwd, env={**os.environ, "PI_CODING_AGENT_DIR": str(agent), "TERM": "xterm-256color", "PI_SKIP_VERSION_CHECK": "1"}, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        output = b""
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    output += chunk
                    if b"\x1b[6n" in chunk:
                        os.write(master, b"\x1b[1;1R")
                    if b"Resume Session" in output and title.encode() in output:
                        break
                if process.poll() is not None:
                    break
            rendered = output.decode(errors="replace")
            assert "Resume Session" in rendered and title in rendered, f"Native history picker was bypassed. argv={args!r}\n{rendered[-3000:]}"
            assert session.read_text() == original, "Opening the picker modified existing history"
            print(json.dumps({"realPiHistoryPicker": True, "existingHistoryVisible": True, "historyUnchanged": True}))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            os.close(master)


if __name__ == "__main__":
    main()
