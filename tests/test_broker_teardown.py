"""Broker teardown contract: socket + sidecar are unlinked only on a clean (status 0) agent exit.

A nonzero exit (crash, OOM kill, V8 abort such as exit 134, signal death)
leaves both files so the server's stale-sidecar path produces a lost
tombstone and the session's snapshot entry survives for restore.
"""

from __future__ import annotations

import json
from pathlib import Path

from codoxear.broker import _unlink_clean_exit_evidence


def _sock_pair(root: Path, name: str = "broker-1") -> tuple[Path, Path]:
    sock = root / f"{name}.sock"
    sidecar = sock.with_suffix(".json")
    sock.touch()
    sidecar.write_text(json.dumps({"session_id": "thread-1", "broker_pid": 1}), encoding="utf-8")
    return sock, sidecar


def test_clean_exit_unlinks_socket_and_sidecar(tmp_path: Path) -> None:
    sock, sidecar = _sock_pair(tmp_path)

    assert _unlink_clean_exit_evidence(sock, 0) is True
    assert not sock.exists()
    assert not sidecar.exists()


def test_crashed_agent_keeps_socket_and_sidecar_for_the_lost_tombstone(tmp_path: Path) -> None:
    for exit_code in (1, 134, 137, 143):
        sock, sidecar = _sock_pair(tmp_path, name=f"broker-{exit_code}")

        assert _unlink_clean_exit_evidence(sock, exit_code) is False
        assert sock.exists()
        assert sidecar.exists()


def test_missing_files_on_clean_exit_are_tolerated(tmp_path: Path) -> None:
    sock, sidecar = _sock_pair(tmp_path)
    sidecar.unlink()

    assert _unlink_clean_exit_evidence(sock, 0) is True
    assert not sock.exists()

    # Both already gone (e.g. pruned by the server first): still fine.
    assert _unlink_clean_exit_evidence(sock, 0) is True


def test_no_socket_path_is_a_no_op(tmp_path: Path) -> None:
    assert _unlink_clean_exit_evidence(None, 0) is False
