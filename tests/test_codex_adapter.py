import json
import unittest
import os
import tempfile

from fleetdash.codex_adapter import (CodexAdapter, _account_usage, _agents,
                           _conversation, _files, _local_model_catalog,
                           _usage_cumulative, _usage_total, _usage_window)
from fleetdash.codex_protocol import CodexAppServer


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
        self.thread_state.setdefault(thread_id, {}).update(
            collaboration_mode=mode, model=model, effort=effort)

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
            state_path=os.path.join(self.tmp.name, "threads.json"),
            models_cache_path=os.path.join(self.tmp.name, "models-cache.json"))
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
        self.assertTrue(session["capabilities"]["spawn_agent"])
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

    def test_file_preview_content_types_keep_html_inert(self):
        expected = {"preview.html": "text/plain; charset=utf-8",
                    "report.pdf": "application/pdf",
                    "data.json": "application/json; charset=utf-8"}
        for name, ctype in expected.items():
            path = os.path.join(self.tmp.name, name)
            with open(path, "wb") as handle:
                handle.write(b"preview")
            self.adapter.context = lambda _key, value=path: {
                "ok": True, "files": [{"path": value}]}
            actual, data, error = self.adapter.file_content("codex:thr-1", path)
            self.assertIsNone(error)
            self.assertEqual(actual, ctype)
            self.assertEqual(data, b"preview")

    def test_empty_unmaterialized_thread_has_empty_context(self):
        out = CodexAdapter(client=EmptyThreadClient()).context("codex:thr-empty")
        self.assertEqual(out, {"ok": True, "messages": [], "files": [], "agents": []})

    def test_actions_strip_provider_prefix(self):
        self.adapter._refresh()
        out = self.adapter.act({"type": "text", "session_id": "codex:thr-1",
                                "text": "continue"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.client.started, ("thr-1", "continue", {
            "mode": "default", "model": "gpt-5.4", "effort": "high"}))

    def test_resume_requires_explicit_fleet_runtime_ownership(self):
        allowed, reason = self.adapter.resume_capability("codex:thr-1")
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertEqual(self.adapter.resume_owned_thread("codex:thr-1"), {
            "ok": True, "session_id": "codex:thr-1", "resumed": True})

        state = self.adapter._state()
        state["threads"].append("external-thread")
        state.setdefault("thread_meta", {})["external-thread"] = {
            "runtime_owner": "external", "unmaterialized": False}
        self.adapter._save_state(state)
        allowed, reason = self.adapter.resume_capability("codex:external-thread")
        self.assertFalse(allowed)
        self.assertIn("view only", reason)
        self.assertFalse(self.adapter.resume_owned_thread(
            "codex:external-thread")["ok"])

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
        self.assertEqual(out, {"ok": True, "mode": "plan", "durable": True})
        self.assertEqual(self.client.mode_changed,
                         ("thr-1", "plan", "gpt-5.4", "high"))
        self.assertEqual(self.adapter.sessions()[0]["collaboration_mode"], "plan")

    def test_relay_routes_through_parent_thread(self):
        self.adapter._refresh()
        out = self.adapter.act({"type": "relay", "session_id": "codex:thr-1",
                                "agent_id": "child-1", "text": "continue"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["relayed_via"], "parent")
        self.assertEqual(self.client.started[0], "thr-1")
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

    def test_subagent_terminal_state_survives_stale_running_projection(self):
        agents = _agents({"turns": [{"items": [
            {"type": "subAgentActivity", "kind": "completed",
             "agentThreadId": "child-1", "agentPath": "/root/worker"},
            {"type": "collabAgentToolCall", "prompt": "review",
             "receiverThreadIds": ["child-1"],
             "agentsStates": {"child-1": {"status": "running"}}}]}]},
            "parent-1")
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
        self.assertIsNone(_usage_cumulative(usage))
        usage["total"] = {"inputTokens": 900, "outputTokens": 100,
                          "reasoningOutputTokens": 50, "totalTokens": 1050}
        self.assertEqual(_usage_cumulative(usage), 1050)

    def test_local_model_catalog_retains_context_window(self):
        path = os.path.join(self.tmp.name, "models-cache.json")
        with open(path, "w") as handle:
            json.dump({"models": [{"slug": "gpt-local", "display_name": "GPT Local",
                                    "context_window": 272000}]}, handle)
        self.assertEqual(_local_model_catalog(path), [{"id": "gpt-local",
                          "name": "GPT Local", "efforts": [],
                          "context_window": 272000}])

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

    def test_repeated_codex_file_change_becomes_newest_without_duplication(self):
        thread = {"turns": [
            {"items": [{"type": "fileChange", "createdAt": 1, "changes": [
                {"path": "first.md", "kind": "add"},
                {"path": "second.md", "kind": "add"}]}]},
            {"items": [{"type": "fileChange", "createdAt": 2, "changes": [
                {"path": "first.md", "kind": "update"}]}]},
        ]}
        files = _files(thread, "/work/project")
        self.assertEqual([item["path"] for item in files],
                         ["/work/project/first.md", "/work/project/second.md"])
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["change_kind"], "update")

    def test_local_model_catalog_uses_codex_cache_schema(self):
        path = os.path.join(self.tmp.name, "models-cache.json")
        with open(path, "w") as handle:
            json.dump({"models": [{"slug": "gpt-next",
                "display_name": "GPT Next",
                "supported_reasoning_levels": [
                    {"effort": "low"}, {"effort": "high"}]}]}, handle)
        self.assertEqual(_local_model_catalog(path), [{
            "id": "gpt-next", "name": "GPT Next",
            "efforts": ["low", "high"]}])

    def test_local_model_catalog_filters_hidden_invalid_and_duplicate_rows(self):
        path = os.path.join(self.tmp.name, "models-cache.json")
        with open(path, "w") as handle:
            json.dump({"models": [
                {"slug": " hidden ", "display_name": "Hidden", "hidden": True},
                {"slug": "invisible", "display_name": "Invisible", "visible": False},
                {"slug": "bad\nmodel", "display_name": "Bad"},
                {"slug": "bad-display", "display_name": "Bad\x00Name"},
                {"slug": " gpt-next ", "display_name": " GPT Next ",
                 "supported_reasoning_levels": [
                    {"effort": " high "}, {"effort": "bad\neffort"}]},
                {"slug": "gpt-next", "display_name": "Ignored duplicate name",
                 "supported_reasoning_levels": ["low", "high"]},
            ]}, handle)
        self.assertEqual(_local_model_catalog(path), [{
            "id": "gpt-next", "name": "GPT Next", "efforts": ["high", "low"]}])

    def test_changed_invalid_model_cache_fails_closed(self):
        path = os.path.join(self.tmp.name, "models-cache.json")
        with open(path, "w") as handle:
            json.dump({"models": [{"slug": "gpt-local", "display_name": "GPT Local",
                                    "supported_reasoning_levels": ["high"]}]}, handle)
        self.adapter._models_cache_path = path
        self.adapter._refresh()
        self.assertEqual([item["id"] for item in self.adapter.models], ["gpt-local"])
        self.assertTrue(self.adapter.sessions()[0]["capabilities"]["model_effort_settings"])

        with open(path, "w") as handle:
            handle.write('{"models": [broken cache payload')
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        self.adapter._refresh()
        self.assertEqual(self.adapter.models, [])
        session = self.adapter.sessions()[0]
        self.assertFalse(session["capabilities"]["model_effort_settings"])
        self.assertFalse(session["capabilities"]["change_model_effort"])

    def test_refresh_reads_local_models_without_calling_app_server_model_list(self):
        path = os.path.join(self.tmp.name, "models-cache.json")
        with open(path, "w") as handle:
            json.dump({"models": [{"slug": "gpt-local",
                                    "display_name": "GPT Local"}]}, handle)
        self.adapter._models_cache_path = path
        self.client.list_models = lambda: self.fail("model/list must not be called")
        self.adapter._refresh()
        self.assertEqual(self.adapter.models[0]["id"], "gpt-local")


if __name__ == "__main__":
    unittest.main()
