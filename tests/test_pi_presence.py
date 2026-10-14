import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from codoxear.pi_presence import unmanaged_presence, resume_blocked_reason


class PiPresenceTests(unittest.TestCase):
    def test_plain_terminal_blocks_its_exact_conversation_when_registered(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "active-sessions").mkdir()
            (root / "active-sessions/123.json").write_text(json.dumps({"pid": 123, "sessionId": "active"}))
            with patch("codoxear.pi_presence._processes", return_value={123: (1, True, "/project")}):
                presence = unmanaged_presence([], config_dir=root)
            self.assertTrue(resume_blocked_reason("active", "/project", presence))
            self.assertEqual(resume_blocked_reason("older", "/project", presence), "")

    def test_legacy_unregistered_terminal_is_conservatively_detected_by_project(self):
        with TemporaryDirectory() as directory:
            with patch("codoxear.pi_presence._processes", return_value={123: (1, True, "/project")}):
                presence = unmanaged_presence([], config_dir=Path(directory))
            self.assertTrue(resume_blocked_reason("unknown", "/project", presence))
            self.assertEqual(resume_blocked_reason("other", "/elsewhere", presence), "")

    def test_broker_owned_pi_and_its_children_are_not_plain_terminals(self):
        with TemporaryDirectory() as directory:
            processes = {123: (1, True, "/project"), 124: (123, False, ""), 125: (124, True, "/project")}
            with patch("codoxear.pi_presence._processes", return_value=processes):
                presence = unmanaged_presence([{"pid": 123}], config_dir=Path(directory))
            self.assertEqual(presence, (set(), set()))
