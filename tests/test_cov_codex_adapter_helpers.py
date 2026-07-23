"""Coverage tests for fleetdash.codex_adapter pure helpers: model catalog
parsing, conversation/file/agent normalization, usage/account math."""
import json
import os
import tempfile
import unittest

from fleetdash import codex_adapter as ca
from fleetdash.codex_adapter import (
    _account_usage, _agents, _conversation, _elicitation_pending, _epoch,
    _files, _item_text, _last_message, _latest_turn_lifecycle,
    _local_model_catalog, _usage_cumulative, _usage_window)
from fleetdash.codex_runtime import CodexError


class ModelCatalogTest(unittest.TestCase):
    def _write(self, payload):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        self.addCleanup(os.remove, path)
        return path

    def test_valid_catalog_with_efforts_and_window(self):
        path = self._write({"models": [
            {"slug": "gpt-5.4", "display_name": "GPT-5.4", "context_window": 272000,
             "supported_reasoning_levels": [{"effort": "high"}, "medium", "high"]},
            "not-a-dict",
            {"slug": "hidden-model", "display_name": "Hidden", "hidden": True},
            {"slug": "bad id!", "display_name": "Bad"},
            {"model": 123, "display_name": "NumId"},
            {"slug": "no-display", "display_name": 5},
            {"slug": "bad-window", "display_name": "BadWindow",
             "context_window": 99_000_000},
        ]})
        out = _local_model_catalog(path)
        self.assertEqual(out[0]["id"], "gpt-5.4")
        self.assertEqual(out[0]["efforts"], ["high", "medium"])
        self.assertEqual(out[0]["context_window"], 272000)
        ids = [m["id"] for m in out]
        self.assertNotIn("hidden-model", ids)
        self.assertNotIn("bad-window", [m["id"] for m in out if "context_window" in m])

    def test_oversized_cache_rejected(self):
        path = self._write({"models": [{"slug": "x", "display_name": "X"}]})
        with self.assertRaisesRegex(CodexError, "unexpectedly large"):
            _local_model_catalog(path, max_bytes=1)

    def test_missing_catalog_and_no_usable_models(self):
        no_catalog = self._write({"nope": []})
        with self.assertRaisesRegex(CodexError, "no model catalog"):
            _local_model_catalog(no_catalog)
        empty = self._write({"models": [{"slug": "bad id!"}]})
        with self.assertRaisesRegex(CodexError, "no usable models"):
            _local_model_catalog(empty)


class EpochAndUsageTest(unittest.TestCase):
    def test_epoch_invalid_string(self):
        self.assertIsNone(_epoch("2026-13-99"))

    def test_usage_cumulative_variants(self):
        self.assertEqual(_usage_cumulative({"total": {"totalTokens": 42}}), 42)
        self.assertEqual(_usage_cumulative({"total": {"inputTokens": 3,
                                                      "outputTokens": 4}}), 7)
        self.assertIsNone(_usage_cumulative({"total": {}}))
        self.assertIsNone(_usage_cumulative({"last": {"totalTokens": 5}}))
        self.assertIsNone(_usage_cumulative("not-a-dict"))

    def test_usage_window_bad_value(self):
        self.assertEqual(_usage_window({"modelContextWindow": "huge"}), 0)


class AccountUsageTest(unittest.TestCase):
    def test_windows_slots_and_resets(self):
        limits = {"rateLimitsByLimitId": {
            "codex": {"planType": "pro", "limitName": "Codex",
                      "primary": {"usedPercent": 40, "windowDurationMins": 300,
                                  "resetsAt": 1_800_000_000},
                      "secondary": {"usedPercent": 10, "windowDurationMins": 10080}},
            "other": {"primary": {"usedPercent": 5, "windowDurationMins": 120}},
            "bad": "not-a-dict",
            "noslot": {"primary": {"windowDurationMins": 300}}},  # no usedPercent
            "rateLimitResetCredits": {"availableCount": 3}}
        tokens = {"summary": {"lifetimeTokens": 99}, "dailyUsageBuckets": []}
        info = {"account": {"email": "codex@example.com", "planType": "team"}}
        out = _account_usage(limits, tokens, info)
        labels = {b["label"] for b in out["buckets"]}
        self.assertIn("Codex 5-hour", labels)
        self.assertIn("Codex weekly", labels)
        self.assertIn("2-hour", labels)
        self.assertEqual(out["plan_type"], "team")
        self.assertEqual(out["reset_credits"], 3)
        primary = next(b for b in out["buckets"] if b["label"] == "Codex 5-hour")
        self.assertTrue(primary["reset"].endswith("Z"))

    def test_rate_limits_fallback_and_bad_reset(self):
        limits = {"rateLimits": {"primary": {"usedPercent": 1,
                                             "windowDurationMins": 0,
                                             "resetsAt": "not-a-number"}}}
        out = _account_usage(limits, {}, {})
        self.assertEqual(out["buckets"][0]["label"], "primary")
        self.assertIsNone(out["buckets"][0]["reset"])


class ConversationTest(unittest.TestCase):
    def test_every_item_type_is_normalized(self):
        thread = {"turns": [{"items": [
            {"type": "userMessage", "text": "hi"},
            {"type": "agentMessage", "content": [{"text": "reply"}]},
            {"type": "plan", "text": "the plan"},
            {"type": "reasoning", "summary": ["a"], "content": ["b"]},
            {"type": "commandExecution", "command": "ls", "exitCode": 1},
            {"type": "fileChange", "changes": [{"path": "/x", "kind": "updated"}],
             "status": "completed"},
            {"type": "mcpToolCall", "server": "srv", "status": "failed"},
            {"type": "webSearch", "query": "python", "action": "searched"},
            {"type": "imageView", "path": "/img.png"},
            {"type": "imageGeneration", "revisedPrompt": "cat", "status": "done"},
            {"type": "sleep", "durationMs": 500},
            {"type": "enteredReviewMode", "review": "start"},
            {"type": "exitedReviewMode", "review": "end"},
            {"type": "contextCompaction"},
            {"type": "subAgentActivity", "kind": "started", "agentPath": "/a/reviewer"},
            {"type": "hookPrompt"},
            {"type": "mysteryType", "foo": "bar"},
        ]}]}
        messages = _conversation(thread)
        kinds = [m.get("kind") for m in messages if m.get("role") == "event"]
        self.assertIn("plan", kinds)
        self.assertIn("reasoning", kinds)
        self.assertIn("sleep", kinds)
        self.assertIn("review", kinds)
        self.assertIn("compact", kinds)
        self.assertIn("agent", kinds)
        self.assertIn("hook", kinds)
        self.assertIn("unknown", kinds)
        cmd = next(m for m in messages if m.get("name") == "commandExecution")
        self.assertTrue(cmd["failed"])

    def test_item_text_content_parts(self):
        self.assertEqual(_item_text({"content": [
            {"text": "line"}, {"type": "image", "path": "/p.png"},
            {"type": "skill", "name": "reviewer"}, "not-a-dict"]}),
            "line\n[image: /p.png]\nreviewer")

    def test_last_message_fallback_and_truncation(self):
        self.assertEqual(_last_message([], fallback="a" * 900)["text"][-1], "…")
        self.assertIsNone(_last_message([]))


class FilesAndAgentsTest(unittest.TestCase):
    def test_files_within_root_and_generated(self):
        with tempfile.TemporaryDirectory() as cwd:
            inside = os.path.join(cwd, "a.txt")
            open(inside, "w").close()
            thread = {"turns": [{"items": [
                {"type": "fileChange", "createdAt": 1,
                 "changes": [{"path": inside, "kind": "modified"},
                             {"path": "/etc/passwd", "kind": "modified"},  # outside
                             "not-a-dict"]},
                {"type": "imageGeneration", "savedPath": os.path.join(cwd, "gen.png"),
                 "createdAt": 2}]}]}
            files = _files(thread, cwd)
            paths = {f["path"] for f in files}
            self.assertIn(os.path.realpath(inside), paths)
            self.assertNotIn("/etc/passwd", paths)

    def test_agents_from_collab_tool_call(self):
        thread = {"turns": [{"items": [
            {"type": "collabAgentToolCall", "prompt": "review it", "model": "gpt-5.4",
             "reasoningEffort": "high",
             "agentsStates": {"child-a": {"status": "running"},
                              "child-b": {"status": "completed"}},
             "receiverThreadIds": ["child-a", "child-b"]}]}]}
        agents = _agents(thread, "parent")
        states = {a["agent_id"]: a["state"] for a in agents}
        self.assertEqual(states, {"child-a": "running", "child-b": "done"})

    def test_agents_terminal_state_is_sticky(self):
        thread = {"turns": [{"items": [
            {"type": "subAgentActivity", "kind": "completed",
             "agentThreadId": "c1", "agentPath": "/a/x"},
            {"type": "subAgentActivity", "kind": "started",
             "agentThreadId": "c1", "agentPath": "/a/x"}]}]}
        agents = _agents(thread, "parent")
        self.assertEqual(agents[0]["state"], "done")


class LifecycleAndElicitationTest(unittest.TestCase):
    def test_latest_turn_lifecycle_active_and_ended(self):
        active = _latest_turn_lifecycle({"turns": [{"startedAt": 1, "status": "running"}]})
        self.assertTrue(active["active"])
        ended = _latest_turn_lifecycle({"turns": [{"startedAt": 1, "completedAt": 2}]})
        self.assertFalse(ended["active"])
        self.assertEqual(_latest_turn_lifecycle({})["turn_id"], None)

    def test_elicitation_boolean_and_text_fields(self):
        pending = _elicitation_pending("7", {"serverName": "deploy", "message": "?",
            "requestedSchema": {"properties": {
                "flag": {"type": "boolean"},
                "note": {"type": "string"}}}})
        types = {f["name"]: f["type"] for f in pending["fields"]}
        self.assertEqual(types["flag"], "boolean")
        self.assertEqual(types["note"], "text")


if __name__ == "__main__":
    unittest.main()
