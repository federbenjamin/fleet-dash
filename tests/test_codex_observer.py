import json
import os
import tempfile
import unittest

from codex_observer import (CodexRolloutObserver, MAX_READ_BYTES_PER_OBSERVE,
                            MAX_ROW_BYTES)


class CodexRolloutObserverTests(unittest.TestCase):
    THREAD = "019f63f8-a459-79b0-be08-609317547156"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        day = os.path.join(self.tmp.name, "2026", "07", "16")
        os.makedirs(day)
        self.path = os.path.join(day, "rollout-2026-07-16T00-00-00-" +
                                 self.THREAD + ".jsonl")
        self.observer = CodexRolloutObserver(self.tmp.name, clock=lambda: 1000,
                                             max_messages=20)

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, *rows, newline=True):
        with open(self.path, "ab") as handle:
            for index, row in enumerate(rows):
                if isinstance(row, bytes):
                    raw = row
                else:
                    raw = json.dumps(row, separators=(",", ":")).encode()
                handle.write(raw)
                if newline or index < len(rows) - 1:
                    handle.write(b"\n")

    @staticmethod
    def event(at, typ, **payload):
        return {"timestamp": at, "type": "event_msg",
                "payload": {"type": typ, **payload}}

    def test_lifecycle_messages_and_incremental_completion(self):
        self.append(
            {"timestamp": "2026-07-16T00:00:00Z", "type": "session_meta",
             "payload": {"id": self.THREAD, "cwd": "/private/repo"}},
            self.event("2026-07-16T00:00:01Z", "task_started", turn_id="turn-1"),
            self.event("2026-07-16T00:00:02Z", "user_message", message="Build it"),
            self.event("2026-07-16T00:00:03Z", "agent_message",
                       message="Working now", phase="commentary"))
        first = self.observer.observe(self.THREAD)
        self.assertTrue(first["active"])
        self.assertEqual(first["turn_id"], "turn-1")
        self.assertEqual([(item["role"], item["text"]) for item in first["messages"]],
                         [("user", "Build it"), ("assistant", "Working now")])

        complete = self.event("2026-07-16T00:00:04Z", "task_complete",
                              turn_id="turn-1", completed_at=1784160004)
        self.append(json.dumps(complete, separators=(",", ":")).encode(), newline=False)
        partial = self.observer.observe(self.THREAD)
        self.assertTrue(partial["active"], "partial JSONL rows must not be consumed")
        self.append(b"", newline=True)
        final = self.observer.observe(self.THREAD)
        self.assertFalse(final["active"])
        self.assertEqual(final["completed_at"], 1784160004)
        self.assertNotEqual(first["revision"], final["revision"])

    def test_turn_context_and_current_context_usage_are_observed(self):
        self.append(
            {"timestamp": "2026-07-16T00:00:00Z", "type": "turn_context",
             "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"}},
            self.event("2026-07-16T00:00:01Z", "token_count", info={
                "last_token_usage": {"input_tokens": 20_000,
                                     "cached_input_tokens": 16_000,
                                     "output_tokens": 200,
                                     "reasoning_output_tokens": 40,
                                     "total_tokens": 20_240},
                "total_token_usage": {"input_tokens": 40_000,
                                      "cached_input_tokens": 32_000,
                                      "output_tokens": 300,
                                      "reasoning_output_tokens": 60,
                                      "total_tokens": 40_360},
                "model_context_window": 272_000}))
        observed = self.observer.observe(self.THREAD)
        self.assertEqual((observed["model"], observed["effort"]),
                         ("gpt-5.6-sol", "xhigh"))
        self.assertEqual(observed["token_usage"], {
            "last": {"inputTokens": 20_000, "cachedInputTokens": 16_000,
                     "outputTokens": 200, "reasoningOutputTokens": 40,
                     "totalTokens": 20_240},
            "total": {"inputTokens": 40_000, "cachedInputTokens": 32_000,
                      "outputTokens": 300, "reasoningOutputTokens": 60,
                      "totalTokens": 40_360},
            "modelContextWindow": 272_000})

    def test_truncate_rebuild_malformed_unknown_and_adjacent_duplicate(self):
        self.append(
            b'{"type":"event_msg","payload":',
            self.event("2026-07-16T00:00:01Z", "future_event", value=1),
            self.event("2026-07-16T00:00:02Z", "agent_message", message="Same"),
            self.event("2026-07-16T00:00:03Z", "agent_message", message="Same"))
        observed = self.observer.observe(self.THREAD)
        self.assertIn("1 malformed rollout rows ignored", observed["warning"])
        self.assertEqual(len(observed["messages"]), 1)

        with open(self.path, "wb") as handle:
            handle.write((json.dumps(self.event("2026-07-16T00:01:00Z", "user_message",
                                                message="After truncate"),
                                     separators=(",", ":")) + "\n").encode())
        rebuilt = self.observer.observe(self.THREAD)
        self.assertEqual([item["text"] for item in rebuilt["messages"]], ["After truncate"])
        self.assertIsNone(rebuilt["warning"])

    def test_rejects_untrusted_ids_and_missing_rollouts(self):
        self.assertIsNone(self.observer.observe("../../config"))
        self.assertIsNone(self.observer.observe("019f63f8-a459-79b0-deadbeef"))

    def test_large_append_is_chunked_and_oversized_row_is_discarded(self):
        huge = b'{"type":"event_msg","payload":{"type":"agent_message","message":"' + \
            (b"x" * (MAX_READ_BYTES_PER_OBSERVE + MAX_ROW_BYTES)) + b'"}}\n'
        valid = json.dumps(self.event(
            "2026-07-16T00:00:05Z", "agent_message", message="after huge"),
            separators=(",", ":")).encode() + b"\n"
        with open(self.path, "wb") as handle:
            handle.write(huge)
            handle.write(valid)

        first = self.observer.observe(self.THREAD)
        entry = self.observer._entries[self.THREAD]
        self.assertLessEqual(entry["offset"], MAX_READ_BYTES_PER_OBSERVE)
        self.assertLessEqual(len(entry["remainder"]), MAX_ROW_BYTES)
        self.assertTrue(entry["discarding_oversized"])
        self.assertIn("oversized rollout row", first["warning"])

        for _ in range(4):
            final = self.observer.observe(self.THREAD)
            if final["messages"]:
                break
        self.assertEqual([item["text"] for item in final["messages"]], ["after huge"])
        self.assertFalse(self.observer._entries[self.THREAD]["discarding_oversized"])


if __name__ == "__main__":
    unittest.main()
