import unittest
import os
import tempfile

from codex_adapter import (CodexAdapter, CodexAppServer, _account_usage, _agents,
                           _conversation, _files, _usage_total, _usage_window)


class ResponseClient(CodexAppServer):
    def __init__(self):
        super().__init__(command=["false"])
        self.responses = []

    def respond(self, rid, result):
        self.responses.append((rid, result))


class FakeClient:
    def __init__(self):
        self.thread_state = {}
        self.approvals = {}
        self.fail_start = False
        self.archived = []

    def list_threads(self):
        return [{"id": "thr-1", "name": "Fix parser", "cwd": "/work/app",
                 "preview": "Please fix it", "createdAt": 100, "updatedAt": 200,
                 "model": "gpt-5.4", "effort": "high",
                 "status": {"type": "notLoaded"},
                 "gitInfo": {"branch": "feature"}}]

    def read_thread(self, thread_id):
        return {"id": thread_id, "turns": [{"items": [
            {"type": "userMessage", "text": "hello"},
            {"type": "agentMessage", "text": "hi"},
            {"type": "commandExecution", "command": "pwd", "status": "completed"},
        ]}]}

    def start_turn(self, thread_id, text, **settings):
        if self.fail_start:
            raise RuntimeError("turn bootstrap failed")
        self.started = (thread_id, text, settings)

    def start_thread(self, cwd, model=None, effort=None):
        return {"id": "thr-new", "cwd": cwd, "model": model or "gpt-5.4",
                "effort": effort or "high"}

    def archive(self, thread_id):
        self.archived.append(thread_id)

    def set_mode(self, thread_id, mode, model, effort):
        self.mode_changed = (thread_id, mode, model, effort)

    def interrupt(self, thread_id):
        self.interrupted = thread_id

    def request(self, method, params):
        self.requested = (method, params)
        return {}


class EmptyThreadClient(FakeClient):
    def read_thread(self, thread_id):
        raise RuntimeError(f"thread {thread_id} is not materialized yet")


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = CodexAdapter(client=self.client,
            state_path=os.path.join(self.tmp.name, "threads.json"))
        self.adapter._remember("thr-1", "default")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sessions_are_provider_qualified(self):
        self.adapter._refresh()
        session = self.adapter.sessions()[0]
        self.assertEqual(session["session_id"], "codex:thr-1")
        self.assertEqual(session["native_session_id"], "thr-1")
        self.assertEqual(session["provider"], "codex")
        self.assertEqual(session["branch"], "feature")
        self.assertEqual(session["cost_source"], "unavailable")
        self.assertEqual(session["_latest_prose"],
                         {"role": "assistant", "text": "hi"})

    def test_refresh_failure_preserves_the_last_state_for_page_placement(self):
        self.adapter._refresh()

        def fail():
            raise RuntimeError("app server disconnected")

        self.client.list_threads = fail
        self.adapter._refresh()
        session = self.adapter.sessions()[0]
        self.assertEqual(session["state"], "stale")
        self.assertEqual(session["stale_previous_state"], "dormant")
        self.assertIn("disconnected", session["stale_reason"])

    def test_context_normalizes_messages_and_tools(self):
        out = self.adapter.context("codex:thr-1")
        self.assertTrue(out["ok"])
        self.assertEqual([m["role"] for m in out["messages"]],
                         ["user", "assistant", "tool"])

    def test_empty_unmaterialized_thread_has_empty_context(self):
        out = CodexAdapter(client=EmptyThreadClient()).context("codex:thr-empty")
        self.assertEqual(out, {"ok": True, "messages": [], "files": []})

    def test_actions_strip_provider_prefix(self):
        self.adapter._refresh()
        out = self.adapter.act({"type": "text", "session_id": "codex:thr-1",
                                "text": "continue"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.client.started, ("thr-1", "continue", {
            "mode": "default", "model": "gpt-5.4", "effort": "high"}))

    def test_new_thread_sends_visible_initial_hi_and_marks_materialized(self):
        thread = self.adapter.start_thread("/work/app", "gpt-5.4", "high",
                                           "plan", initial_text="hi")
        self.assertEqual(thread["id"], "thr-new")
        self.assertEqual(self.client.started, ("thr-new", "hi", {
            "mode": "plan", "model": "gpt-5.4", "effort": "high"}))
        state = self.adapter._state()
        self.assertFalse(state["thread_meta"]["thr-new"]["unmaterialized"])
        self.assertEqual(state["thread_meta"]["thr-new"]["runtime_owner"],
                         "fleet_shared")

    def test_failed_initial_hi_removes_empty_owned_thread(self):
        self.client.fail_start = True
        with self.assertRaisesRegex(Exception, "failed to start initial Codex turn"):
            self.adapter.start_thread("/work/app", "gpt-5.4", "high",
                                      "default", initial_text="hi")
        self.assertEqual(self.client.archived, ["thr-new"])
        self.assertNotIn("thr-new", self.adapter._managed())
        self.assertFalse(any(s.get("native_session_id") == "thr-new"
                             for s in self.adapter._sessions))

    def test_mode_action_updates_thread_settings(self):
        self.adapter._refresh()
        out = self.adapter.act({"type": "mode", "session_id": "codex:thr-1",
                                "mode": "plan"})
        self.assertEqual(out, {"ok": True, "mode": "plan"})
        self.assertEqual(self.client.mode_changed,
                         ("thr-1", "plan", "gpt-5.4", "high"))
        self.assertEqual(self.adapter.sessions()[0]["collaboration_mode"], "plan")

    def test_relay_routes_through_parent_thread(self):
        out = self.adapter.act({"type": "relay", "session_id": "codex:parent",
                                "agent_id": "child-1", "text": "continue"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["relayed_via"], "parent")
        self.assertEqual(self.client.started[0], "parent")
        self.assertIn("child-1", self.client.started[1])

    def test_conversation_exposes_reasoning_and_unknown_items(self):
        out = _conversation({"turns": [{"items": [
            {"type": "reasoning", "summary": ["checked parser"]},
            {"type": "futureItem", "value": 7}]}]})
        self.assertEqual([item["kind"] for item in out], ["reasoning", "unknown"])
        self.assertIn("futureItem", out[1]["title"])

    def test_subagent_activity_history_is_discovered(self):
        agents = _agents({"turns": [{"items": [
            {"type": "subAgentActivity", "kind": "started",
             "agentThreadId": "child-1", "agentPath": "/root/worker"},
            {"type": "subAgentActivity", "kind": "completed",
             "agentThreadId": "child-1", "agentPath": "/root/worker"}]}]},
            "parent-1")
        self.assertEqual(agents[0]["agent_id"], "child-1")
        self.assertEqual(agents[0]["agent_type"], "worker")
        self.assertEqual(agents[0]["session_id"], "codex:parent-1")
        self.assertEqual(agents[0]["state"], "done")

    def test_codex_question_answers_use_option_labels(self):
        client = ResponseClient()
        client.approvals["9"] = {"request_id": 9, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "scope", "options": [
                {"label": "Full"}, {"label": "Small"}]}]}}
        client.answer_questions("9", [{"digits": [2]}])
        self.assertEqual(client.responses, [(9, {"answers": {
            "scope": {"answers": ["Small"]}}})])

    def test_permission_grants_are_method_specific(self):
        client = ResponseClient()
        client.approvals["7"] = {"request_id": 7,
            "method": "item/permissions/requestApproval",
            "params": {"permissions": {"network": {"enabled": True}, "fileSystem": None}}}
        client.decide("7", "always")
        self.assertEqual(client.responses[0][1], {
            "permissions": {"network": {"enabled": True}}, "scope": "session"})

    def test_token_usage_uses_total_and_context_window(self):
        usage = {"last": {"inputTokens": 80, "cachedInputTokens": 60,
                           "outputTokens": 20, "reasoningOutputTokens": 5,
                           "totalTokens": 105}, "modelContextWindow": 1000}
        self.assertEqual(_usage_total(usage), 105)
        self.assertEqual(_usage_window(usage), 1000)

    def test_account_usage_flattens_quota_windows(self):
        normalized = _account_usage({"rateLimitsByLimitId": {"codex": {
            "planType": "pro", "primary": {"usedPercent": 25,
                "windowDurationMins": 300, "resetsAt": 2_000_000_000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080,
                "resetsAt": 2_000_000_100}}}},
            {"summary": {"lifetimeTokens": 1234}},
            {"account": {"type": "chatgpt", "email": "codex@example.com",
                         "planType": "pro"}})
        self.assertEqual([b["label"] for b in normalized["buckets"]],
                         ["5-hour", "weekly"])
        self.assertEqual(normalized["lifetime_tokens"], 1234)
        self.assertEqual(normalized["email"], "codex@example.com")

    def test_file_changes_are_safe_and_not_claimed_as_deliveries(self):
        thread = {"turns": [{"items": [{"type": "fileChange", "changes": [
            {"path": "src/app.py", "kind": "update"},
            {"path": "../escape.txt", "kind": "add"}]}]}]}
        files = _files(thread, "/work/project")
        self.assertEqual([item["path"] for item in files], ["/work/project/src/app.py"])
        self.assertFalse(files[0]["delivered"])


if __name__ == "__main__":
    unittest.main()
