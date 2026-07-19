import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "pending-capture.py"


class PendingCaptureHookTest(unittest.TestCase):
    def run_hook(self, home, payload):
        result = subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, timeout=5,
            env={**os.environ, "HOME": str(home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    @staticmethod
    def capture_path(home, session_id):
        return Path(home) / ".claude" / "fleet-dash" / "pending" / f"{session_id}.json"

    def test_question_and_notification_captures_are_complete_atomic_0600_json(self):
        cases = [
            ({"session_id": "question-session", "hook_event_name": "PreToolUse",
              "tool_name": "AskUserQuestion", "tool_input": {"questions": [
                  {"header": "Scope", "question": "How broad?",
                   "options": [{"label": "Focused"}]}]}}, "question"),
            ({"session_id": "permission-session", "hook_event_name": "Notification",
              "message": "Permission required to run the command"}, "permission"),
        ]
        for payload, kind in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as home:
                self.run_hook(home, payload)
                path = self.capture_path(home, payload["session_id"])
                raw = path.read_bytes()
                capture = json.loads(raw)

                self.assertEqual(capture["kind"], kind)
                self.assertTrue(capture["nonce"].startswith("hook-"))
                self.assertIsInstance(capture["ts"], float)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(list(path.parent.glob(".*.tmp")), [])
                if kind == "question":
                    self.assertEqual(capture["questions"],
                                     payload["tool_input"]["questions"])
                else:
                    self.assertEqual(capture["message"], payload["message"])

    def test_permission_notification_does_not_clobber_richer_question(self):
        session_id = "same-session"
        question = {"session_id": session_id, "hook_event_name": "PreToolUse",
                    "tool_name": "AskUserQuestion", "tool_input": {"questions": [
                        {"header": "Target", "question": "Where should this deploy?",
                         "multiSelect": False,
                         "options": [{"label": "Staging"}, {"label": "Production"}]}]}}
        notification = {"session_id": session_id, "hook_event_name": "Notification",
                        "message": "Permission required for this action"}
        with tempfile.TemporaryDirectory() as home:
            self.run_hook(home, question)
            path = self.capture_path(home, session_id)
            before = path.read_bytes()
            before_inode = path.stat().st_ino

            self.run_hook(home, notification)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_ino, before_inode)
            self.assertEqual(json.loads(before)["kind"], "question")
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
