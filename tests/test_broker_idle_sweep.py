import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from codoxear import broker as broker_mod
from codoxear.broker import BUSY_QUIET_SECONDS, Broker, State

TICK_SECONDS = 0.25  # the watcher's sleep between sweeps


def _state(log_path: Path | None = None) -> State:
    st = State(
        codex_pid=1,
        pty_master_fd=1,
        cwd="/tmp",
        start_ts=0.0,
        codex_home=Path("/tmp"),
        sessions_dir=Path("/tmp"),
    )
    st.log_path = log_path
    return st


def _latch_send(st: State, *, now_ts: float) -> None:
    # The fields the broker's confirmed-send handler latches (broker_control.send_handler).
    st.busy = True
    st.turn_open = True
    st.turn_has_completion_candidate = False
    st.send_latch_log_off = st.log_off
    st.last_turn_activity_ts = now_ts
    st.send_latch_activity_ts = st.last_turn_activity_ts


class _FakeClock:
    def __init__(self, start: float) -> None:
        self.t = start

    def now(self) -> float:
        return self.t


def _run_watcher_until_idle(broker: Broker, clock: _FakeClock, *, max_ticks: int = 80) -> float | None:
    """Run the real ``_log_watcher`` loop on a fake clock with zero PTY output.

    Each watcher sleep advances the clock one tick. Returns the clock time at
    which the state first reads not-busy, or ``None`` if it never clears.
    """
    cleared_at: list[float] = []
    ticks = 0

    def fake_sleep(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        clock.t += TICK_SECONDS
        with broker._lock:
            idle = not broker.state.busy
        if idle and not cleared_at:
            cleared_at.append(clock.t)
        if idle or ticks >= max_ticks:
            broker._stop.set()

    with patch.object(broker_mod, "_now", clock.now), patch.object(broker_mod.time, "sleep", fake_sleep):
        broker._log_watcher()
    return cleared_at[0] if cleared_at else None


def _broker(st: State) -> Broker:
    broker = Broker.__new__(Broker)
    broker._lock = threading.Lock()
    broker._stop = threading.Event()
    broker.state = st
    return broker


class TestBrokerIdleSweepIsTimerDriven(unittest.TestCase):
    def test_unbound_log_latch_clears_after_quiet_window_with_no_pty_output(self) -> None:
        # A send swallowed by a modal dialog on a fresh Pi session: the backend never
        # creates its log, so the broker stays unbound and no log or PTY activity
        # ever re-evaluates the latch.
        st = _state(log_path=None)
        _latch_send(st, now_ts=100.0)
        clock = _FakeClock(100.0)

        cleared_at = _run_watcher_until_idle(_broker(st), clock)

        self.assertIsNotNone(cleared_at, "busy latch never cleared without PTY output")
        self.assertGreaterEqual(cleared_at - 100.0, BUSY_QUIET_SECONDS)
        self.assertLessEqual(cleared_at - 100.0, BUSY_QUIET_SECONDS + 2 * TICK_SECONDS)
        self.assertFalse(st.busy)
        self.assertFalse(st.turn_open)
        # Latch-settle semantics: the idle projection is re-established because no
        # turn row appeared since the send.
        self.assertGreater(st.last_interrupted_idle_ts, 0.0)

    def test_latch_still_holds_inside_the_quiet_window(self) -> None:
        st = _state(log_path=None)
        _latch_send(st, now_ts=100.0)
        clock = _FakeClock(100.0)
        broker = _broker(st)

        with patch.object(broker_mod, "_now", clock.now), patch.object(broker_mod.time, "sleep", lambda _s: broker._stop.set()):
            clock.t = 100.0 + BUSY_QUIET_SECONDS - 0.5
            broker._log_watcher()

        self.assertTrue(st.busy)
        self.assertTrue(st.turn_open)

    def test_bound_log_latch_clears_on_the_same_schedule(self) -> None:
        with TemporaryDirectory() as td:
            log_path = Path(td) / "pi.jsonl"
            log_path.write_text("", encoding="utf-8")
            st = _state(log_path=log_path)
            _latch_send(st, now_ts=100.0)

            cleared_at = _run_watcher_until_idle(_broker(st), _FakeClock(100.0))

        self.assertIsNotNone(cleared_at)
        self.assertGreaterEqual(cleared_at - 100.0, BUSY_QUIET_SECONDS)
        self.assertGreater(st.last_interrupted_idle_ts, 0.0)

    def test_unbound_log_with_real_turn_activity_after_send_keeps_holding(self) -> None:
        st = _state(log_path=None)
        _latch_send(st, now_ts=100.0)
        st.last_turn_activity_ts = 100.0 + 0.1  # a reducer-observed turn row after the send
        clock = _FakeClock(100.0)
        broker = _broker(st)

        ticks = 0

        def fake_sleep(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            clock.t += TICK_SECONDS
            # Keep the turn alive the way real output would: activity within the quiet window.
            st.last_turn_activity_ts = clock.t
            if ticks >= 40:
                broker._stop.set()

        with patch.object(broker_mod, "_now", clock.now), patch.object(broker_mod.time, "sleep", fake_sleep):
            broker._log_watcher()

        self.assertTrue(st.busy)
