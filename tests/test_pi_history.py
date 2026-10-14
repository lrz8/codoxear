import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from codoxear.pi_history import list_pi_history, pi_history_log


class PiHistoryTests(unittest.TestCase):
    def test_named_handoff_keeps_its_source_and_does_not_use_boilerplate_as_title(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "--project--" / "local.jsonl"
            log.parent.mkdir()
            rows = [
                {"type": "session", "id": "local-id", "cwd": directory},
                {"type": "message", "message": {"role": "user", "content": "# Synced session text handoff\nSource: Mac"}},
                {"type": "custom", "customType": "pi-sync.session-handoff", "data": {
                    "sourceHostname": "Mac", "sourceSessionId": "original", "transfer": "sanitized-text",
                }},
                {"type": "session_info", "name": "Repair the deployment"},
            ]
            log.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = list_pi_history(root, local_hostname="Vultr")[0]
            self.assertEqual(result["display_name"], "Repair the deployment")
            self.assertEqual(result["conversation_id"], "Mac/original")
            self.assertEqual(result["branch_id"], "Vultr/local-id")
            self.assertEqual(result["history_kind"], "handoff")
            self.assertEqual(result["first_user_message"], "")
            with log.open("a") as stream:
                stream.write(json.dumps({"type": "session_info", "name": "Updated name"}) + "\n")
            self.assertEqual(list_pi_history(root)[0]["display_name"], "Updated name")

    def test_metadata_discovery_skips_oversized_records_and_retains_later_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "--project--" / "large.jsonl"
            log.parent.mkdir()
            log.write_text(json.dumps({"type": "session", "id": "large", "cwd": directory}) + "\n"
                           + json.dumps({"type": "custom", "data": "x" * (512 * 1024)}) + "\n"
                           + json.dumps({"type": "session_info", "name": "Large history"}) + "\n")
            self.assertEqual(list_pi_history(root)[0]["display_name"], "Large history")

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
