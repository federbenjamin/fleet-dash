import json
import os
import tempfile
import unittest

from codex_adapter import CodexAdapter, _elicitation_pending, _last_message, _revision


class FixtureClient:
    def __init__(self, threads):
        self.threads = threads
        self.details = {item["id"]: dict(item) for item in threads}
        self.thread_state = {}
        self.approvals = {}
        self.fail_list = False
        self.fail_read = False
        self.fail_account = False
        self.archive_error = None
        self.interrupts = []

    def list_threads(self):
        if self.fail_list:
            raise RuntimeError("app-server unavailable")
        return [dict(item) for item in self.threads]

    def list_models(self):
        return [{"model": "gpt-5.4", "displayName": "GPT-5.4",
                 "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]

    def read_thread(self, thread_id):
        if self.fail_read:
            raise RuntimeError("thread read failed")
        return dict(self.details[thread_id])

    def resume_thread(self, thread_id):
        return {"id": thread_id, "model": "gpt-5.4", "effort": "high"}

    def request(self, method, params):
        return {}

    def archive(self, thread_id):
        if self.archive_error:
            raise RuntimeError(self.archive_error)
        self.threads = [item for item in self.threads if item["id"] != thread_id]

    def interrupt(self, thread_id):
        self.interrupts.append(thread_id)

    def account_limits(self):
        if self.fail_account:
            raise RuntimeError("credentials expired")
        return {"rateLimitsByLimitId": {"codex": {"planType": "pro",
            "primary": {"usedPercent": 10, "windowDurationMins": 300}}}}

    def account_usage(self):
        return {"summary": {"lifetimeTokens": 50}}

    def list_skills(self, cwd):
        return [{"cwd": cwd, "errors": [], "skills": [{"name": "reviewer",
            "description": "Review code", "path": os.path.join(cwd, "SKILL.md"),
            "scope": "repo", "enabled": True}]}]


class CodexAdapterFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "codex.json")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def thread(thread_id="managed", status=None, updated=1000):
        return {"id": thread_id, "cwd": "/work/project", "name": thread_id,
                "createdAt": 900, "updatedAt": updated,
                "model": "gpt-5.4", "effort": "high",
                "status": status or {"type": "idle"}, "turns": []}

    def adapter(self, threads, now=1000):
        client = FixtureClient(threads)
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: now, stall_seconds=30)
        return adapter, client

    def test_every_thread_state_and_external_discovery(self):
        threads = [
            self.thread("running", {"type": "active", "activeFlags": []}),
            self.thread("waiting", {"type": "active",
                                     "activeFlags": ["waitingOnApproval"]}),
            self.thread("broken", {"type": "systemError"}),
            self.thread("done", {"type": "idle"}),
            self.thread("external", {"type": "notLoaded"}),
        ]
        adapter, client = self.adapter(threads)
        for item in ("running", "waiting", "broken", "done"):
            adapter._remember(item, "default")
        client.thread_state["done"] = {"completed_at": 990, "revision": 1}
        adapter._refresh()
        states = {item["native_session_id"]: item["state"] for item in adapter.sessions()}
        self.assertEqual(states, {"running": "running", "waiting": "needs_you",
                                  "broken": "error", "done": "turn_done",
                                  "external": "reopenable"})
        external = next(item for item in adapter.sessions()
                        if item["native_session_id"] == "external")
        self.assertTrue(external["capabilities"]["takeover"])
        self.assertFalse(external["capabilities"]["submit"])

    def test_cross_app_server_turn_without_completion_is_running(self):
        thread = self.thread("managed", {"type": "notLoaded"}, updated=100)
        thread["turns"] = [{"id": "desktop-turn", "status": "interrupted",
                            "startedAt": 995, "completedAt": None,
                            "items": [{"id": "a1", "type": "agentMessage",
                                       "phase": "commentary", "text": "working"}]}]
        adapter, _ = self.adapter([thread], now=1000)
        adapter._remember("managed", "default")
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual(session["state"], "running")
        self.assertEqual(session["reg_status"], "running")
        self.assertEqual(session["quiet_s"], 5)
        # Fleet Dash can observe this desktop-owned turn but cannot safely steer,
        # interrupt, or close it without the owning App Server's turn ID.
        self.assertFalse(session["capabilities"]["submit"])
        self.assertFalse(session["capabilities"]["interrupt"])
        self.assertFalse(session["capabilities"]["close"])
        self.assertFalse(session["capabilities"]["compact"])
        self.assertFalse(session["capabilities"]["review"])
        self.assertFalse(session["capabilities"]["relay_agent"])
        denied = adapter.act({"type": "text", "session_id": "codex:managed",
                              "text": "do not start a concurrent turn"})
        self.assertFalse(denied["ok"])
        self.assertIn("another client", denied["error"])

    def test_cross_app_server_completed_turn_becomes_turn_done(self):
        thread = self.thread("managed", {"type": "notLoaded"}, updated=100)
        thread["turns"] = [{"id": "desktop-turn", "status": "completed",
                            "startedAt": 950, "completedAt": 995,
                            "items": [{"id": "a1", "type": "agentMessage",
                                       "phase": "final_answer", "text": "done"}]}]
        adapter, _ = self.adapter([thread], now=1000)
        adapter._remember("managed", "default")
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual(session["state"], "turn_done")
        self.assertEqual(session["reg_status"], "turn_done")
        self.assertEqual(session["quiet_s"], 5)
        self.assertTrue(session["capabilities"]["submit"])
        self.assertTrue(session["capabilities"]["close"])

    def test_owned_app_server_turn_keeps_control_capabilities(self):
        thread = self.thread("managed", {"type": "active"}, updated=990)
        adapter, client = self.adapter([thread], now=1000)
        adapter._remember("managed", "default")
        client.thread_state["managed"] = {
            "status": "running", "turn_id": "owned-turn", "updated_at": 998}
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual(session["state"], "running")
        self.assertTrue(session["capabilities"]["submit"])
        self.assertTrue(session["capabilities"]["interrupt"])
        self.assertTrue(session["capabilities"]["close"])

    def test_stalled_and_dormant_are_time_based(self):
        threads = [self.thread("stalled", {"type": "active"}, updated=99_900),
                   self.thread("dormant", {"type": "notLoaded"}, updated=1)]
        adapter, _ = self.adapter(threads, now=100_000)
        adapter._remember("stalled", "default")
        adapter._remember("dormant", "default")
        adapter._refresh()
        states = {item["native_session_id"]: item["state"] for item in adapter.sessions()}
        self.assertEqual(states["stalled"], "stalled")
        self.assertEqual(states["dormant"], "dormant")

    def test_refresh_failure_marks_cached_sessions_stale(self):
        adapter, client = self.adapter([self.thread()])
        adapter._remember("managed", "default")
        adapter._refresh()
        client.fail_list = True
        adapter._refresh()
        session = adapter.sessions()[0]
        self.assertEqual(session["state"], "stale")
        self.assertIn("app-server unavailable", session["stale_reason"])
        self.assertFalse(session["capabilities"]["submit"])
        self.assertFalse(session["capabilities"]["close"])

    def test_durable_context_fallback_survives_read_failure(self):
        thread = self.thread()
        thread["turns"] = [{"items": [{"id": "u1", "type": "userMessage",
                                        "content": [{"type": "text", "text": "hello"}]}]}]
        adapter, client = self.adapter([thread])
        adapter._remember("managed", "default")
        first = adapter.context("codex:managed")
        self.assertEqual(first["messages"][0]["text"], "hello")
        client.fail_read = True
        cached = adapter.context("codex:managed")
        self.assertTrue(cached["ok"])
        self.assertTrue(cached["closed"])
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["messages"][0]["text"], "hello")

    def test_same_second_events_change_revision(self):
        thread = self.thread()
        live = {"revision": 1}
        first = _revision(thread, live)
        live["revision"] = 2
        second = _revision(thread, live)
        self.assertNotEqual(first, second)

    def test_codex_peek_preserves_markdown_blocks(self):
        text = "### Default width\n\nUse **Fit the screen**."
        self.assertEqual(_last_message([{"role": "assistant", "text": text}]),
                         {"role": "assistant", "text": text})

    def test_native_skills_and_actions_are_intentional(self):
        adapter, _ = self.adapter([self.thread()])
        adapter._remember("managed", "default")
        adapter._refresh()
        result = adapter.commands("codex:managed", "/work/project")
        commands = {item["name"]: item for item in result["commands"]}
        self.assertEqual(commands["/compact"]["execution"], "action")
        self.assertEqual(commands["$reviewer"]["execution"], "skill")
        self.assertNotIn("/model", commands)

    def test_account_failure_retains_explicit_stale_data(self):
        adapter, client = self.adapter([self.thread()])
        adapter._refresh_account(1000)
        self.assertFalse(adapter.account_usage()["stale"])
        client.fail_account = True
        adapter._refresh_account(1040)
        usage = adapter.account_usage()
        self.assertTrue(usage["stale"])
        self.assertIn("credentials expired", usage["error"])

    def test_mcp_elicitation_normalizes_single_multi_and_text_fields(self):
        pending = _elicitation_pending("7", {"serverName": "deploy", "message": "Choose",
            "mode": "form", "requestedSchema": {"type": "object", "required": ["env"],
                "properties": {"env": {"type": "string", "enum": ["dev", "prod"]},
                               "regions": {"type": "array", "items": {
                                   "type": "string", "enum": ["us", "eu"]}},
                               "note": {"type": "string"}}}})
        self.assertEqual(pending["kind"], "elicitation")
        self.assertEqual([field["multiSelect"] for field in pending["fields"]],
                         [False, True, False])
        self.assertTrue(pending["fields"][0]["required"])

    def test_state_file_is_valid_after_repeated_updates(self):
        adapter, _ = self.adapter([self.thread()])
        for index in range(20):
            adapter._remember(f"thread-{index}", "plan" if index % 2 else "default")
        with open(self.state_path) as handle:
            state = json.load(handle)
        self.assertEqual(len(state["threads"]), 20)
        self.assertFalse(os.path.exists(self.state_path + ".tmp"))

    def test_many_sessions_refresh_and_recover_after_repeated_outages(self):
        threads = [self.thread(f"thread-{index}", updated=1000 + index)
                   for index in range(150)]
        adapter, client = self.adapter(threads, now=2000)
        for index in range(0, 150, 3):
            adapter._remember(f"thread-{index}", "plan" if index % 2 else "default")
        adapter._refresh()
        self.assertEqual(len(adapter.sessions()), 150)
        for _ in range(10):
            client.fail_list = True
            adapter._refresh()
            self.assertTrue(all(item["state"] == "stale" for item in adapter.sessions()))
            client.fail_list = False
            adapter._refresh()
            self.assertFalse(any(item.get("stale") for item in adapter.sessions()))

    def test_large_conversation_is_normalized_and_snapshot_is_bounded(self):
        thread = self.thread()
        thread["turns"] = [{"id": "large", "items": [
            {"id": f"m-{index}", "type": "agentMessage", "text": f"message {index}"}
            for index in range(5000)]}]
        adapter, _ = self.adapter([thread])
        adapter._remember("managed", "default")
        context = adapter.context("codex:managed")
        self.assertEqual(len(context["messages"]), 5000)
        self.assertEqual(context["messages"][-1]["text"], "message 4999")
        with open(self.state_path) as handle:
            state = json.load(handle)
        self.assertEqual(len(state["snapshots"]["managed"]["messages"]), 300)

    def test_child_thread_level_idle_status_marks_subagent_done(self):
        parent = self.thread()
        parent["turns"] = [{"items": [{"type": "subAgentActivity", "kind": "started",
            "agentThreadId": "child", "agentPath": "/agents/reviewer"}]}]
        adapter, client = self.adapter([parent])
        adapter._remember("managed", "default")
        client.details["child"] = {"id": "child", "status": {"type": "idle"},
            "turns": [{"items": [{"type": "agentMessage", "text": "done"}]}]}
        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["agents"][0]["state"], "done")

    def test_unmaterialized_empty_thread_archive_discards_local_state(self):
        adapter, client = self.adapter([self.thread()])
        adapter._remember("managed", "plan")
        adapter._refresh()
        client.archive_error = "no rollout found for thread id managed"
        self.assertTrue(adapter.act({"type": "archive",
                                    "session_id": "codex:managed"})["ok"])
        self.assertEqual(adapter.sessions(), [])
        self.assertNotIn("managed", adapter._managed())

    def test_close_interrupts_active_thread_then_archives_it(self):
        thread = self.thread(status={"type": "active"})
        adapter, client = self.adapter([thread])
        adapter._remember("managed", "default")
        client.thread_state["managed"] = {
            "status": "running", "turn_id": "owned-turn", "updated_at": 1000}
        adapter._refresh()
        session = adapter.sessions()[0]
        self.assertTrue(session["capabilities"]["close"])
        result = adapter.act({"type": "close", "session_id": "codex:managed"})
        self.assertTrue(result["ok"])
        self.assertEqual(client.interrupts, ["managed"])
        self.assertEqual(adapter.sessions(), [])
        self.assertNotIn("managed", adapter._managed())

    def test_unmaterialized_thread_survives_refresh_and_adapter_restart(self):
        adapter, client = self.adapter([])
        meta = {"cwd": "/work/project", "model": "gpt-5.4", "effort": "high",
                "name": "empty", "created_at": 900, "unmaterialized": True}
        adapter._remember("empty", "plan", meta)
        adapter._refresh()
        first = adapter.sessions()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["collaboration_mode"], "plan")
        restarted = CodexAdapter(client=client, state_path=self.state_path,
                                 clock=lambda: 1000, stall_seconds=30)
        restarted._refresh()
        self.assertEqual(restarted.sessions()[0]["native_session_id"], "empty")

    def test_failed_detail_read_does_not_materialize_transient_list_row(self):
        adapter, client = self.adapter([self.thread("empty")])
        meta = {"cwd": "/work/project", "model": "gpt-5.4", "effort": "high",
                "name": "empty", "created_at": 900, "unmaterialized": True}
        adapter._remember("empty", "plan", meta)
        client.fail_read = True
        adapter._refresh()
        client.threads = []
        client.fail_read = False
        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["native_session_id"], "empty")


if __name__ == "__main__":
    unittest.main()
