import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from codoxear.pi_history import list_pi_history, pi_history_log


class PiHistoryTests(unittest.TestCase):
    def test_history_includes_other_projects_and_mirrors_but_not_subagents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "project"
            cwd.mkdir()
            for name, workdir in [
                ("--project--/one.jsonl", str(cwd)),
                ("--other--/two.jsonl", str(root)),
                ("Mac/--remote--/mirror.jsonl", "/unavailable/remote"),
                ("Mac/--remote--/parent/run-0/session.jsonl", "/unavailable/remote"),
            ]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"type": "session", "id": name, "cwd": workdir}) + "\n")
            rows = list_pi_history(root)
            self.assertEqual(len(rows), 3)
            self.assertEqual(sum(row["resumable"] for row in rows), 2)
            mirror = next(row for row in rows if row["source_host"] == "Mac")
            self.assertFalse(mirror["resumable"])
            self.assertEqual(pi_history_log(root, mirror["history_id"]), root / "Mac/--remote--/mirror.jsonl")
            self.assertIsNone(pi_history_log(root, "../../outside"))

    def test_history_does_not_follow_log_symlinks_outside_the_session_directory(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "sessions"
            root.mkdir()
            outside = base / "outside.jsonl"
            outside.write_text(json.dumps({"type": "session", "id": "secret", "cwd": directory}) + "\n")
            (root / "escape.jsonl").symlink_to(outside)
            self.assertEqual(list_pi_history(root), [])
