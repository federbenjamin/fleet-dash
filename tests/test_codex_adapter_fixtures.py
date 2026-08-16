import json
import os
import tempfile
import threading
import time
import unittest

from fleetdash.codex_adapter import (CodexAdapter, _elicitation_pending,
                           _last_message, _revision)
from fleetdash.codex_runtime import CodexError


class FixtureClient:
    def __init__(self, threads):
        self.threads = threads
        self.details = {item["id"]: dict(item) for item in threads}
        self.thread_state = {}
        self.approvals = {}
        self.fail_list = False
        self.fail_read = False
        self.fail_loaded = False
        self.fail_account = False
        self.archive_error = None
        self.interrupts = []
        self.loaded = []
        self.read_calls = []
        self.started_turns = []
        self.steered_turns = []
        self.steer_error = None
        self.mode_changed = None
        self.mode_error = None
        self.compactions = []

    def list_threads(self):
        if self.fail_list:
            raise RuntimeError("app-server unavailable")
        return [dict(item) for item in self.threads]

    def list_models(self):
        return [{"model": "gpt-5.4", "displayName": "GPT-5.4",
                 "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]

    def loaded_thread_ids(self):
        if self.fail_loaded:
            raise RuntimeError("loaded list unavailable")
        return list(self.loaded)

    def read_thread(self, thread_id):
        self.read_calls.append(thread_id)
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

    def start_turn(self, thread_id, text, **kwargs):
        self.started_turns.append((thread_id, text, kwargs))

    def steer_turn(self, thread_id, text, **kwargs):
        self.steered_turns.append((thread_id, text, kwargs))
        if self.steer_error:
            raise self.steer_error

    def set_mode(self, thread_id, mode, model, effort):
        if self.mode_error:
            raise self.mode_error
        self.mode_changed = (thread_id, mode, model, effort)
        self.thread_state.setdefault(thread_id, {}).update(
            collaboration_mode=mode, model=model, effort=effort)

    def compact(self, thread_id):
        self.compactions.append(thread_id)

    def account_limits(self):
        if self.fail_account:
            raise RuntimeError("credentials expired")
        return {"rateLimitsByLimitId": {"codex": {"planType": "pro",
            "primary": {"usedPercent": 10, "windowDurationMins": 300}}}}

    def account_usage(self):
        return {"summary": {"lifetimeTokens": 50}}

    def account_info(self):
        if self.fail_account:
            raise RuntimeError("credentials expired")
        return {"account": {"type": "chatgpt", "email": "codex@example.com",
                            "planType": "pro"}}

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
                               clock=lambda: now, stall_seconds=30,
                               models_cache_path=os.path.join(
                                   self.tmp.name, "models-cache.json"))
        return adapter, client

    def test_every_thread_state_and_external_discovery(self):
        threads = [
            self.thread("running", {"type": "active", "activeFlags": []}),
            self.thread("waiting", {"type": "active",
                                     "activeFlags": ["waitingOnApproval"]}),
            self.thread("broken", {"type": "systemError"}),
            self.thread("done", {"type": "idle"}),
            {**self.thread("external", {"type": "notLoaded"}), "source": "vscode"},
        ]
        adapter, client = self.adapter(threads)
        for item in ("running", "waiting", "broken", "done"):
            adapter._remember(item, "default")
        client.thread_state["done"] = {"completed_at": 990, "revision": 1}
        adapter._refresh()
        states = {item["native_session_id"]: item["state"] for item in adapter.sessions()}
        self.assertEqual(states, {"running": "running", "waiting": "needs_you",
                                  "broken": "error", "done": "turn_done",
                                  "external": "idle"})
        external = next(item for item in adapter.sessions()
                        if item["native_session_id"] == "external")
        self.assertTrue(external["headless"])
        self.assertTrue(external["read_only"])
        self.assertFalse(external["capabilities"]["takeover"])
        self.assertFalse(external["capabilities"]["submit"])

    def test_archived_external_thread_is_not_retained_as_a_view_only_card(self):
        archived = {**self.thread("closed-external", {"type": "notLoaded"}),
                    "source": "vscode", "archived": True}
        adapter, _ = self.adapter([archived])
        adapter._refresh()
        self.assertEqual(adapter.sessions(), [])

    def test_catalog_window_fills_missing_live_usage_window(self):
        adapter, client = self.adapter([self.thread("managed")])
        with open(adapter._models_cache_path, "w") as handle:
            json.dump({"models": [{"slug": "gpt-5.4", "display_name": "GPT-5.4",
                                    "context_window": 272000}]}, handle)
        adapter._remember("managed", "default")
        client.thread_state["managed"] = {"token_usage": {
            "last": {"inputTokens": 8000, "totalTokens": 8000}}}
        adapter._refresh()
        session = adapter.sessions()[0]
        self.assertEqual((session["ctx_tokens"], session["ctx_window"], session["ctx_pct"]),
                         (8000, 272000, 2.9))

    def test_structured_limit_error_is_one_blocked_session_not_provider_failure(self):
        limited = self.thread("limited", {"type": "systemError"})
        limited["error"] = {"code": "rate_limit", "message": "Rate limit reached"}
        healthy = self.thread("healthy", {"type": "idle"})
        adapter, _ = self.adapter([limited, healthy])
        adapter._refresh()
        sessions = {item["native_session_id"]: item for item in adapter.sessions()}
        self.assertEqual(sessions["limited"]["state"], "blocked")
        self.assertEqual(sessions["limited"]["error"], "Rate limit reached")
        self.assertFalse(sessions["limited"]["capabilities"]["submit"])
        self.assertEqual(sessions["healthy"]["state"], "idle")

    def test_recent_external_rollout_is_live_but_remains_view_only(self):
        class Observer:
            def observe(self, thread_id):
                self.seen = thread_id
                return {"active": True, "turn_id": "desktop-turn",
                        "started_at": 995, "completed_at": None,
                        "last_activity_at": 998,
                        "messages": [{"role": "user", "text": "Build it"},
                                     {"role": "assistant", "text": "Working now",
                                      "phase": "commentary"}],
                        "revision": "rollout:2", "confidence": "observed_local_rollout",
                        "warning": None, "error": None}

        thread = {**self.thread("external", {"type": "notLoaded"}, updated=100),
                  "source": "vscode"}
        client = FixtureClient([thread])
        observer = Observer()
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: 1000, stall_seconds=30,
                               external_observer=observer,
                               models_cache_path=os.path.join(
                                   self.tmp.name, "models-cache.json"))
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual((session["state"], session["reg_status"], session["quiet_s"]),
                         ("running", "running", 2))
        self.assertEqual(session["last_msg"],
                         {"role": "assistant", "text": "Working now"})
        self.assertEqual(session["card_peek"], [
            {"type": "user", "text": "Build it"},
            {"type": "assistant", "text": "Working now"},
        ])
        self.assertTrue(session["observed_external"])
        self.assertEqual(session["observation_confidence"], "observed_local_rollout")
        self.assertTrue(session["read_only"])
        self.assertFalse(session["capabilities"]["submit"])
        self.assertFalse(session["capabilities"]["interrupt"])
        context = adapter.context("codex:external")
        self.assertTrue(context["ok"])
        self.assertTrue(context["read_only"])
        self.assertEqual(context["messages"][-1]["text"], "Working now")

    def test_managed_thread_uses_rollout_only_when_app_server_omits_settings(self):
        class Observer:
            def observe(self, thread_id):
                self.seen = thread_id
                return {"model": "gpt-5.6-sol", "effort": "xhigh",
                        "messages": [{"role": "assistant",
                                      "text": "rollout text must not replace App Server"}],
                        "agents": [{"native_session_id": "child-one",
                                    "model": "gpt-5.6-terra", "effort": "high"}],
                        "revision": "rollout:settings"}

        thread = {**self.thread("managed"), "model": "", "effort": None,
                  "turns": [{"id": "turn", "status": "completed",
                             "items": [{"type": "agentMessage",
                                        "text": "App Server message"},
                                       {"type": "collabAgentToolCall",
                                        "receiverThreadIds": ["child-one"],
                                        "agentsStates": {
                                            "child-one": {"status": "completed"}}}]}]}
        client = FixtureClient([thread])
        client.details["managed"] = dict(thread)
        observer = Observer()
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: 1000, stall_seconds=30,
                               external_observer=observer,
                               models_cache_path=os.path.join(
                                   self.tmp.name, "models-cache.json"))
        adapter._remember("managed", "default")
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual((session["model"], session["effort"]),
                         ("gpt-5.6-sol", "xhigh"))
        self.assertEqual((session["agents"][0]["model"],
                          session["agents"][0]["effort"]),
                         ("gpt-5.6-terra", "high"))
        self.assertEqual(session["last_msg"]["text"], "App Server message")
        self.assertEqual(session["card_peek"][0],
                         {"type": "assistant", "text": "App Server message"})
        self.assertEqual((session["card_peek"][-1]["type"],
                          session["card_peek"][-1]["label"]),
                         ("tool", "Agent"))
        self.assertFalse(session["observed_external"])

    def test_managed_saved_settings_beat_an_older_rollout_fallback(self):
        class Observer:
            def observe(self, thread_id):
                return {"model": "gpt-5.4", "effort": "medium",
                        "messages": [], "agents": [], "revision": "rollout:old"}

        thread = {**self.thread("managed"), "model": "", "effort": None}
        client = FixtureClient([thread])
        client.details["managed"] = dict(thread)
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: 1000, external_observer=Observer(),
                               models_cache_path=os.path.join(
                                   self.tmp.name, "models-cache.json"))
        adapter._remember("managed", "default", {
            "model": "gpt-5.6-sol", "effort": "high", "unmaterialized": False})
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual((session["model"], session["effort"]),
                         ("gpt-5.6-sol", "high"))

    def test_completed_external_rollout_remains_available_until_dormant(self):
        class Observer:
            def observe(self, thread_id):
                return {"active": False, "completed_at": 999,
                        "last_activity_at": 999, "messages": [],
                        "revision": "rollout:done", "confidence": "observed_local_rollout"}

        thread = {**self.thread("closed", {"type": "notLoaded"}, updated=999),
                  "source": "cli"}
        client = FixtureClient([thread])
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: 1000, external_observer=Observer(),
                               models_cache_path=os.path.join(self.tmp.name, "models-cache.json"))
        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["state"], "turn_done")

    def test_external_rollout_supplies_model_effort_and_context(self):
        class Observer:
            def observe(self, thread_id):
                return {"active": True, "started_at": 999, "last_activity_at": 999,
                        "model": "gpt-5.6-sol", "effort": "xhigh",
                        "token_usage": {"last": {"totalTokens": 20_240},
                                        "modelContextWindow": 272_000},
                        "messages": [], "revision": "rollout:live",
                        "confidence": "observed_local_rollout"}

        thread = {**self.thread("external-meta", {"type": "notLoaded"}, updated=999),
                  "source": "cli"}
        client = FixtureClient([thread])
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: 1000, external_observer=Observer(),
                               models_cache_path=os.path.join(self.tmp.name, "models-cache.json"))
        adapter._refresh()
        session = adapter.sessions()[0]
        self.assertEqual((session["model"], session["effort"]), ("gpt-5.6-sol", "xhigh"))
        self.assertEqual((session["ctx_tokens"], session["ctx_window"], session["ctx_pct"]),
                         (20_240, 272_000, 7.4))

    def test_recent_external_observation_is_bounded_and_old_pin_is_preserved(self):
        class Observer:
            def __init__(self):
                self.seen = []

            def observe(self, thread_id):
                self.seen.append(thread_id)
                return None

        threads = [
            {**self.thread(f"recent-{index}", {"type": "notLoaded"},
                           updated=999 - index), "source": "cli"}
            for index in range(40)
        ]
        threads.append({**self.thread("old-pinned", {"type": "notLoaded"},
                                     updated=1), "source": "cli"})
        client = FixtureClient(threads)
        observer = Observer()
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: 1000, stall_seconds=30,
                               external_observer=observer,
                               models_cache_path=os.path.join(
                                   self.tmp.name, "models-cache.json"))
        adapter.track_external(["codex:old-pinned"])
        adapter._refresh()

        self.assertEqual(len(observer.seen), 33)
        self.assertIn("old-pinned", observer.seen)
        self.assertIn("recent-0", observer.seen)
        self.assertIn("recent-31", observer.seen)
        self.assertNotIn("recent-32", observer.seen)

    def test_cli_connected_to_shared_socket_is_adopted_without_claiming_turn_control(self):
        thread = {**self.thread("attached", {"type": "active"}), "source": "cli"}
        thread["turns"] = [{"id": "same-turn", "status": "inProgress",
                            "startedAt": 999, "completedAt": None, "items": []}]
        adapter, client = self.adapter([thread])
        client.loaded = ["attached"]
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertFalse(session["headless"])
        self.assertFalse(session["read_only"])
        self.assertFalse(session["capabilities"]["submit"])
        self.assertTrue(session["capabilities"]["queue_submit"])
        self.assertFalse(session["capabilities"]["interrupt"])
        self.assertNotIn("turn_id", client.thread_state["attached"])
        self.assertEqual(session["control_state"], "reconnecting")
        self.assertEqual(adapter._managed(), ["attached"])

    def test_loaded_remote_cli_is_adopted_even_when_provider_labels_it_vscode(self):
        thread = {**self.thread("remote-cli", {"type": "idle"}), "source": "vscode"}
        adapter, client = self.adapter([thread])
        client.loaded = ["remote-cli"]

        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertFalse(session["headless"])
        self.assertFalse(session["read_only"])
        self.assertIsNone(session["read_only_reason"])
        self.assertEqual(session["codex_source"], adapter.runtime_owner)
        self.assertEqual(session["codex_provider_source"], "vscode")
        self.assertEqual(adapter._managed(), ["remote-cli"])
        self.assertIn("remote-cli", client.read_calls)

    def test_cross_app_server_turn_without_completion_is_running(self):
        thread = {**self.thread("managed", {"type": "notLoaded"}, updated=100),
                  "source": "vscode"}
        thread["turns"] = [{"id": "desktop-turn", "status": "interrupted",
                            "startedAt": 995, "completedAt": None,
                            "items": [{"id": "a1", "type": "agentMessage",
                                       "phase": "commentary", "text": "working"}]}]
        adapter, _ = self.adapter([thread], now=1000)
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
        self.assertEqual(session["codex_source"], "external")
        self.assertEqual(session["codex_provider_source"], "vscode")
        self.assertTrue(session["headless"])
        self.assertTrue(session["read_only"])
        denied = adapter.act({"type": "text", "session_id": "codex:managed",
                              "text": "do not start a concurrent turn"})
        self.assertFalse(denied["ok"])
        self.assertIn("view only", denied["error"])

    def test_desktop_completed_turn_stays_view_only(self):
        thread = {**self.thread("managed", {"type": "notLoaded"}, updated=100),
                  "source": "vscode"}
        thread["turns"] = [{"id": "desktop-turn", "status": "completed",
                            "startedAt": 950, "completedAt": 995,
                            "items": [{"id": "a1", "type": "agentMessage",
                                       "phase": "final_answer", "text": "done"}]}]
        adapter, _ = self.adapter([thread], now=1000)
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual(session["state"], "turn_done")
        self.assertEqual(session["reg_status"], "turn_done")
        self.assertEqual(session["quiet_s"], 5)
        self.assertFalse(session["capabilities"]["submit"])
        self.assertFalse(session["capabilities"]["close"])
        self.assertTrue(session["headless"])

    def test_image_action_builds_native_local_image_inputs(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter._remember("managed", "default")
        adapter._sessions = [{"native_session_id": "managed", "read_only": False,
            "capabilities": {"submit": True}, "collaboration_mode": "default",
            "model": "gpt-5.4", "effort": "high"}]
        client.thread_state["managed"] = {"status": "idle"}
        result = adapter.act({"type": "image_text", "session_id": "codex:managed",
                              "text": "Inspect", "image_paths": ["/private/fleet/photo.jpg"]})
        self.assertTrue(result["ok"])
        self.assertEqual(client.started_turns[0][2]["inputs"], [
            {"type": "text", "text": "Inspect"},
            {"type": "localImage", "path": "/private/fleet/photo.jpg"}])

    def test_fleet_owned_vscode_source_stays_managed(self):
        thread = {**self.thread("fleet-rich-client", {"type": "idle"}),
                  "source": "vscode"}
        adapter, _ = self.adapter([thread])
        adapter._remember("fleet-rich-client", "plan", {
            "runtime_owner": "fleet_shared", "unmaterialized": False})
        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertFalse(session["headless"])
        self.assertFalse(session["read_only"])
        self.assertTrue(session["capabilities"]["submit"])
        self.assertFalse(session["capabilities"]["focus_terminal"])
        self.assertIsNone(session["capabilities"]["focus_terminal_mode"])

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

    def test_rejected_stale_steer_restarts_exact_text_and_image_payload_once(self):
        for action in (
                {"type": "text", "session_id": "codex:managed", "text": "Do it"},
                {"type": "image_text", "session_id": "codex:managed", "text": "Inspect",
                 "image_paths": ["/private/fleet/photo.jpg"]}):
            with self.subTest(action=action["type"]):
                adapter, client = self.adapter([self.thread("managed")])
                adapter._remember("managed", "default")
                adapter._sessions = [{"native_session_id": "managed", "read_only": False,
                    "capabilities": {"submit": True}, "collaboration_mode": "default",
                    "model": "gpt-5.4", "effort": "high"}]
                client.thread_state["managed"] = {"status": "running",
                                                    "turn_id": "stale-turn"}
                client.steer_error = CodexError(
                    "Codex turn ended before the message was accepted", code="turn_ended")

                result = adapter.act(action)

                self.assertTrue(result["ok"], result)
                self.assertEqual(len(client.steered_turns), 1)
                self.assertEqual(len(client.started_turns), 1)
                self.assertEqual(client.started_turns[0][1], action["text"])
                if action["type"] == "image_text":
                    self.assertEqual(client.started_turns[0][2]["inputs"][-1],
                                     {"type": "localImage",
                                      "path": "/private/fleet/photo.jpg"})

    def test_provider_proved_idle_overrides_stale_active_thread_metadata(self):
        thread = self.thread("managed", {"type": "active"}, updated=998)
        adapter, client = self.adapter([thread], now=1000)
        adapter._remember("managed", "default")
        client.thread_state["managed"] = {
            "status": "idle", "turn_id": None, "completed_at": 999,
            "no_active_turn_at": 999, "updated_at": 998}

        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual(session["state"], "turn_done")
        self.assertEqual(session["control_state"], "connected_idle")
        self.assertFalse(session["capabilities"]["interrupt"])

    def test_stalled_and_dormant_are_time_based(self):
        threads = [self.thread("stalled", {"type": "active"}, updated=99_900),
                   self.thread("threshold", {"type": "notLoaded"}, updated=92_800),
                   self.thread("dormant", {"type": "notLoaded"}, updated=92_799)]
        adapter, _ = self.adapter(threads, now=100_000)
        adapter._remember("stalled", "default")
        adapter._remember("threshold", "default")
        adapter._remember("dormant", "default")
        adapter._refresh()
        states = {item["native_session_id"]: item["state"] for item in adapter.sessions()}
        self.assertEqual(states["stalled"], "stalled")
        self.assertEqual(states["threshold"], "idle")
        self.assertEqual(states["dormant"], "dormant")

    def test_old_not_loaded_attention_becomes_dormant_without_active_evidence(self):
        old = 92_799
        threads = [self.thread("limited", {"type": "notLoaded"}, updated=old),
                   self.thread("broken", {"type": "notLoaded"}, updated=old),
                   self.thread("pending", {"type": "notLoaded"}, updated=old),
                   self.thread("active", {"type": "notLoaded"}, updated=old),
                   self.thread("loaded", {"type": "notLoaded"}, updated=old)]
        adapter, client = self.adapter(threads, now=100_000)
        for item in ("limited", "broken", "pending", "active", "loaded"):
            adapter._remember(item, "default")
        client.thread_state.update({
            "limited": {"error": "Rate limit reached"},
            "broken": {"error": "provider protocol failed"},
            "pending": {"pending": "approval-1"},
            "active": {"status": "running", "turn_id": "turn-1"},
        })
        client.approvals["approval-1"] = {
            "thread_id": "pending", "method": "item/commandExecution/requestApproval",
            "params": {"command": "make test"}}
        client.loaded = ["loaded"]

        adapter._refresh()

        states = {item["native_session_id"]: item["state"] for item in adapter.sessions()}
        self.assertEqual(states["limited"], "dormant")
        self.assertEqual(states["broken"], "dormant")
        self.assertEqual(states["pending"], "dormant")
        self.assertEqual(states["active"], "stalled")
        self.assertEqual(states["loaded"], "idle")

    def test_stale_snapshot_quiet_age_keeps_advancing(self):
        now = [1000]
        thread = self.thread("managed", {"type": "notLoaded"}, updated=950)
        client = FixtureClient([thread])
        adapter = CodexAdapter(client=client, state_path=self.state_path,
                               clock=lambda: now[0], stall_seconds=30,
                               models_cache_path=os.path.join(
                                   self.tmp.name, "models-cache.json"))
        adapter._remember("managed", "default")
        client.thread_state["managed"] = {"error": "Rate limit reached"}
        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["state"], "blocked")

        now[0] = 9000
        client.fail_list = True
        adapter._refresh()
        stale = adapter.sessions()[0]
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(stale["stale_previous_state"], "blocked")
        self.assertEqual(stale["quiet_s"], 8050)

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
        self.assertTrue(session["capabilities"]["queue_submit"])
        self.assertFalse(session["capabilities"]["close"])

    def test_unknown_and_external_ids_reject_every_thread_action_without_provider_calls(self):
        adapter, client = self.adapter([self.thread("external")])
        adapter._refresh()
        provider_calls = []

        def record(name):
            return lambda *args, **kwargs: provider_calls.append((name, args, kwargs)) or {
                "ok": True}

        for name in ("resume_thread", "start_turn", "steer_turn", "set_mode", "interrupt",
                     "archive", "compact", "review", "decide", "answer_questions",
                     "answer_elicitation"):
            setattr(client, name, record(name))
        actions = [
            {"type": "text", "text": "mutate"},
            {"type": "image_text", "text": "mutate", "image_paths": ["/tmp/x.jpg"]},
            {"type": "session_settings", "model": "gpt-5.4", "effort": "high",
             "expected_model": "gpt-5.4", "expected_effort": "high"},
            {"type": "mode", "mode": "plan"}, {"type": "interrupt"},
            {"type": "archive"}, {"type": "close"}, {"type": "compact"},
            {"type": "review"}, {"type": "skill", "name": "reviewer"},
            {"type": "permission", "nonce": "9", "choice": "allow"},
            {"type": "multiq", "nonce": "9", "answers": []},
            {"type": "option", "nonce": "9", "digits": [1]},
            {"type": "elicitation", "nonce": "9", "choice": "decline"},
            {"type": "dismiss", "nonce": "9"},
            {"type": "relay", "agent_id": "child", "text": "mutate"},
        ]
        for target in ("codex:forged-unknown", "codex:external"):
            for action in actions:
                with self.subTest(target=target, action=action["type"]):
                    result = adapter.act({**action, "session_id": target})
                    self.assertFalse(result["ok"], result)
        self.assertEqual(provider_calls, [])

    def test_stale_projection_disables_mutations_and_rejects_direct_actions(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter._remember("managed", "default")
        adapter._refresh()
        client.fail_list = True
        adapter._refresh()
        session = adapter.sessions()[0]
        for capability in ("submit", "interrupt", "archive", "close", "compact", "review",
                           "answer_structured", "decide_approval", "relay_agent",
                           "change_model_effort"):
            self.assertFalse(session["capabilities"][capability], capability)
        self.assertTrue(session["capabilities"]["queue_submit"])
        before = (list(client.started_turns), list(client.steered_turns),
                  list(client.interrupts), client.mode_changed)
        queued = adapter.act({"type": "text", "session_id": "codex:managed",
                              "text": "queue me"})
        self.assertFalse(queued["ok"])
        self.assertTrue(queued["queueable"])
        for action in ({"type": "mode", "mode": "plan"},
                       {"type": "session_settings", "model": "gpt-5.4",
                        "effort": "high"}, {"type": "interrupt"},
                       {"type": "archive"}, {"type": "close"}, {"type": "compact"},
                       {"type": "review"}, {"type": "permission", "nonce": "9",
                                              "choice": "allow"},
                       {"type": "option", "nonce": "9", "digits": [1]},
                       {"type": "relay", "agent_id": "child", "text": "do it"}):
            with self.subTest(action=action["type"]):
                result = adapter.act({**action, "session_id": "codex:managed"})
                self.assertFalse(result["ok"], result)
        self.assertEqual(before, (client.started_turns, client.steered_turns,
                                  client.interrupts, client.mode_changed))

    def test_agent_context_requires_exact_parent_membership_before_provider_read(self):
        parent = self.thread("parent")
        parent["turns"] = [{"items": [{"type": "subAgentActivity", "kind": "started",
            "agentThreadId": "child-one", "agentPath": "/agents/one"}]}]
        sibling = self.thread("sibling")
        sibling["turns"] = [{"items": [{"type": "subAgentActivity", "kind": "started",
            "agentThreadId": "child-two", "agentPath": "/agents/two"}]}]
        adapter, client = self.adapter([parent, sibling])
        adapter._remember("parent", "default")
        adapter._remember("sibling", "default")
        client.details.update({
            "child-one": {"id": "child-one", "status": {"type": "idle"}, "turns": []},
            "child-two": {"id": "child-two", "status": {"type": "idle"}, "turns": []},
        })
        adapter._refresh()
        client.read_calls.clear()
        for invalid in ("child-two", "unknown-child"):
            result = adapter.agent_context("codex:parent", invalid)
            self.assertEqual(result, {"ok": False, "error": "no such subagent"})
        self.assertEqual(client.read_calls, [])
        self.assertTrue(adapter.agent_context("codex:parent", "child-one")["ok"])
        self.assertEqual(client.read_calls, ["child-one"])

    def test_malformed_thread_is_isolated_and_next_refresh_recovers(self):
        bad, healthy = self.thread("bad"), self.thread("healthy")
        adapter, client = self.adapter([bad, healthy])
        adapter._remember("bad", "default")
        adapter._remember("healthy", "default")
        adapter._refresh()
        client.threads = [{**bad, "turns": [None]}, healthy]
        adapter._refresh()
        sessions = {item["native_session_id"]: item for item in adapter.sessions()}
        self.assertTrue(sessions["bad"]["stale"])
        self.assertFalse(sessions["healthy"]["stale"])
        self.assertFalse(adapter._refreshing)
        self.assertTrue(adapter.diagnostics()["refresh_errors"])
        client.threads = [bad, healthy]
        adapter._refresh()
        sessions = {item["native_session_id"]: item for item in adapter.sessions()}
        self.assertFalse(sessions["bad"]["stale"])
        self.assertFalse(sessions["healthy"]["stale"])

    def test_malformed_owned_thread_has_stale_stub_on_first_refresh(self):
        bad, healthy = self.thread("bad"), self.thread("healthy")
        bad["turns"] = [None]
        adapter, _client = self.adapter([bad, healthy])
        adapter._remember("bad", "plan", {"cwd": "/work/bad", "model": "gpt-5.4",
                                             "effort": "high", "unmaterialized": False})
        adapter._refresh()
        sessions = {item["native_session_id"]: item for item in adapter.sessions()}
        self.assertIn("bad", sessions)
        self.assertTrue(sessions["bad"]["stale"])
        self.assertFalse(sessions["bad"]["capabilities"]["submit"])
        self.assertFalse(sessions["healthy"]["stale"])

    def test_missing_persisted_owned_thread_is_target_read_after_archive_page(self):
        adapter, client = self.adapter([self.thread(f"external-{index}") for index in range(100)])
        adapter._remember("owned-old", "plan", {
            "cwd": "/work/project", "model": "gpt-5.4", "effort": "high",
            "unmaterialized": False})
        client.details["owned-old"] = self.thread("owned-old", updated=1)
        adapter._refresh()
        sessions = {item["native_session_id"]: item for item in adapter.sessions()}
        self.assertIn("owned-old", sessions)
        self.assertFalse(sessions["owned-old"]["stale"])
        self.assertIn("owned-old", client.read_calls)

    def test_refresh_detail_reads_obey_one_total_budget(self):
        release = threading.Event()

        class SlowClient(FixtureClient):
            def read_thread(self, thread_id):
                self.read_calls.append(thread_id)
                if thread_id == "slow":
                    release.wait(1)
                return dict(self.details[thread_id])

        threads = [self.thread("slow"), self.thread("fast")]
        client = SlowClient(threads)
        adapter = CodexAdapter(
            client=client, state_path=self.state_path, clock=lambda: 1000,
            stall_seconds=30, refresh_budget_seconds=.05, refresh_workers=2,
            models_cache_path=os.path.join(self.tmp.name, "models-cache.json"))
        adapter._remember("slow", "default")
        adapter._remember("fast", "default")
        started = time.monotonic()
        adapter._refresh()
        elapsed = time.monotonic() - started
        try:
            # The provider read blocks for one second. The adapter returns well
            # before that even after durable snapshot fsync work on slower CI.
            self.assertLess(elapsed, .5)
            sessions = {item["native_session_id"]: item for item in adapter.sessions()}
            self.assertEqual(set(sessions), {"slow", "fast"})
            self.assertIn("bounded refresh budget", sessions["slow"]["refresh_warning"])
            self.assertIsNone(sessions["fast"]["refresh_warning"])
        finally:
            release.set()

    def test_detail_read_failure_keeps_owned_session_interactive_with_warning(self):
        adapter, client = self.adapter([self.thread()])
        adapter._remember("managed", "default")
        adapter._refresh()
        client.fail_read = True
        adapter._refresh()
        session = adapter.sessions()[0]
        self.assertEqual(session["state"], "idle")
        self.assertFalse(session["read_only"])
        self.assertTrue(session["capabilities"]["submit"])
        self.assertIn("thread read failed", session["refresh_warning"])
        adapter._refresh()
        self.assertEqual(client.read_calls, ["managed", "managed"])

    def test_loaded_list_failure_is_advisory(self):
        adapter, client = self.adapter([self.thread()])
        adapter._remember("managed", "default")
        client.fail_loaded = True
        adapter._refresh()
        session = adapter.sessions()[0]
        self.assertIsNone(adapter.error)
        self.assertFalse(session["stale"])
        self.assertTrue(session["capabilities"]["submit"])
        self.assertIn("loaded list unavailable", adapter.diagnostics()["loaded_error"])

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
        preview = _last_message([{"role": "assistant", "text": "x" * 900}])["text"]
        self.assertEqual(len(preview), 800)
        self.assertTrue(preview.endswith("…"))

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

    def test_child_lifecycle_has_its_own_budget_and_terminal_state_is_sticky(self):
        class SlowParentClient(FixtureClient):
            def read_thread(self, thread_id):
                self.read_calls.append(thread_id)
                if thread_id == "managed":
                    time.sleep(.04)
                return dict(self.details[thread_id])

        parent = self.thread()
        parent["turns"] = [{"items": [{"type": "subAgentActivity", "kind": "started",
            "agentThreadId": "child", "agentPath": "/agents/reviewer"}]}]
        client = SlowParentClient([parent])
        client.details["child"] = {"id": "child", "status": {"type": "idle"},
            "turns": [{"status": "completed", "items": [
                {"type": "agentMessage", "text": "done"}]}]}
        adapter = CodexAdapter(
            client=client, state_path=self.state_path, clock=lambda: 1000,
            stall_seconds=30, refresh_budget_seconds=.02, refresh_workers=2,
            models_cache_path=os.path.join(self.tmp.name, "models-cache.json"))
        adapter._remember("managed", "default")

        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["agents"][0]["state"], "done")
        child_reads = client.read_calls.count("child")

        # Parent projections contain only started/interacted activity. A later
        # partial refresh must neither re-read nor resurrect a terminal child.
        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["agents"][0]["state"], "done")
        self.assertEqual(client.read_calls.count("child"), child_reads)

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
        client.loaded = ["empty"]
        adapter._refresh()
        first = adapter.sessions()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["collaboration_mode"], "plan")
        self.assertFalse(first[0]["capabilities"]["focus_terminal"])
        self.assertNotIn("focus_terminal_label", first[0]["capabilities"])
        restarted = CodexAdapter(client=client, state_path=self.state_path,
                                 clock=lambda: 1000, stall_seconds=30,
                                 models_cache_path=os.path.join(
                                     self.tmp.name, "models-cache.json"))
        restarted._refresh()
        self.assertEqual(restarted.sessions()[0]["native_session_id"], "empty")

    def test_restart_uses_persisted_model_and_effort_when_thread_omits_them(self):
        thread = self.thread("managed")
        thread.pop("model")
        thread.pop("effort")
        adapter, client = self.adapter([thread])
        adapter._remember("managed", "plan", {
            "cwd": "/work/project", "model": "gpt-5.6-sol", "effort": "xhigh",
            "unmaterialized": False})

        restarted = CodexAdapter(
            client=client, state_path=self.state_path, clock=lambda: 1000,
            stall_seconds=30,
            models_cache_path=os.path.join(self.tmp.name, "models-cache.json"))
        restarted._refresh()

        session = restarted.sessions()[0]
        self.assertEqual(session["collaboration_mode"], "plan")
        self.assertEqual(session["model"], "gpt-5.6-sol")
        self.assertEqual(session["effort"], "xhigh")
        result = restarted.act({
            "type": "text", "session_id": "codex:managed", "text": "after compact"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.started_turns, [("managed", "after compact", {
            "mode": "plan", "model": "gpt-5.6-sol", "effort": "xhigh"})])

    def test_mode_change_keeps_model_and_effort_in_persisted_metadata(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()

        result = adapter.act({
            "type": "mode", "session_id": "codex:managed", "mode": "plan"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(client.mode_changed, ("managed", "plan", "gpt-5.4", "high"))
        meta = adapter._state()["thread_meta"]["managed"]
        self.assertEqual(meta["model"], "gpt-5.4")
        self.assertEqual(meta["effort"], "high")
        self.assertEqual(meta["settings_revision"], 1)

    def test_mode_change_preserves_explicit_live_null_effort(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()
        client.thread_state["managed"] = {
            "status": "idle", "model": "gpt-5.4", "effort": None}

        result = adapter.act({
            "type": "mode", "session_id": "codex:managed", "mode": "plan"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(client.mode_changed, ("managed", "plan", "gpt-5.4", None))
        self.assertIsNone(adapter.sessions()[0]["effort"])
        self.assertIsNone(adapter._state()["thread_meta"]["managed"]["effort"])
        adapter._refresh()
        self.assertIsNone(adapter.sessions()[0]["effort"])
        self.assertIsNone(adapter._state()["thread_meta"]["managed"]["effort"])

    def test_mode_change_wins_over_refresh_derived_before_provider_acceptance(self):
        thread = self.thread("managed")
        thread.pop("effort")
        adapter, client = self.adapter([thread])
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": None, "unmaterialized": False,
            "settings_revision": 0})
        adapter._refresh()
        derived = threading.Event()
        resume = threading.Event()
        original_cache = adapter._cache_snapshot

        def pause_after_derivation(*args, **kwargs):
            derived.set()
            self.assertTrue(resume.wait(2))
            return original_cache(*args, **kwargs)

        adapter._cache_snapshot = pause_after_derivation
        refresh = threading.Thread(target=adapter._refresh)
        refresh.start()
        self.assertTrue(derived.wait(2))
        result = adapter.act({
            "type": "mode", "session_id": "codex:managed", "mode": "plan"})
        self.assertEqual(result, {"ok": True, "mode": "plan", "durable": True})
        resume.set()
        refresh.join(2)
        self.assertFalse(refresh.is_alive())
        session = adapter.sessions()[0]
        self.assertEqual(session["collaboration_mode"], "plan")
        self.assertEqual(session["effort"], "medium")
        self.assertEqual(session["settings_revision"], 1)
        meta = adapter._state()["thread_meta"]["managed"]
        self.assertEqual((meta["effort"], meta["settings_revision"]), ("medium", 1))
        self.assertEqual(client.mode_changed, ("managed", "plan", "gpt-5.4", "medium"))

    def test_mode_provider_acceptance_survives_metadata_failure_with_warning(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()
        original_remember = adapter._remember

        def fail_mode_persistence(tid, mode=None, meta=None, **kwargs):
            if mode == "plan" and meta and "settings_revision" in meta:
                raise OSError("disk full")
            return original_remember(tid, mode, meta, **kwargs)

        adapter._remember = fail_mode_persistence
        result = adapter.act({
            "type": "mode", "session_id": "codex:managed", "mode": "plan"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["durable"])
        self.assertIn("Applied in Codex", result["warning"])
        self.assertEqual(client.mode_changed, ("managed", "plan", "gpt-5.4", "high"))
        self.assertEqual(adapter.sessions()[0]["collaboration_mode"], "plan")

    def test_existing_session_settings_persist_through_compaction_restart_and_next_turn(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter.models = [
            {"id": "gpt-5.4", "name": "GPT-5.4", "efforts": ["high"]},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "efforts": ["medium", "xhigh"]},
        ]
        adapter._remember("managed", "plan", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()

        changed = adapter.act({"type": "session_settings",
            "session_id": "codex:managed", "model": "gpt-5.6-sol", "effort": "xhigh",
            "expected_model": "gpt-5.4", "expected_effort": "high"})
        self.assertEqual(changed, {"ok": True, "model": "gpt-5.6-sol",
                                   "effort": "xhigh", "durable": True})
        self.assertEqual(client.mode_changed,
                         ("managed", "plan", "gpt-5.6-sol", "xhigh"))
        self.assertEqual(adapter.sessions()[0]["model"], "gpt-5.6-sol")
        self.assertEqual(adapter.sessions()[0]["effort"], "xhigh")
        self.assertTrue(adapter.act({"type": "compact",
                                    "session_id": "codex:managed"})["ok"])
        self.assertEqual(client.compactions, ["managed"])

        for row in client.threads:
            row.pop("model", None)
            row.pop("effort", None)
        for row in client.details.values():
            row.pop("model", None)
            row.pop("effort", None)
        client.thread_state.clear()
        restarted = CodexAdapter(
            client=client, state_path=self.state_path, clock=lambda: 1000,
            stall_seconds=30,
            models_cache_path=os.path.join(self.tmp.name, "models-cache.json"))
        restarted.models = list(adapter.models)
        restarted._refresh()
        session = restarted.sessions()[0]
        self.assertEqual((session["model"], session["effort"]),
                         ("gpt-5.6-sol", "xhigh"))
        self.assertTrue(restarted.act({"type": "text", "session_id": "codex:managed",
                                      "text": "continue after compact"})["ok"])
        self.assertEqual(client.started_turns[-1], ("managed", "continue after compact", {
            "mode": "plan", "model": "gpt-5.6-sol", "effort": "xhigh"}))

    def test_settings_save_preserves_newer_live_collaboration_mode(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter.models = [{"id": "gpt-5.4", "name": "GPT-5.4",
                           "efforts": ["medium", "high"]}]
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()
        client.thread_state["managed"] = {
            "status": "idle", "model": "gpt-5.4", "effort": "high",
            "collaboration_mode": "plan"}

        result = adapter.act({"type": "session_settings",
            "session_id": "codex:managed", "model": "gpt-5.4", "effort": "medium",
            "expected_model": "gpt-5.4", "expected_effort": "high"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(client.mode_changed,
                         ("managed", "plan", "gpt-5.4", "medium"))
        self.assertEqual(adapter._state()["modes"]["managed"], "plan")

    def test_existing_session_settings_reject_invalid_stale_active_and_provider_failure(self):
        active = self.thread("managed", {"type": "active", "activeFlags": []})
        adapter, client = self.adapter([active])
        adapter.models = [{"id": "gpt-5.4", "name": "GPT-5.4",
                           "efforts": ["medium", "high"]}]
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()
        self.assertFalse(adapter.sessions()[0]["capabilities"]["change_model_effort"])
        rejected = adapter.act({"type": "session_settings", "session_id": "codex:managed",
                                "model": "gpt-5.4", "effort": "medium"})
        self.assertFalse(rejected["ok"])
        self.assertIsNone(client.mode_changed)

        client.threads[0]["status"] = {"type": "idle"}
        client.details["managed"]["status"] = {"type": "idle"}
        adapter._refresh()
        invalid = adapter.act({"type": "session_settings", "session_id": "codex:managed",
                               "model": "gpt-5.4", "effort": "xhigh"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid.get("code"), "stale_settings")
        client.thread_state["managed"] = {"model": "gpt-5.4", "effort": "medium"}
        live_stale = adapter.act({"type": "session_settings",
            "session_id": "codex:managed", "model": "gpt-5.4", "effort": "medium",
            "expected_model": "gpt-5.4", "expected_effort": "high"})
        self.assertEqual(live_stale.get("code"), "stale_settings")
        client.thread_state["managed"] = {"model": "gpt-5.4", "effort": "high"}
        stale = adapter.act({"type": "session_settings", "session_id": "codex:managed",
            "model": "gpt-5.4", "effort": "medium",
            "expected_model": "gpt-5.4", "expected_effort": "low"})
        self.assertEqual(stale.get("code"), "stale_settings")
        client.mode_error = CodexError("provider rejected settings")
        failed = adapter.act({"type": "session_settings", "session_id": "codex:managed",
            "model": "gpt-5.4", "effort": "medium",
            "expected_model": "gpt-5.4", "expected_effort": "high"})
        self.assertFalse(failed["ok"])
        self.assertIn("provider rejected", failed["error"])
        self.assertEqual((adapter.sessions()[0]["model"], adapter.sessions()[0]["effort"]),
                         ("gpt-5.4", "high"))
        meta = adapter._state()["thread_meta"]["managed"]
        self.assertEqual((meta["model"], meta["effort"]), ("gpt-5.4", "high"))

    def test_settings_save_wins_over_refresh_built_from_old_projection(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter.models = [
            {"id": "gpt-5.4", "name": "GPT-5.4", "efforts": ["high"]},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "efforts": ["xhigh"]},
        ]
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False,
            "settings_revision": 0})
        adapter._refresh()
        derived = threading.Event()
        resume = threading.Event()
        original_cache = adapter._cache_snapshot

        def pause_after_derivation(*args, **kwargs):
            derived.set()
            self.assertTrue(resume.wait(2))
            return original_cache(*args, **kwargs)

        adapter._cache_snapshot = pause_after_derivation
        refresh = threading.Thread(target=adapter._refresh)
        refresh.start()
        self.assertTrue(derived.wait(2))
        changed = adapter.act({"type": "session_settings",
            "session_id": "codex:managed", "model": "gpt-5.6-sol", "effort": "xhigh",
            "expected_model": "gpt-5.4", "expected_effort": "high"})
        self.assertTrue(changed["ok"], changed)
        resume.set()
        refresh.join(2)
        self.assertFalse(refresh.is_alive())
        session = adapter.sessions()[0]
        self.assertEqual((session["model"], session["effort"]),
                         ("gpt-5.6-sol", "xhigh"))
        meta = adapter._state()["thread_meta"]["managed"]
        self.assertEqual((meta["model"], meta["effort"]),
                         ("gpt-5.6-sol", "xhigh"))
        self.assertFalse(adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "settings_revision": 1},
            expected_settings_revision=0))

    def test_mutation_lock_serializes_settings_before_next_turn_and_rechecks_blockers(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter.models = [
            {"id": "gpt-5.4", "name": "GPT-5.4", "efforts": ["high"]},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "efforts": ["xhigh"]},
        ]
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()
        entered = threading.Event()
        release = threading.Event()
        original_set_mode = client.set_mode

        def blocked_set_mode(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return original_set_mode(*args)

        client.set_mode = blocked_set_mode
        results = {}
        setting = threading.Thread(target=lambda: results.setdefault("settings", adapter.act({
            "type": "session_settings", "session_id": "codex:managed",
            "model": "gpt-5.6-sol", "effort": "xhigh",
            "expected_model": "gpt-5.4", "expected_effort": "high"})))
        turn = threading.Thread(target=lambda: results.setdefault("turn", adapter.act({
            "type": "text", "session_id": "codex:managed", "text": "use new settings"})))
        setting.start()
        self.assertTrue(entered.wait(2))
        turn.start()
        time.sleep(.03)
        self.assertEqual(client.started_turns, [])
        release.set()
        setting.join(2)
        turn.join(2)
        self.assertTrue(results["settings"]["ok"], results)
        self.assertTrue(results["turn"]["ok"], results)
        self.assertEqual(client.started_turns[-1][2], {
            "mode": "default", "model": "gpt-5.6-sol", "effort": "xhigh"})

        # Refresh said idle, but a request/compaction appeared before the click.
        client.approvals["pending"] = {"thread_id": "managed", "state": "pending"}
        blocked = adapter.act({"type": "session_settings", "session_id": "codex:managed",
            "model": "gpt-5.4", "effort": "high",
            "expected_model": "gpt-5.6-sol", "expected_effort": "xhigh"})
        self.assertIn("waiting on a request", blocked["error"])
        client.approvals.clear()
        client.thread_state["managed"].update(compacting=0, turn_id=None, status="idle")
        adapter._refresh()
        session = adapter.sessions()[0]
        self.assertFalse(session["capabilities"]["change_model_effort"])
        self.assertFalse(session["capabilities"]["compact"])
        started_before = len(client.started_turns)
        queued = adapter.act({"type": "text", "session_id": "codex:managed",
                              "text": "wait for compact"})
        self.assertFalse(queued["ok"])
        self.assertTrue(queued["queueable"])
        self.assertEqual(len(client.started_turns), started_before)

    def test_provider_acceptance_with_metadata_failure_returns_durability_warning(self):
        adapter, client = self.adapter([self.thread("managed")])
        adapter.models = [
            {"id": "gpt-5.4", "name": "GPT-5.4", "efforts": ["high"]},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "efforts": ["xhigh"]},
        ]
        adapter._remember("managed", "default", {
            "model": "gpt-5.4", "effort": "high", "unmaterialized": False})
        adapter._refresh()
        original_remember = adapter._remember

        def fail_settings_persistence(tid, mode=None, meta=None, **kwargs):
            if meta and meta.get("model") == "gpt-5.6-sol":
                raise OSError("disk full")
            return original_remember(tid, mode, meta, **kwargs)

        adapter._remember = fail_settings_persistence
        result = adapter.act({"type": "session_settings", "session_id": "codex:managed",
            "model": "gpt-5.6-sol", "effort": "xhigh",
            "expected_model": "gpt-5.4", "expected_effort": "high"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["durable"])
        self.assertIn("Applied in Codex", result["warning"])
        self.assertEqual(client.mode_changed,
                         ("managed", "default", "gpt-5.6-sol", "xhigh"))
        self.assertEqual((adapter.sessions()[0]["model"], adapter.sessions()[0]["effort"]),
                         ("gpt-5.6-sol", "xhigh"))
        adapter._refresh()
        self.assertEqual((adapter.sessions()[0]["model"], adapter.sessions()[0]["effort"]),
                         ("gpt-5.6-sol", "xhigh"))

    def test_unmaterialized_thread_missing_from_runtime_is_discarded(self):
        adapter, _ = self.adapter([])
        adapter._remember("ghost", "plan", {
            "cwd": "/work/project", "model": "gpt-5.4", "effort": "high",
            "created_at": 900, "unmaterialized": True})

        adapter._refresh()

        self.assertEqual(adapter.sessions(), [])
        self.assertNotIn("ghost", adapter._managed())

    def test_new_materialized_thread_does_not_disappear_before_thread_list_catches_up(self):
        adapter, client = self.adapter([])
        meta = {"cwd": "/work/project", "model": "gpt-5.4", "effort": "high",
                "name": "fresh", "created_at": 995, "unmaterialized": False}
        adapter._remember("fresh", "default", meta)
        previous = adapter._stub_session("fresh", meta, "default")
        previous.update(state="running", reg_status="running")
        adapter._sessions = [previous]
        client.loaded = ["fresh"]
        client.thread_state["fresh"] = {
            "status": "running", "turn_id": "turn-1", "updated_at": 1000}

        adapter._refresh()

        session = adapter.sessions()[0]
        self.assertEqual(session["native_session_id"], "fresh")
        self.assertEqual(session["state"], "running")
        self.assertTrue(session["capabilities"]["interrupt"])

        client.thread_state["fresh"].update(
            status="idle", turn_id=None, completed_at=1000)
        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["state"], "turn_done")

    def test_failed_detail_read_does_not_materialize_transient_list_row(self):
        adapter, client = self.adapter([self.thread("empty")])
        meta = {"cwd": "/work/project", "model": "gpt-5.4", "effort": "high",
                "name": "empty", "created_at": 900, "unmaterialized": True}
        adapter._remember("empty", "plan", meta)
        client.loaded = ["empty"]
        client.fail_read = True
        adapter._refresh()
        client.threads = []
        client.fail_read = False
        adapter._refresh()
        self.assertEqual(adapter.sessions()[0]["native_session_id"], "empty")


if __name__ == "__main__":
    unittest.main()
