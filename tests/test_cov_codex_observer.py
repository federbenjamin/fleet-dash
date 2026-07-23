"""Coverage tests for fleetdash.codex_observer: epoch/token/message helpers,
malformed rows, error handling, and child-agent rollout discovery."""
import json
import os
import tempfile
import unittest
from unittest import mock

from fleetdash import codex_observer as co
from fleetdash.codex_observer import CodexRolloutObserver


class HelperTest(unittest.TestCase):
    def test_epoch_variants(self):
        self.assertEqual(co._epoch(5), 5)
        self.assertEqual(co._epoch(20_000_000_000), 20_000_000)  # ms scaled
        self.assertIsNone(co._epoch("not-a-timestamp"))
        self.assertIsNone(co._epoch(None))
        self.assertAlmostEqual(co._epoch("2026-07-16T00:00:00Z"), 1784160000, delta=1)

    def test_token_usage_rejects_non_dict_and_bad_window(self):
        self.assertIsNone(co.CodexRolloutObserver._token_usage({"info": "nope"}))
        usage = co.CodexRolloutObserver._token_usage({"info": {
            "last_token_usage": "bad", "total_token_usage": {"input_tokens": 5},
            "model_context_window": "huge"}})
        self.assertEqual(usage, {"total": {"inputTokens": 5}})

    def test_message_skips_empty_and_keeps_phase(self):
        entry = {"messages": __import__("collections").deque(maxlen=10)}
        CodexRolloutObserver._message(entry, "assistant", "  ", 1)  # empty -> skipped
        self.assertEqual(len(entry["messages"]), 0)
        CodexRolloutObserver._message(entry, "assistant", "hi", 1, phase="commentary")
        self.assertEqual(entry["messages"][0]["phase"], "commentary")


class ObserverBranchTest(unittest.TestCase):
    THREAD = "019f63f8-a459-79b0-be08-609317547156"
    CHILD = "019f63f8-a459-79b0-be08-609317547999"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.day = os.path.join(self.tmp.name, "2026", "07", "16")
        os.makedirs(self.day)
        self.path = os.path.join(self.day, "rollout-2026-07-16T00-00-00-" +
                                 self.THREAD + ".jsonl")
        self.observer = CodexRolloutObserver(self.tmp.name, clock=lambda: 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, rows):
        with open(path, "wb") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")).encode() + b"\n")

    def test_malformed_rows_counted(self):
        with open(self.path, "wb") as handle:
            handle.write(b"123\n")                       # valid json, not a dict
            handle.write(b'{"type":"event_msg"}\n')      # no payload dict
        observed = self.observer.observe(self.THREAD)
        self.assertIn("malformed", observed["warning"])

    def test_observe_error_without_entry(self):
        # A rollout path that resolves to a directory makes open() fail with no
        # prior entry -> the bare error snapshot branch.
        os.makedirs(os.path.join(self.day, "rollout-2026-07-16T00-00-00-" +
                                 self.THREAD + ".jsonl"))
        observed = self.observer.observe(self.THREAD)
        self.assertIn("error", observed)
        self.assertEqual(observed["messages"], [])

    def test_observe_error_retains_entry(self):
        self.write(self.path, [{"timestamp": "2026-07-16T00:00:01Z",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": "hi"}}])
        self.observer.observe(self.THREAD)  # seeds an entry
        os.remove(self.path)
        os.makedirs(self.path)  # same name now resolves to a directory
        errored = self.observer.observe(self.THREAD)
        self.assertIsNotNone(errored["error"])

    def test_line_rejects_oversized_row(self):
        entry = {"oversized_rows": 0}
        self.observer._line(entry, b"x" * (co.MAX_ROW_BYTES + 1))
        self.assertEqual(entry["oversized_rows"], 1)

    def test_oversized_row_discard_resets_after_newline(self):
        huge = (b'{"type":"event_msg","payload":{"type":"agent_message",'
                b'"message":"' + b"x" * (3 * 1024 * 1024) + b'"}}\n')
        valid = json.dumps({"timestamp": "2026-07-16T00:00:05Z", "type": "event_msg",
                            "payload": {"type": "agent_message", "message": "after"}},
                           separators=(",", ":")).encode() + b"\n"
        with open(self.path, "wb") as handle:
            handle.write(huge)
            handle.write(valid)
        for _ in range(4):
            observed = self.observer.observe(self.THREAD)
            if observed.get("messages"):
                break
        self.assertFalse(self.observer._entries[self.THREAD]["discarding_oversized"])
        self.assertEqual([m["text"] for m in observed["messages"]], ["after"])

    def test_child_agents_are_discovered(self):
        self.write(self.path, [
            {"timestamp": "2026-07-16T00:00:00Z", "type": "session_meta",
             "payload": {"id": self.THREAD}},
            {"timestamp": "2026-07-16T00:00:01Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "t1"}}])
        child_path = os.path.join(self.day, "rollout-2026-07-16T00-01-00-" +
                                  self.CHILD + ".jsonl")
        self.write(child_path, [
            {"timestamp": "2026-07-16T00:01:00Z", "type": "session_meta",
             "payload": {"id": self.CHILD, "parent_thread_id": self.THREAD}},
            {"timestamp": "2026-07-16T00:01:01Z", "type": "event_msg",
             "payload": {"type": "agent_message", "message": "child working"}}])
        observed = self.observer.observe(self.THREAD)
        agents = observed["agents"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["native_session_id"], self.CHILD)
        self.assertEqual(agents[0]["last_msg"]["text"], "child working")

    def test_child_discovery_ignores_unrelated_and_malformed_rollouts(self):
        self.write(self.path, [
            {"timestamp": "2026-07-16T00:00:00Z", "type": "session_meta",
             "payload": {"id": self.THREAD}}])
        # An unrelated rollout without the parent marker is skipped.
        other = os.path.join(self.day, "rollout-2026-07-16T00-02-00-other.jsonl")
        with open(other, "wb") as handle:
            handle.write(b'{"type":"session_meta","payload":{"id":"unrelated"}}\n')
        # A rollout dir carrying the parent marker cannot be opened -> skipped.
        marker = ('"parent_thread_id":"%s"' % self.THREAD).encode()
        baddir = os.path.join(self.day, "rollout-2026-07-16T00-03-00-baddir.jsonl")
        os.makedirs(baddir)
        # Parent marker but a too-short child id -> id regex fails, skipped.
        short = os.path.join(self.day, "rollout-2026-07-16T00-04-00-shortid.jsonl")
        with open(short, "wb") as handle:
            handle.write(b'{"payload":{"id":"abc",' + marker + b'}}\n')
        # Parent marker + valid child id whose own rollout file is absent -> None.
        missing = os.path.join(self.day, "rollout-2026-07-16T00-05-00-missing.jsonl")
        with open(missing, "wb") as handle:
            handle.write(b'{"payload":{"id":"019f0000-a459-79b0-be08-000000000001",'
                         + marker + b'}}\n')
        observed = self.observer.observe(self.THREAD)
        self.assertEqual(observed["agents"], [])


if __name__ == "__main__":
    unittest.main()
