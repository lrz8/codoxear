import os
import unittest

from codoxear import process_runtime
from codoxear import util


class TestProcessLiveness(unittest.TestCase):
    def test_liveness_facade_exports_runtime_functions(self) -> None:
        self.assertIs(util.pid_alive, process_runtime.pid_alive)
        self.assertIs(util.process_group_alive, process_runtime.process_group_alive)

    def test_liveness_helpers_reject_invalid_pids_and_detect_current_process(self) -> None:
        self.assertFalse(util.pid_alive(0))
        self.assertFalse(util.pid_alive(-1))
        self.assertFalse(util.process_group_alive(0))
        self.assertFalse(util.process_group_alive(-1))
        self.assertTrue(util.pid_alive(os.getpid()))

    @unittest.skipUnless(os.path.isdir("/proc"), "zombie detection needs procfs")
    def test_zombie_child_is_not_alive(self) -> None:
        # A zombie answered os.kill(pid, 0) but can never serve a socket again;
        # treating it as live let prune take the dead branch and wipe session state.
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            # The child exits immediately; without a waitpid it lingers as a zombie.
            # Give the kernel a moment to transition it.
            import time

            for _ in range(100):
                if not util.pid_alive(pid):
                    break
                time.sleep(0.01)
            self.assertFalse(util.pid_alive(pid))
        finally:
            os.waitpid(pid, 0)
        self.assertFalse(util.pid_alive(pid))


if __name__ == "__main__":
    unittest.main()
