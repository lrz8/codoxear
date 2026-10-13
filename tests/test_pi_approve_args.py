import stat
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from codoxear import broker as broker_module
from codoxear.backend_launch import build_backend_args
from codoxear.backend_launch import build_backend_resume_args
from codoxear.broker_launch import _ensure_pi_approve_args
from codoxear.broker_launch import _pi_supports_approve


def _fake_cli(directory: Path, name: str, help_text: str, *, exit_code: int = 0) -> Path:
    """An executable that prints ``help_text`` for --help and records every invocation."""
    path = directory / name
    log = directory / f"{name}.calls"
    path.write_text(
        f"#!/bin/sh\necho \"$@\" >> '{log}'\ncat <<'EOF'\n{help_text}\nEOF\nexit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    _pi_supports_approve.cache_clear()
    yield
    _pi_supports_approve.cache_clear()


def _pi_launch_args(*extra: str) -> list[str]:
    return [*build_backend_args(agent_backend="pi", spawn_cwd=Path("/tmp"), codex_trust_override="", model="m", reasoning_effort="high"), *extra]


def test_pi_launch_and_resume_args_gain_approve_when_pi_supports_it() -> None:
    with TemporaryDirectory() as td:
        pi = _fake_cli(Path(td), "pi", "Options:\n  --approve, -a   Trust project-local files for this run")

        launch = _ensure_pi_approve_args(args=_pi_launch_args(), agent_backend="pi", agent_bin=str(pi))
        resume = _ensure_pi_approve_args(
            args=build_backend_resume_args(agent_backend="pi", resume_id="abc", resume_row={"log_path": "/tmp/s.jsonl"}),
            agent_backend="pi",
            agent_bin=str(pi),
        )

    assert launch == ["--model", "m", "--thinking", "high", "--approve"]
    assert resume == ["--session", "/tmp/s.jsonl", "--approve"]


def test_approve_is_last_so_old_pi_cannot_read_a_following_token_as_its_value() -> None:
    with TemporaryDirectory() as td:
        pi = _fake_cli(Path(td), "pi", "--approve")
        out = _ensure_pi_approve_args(args=["--extension", "/x/bridge.ts", "hello there"], agent_backend="pi", agent_bin=str(pi))

    assert out[-1] == "--approve"
    assert out[:-1] == ["--extension", "/x/bridge.ts", "hello there"]


def test_pi_without_approve_support_is_launched_without_the_flag() -> None:
    # Pre-trust Pi rejects unknown long options with "Unknown option: --approve" and exits 1.
    with TemporaryDirectory() as td:
        old_pi = _fake_cli(Path(td), "pi", "Options:\n  --no-session  Don't save session")
        out = _ensure_pi_approve_args(args=["--model", "m"], agent_backend="pi", agent_bin=str(old_pi))

    assert out == ["--model", "m"]


def test_unrunnable_pi_withholds_the_flag_and_reports_on_stderr(capsys) -> None:
    out = _ensure_pi_approve_args(args=["--model", "m"], agent_backend="pi", agent_bin="/nonexistent/pi-binary")

    assert out == ["--model", "m"]
    assert "could not probe /nonexistent/pi-binary --help for --approve support" in capsys.readouterr().err


def test_probe_runs_once_per_binary() -> None:
    with TemporaryDirectory() as td:
        pi = _fake_cli(Path(td), "pi", "--approve")
        for _ in range(3):
            _ensure_pi_approve_args(args=[], agent_backend="pi", agent_bin=str(pi))

        assert (Path(td) / "pi.calls").read_text(encoding="utf-8").splitlines() == ["--help"]


@pytest.mark.parametrize("backend", ["codex", "cc"])
def test_other_backends_never_get_approve_and_never_probe(backend: str) -> None:
    with TemporaryDirectory() as td:
        cli = _fake_cli(Path(td), backend, "--approve")
        args = build_backend_args(agent_backend=backend, spawn_cwd=Path("/tmp"), codex_trust_override="trust=1")
        resume = build_backend_resume_args(agent_backend=backend, resume_id="abc")

        assert _ensure_pi_approve_args(args=args, agent_backend=backend, agent_bin=str(cli)) == args
        assert _ensure_pi_approve_args(args=resume, agent_backend=backend, agent_bin=str(cli)) == resume
        assert not (Path(td) / f"{backend}.calls").exists()


def test_broker_wrapper_applies_to_the_broker_backend_and_binary(monkeypatch) -> None:
    with TemporaryDirectory() as td:
        pi = _fake_cli(Path(td), "pi", "--approve")
        monkeypatch.setattr(broker_module, "AGENT_BACKEND", "pi")
        monkeypatch.setattr(broker_module, "AGENT_BIN", str(pi))
        assert broker_module._ensure_pi_approve_args(args=["--session", "s"]) == ["--session", "s", "--approve"]

        monkeypatch.setattr(broker_module, "AGENT_BACKEND", "cc")
        assert broker_module._ensure_pi_approve_args(args=["--resume", "s"]) == ["--resume", "s"]
