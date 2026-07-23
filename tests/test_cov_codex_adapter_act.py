"""Coverage tests for fleetdash.codex_adapter act() dispatch branches and the
remaining adapter methods (pending projection, context, commands, resume,
diagnostics, state persistence)."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from fleetdash.codex_adapter import CodexAdapter
from fleetdash.codex_runtime import CodexError


class Client:
    def __init__(self):
        self.thread_state = {}
        self.approvals = {}
        self.generation = 1
        self.lock = None
        self.decided = []
        self.answered = []
        self.elicited = []
        self.interrupts = []
        self.compactions = []
        self.reviews = []
        self.started = []
        self.steered = []
        self.resumed = []
        self.archived = []
        self.loaded_ids = []
        self.steer_error = None
        self.archive_error = None

    def list_threads(self):
        return []

    def owns_active_turn(self, tid):
        return bool(self.thread_state.get(tid, {}).get("turn_id"))

    def decide(self, nonce, choice):
        self.decided.append((nonce, choice))
        return {"ok": True}

    def answer_questions(self, nonce, answers):
        self.answered.append((nonce, answers))
        return {"ok": True}

    def answer_elicitation(self, nonce, action, content=None):
        self.elicited.append((nonce, action, content))
        return {"ok": True}

    def interrupt(self, tid):
        self.interrupts.append(tid)

    def compact(self, tid):
        self.compactions.append(tid)

    def review(self, tid, target=None):
        self.reviews.append(tid)

    def start_turn(self, tid, text, **kwargs):
        self.started.append((tid, text, kwargs))

    def steer_turn(self, tid, text, **kwargs):
        self.steered.append((tid, text, kwargs))
        if self.steer_error:
            raise self.steer_error

    def resume_thread(self, tid):
        self.resumed.append(tid)
        return {}

    def archive(self, tid):
        self.archived.append(tid)
        if self.archive_error:
            raise self.archive_error

    def loaded_thread_ids(self):
        return [{"id": x} for x in self.loaded_ids]

    def start_thread(self, cwd, model=None, effort=None):
        return {"id": "new-thread"}

    def set_mode(self, tid, mode, model, effort):
        pass


class ActTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _adapter(self, client=None):
        client = client or Client()
        adapter = CodexAdapter(
            client=client, state_path=os.path.join(self.tmp.name, "state.json"),
            clock=lambda: 1000, stall_seconds=30,
            models_cache_path=os.path.join(self.tmp.name, "models.json"))
        return adapter, client

    def _ready(self, adapter, tid="t", caps=None, mode="default", state="idle"):
        adapter._remember(tid, mode, {"model": "gpt-5.4", "effort": "high",
                                      "unmaterialized": False})
        session = {"native_session_id": tid, "session_id": f"codex:{tid}",
                   "read_only": False, "stale": False, "state": state,
                   "collaboration_mode": mode, "model": "gpt-5.4", "effort": "high",
                   "capabilities": caps or {}}
        adapter._sessions = [session]
        return session

    def test_permission_decide(self):
        adapter, client = self._adapter()
        client.approvals["9"] = {"thread_id": "t", "method": "execCommandApproval",
                                 "params": {}, "state": "pending"}
        self._ready(adapter, caps={"decide_approval": True})
        result = adapter.act({"type": "permission", "session_id": "codex:t",
                              "nonce": "9", "choice": "allow"})
        self.assertTrue(result["ok"])
        self.assertEqual(client.decided, [("9", "allow")])

    def test_multiq_and_option(self):
        adapter, client = self._adapter()
        client.approvals["9"] = {"thread_id": "t", "method": "item/tool/requestUserInput",
                                 "params": {"questions": [{"id": "q",
                                            "options": [{"label": "One"}]}]},
                                 "state": "pending"}
        self._ready(adapter, caps={"answer_structured": True})
        adapter.act({"type": "multiq", "session_id": "codex:t", "nonce": "9",
                     "answers": [{"digits": [1]}]})
        adapter.act({"type": "option", "session_id": "codex:t", "nonce": "9",
                     "digits": [1]})
        self.assertEqual(len(client.answered), 2)

    def test_elicitation_answer(self):
        adapter, client = self._adapter()
        client.approvals["9"] = {"thread_id": "t",
                                 "method": "mcpServer/elicitation/request",
                                 "params": {}, "state": "pending"}
        self._ready(adapter, caps={"answer_structured": True})
        adapter.act({"type": "elicitation", "session_id": "codex:t", "nonce": "9",
                     "choice": "decline"})
        self.assertEqual(client.elicited, [("9", "decline", None)])

    def test_dismiss_variants(self):
        for method, expect in (("mcpServer/elicitation/request", "elicit"),
                               ("item/tool/requestUserInput", "interrupt"),
                               ("execCommandApproval", "decide")):
            adapter, client = self._adapter()
            client.approvals["9"] = {"thread_id": "t", "method": method,
                                     "params": {}, "state": "pending"}
            self._ready(adapter, caps={"decide_approval": True})
            result = adapter.act({"type": "dismiss", "session_id": "codex:t",
                                  "nonce": "9"})
            self.assertTrue(result["ok"])
            if expect == "elicit":
                self.assertEqual(client.elicited[0][1], "decline")
            elif expect == "interrupt":
                self.assertEqual(client.interrupts, ["t"])
            else:
                self.assertEqual(client.decided[0], ("9", "cancel"))

    def test_dismiss_stale_request(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"decide_approval": True})
        # Nonce present in capabilities gate but the approval is gone -> stale.
        client.approvals["9"] = {"thread_id": "t", "method": "x", "state": "pending"}
        adapter._sessions[0]["capabilities"]["decide_approval"] = True
        result = adapter.act({"type": "dismiss", "session_id": "codex:t", "nonce": "9"})
        # method 'x' is not elicitation/question -> falls through to decide cancel
        self.assertTrue(result["ok"])

    def test_interrupt(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"interrupt": True})
        adapter.act({"type": "interrupt", "session_id": "codex:t"})
        self.assertEqual(client.interrupts, ["t"])

    def test_compact_and_review(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"compact": True, "review": True})
        client.loaded_ids = ["t"]
        self.assertTrue(adapter.act({"type": "compact", "session_id": "codex:t"})["ok"])
        self.assertEqual(client.compactions, ["t"])
        self.assertTrue(adapter.act({"type": "review", "session_id": "codex:t"})["ok"])
        self.assertEqual(client.reviews, ["t"])

    def test_compact_blocked_by_active_turn(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"compact": True})
        client.thread_state["t"] = {"turn_id": "live", "status": "running"}
        result = adapter.act({"type": "compact", "session_id": "codex:t"})
        self.assertFalse(result["ok"])

    def test_skill(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"submit": True})
        adapter._skills["t"] = {"$reviewer": {"name": "reviewer", "path": "/p/SKILL.md"}}
        result = adapter.act({"type": "skill", "session_id": "codex:t",
                              "name": "$reviewer", "args": "focus"})
        self.assertTrue(result["ok"])
        self.assertEqual(client.started[0][0], "t")

    def test_skill_unknown(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"submit": True})
        result = adapter.act({"type": "skill", "session_id": "codex:t",
                              "name": "$missing"})
        self.assertFalse(result["ok"])

    def test_relay_via_steer_and_start(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"relay_agent": True})
        client.thread_state["t"] = {"turn_id": "live", "status": "running"}
        self.assertTrue(adapter.act({"type": "relay", "session_id": "codex:t",
                                     "agent_id": "child", "text": "go"})["ok"])
        self.assertEqual(len(client.steered), 1)

        adapter2, client2 = self._adapter()
        self._ready(adapter2, caps={"relay_agent": True})
        self.assertTrue(adapter2.act({"type": "relay", "session_id": "codex:t",
                                      "agent_id": "child", "text": "go"})["ok"])
        self.assertEqual(len(client2.started), 1)

    def test_relay_requires_text_and_agent(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"relay_agent": True})
        result = adapter.act({"type": "relay", "session_id": "codex:t",
                              "agent_id": "", "text": ""})
        self.assertFalse(result["ok"])

    def test_text_steer_turn_ended_falls_back_to_start(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"submit": True})
        client.thread_state["t"] = {"turn_id": "live", "status": "running",
                                    "model": "gpt-5.4", "effort": "high"}
        client.steer_error = CodexError("turn ended", code="turn_ended")
        result = adapter.act({"type": "text", "session_id": "codex:t", "text": "hi"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.started), 1)

    def test_text_empty_is_rejected(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"submit": True})
        result = adapter.act({"type": "text", "session_id": "codex:t", "text": "  "})
        self.assertFalse(result["ok"])

    def test_image_text_invalid_inputs(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"submit": True})
        result = adapter.act({"type": "image_text", "session_id": "codex:t",
                              "text": "x", "image_paths": ["relative/path.jpg"]})
        self.assertFalse(result["ok"])

    def test_mode_plan_without_model_rejected(self):
        adapter, client = self._adapter()
        session = self._ready(adapter, caps={"submit": True}, mode="plan")
        session["model"] = ""
        self.assertTrue(adapter._owns_metadata(
            (adapter._state().get("thread_meta") or {}).get("t")))
        result = adapter.act({"type": "text", "session_id": "codex:t", "text": "hi"})
        self.assertFalse(result["ok"])

    def test_unsupported_action_type(self):
        adapter, client = self._adapter()
        self._ready(adapter, caps={"submit": True})
        result = adapter.act({"type": "teleport", "session_id": "codex:t"})
        self.assertFalse(result["ok"])

    def test_runtime_migration_blocks_act(self):
        adapter, client = self._adapter()
        adapter.runtime_migration = SimpleNamespace(mutation_blocked=lambda: True)
        result = adapter.act({"type": "text", "session_id": "codex:t", "text": "hi"})
        self.assertTrue(result["queueable"])


class ShapeAndBlockerTest(unittest.TestCase):
    def test_thread_shape_error_variants(self):
        shape = CodexAdapter._thread_shape_error
        self.assertIn("not an object", shape("nope"))
        self.assertIn("no string id", shape({"id": 5}))
        self.assertIn("gitInfo", shape({"id": "t", "gitInfo": "x"}))
        self.assertIn("status", shape({"id": "t", "status": 5}))
        self.assertIn("active flags", shape(
            {"id": "t", "status": {"activeFlags": [1]}}))
        self.assertIn("turns are not a list", shape({"id": "t", "turns": {}}))
        self.assertIn("malformed turn", shape({"id": "t", "turns": [None]}))
        self.assertIn("items are not a list", shape(
            {"id": "t", "turns": [{"items": {}}]}))
        self.assertIn("malformed item", shape(
            {"id": "t", "turns": [{"items": [None]}]}))
        self.assertIn("subagent states", shape({"id": "t", "turns": [{"items": [
            {"type": "collabAgentToolCall", "agentsStates": []}]}]}))
        self.assertIn("receiver ids", shape({"id": "t", "turns": [{"items": [
            {"type": "collabAgentToolCall", "receiverThreadIds": [1]}]}]}))
        self.assertIn("subagent id", shape({"id": "t", "turns": [{"items": [
            {"type": "subAgentActivity", "agentThreadId": 5}]}]}))
        self.assertIn("subagent path", shape({"id": "t", "turns": [{"items": [
            {"type": "subAgentActivity", "agentPath": 5}]}]}))
        self.assertIsNone(shape({"id": "t", "turns": [{"items": [
            {"type": "userMessage", "text": "hi"}]}]}))

    def test_context_window_for_model_bad_value(self):
        adapter = CodexAdapter(client=Client(), state_path=None, clock=lambda: 1,
                               models_cache_path="/nope.json")
        adapter.models = [{"id": "gpt-5.4", "context_window": "not-int"}]
        self.assertIsNone(adapter._context_window_for_model("gpt-5.4"))
        self.assertIsNone(adapter._context_window_for_model("missing"))

    def test_settings_runtime_blocker_with_client_lock(self):
        import threading
        client = Client()
        client.lock = threading.Lock()
        adapter = CodexAdapter(client=client, state_path=None, clock=lambda: 1,
                               models_cache_path="/nope.json")
        client.thread_state["t"] = {"compacting": 0}
        blocker, _live = adapter._settings_runtime_blocker("t")
        self.assertIn("compacting", blocker)
        client.thread_state["t"] = {}
        client.approvals["9"] = {"thread_id": "t", "state": "pending"}
        blocker, _live = adapter._settings_runtime_blocker("t")
        self.assertIn("waiting on a request", blocker)
        client.approvals.clear()
        client.thread_state["t"] = {"turn_id": "live"}
        blocker, _live = adapter._settings_runtime_blocker("t")
        self.assertIn("only while the turn is idle", blocker)


class SessionSettingsActTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _ready(self, catalog_efforts=("high", "medium")):
        client = Client()
        adapter = CodexAdapter(
            client=client, state_path=os.path.join(self.tmp.name, "s.json"),
            clock=lambda: 1000,
            models_cache_path=os.path.join(self.tmp.name, "m.json"))
        adapter.models = [{"id": "gpt-5.4", "name": "G",
                           "efforts": list(catalog_efforts)}]
        adapter._remember("t", "default", {"model": "gpt-5.4", "effort": "high",
                                           "unmaterialized": False})
        client.thread_state["t"] = {"status": "idle", "model": "gpt-5.4",
                                    "effort": "high"}
        adapter._sessions = [{"native_session_id": "t", "session_id": "codex:t",
            "read_only": False, "stale": False, "state": "idle",
            "collaboration_mode": "default", "model": "gpt-5.4", "effort": "high",
            "capabilities": {"change_model_effort": True}}]
        return adapter, client

    def _act(self, adapter, **extra):
        base = {"type": "session_settings", "session_id": "codex:t",
                "model": "gpt-5.4", "effort": "high",
                "expected_model": "gpt-5.4", "expected_effort": "high"}
        base.update(extra)
        return adapter.act(base)

    def test_missing_expected_values(self):
        adapter, _ = self._ready()
        result = adapter.act({"type": "session_settings", "session_id": "codex:t",
                              "model": "gpt-5.4", "effort": "high"})
        self.assertEqual(result["code"], "stale_settings")

    def test_unknown_model(self):
        adapter, _ = self._ready()
        result = self._act(adapter, model="unknown-model")
        self.assertIn("unknown Codex model", result["error"])

    def test_unsupported_effort(self):
        adapter, _ = self._ready()
        result = self._act(adapter, effort="xhigh")
        self.assertIn("unsupported effort", result["error"])

    def test_effort_when_model_has_none(self):
        adapter, _ = self._ready(catalog_efforts=())
        result = self._act(adapter, effort="high")
        self.assertIn("does not advertise effort", result["error"])

    def test_durable_false_warns(self):
        adapter, client = self._ready()
        adapter._remember = lambda *a, **k: False
        result = self._act(adapter, effort="medium")
        self.assertTrue(result["ok"])
        self.assertFalse(result["durable"])
        self.assertIn("durably save", result["warning"])


class TextAndModeBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _ready(self, caps, mode="default"):
        client = Client()
        adapter = CodexAdapter(
            client=client, state_path=os.path.join(self.tmp.name, "s.json"),
            clock=lambda: 1000,
            models_cache_path=os.path.join(self.tmp.name, "m.json"))
        adapter._remember("t", mode, {"model": "gpt-5.4", "effort": "high",
                                      "unmaterialized": False})
        adapter._sessions = [{"native_session_id": "t", "session_id": "codex:t",
            "read_only": False, "stale": False, "state": "idle",
            "collaboration_mode": mode, "model": "gpt-5.4", "effort": "high",
            "capabilities": caps}]
        return adapter, client

    def test_text_steer_success(self):
        adapter, client = self._ready({"submit": True})
        client.thread_state["t"] = {"turn_id": "live", "status": "running",
                                    "model": "gpt-5.4", "effort": "high"}
        result = adapter.act({"type": "text", "session_id": "codex:t", "text": "go"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.steered), 1)
        self.assertEqual(client.started, [])

    def test_text_steer_other_error_propagates(self):
        adapter, client = self._ready({"submit": True})
        client.thread_state["t"] = {"turn_id": "live", "status": "running"}
        client.steer_error = CodexError("provider unavailable",
                                        code="provider_control_unavailable",
                                        queueable=True)
        result = adapter.act({"type": "text", "session_id": "codex:t", "text": "go"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["queueable"])

    def test_mode_invalid_value(self):
        adapter, client = self._ready({"submit": True})
        result = adapter.act({"type": "mode", "session_id": "codex:t", "mode": "weird"})
        self.assertFalse(result["ok"])

    def test_mode_blocked_by_turn(self):
        adapter, client = self._ready({"submit": True})
        client.thread_state["t"] = {"turn_id": "live", "status": "running"}
        result = adapter.act({"type": "mode", "session_id": "codex:t", "mode": "plan"})
        self.assertFalse(result["ok"])

    def test_mode_durable_false_warns(self):
        adapter, client = self._ready({"submit": True})
        adapter._remember = lambda *a, **k: False
        result = adapter.act({"type": "mode", "session_id": "codex:t", "mode": "plan"})
        self.assertTrue(result["ok"])
        self.assertIn("durably save", result["warning"])

    def test_archive_generic_error_propagates(self):
        adapter, client = self._ready({"archive": True})
        client.archive_error = RuntimeError("archive service down")
        result = adapter.act({"type": "archive", "session_id": "codex:t"})
        self.assertFalse(result["ok"])
        self.assertIn("archive service down", result["error"])


class PendingProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = CodexAdapter(
            client=Client(), state_path=os.path.join(self.tmp.name, "s.json"),
            clock=lambda: 1000,
            models_cache_path=os.path.join(self.tmp.name, "m.json"))

    def test_question_elicitation_and_permission(self):
        self.adapter.client.approvals["1"] = {"thread_id": "t",
            "method": "item/tool/requestUserInput",
            "params": {"questions": [{"header": "H", "question": "Pick",
                       "options": [{"label": "A", "description": "first"}]}]}}
        self.adapter.client.approvals["2"] = {"thread_id": "t",
            "method": "mcpServer/elicitation/request",
            "params": {"serverName": "s", "message": "m", "requestedSchema": {}}}
        self.adapter.client.approvals["3"] = {"thread_id": "t",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "ls", "cwd": "/work"}}
        self.assertEqual(self.adapter._pending("t", "1")["kind"], "question")
        self.assertEqual(self.adapter._pending("t", "2")["kind"], "elicitation")
        perm = self.adapter._pending("t", "3")
        self.assertEqual(perm["kind"], "permission")
        self.assertIn("working directory", perm["input_summary"])
        self.assertIsNone(self.adapter._pending("t", None))
        self.assertEqual(self.adapter._pending("t", ["1"])["kind"], "question")


class MethodsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _adapter(self, client=None, state_path="s.json"):
        return CodexAdapter(
            client=client or Client(),
            state_path=os.path.join(self.tmp.name, state_path) if state_path else None,
            clock=lambda: 1000,
            models_cache_path=os.path.join(self.tmp.name, "m.json"))

    def test_sessions_disabled_returns_empty(self):
        adapter = self._adapter()
        adapter.enabled = False
        self.assertEqual(adapter.sessions(), [])

    def test_stateless_adapter_methods(self):
        adapter = self._adapter(state_path=None)
        self.assertEqual(adapter._state(), {})
        self.assertFalse(adapter._remember("t"))
        self.assertEqual(adapter._modes(), {})
        adapter._save_state({"x": 1})   # no-op, no path
        adapter._cache_snapshot("t", [], [], "r", {})  # no-op

    def test_diagnostics_and_runtime_status(self):
        adapter = self._adapter()
        diag = adapter.diagnostics()
        self.assertIn("provider_error", diag)
        self.assertEqual(diag["runtime"]["mode"], "private")
        self.assertEqual(adapter.runtime_status()["mode"], "private")
        adapter.runtime_migration = SimpleNamespace(
            diagnostics=lambda: {"mode": "migrating"})
        self.assertEqual(adapter.runtime_status()["mode"], "migrating")
        self.assertEqual(adapter.diagnostics()["runtime"]["mode"], "migrating")

    def test_account_usage_stale_error(self):
        adapter = self._adapter()
        adapter._account_error = "credentials expired"
        usage = adapter.account_usage()
        self.assertTrue(usage["stale"])

    def test_resume_capability_and_owned(self):
        client = Client()
        adapter = self._adapter(client)
        self.assertEqual(adapter.resume_capability("codex:ghost"),
                         (False, "external Codex thread is view only"))
        adapter._remember("owned", "plan", {"unmaterialized": True})
        self.assertFalse(adapter.resume_capability("codex:owned")[0])
        adapter._remember("owned", "plan", {"unmaterialized": False})
        allowed, _ = adapter.resume_capability("codex:owned")
        self.assertTrue(allowed)
        client.loaded_ids = []
        result = adapter.resume_owned_thread("codex:owned")
        self.assertTrue(result["ok"])
        self.assertIn("owned", client.resumed)

    def test_resume_owned_thread_rejects_external(self):
        adapter = self._adapter()
        result = adapter.resume_owned_thread("codex:external")
        self.assertFalse(result["ok"])

    def test_ensure_loaded_advisory_loaded_list_failure(self):
        client = Client()

        def boom():
            raise RuntimeError("loaded list down")
        client.loaded_thread_ids = boom
        adapter = self._adapter(client)
        adapter._remember("owned", "plan", {"unmaterialized": False})
        # loaded list failure must not resume (avoids active-turn abort)
        adapter._ensure_loaded("owned")
        self.assertEqual(client.resumed, [])

    def test_context_and_agent_context_and_commands(self):
        thread = {"id": "t", "cwd": "/work", "turns": [{"items": [
            {"id": "u", "type": "userMessage", "content": [{"text": "hi"}]},
            {"type": "subAgentActivity", "kind": "started",
             "agentThreadId": "child", "agentPath": "/a/x"}]}]}

        class ReadClient(Client):
            def read_thread(self, tid):
                if tid == "child":
                    return {"id": "child", "turns": []}
                return dict(thread)

            def list_skills(self, cwd):
                return [{"errors": ["oops"], "skills": [
                    {"name": "reviewer", "path": "/p", "enabled": True,
                     "description": "Review"}]}]

        client = ReadClient()
        adapter = self._adapter(client)
        adapter._remember("t", "default", {"unmaterialized": False})
        ctx = adapter.context("codex:t")
        self.assertTrue(ctx["ok"])
        self.assertEqual(ctx["messages"][0]["text"], "hi")
        adapter._sessions = [{"native_session_id": "t", "read_only": False,
            "agents": [{"agent_id": "child", "native_session_id": "child",
                        "agent_type": "codex", "description": "x", "model": ""}]}]
        agent_ctx = adapter.agent_context("codex:t", "child")
        self.assertTrue(agent_ctx["ok"])
        self.assertIn("info", agent_ctx)
        cmds = adapter.commands("codex:t", "/work")
        names = {c["name"] for c in cmds["commands"]}
        self.assertIn("$reviewer", names)

    def test_commands_skills_failure_is_warning(self):
        class FailClient(Client):
            def list_skills(self, cwd):
                raise RuntimeError("skills down")
        adapter = self._adapter(FailClient())
        result = adapter.commands("codex:t", "/work")
        self.assertIn("warning", result)

    def test_context_read_failure_uses_snapshot(self):
        class FailReadClient(Client):
            def read_thread(self, tid):
                raise RuntimeError("read boom")
        adapter = self._adapter(FailReadClient())
        adapter._remember("t", "default", {"unmaterialized": False})
        # seed a snapshot so the failure path returns cached content
        state = adapter._state()
        state.setdefault("snapshots", {})["t"] = {"revision": "r1",
            "messages": [{"role": "user", "text": "cached"}], "files": [],
            "info": {}}
        adapter._save_state(state)
        ctx = adapter.context("codex:t")
        self.assertTrue(ctx["stale"])
        self.assertEqual(ctx["messages"][0]["text"], "cached")

    def test_file_content_paths(self):
        with tempfile.TemporaryDirectory() as cwd:
            fpath = os.path.join(cwd, "a.txt")
            with open(fpath, "w") as handle:
                handle.write("hello")
            real = os.path.realpath(fpath)
            thread = {"id": "t", "cwd": cwd, "turns": [{"items": [
                {"type": "fileChange", "changes": [{"path": real, "kind": "updated"}]}]}]}

            class ReadClient(Client):
                def read_thread(self, tid):
                    return dict(thread)
            adapter = self._adapter(ReadClient())
            adapter._remember("t", "default", {"unmaterialized": False})
            ctype, data, err = adapter.file_content("codex:t", real)
            self.assertIsNone(err)
            self.assertEqual(data, b"hello")
            _c, _d, err2 = adapter.file_content("codex:t", "/not/allowed.txt")
            self.assertIsNotNone(err2)

    def test_start_thread_initial_turn_failure_cleans_up(self):
        class FailTurnClient(Client):
            def start_turn(self, tid, text, **kwargs):
                raise RuntimeError("turn boom")
        client = FailTurnClient()
        client.archive_error = RuntimeError("no rollout found for thread id new-thread")
        adapter = self._adapter(client)
        with self.assertRaisesRegex(CodexError, "failed to start initial"):
            adapter.start_thread("/work", model="gpt-5.4", effort="high",
                                 mode="default", initial_text="go")
        self.assertNotIn("new-thread", adapter._managed())


if __name__ == "__main__":
    unittest.main()
