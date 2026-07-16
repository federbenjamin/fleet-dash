import copy
import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import engine as engine_module
from engine import (DEFAULT_CONFIG, WAITING_CONFIRM_SECONDS, Engine, Tail,
                    classify_placement, redact_handoff_text, requests_reply)
from server import Handler


class FakeCodex:
    def __init__(self, session=None):
        self.error = None
        self.models = [{"id": "gpt-5.4", "name": "GPT-5.4", "efforts": ["high"]}]
        self.session = session
        self.actions = []
        self.fail_sessions = False
        self.muted_at_call = None
        self.started_thread = None

    def sessions(self):
        if self.fail_sessions:
            raise RuntimeError("Codex crashed")
        return [dict(self.session)] if self.session else []

    def account_usage(self):
        return {"provider": "codex", "buckets": []}

    def act(self, action):
        self.actions.append(action)
        return {"ok": True, "provider": "codex"}

    def context(self, sid):
        return {"ok": True, "messages": [{"role": "assistant", "text": "cached"}],
                "files": [], "closed": True}

    def agent_context(self, sid, aid):
        return {"ok": True, "messages": [], "info": {"agent_id": aid}}

    def file_content(self, sid, path):
        return "text/plain", b"codex", None

    def commands(self, sid, cwd):
        return {"ok": True, "commands": [{"name": "/compact"}]}

    @staticmethod
    def native(sid):
        return sid.split(":", 1)[1]

    @staticmethod
    def key(tid):
        return "codex:" + tid

    def start_thread(self, cwd, model=None, effort=None, mode="plan",
                     initial_text=None):
        self.started_thread = {"cwd": cwd, "model": model, "effort": effort,
                               "mode": mode, "initial_text": initial_text}
        return {"id": "new-thread"}


def codex_session():
    return {"session_id": "codex:same", "native_session_id": "same",
            "provider": "codex", "name": "Codex", "title": "Codex",
            "project": "repo", "cwd": "/work/repo", "branch": "feature",
            "model": "gpt-5.4", "family": "codex", "effort": "high",
            "collaboration_mode": "default", "running": None, "last_msg": None,
            "state": "idle", "reg_status": "idle", "quiet_s": 0,
            "ctx_tokens": 100, "ctx_pct": 1.0, "cost": None,
            "cost_source": "unavailable", "bridge_url": None, "started_ms": 1,
            "pending": None, "compacting": None, "muted": False, "convo_v": "1:1",
            "files_n": 0, "agents": [], "agents_running": 0, "agents_total": 0,
            "agent_cost": None, "capabilities": {"submit": True, "interrupt": False,
                "close": True, "exact_cost": False, "focus_terminal": False}}


class EngineProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "fleet")
        self.sessions = os.path.join(self.tmp.name, "sessions")
        self.projects = os.path.join(self.tmp.name, "projects")
        self.claude_account = os.path.join(self.tmp.name, ".claude.json")
        self.claude_usage = os.path.join(self.base, "usage.json")
        self.claude_stats = os.path.join(self.tmp.name, "stats-cache.json")
        self.claude_history = os.path.join(self.tmp.name, "history.jsonl")
        self.claude_usage_prefs = os.path.join(self.tmp.name, "claude-usage.plist")
        os.makedirs(self.base)
        os.makedirs(self.sessions)
        os.makedirs(self.projects)
        self.patchers = [
            mock.patch.object(engine_module, "HOME", self.tmp.name),
            mock.patch.object(engine_module, "BASE", self.base),
            mock.patch.object(engine_module, "SESSIONS", self.sessions),
            mock.patch.object(engine_module, "PROJECTS", self.projects),
            mock.patch.object(engine_module, "CLAUDE_ACCOUNT", self.claude_account),
            mock.patch.object(engine_module, "CLAUDE_USAGE", self.claude_usage),
            mock.patch.object(engine_module, "CLAUDE_STATS", self.claude_stats),
            mock.patch.object(engine_module, "CLAUDE_HISTORY", self.claude_history),
            mock.patch.object(engine_module, "CLAUDE_USAGE_PREFS",
                              self.claude_usage_prefs),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.cwd = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.cwd)
        project_dir = os.path.join(self.projects,
                                   self.cwd.replace("/", "-").replace(".", "-"))
        os.makedirs(project_dir)
        self.transcript = os.path.join(project_dir, "same.jsonl")
        rows = [
            {"type": "user", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "end_turn", "usage": {"input_tokens": 10,
                         "output_tokens": 5}, "content": [{"type": "text",
                                                             "text": "hi"}]}},
        ]
        with open(self.transcript, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        with open(os.path.join(self.sessions, "same.json"), "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "idle", "name": "Claude", "startedAt": 1}, handle)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({"codex_enabled": False, "act_token": "secret", "ntfy_topic": "",
                    "muted_sessions": {"codex:same": time.time()}})
        self.engine = Engine(cfg)
        self.codex = FakeCodex(codex_session())
        self.engine.codex = self.codex

    def tearDown(self):
        if self.engine.db:
            self.engine.db.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_shared_fleet_keeps_provider_ids_independent_and_unknown_cost_partial(self):
        fleet = self.engine.scan()
        self.assertEqual({item["session_id"] for item in fleet["sessions"]},
                         {"same", "codex:same"})
        codex = next(item for item in fleet["sessions"] if item["provider"] == "codex")
        claude = next(item for item in fleet["sessions"] if item["provider"] == "claude")
        self.assertTrue(codex["muted"])
        self.assertFalse(claude["muted"])
        self.assertTrue(claude["capabilities"]["close"])
        self.assertTrue(codex["capabilities"]["close"])
        self.assertTrue(fleet["totals"]["cost_partial"])
        self.assertGreaterEqual(fleet["totals"]["session_cost"], 0)
        self.assertGreaterEqual(fleet["diagnostics"]["scan_ms"], 0)
        self.assertEqual(fleet["diagnostics"]["scan_samples"], 1)
        second = self.engine.scan()
        self.assertEqual(second["diagnostics"]["scan_samples"], 2)
        self.assertGreaterEqual(second["diagnostics"]["scan_p95_ms"], 0)

    def test_new_claude_registry_session_is_interactive_before_first_transcript(self):
        os.unlink(self.transcript)
        fleet = self.engine.scan()
        session = next(item for item in fleet["sessions"]
                       if item["provider"] == "claude")
        self.assertEqual((session["state"], session["ui_group"]),
                         ("idle", "available"))
        self.assertTrue(session["capabilities"]["submit"])
        self.assertIsNone(session["ctx_tokens"])
        self.assertIsNone(session["cost"])
        self.assertEqual(self.engine.session_context("same"), {
            "ok": True, "messages": [], "files": [], "starting": True})

    def test_claude_usage_includes_email_and_all_local_transcript_token_types(self):
        with open(self.claude_account, "w") as handle:
            json.dump({"oauthAccount": {"emailAddress": "claude@example.com"}}, handle)
        with open(self.claude_usage, "w") as handle:
            json.dump({"five_hour_pct": 12, "seven_day_pct": 34}, handle)
        with open(self.claude_stats, "w") as handle:
            json.dump({"modelUsage": {
                "claude-sonnet": {"inputTokens": 10,
                    "cacheCreationInputTokens": 20, "cacheReadInputTokens": 30,
                    "outputTokens": 40},
                "claude-haiku": {"inputTokens": 1, "outputTokens": 2,
                    "cacheReadInputTokens": None}}}, handle)

        usage = self.engine.read_usage()
        self.assertEqual(usage["email"], "claude@example.com")
        self.assertEqual(usage["lifetime_tokens"], 103)
        self.assertEqual(usage["lifetime_scope"], "local_transcripts")
        self.assertEqual((usage["five_hour_pct"], usage["weekly_pct"]), (12, 34))

    def test_claude_usage_tracks_selected_profiles_and_live_file_changes(self):
        def write(active, first_pct):
            profiles = [
                {"id": "profile-one", "name": "first", "isSelectedForDisplay": True,
                 "refreshInterval": 30,
                 "apiSessionKey": "must-never-leave-the-plist",
                 "oauthAccountJSON": json.dumps({"emailAddress": "first@example.com"}),
                 "claudeUsage": {"sessionPercentage": first_pct,
                                  "weeklyPercentage": 22,
                                  "fableWeeklyPercentage": 7,
                                  "sessionResetTime": 800_000_000,
                                  "weeklyResetTime": 800_100_000,
                                  "fableWeeklyResetTime": 800_150_000}},
                {"id": "profile-two", "name": "second", "isSelectedForDisplay": True,
                 "refreshInterval": 30,
                 "oauthAccountJSON": json.dumps({"emailAddress": "second@example.com"}),
                 "claudeUsage": {"sessionPercentage": 33,
                                  "weeklyPercentage": 44,
                                  "fableWeeklyPercentage": 66,
                                  "sessionResetTime": 800_200_000,
                                  "weeklyResetTime": 800_300_000,
                                  "fableWeeklyResetTime": 800_350_000}},
            ]
            with open(self.claude_usage_prefs, "wb") as handle:
                plistlib.dump({"profiles_v3": json.dumps(profiles).encode(),
                    "multiProfileDisplayConfig": json.dumps({"showWeek": True,
                        "showActiveProfileIndicator": True}).encode(),
                    "activeProfileId": active, "profileDisplayMode": "multi"}, handle)

        write("profile-one", 11)
        usage = self.engine.read_usage()
        self.assertEqual(usage["source"], "claude_usage")
        self.assertEqual([item["email"] for item in usage["profiles"]],
                         ["first@example.com", "second@example.com"])
        self.assertEqual([item["active"] for item in usage["profiles"]], [True, False])
        self.assertEqual([item["fable_weekly_pct"] for item in usage["profiles"]],
                         [7, 66])
        self.assertIsNotNone(usage["profiles"][0]["fable_weekly_reset"])
        self.assertEqual(usage["five_hour_pct"], 11)
        self.assertEqual(usage["refresh_seconds"], 30)
        self.assertNotIn("must-never-leave-the-plist", json.dumps(usage))

        write("profile-two", 55)
        usage = self.engine.read_usage()
        self.assertEqual([item["active"] for item in usage["profiles"]], [False, True])
        self.assertEqual(usage["five_hour_pct"], 33)

    def test_working_order_is_entry_order_and_persists(self):
        def rows(*ids):
            return [{"session_id": sid, "ui_group": "working"} for sid in ids]

        first = self.engine.stable_working_order(rows("a", "b"))
        self.assertEqual(first, {"a": 0, "b": 1})
        self.engine.cfg["working_order"] = ["a", "a", "b"]
        self.assertEqual(self.engine.stable_working_order(rows("b", "a")),
                         {"a": 0, "b": 1})
        self.assertEqual(self.engine.stable_working_order(rows("c", "b", "a")),
                         {"a": 0, "b": 1, "c": 2})
        self.assertEqual(self.engine.stable_working_order(rows("c", "b")),
                         {"b": 0, "c": 1})
        self.assertEqual(self.engine.stable_working_order(rows("a", "c", "b")),
                         {"b": 0, "c": 1, "a": 2})
        with open(os.path.join(self.base, "config.json")) as handle:
            stored = json.load(handle)
        self.assertEqual(stored["working_order"], ["b", "c", "a"])
        self.assertTrue(self.engine.update_settings({"preview_agents": True})["ok"])
        with open(os.path.join(self.base, "config.json")) as handle:
            stored = json.load(handle)
        self.assertEqual(stored["working_order"], ["b", "c", "a"])
        self.assertTrue(stored["preview_agents"])
        restarted_cfg = dict(DEFAULT_CONFIG)
        restarted_cfg.update(stored)
        restarted_cfg["codex_enabled"] = False
        restarted = Engine(restarted_cfg)
        try:
            self.assertEqual(restarted.stable_working_order(rows("a", "c", "b")),
                             {"b": 0, "c": 1, "a": 2})
        finally:
            if restarted.db:
                restarted.db.close()

    def test_codex_failure_keeps_last_good_codex_snapshot_stale_without_removing_claude(self):
        healthy = self.engine.scan()
        self.assertEqual({item["provider"] for item in healthy["sessions"]},
                         {"claude", "codex"})
        self.codex.fail_sessions = True
        fleet = self.engine.scan()
        self.assertEqual({item["provider"] for item in fleet["sessions"]},
                         {"claude", "codex"})
        codex = next(item for item in fleet["sessions"] if item["provider"] == "codex")
        self.assertEqual((codex["state"], codex["provider_stale"],
                          codex["state_confidence"], codex["access"]),
                         ("stale", True, "stale", "view_only"))
        self.assertFalse(fleet["providers"]["codex"]["ok"])
        self.assertIn("Codex crashed", fleet["providers"]["codex"]["error"])

        self.codex.fail_sessions = False
        recovered = self.engine.scan()
        codex = next(item for item in recovered["sessions"] if item["provider"] == "codex")
        self.assertEqual((codex["state"], codex["provider_stale"],
                          recovered["providers"]["codex"]["ok"]),
                         ("idle", False, True))

    def test_corrupt_shared_ledger_is_quarantined_and_fleet_still_starts(self):
        recovery_base = os.path.join(self.tmp.name, "recovery")
        os.makedirs(recovery_base)
        ledger_path = os.path.join(recovery_base, "ledger.db")
        with open(ledger_path, "wb") as handle:
            handle.write(b"not a sqlite database")
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({"codex_enabled": False, "ntfy_topic": ""})
        with mock.patch.object(engine_module, "BASE", recovery_base):
            recovered = Engine(cfg)
            try:
                fleet = recovered.scan()
                self.assertFalse(fleet["ledger"]["ok"])
                self.assertTrue(fleet["ledger"]["recovered"])
                self.assertNotIn("detail", fleet["ledger"])
                self.assertTrue(os.path.exists(ledger_path))
                quarantine = os.path.join(recovery_base, fleet["ledger"]["quarantine"])
                self.assertTrue(os.path.exists(quarantine))
                with sqlite3.connect(ledger_path) as db:
                    self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                if recovered.db:
                    recovered.db.close()

    def test_http_ledger_reads_do_not_share_the_scan_connection(self):
        original = self.engine.ensure_db()

        class PoisonedScanConnection:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.DatabaseError("file is not a database")

            def close(self):
                pass

        self.engine.db = PoisonedScanConnection()
        try:
            insights = self.engine.insights(7)
            self.assertTrue(insights["ok"], insights)
            self.assertEqual(self.engine.recent_dirs()[0]["path"], self.cwd)
        finally:
            self.engine.db = original

    def test_claude_history_backfill_is_viewable_reopenable_and_idempotent(self):
        sid = "11111111-2222-3333-4444-555555555555"
        path = os.path.join(os.path.dirname(self.transcript), sid + ".jsonl")
        rows = [
            {"type": "user", "sessionId": sid, "cwd": self.cwd,
             "gitBranch": "archive", "timestamp": "2026-07-14T00:00:00Z",
             "message": {"role": "user", "content": "Review the old parser"}},
            {"type": "ai-title", "sessionId": sid,
             "aiTitle": "Historical parser review"},
            {"type": "assistant", "sessionId": sid, "cwd": self.cwd,
             "gitBranch": "archive", "timestamp": "2026-07-14T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "end_turn", "usage": {"input_tokens": 2,
                         "output_tokens": 1}, "content": [{"type": "text",
                                                             "text": "Review complete"}]}},
        ]
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        subdir = os.path.join(os.path.dirname(self.transcript), sid, "subagents")
        os.makedirs(subdir)
        with open(os.path.join(subdir, "agent-child.jsonl"), "w") as handle:
            handle.write(json.dumps(rows[-1]) + "\n")

        fleet = self.engine.scan()

        archived = next(item for item in fleet["closed"] if item["session_id"] == sid)
        self.assertEqual(archived["title"], "Historical parser review")
        self.assertTrue(archived["can_reopen"])
        self.assertEqual((archived["primary_action"], archived["access"]),
                         ("reopen", "reopen"))
        stored = self.engine.ensure_db().execute(
            "SELECT cost, agent_cost, agents_total FROM session_runs WHERE session_id=?",
            (sid,)).fetchone()
        self.assertEqual(stored, (None, None, None))
        context = self.engine.closed_context(sid)
        self.assertTrue(context["ok"])
        self.assertEqual(context["messages"][-1]["text"], "Review complete")
        self.assertEqual(self.engine.backfill_claude_history(), 0)
        ids = {row[0] for row in self.engine.ensure_db().execute(
            "SELECT session_id FROM session_runs")}
        self.assertNotIn("agent-child", ids)

        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps)) or {"ok": True})
        reopened = self.engine.act({"type": "reopen", "session_id": sid})
        self.assertTrue(reopened["ok"])
        self.assertTrue(reopened["reopened"])
        self.assertEqual(writes[0][0], "SPAWN")
        self.assertIn(f"claude --resume {sid}", writes[0][1][0][0])

    def test_claude_reopen_rejects_unindexed_and_unsafe_transcripts(self):
        unknown = self.engine.act({"type": "reopen",
                                   "session_id": "11111111-2222-3333-4444-555555555555"})
        self.assertFalse(unknown["ok"])
        self.assertIn("closed Claude", unknown["error"])
        self.assertIsNone(self.engine._safe_claude_transcript(
            "11111111-2222-3333-4444-555555555555", self.transcript))

    def test_actions_context_commands_and_files_route_by_provider(self):
        action = {"type": "text", "session_id": "codex:same", "text": "go"}
        self.assertEqual(self.engine.act(action)["provider"], "codex")
        self.assertEqual(self.codex.actions, [action])
        self.assertTrue(self.engine.closed_context("codex:same")["closed"])
        self.engine.snapshot_cache = {"sessions": [codex_session()]}
        self.assertEqual(self.engine.commands("codex:same")["commands"][0]["name"],
                         "/compact")
        self.assertEqual(self.engine.file_content("codex:same", "/work/repo/a.txt"),
                         ("text/plain", b"codex", None))

    def test_codex_terminal_attaches_to_shared_runtime(self):
        session = codex_session()
        session["cwd"] = self.cwd
        session["capabilities"].update(focus_terminal=True,
                                       focus_terminal_mode="attach")
        self.codex.session = session
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps)) or {"ok": True})
        with mock.patch("codex_adapter.codex_command", return_value="/opt/codex"), \
             mock.patch("codex_adapter.codex_control_socket",
                        return_value="/Users/test/.codex/app-server-control.sock"):
            result = self.engine.act({"type": "focus", "session_id": "codex:same"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["shared_runtime"])
        self.assertEqual(writes[0][0], "SPAWN")
        command = writes[0][1][0][0]
        self.assertIn("codex resume --remote", command)
        self.assertIn("unix:///Users/test/.codex/app-server-control.sock", command)
        self.assertTrue(command.endswith(" same"))
        self.assertEqual(self.codex.actions, [])

    def test_codex_spawn_starts_visible_initial_hi(self):
        with mock.patch.object(engine_module, "HOME", self.tmp.name):
            result = self.engine.spawn_codex_session({
                "provider": "codex", "cwd": self.cwd, "model": "gpt-5.4",
                "effort": "high", "mode": "plan"})
        cwd = os.path.realpath(self.cwd)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "codex:new-thread")
        self.assertEqual(result["initial_message"], "hi")
        self.assertEqual(self.codex.started_thread, {
            "cwd": cwd, "model": "gpt-5.4", "effort": "high",
            "mode": "plan", "initial_text": "hi"})

    def test_codex_spawn_uses_explicit_initial_message_when_supplied(self):
        with mock.patch.object(engine_module, "HOME", self.tmp.name):
            result = self.engine.spawn_codex_session({
                "provider": "codex", "cwd": self.cwd, "model": "gpt-5.4",
                "effort": "high", "mode": "default", "initial_text": "Start exact work"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["initial_message"], "Start exact work")
        self.assertEqual(self.codex.started_thread["initial_text"], "Start exact work")

    def test_outbox_action_dispatches_to_exact_available_provider_session(self):
        self.engine.scan()
        created = self.engine.act({"type": "outbox_create", "kind": "when_available",
            "message": "Queued exact work", "created_zone": "UTC",
            "target_provider": "codex", "target_session_id": "codex:same"})
        self.assertTrue(created["ok"])
        self.engine.run_outbox()
        row = self.engine.outbox.get(created["item"]["id"])
        self.assertEqual(row["state"], "sent")
        self.assertEqual(self.codex.actions[-1], {"type": "text",
            "session_id": "codex:same", "text": "Queued exact work"})
        self.assertNotIn("Queued exact work", json.dumps(row["provider_receipt"]))

    def test_outbox_rejects_view_only_target_before_persisting(self):
        self.engine.scan()
        with self.engine.lock:
            target = next(item for item in self.engine.snapshot_cache["sessions"]
                          if item["session_id"] == "codex:same")
            target.update(read_only=True, access="view_only",
                          read_only_reason="another runtime owns it")
        result = self.engine.act({"type": "outbox_create", "kind": "when_available",
            "message": "Do not redirect", "created_zone": "UTC",
            "target_provider": "codex", "target_session_id": "codex:same"})
        self.assertFalse(result["ok"])
        self.assertIn("another runtime", result["error"])
        self.assertEqual(self.engine.outbox.counts()["pending"], 0)

    def test_scheduled_new_codex_session_sends_message_in_exact_first_turn(self):
        self.engine.scan()
        created = self.engine.act({"type": "outbox_create", "kind": "new_session",
            "message": "Scheduled first turn", "created_zone": "UTC",
            "trigger_at": time.time() - 1, "spawn_spec": {"provider": "codex",
                "cwd": self.cwd, "model": "gpt-5.4", "effort": "high",
                "mode": "plan", "worktree": False, "worktree_name": ""}})
        self.assertTrue(created["ok"])
        with mock.patch.object(engine_module, "HOME", self.tmp.name):
            self.engine.run_outbox()
        row = self.engine.outbox.get(created["item"]["id"])
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["destination_session_id"], "codex:new-thread")
        self.assertEqual(self.codex.started_thread["initial_text"], "Scheduled first turn")

    def test_arbitrary_claude_file_path_is_rejected(self):
        ctype, data, error = self.engine.file_content("same", self.transcript)
        self.assertIsNone(ctype)
        self.assertIsNone(data)
        self.assertIn("not a file this session delivered", error)

    def test_notifications_skip_unknown_codex_cost_and_muted_sessions(self):
        sent = []
        self.engine.once = lambda *args: sent.append(args)
        session = codex_session()
        session.update(state="needs_you", quiet_s=1000, muted=True,
                       pending={"kind": "permission", "tool": "command"})
        fleet = {"sessions": [session], "totals": {"busy": 0, "agents_running": 0,
                                                     "sessions": 1}}
        self.engine.check_notifications(fleet)
        self.assertEqual(sent, [])

    def test_scheduled_digest_dispatches_after_restart_due_time_and_dedupes(self):
        self.engine.cfg["notify"] = {**self.engine.cfg["notify"],
                                      "scheduled_digest": True,
                                      "fleet_quiet": False}
        self.engine.cfg["digest_schedule_time"] = "00:00"
        self.engine.cfg["digest_schedule_zone"] = "UTC"
        sent = []
        self.engine.ntfy = lambda *args, **kwargs: sent.append(args)
        snapshot = {"sessions": [], "totals": {"busy": 0, "agents_running": 0,
                                                  "sessions": 0}}
        self.engine.check_notifications(snapshot)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].startswith("digest:UTC:"))

        restarted = Engine(dict(self.engine.cfg))
        restarted.codex = self.codex
        duplicate = []
        restarted.ntfy = lambda *args, **kwargs: duplicate.append(args)
        restarted.check_notifications(snapshot)
        self.assertEqual(duplicate, [])
        if restarted.db:
            restarted.db.close()

    def test_token_cookie_requires_exact_cookie_name_and_value(self):
        def check(cookie="", header=""):
            obj = SimpleNamespace(eng=SimpleNamespace(cfg={"act_token": "secret"}),
                                  headers={"Cookie": cookie, "X-Act-Token": header})
            return Handler.token_ok(obj)

        self.assertTrue(check(cookie="act_token=secret"))
        self.assertTrue(check(header="secret"))
        self.assertFalse(check(cookie="xact_token=secret"))
        self.assertFalse(check(cookie="act_token=secret-suffix"))

    def test_claude_actions_share_validated_engine_surface(self):
        reg = {"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[os.getpid()] = "ttys-test"
        tail = SimpleNamespace(pending={}, poll=lambda: None)
        self.engine.tail_for = lambda path: tail
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        self.assertTrue(self.engine.act({"type": "text", "session_id": "same",
                                         "text": "hello"})["ok"])
        self.assertEqual(writes[-1][1], [("hello", True)])
        self.assertTrue(self.engine.act({"type": "focus",
                                         "session_id": "same"})["ok"])
        self.assertEqual(writes[-1][1], [("__FOCUS__", False)])

        reg["status"] = "busy"
        self.assertTrue(self.engine.act({"type": "interrupt",
                                         "session_id": "same"})["ok"])
        self.assertEqual(writes[-1][1], [("\x1b", False)])
        reg["status"] = "waiting"
        self.engine.hook_pending = lambda sid, status: {
            "kind": "question", "nonce": "q1", "questions": []}
        answered = self.engine.act({"type": "option", "session_id": "same",
                                    "nonce": "q1", "digits": [1], "n_options": 2})
        self.assertTrue(answered["ok"])
        self.assertEqual(writes[-1][1], [("1", False), ("", True)])

        self.engine.hook_pending = lambda sid, status: {
            "kind": "permission", "nonce": "p1"}
        allowed = self.engine.act({"type": "permission", "session_id": "same",
                                   "nonce": "p1", "choice": "allow"})
        self.assertTrue(allowed["ok"])
        self.assertEqual(writes[-1][1], [("1", False), ("", True)])
        reg["status"] = "idle"
        self.engine._agent_paths = lambda sid, aid: (self.transcript,
                                                     os.path.join(self.tmp.name, "missing-meta"))
        relayed = self.engine.act({"type": "relay", "session_id": "same",
            "agent_id": "agent-child", "text": "report status"})
        self.assertTrue(relayed["ok"])
        self.assertIn("agent-child", writes[-1][1][0][0])

    def test_claude_permission_mode_uses_only_verified_native_cycle(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine._claude_command_cache[pid] = "/usr/local/bin/claude --model sonnet"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet-5")
        self.engine.tail_for = lambda path: tail
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        changed = self.engine.act({"type": "permission_mode", "session_id": "same",
                                   "mode": "plan"})
        self.assertEqual(changed, {"ok": True, "mode": "plan"})
        self.assertEqual(writes[-1], ("/dev/ttys-test",
            [("\x1b[Z", False), ("\x1b[Z", False)], 0.4))
        self.assertEqual(tail.permission_mode, "plan")

        self.engine._claude_command_cache[pid] = (
            "/usr/local/bin/claude --allow-dangerously-skip-permissions")
        tail.permission_mode = "plan"
        bypass = self.engine.act({"type": "permission_mode", "session_id": "same",
                                  "mode": "bypassPermissions"})
        self.assertTrue(bypass["ok"])
        self.assertEqual(writes[-1][1], [("\x1b[Z", False)])
        tail.permission_mode = "plan"
        auto = self.engine.act({"type": "permission_mode", "session_id": "same",
                                "mode": "auto"})
        self.assertTrue(auto["ok"])
        self.assertEqual(writes[-1][1], [("\x1b[Z", False), ("\x1b[Z", False)])

        tail.permission_mode = "dontAsk"
        self.assertIn("startup-only", self.engine.act({"type": "permission_mode",
            "session_id": "same", "mode": "default"})["error"])
        reg["status"] = "busy"
        self.assertIn("idle", self.engine.act({"type": "permission_mode",
            "session_id": "same", "mode": "plan"})["error"])

    def test_claude_close_interrupts_then_terminates_only_registered_process(self):
        pid = 424242
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "busy", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        process = SimpleNamespace(stdout="/usr/local/bin/claude --model sonnet")
        with mock.patch.object(engine_module.subprocess, "run", return_value=process), \
             mock.patch.object(engine_module.os, "kill") as kill, \
             mock.patch.object(engine_module.time, "sleep"):
            result = self.engine.act({"type": "close", "session_id": "same"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["interrupted"])
        self.assertEqual(writes, [("/dev/ttys-test", [("\x1b", False)], 0.05)])
        kill.assert_called_once_with(pid, engine_module.signal.SIGTERM)

    def test_claude_close_refuses_reused_non_claude_pid(self):
        pid = 424243
        self.engine.live_sessions = lambda: [{"sessionId": "same", "pid": pid,
            "cwd": self.cwd, "status": "idle", "name": "Claude"}]
        process = SimpleNamespace(stdout="/usr/bin/python /work/.claude/fleet-dash/server.py")
        with mock.patch.object(engine_module.subprocess, "run", return_value=process), \
             mock.patch.object(engine_module.os, "kill") as kill:
            result = self.engine.act({"type": "close", "session_id": "same"})
        self.assertFalse(result["ok"])
        self.assertIn("non-Claude", result["error"])
        kill.assert_not_called()

    def test_claude_worktree_spawn_is_allowlisted_and_mute_persists(self):
        os.makedirs(os.path.join(self.cwd, ".git"))
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps)) or {"ok": True})
        self.engine.is_trusted = lambda cwd, trusted=None: True
        with mock.patch.object(engine_module, "HOME", self.tmp.name):
            spawned = self.engine.spawn_session({"cwd": self.cwd, "model": "sonnet",
                "effort": "high", "permission_mode": "acceptEdits",
                "worktree": True, "worktree_name": "live-e2e"})
        self.assertTrue(spawned["ok"])
        command = writes[-1][1][0][0]
        self.assertRegex(command, r"claude --session-id [0-9a-f-]{36} --model sonnet ")
        self.assertIn("--effort high --permission-mode acceptEdits --worktree live-e2e", command)
        self.assertEqual(spawned["session_id"], command.split("--session-id ", 1)[1].split()[0])
        self.assertFalse(spawned["trust_prompt"])
        rejected = self.engine.spawn_session({"cwd": self.cwd,
            "permission_mode": "bypassPermissions"})
        self.assertFalse(rejected["ok"])
        self.assertIn("permission mode", rejected["error"])

        changed = self.engine.update_settings({"mute_session": "same", "muted": True})
        self.assertTrue(changed["ok"])
        self.assertIn("same", self.engine.cfg["muted_sessions"])
        fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"] if item["provider"] == "claude")
        self.assertTrue(claude["muted"])

    def test_reader_width_is_persisted_validated_and_exposed(self):
        self.assertEqual(DEFAULT_CONFIG["reader_width"], "fit")
        changed = self.engine.update_settings({"reader_width": "centered"})
        self.assertEqual(changed, {"ok": True, "reader_width": "centered"})
        self.assertEqual(self.engine.scan()["settings"]["reader_width"], "centered")
        with open(os.path.join(self.base, "config.json")) as handle:
            self.assertEqual(json.load(handle)["reader_width"], "centered")
        invalid = self.engine.update_settings({"reader_width": "left"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(self.engine.cfg["reader_width"], "centered")

    def test_settings_validation_is_atomic_strict_and_concurrency_safe(self):
        before_notify = copy.deepcopy(self.engine.cfg["notify"])
        rejected = self.engine.update_settings({
            "notify": {"needs_you": False}, "reader_width": "left"})
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.engine.cfg["notify"], before_notify)
        config_path = os.path.join(self.base, "config.json")
        if os.path.exists(config_path):
            with open(config_path) as handle:
                self.assertNotEqual((json.load(handle).get("notify") or {}).get("needs_you"), False)

        for patch in ({"preview_agents": "false"},
                      {"notify": {"needs_you": "false"}},
                      {"mute_session": "same", "muted": 1},
                      {"pin_session": "same", "pinned": "yes"},
                      {"unknown_setting": True},
                      {"fleet_quiet_minutes": True}):
            with self.subTest(patch=patch):
                self.assertFalse(self.engine.update_settings(patch)["ok"])

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(
                self.engine.update_settings({"preview_agents": True}))),
            threading.Thread(target=lambda: results.append(
                self.engine.update_settings({"reader_width": "centered"}))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(item["ok"] for item in results))
        with open(config_path) as handle:
            saved = json.load(handle)
        self.assertTrue(saved["preview_agents"])
        self.assertEqual(saved["reader_width"], "centered")

    def test_handoff_preview_redacts_credentials_and_exposes_safe_defaults(self):
        fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"] if item["provider"] == "claude")
        claude["last_msg"] = {"role": "assistant", "text":
            "Next step: verify Authorization: Bearer abcdefghijklmnop"}
        preview = self.engine.handoff_preview("same", "codex")
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["independent_session"])
        self.assertEqual(preview["defaults"]["cwd"], self.cwd)
        self.assertEqual(preview["defaults"]["mode"], "plan")
        self.assertIn("Source session: claude · same", preview["preview"])
        self.assertIn("Treat this as an independent session", preview["preview"])
        self.assertNotIn("abcdefghijklmnop", redact_handoff_text(
            "Authorization: Bearer abcdefghijklmnop"))
        self.assertIn("[REDACTED", redact_handoff_text(
            "api_key=abcdefghijklmnop"))
        self.assertNotIn("private-material", redact_handoff_text(
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"))

    def test_codex_handoff_starts_exact_thread_with_hi_and_durable_link(self):
        self.engine.scan()
        result = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "cwd": self.cwd, "preview": "Continue exact work",
            "model": "gpt-5.4", "effort": "high", "mode": "default"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["destination_session_id"], "codex:new-thread")
        self.assertEqual(self.codex.started_thread["initial_text"],
                         "hi\n\nContinue exact work")
        link = self.engine._handoff_link("same", "codex:new-thread")
        self.assertEqual(link["status"], "delivered")
        self.assertNotIn("Continue exact work", json.dumps(link))
        fleet = self.engine.scan()
        source = next(item for item in fleet["sessions"] if item["session_id"] == "same")
        self.assertEqual(source["handoff_links"][0]["session_id"], "codex:new-thread")

    def test_claude_handoff_uses_reserved_uuid_and_delivers_only_to_exact_session(self):
        self.engine.scan()
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        self.engine.is_trusted = lambda cwd, trusted=None: True
        original_live = self.engine.live_sessions
        spawned_ids = []

        def live():
            if not spawned_ids:
                return original_live()
            return [{"sessionId": spawned_ids[-1], "pid": 9090, "cwd": self.cwd,
                     "status": "idle", "name": "Handoff"},
                    {"sessionId": "similar-but-wrong", "pid": 9191, "cwd": self.cwd,
                     "status": "idle", "name": "Wrong"}]

        def write(tty, steps, step_delay=None):
            writes.append((tty, steps, step_delay))
            if tty == "SPAWN":
                command = steps[0][0]
                spawned_ids.append(command.split("--session-id ", 1)[1].split()[0])
            return {"ok": True}

        self.engine._iterm_write = write
        self.engine.live_sessions = live
        self.engine._tty_cache[9090] = "ttys-exact"
        result = self.engine.execute_handoff({"type": "handoff", "session_id": "codex:same",
            "provider": "claude", "cwd": self.cwd, "preview": "Exact destination"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["destination_session_id"], spawned_ids[0])
        self.assertEqual(writes[-1][0], "/dev/ttys-exact")
        self.assertEqual(writes[-1][1], [("Exact destination", True)])
        self.assertNotEqual(result["destination_session_id"], "similar-but-wrong")

    def test_same_provider_handoff_remains_an_independent_codex_thread(self):
        self.engine.scan()
        result = self.engine.execute_handoff({"type": "handoff",
            "session_id": "codex:same", "provider": "codex", "cwd": self.cwd,
            "preview": "Continue independently", "mode": "plan"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["destination_session_id"], "codex:new-thread")
        self.assertNotEqual(result["destination_session_id"], "codex:same")
        self.assertEqual(self.codex.started_thread["initial_text"],
                         "hi\n\nContinue independently")

    def test_claude_handoff_timeout_is_linked_and_retryable(self):
        self.engine.scan()
        self.engine.is_trusted = lambda cwd, trusted=None: True
        self.engine._iterm_write = lambda tty, steps, step_delay=None: {"ok": True}
        self.engine.live_sessions = lambda: []
        with mock.patch.object(engine_module.time, "monotonic", side_effect=[0, 31]):
            result = self.engine.execute_handoff({"type": "handoff",
                "session_id": "same", "provider": "claude", "cwd": self.cwd,
                "preview": "Wait for exact identity"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["created"])
        self.assertTrue(result["retryable"])
        link = self.engine._handoff_link("same", result["destination_session_id"])
        self.assertEqual(link["status"], "delivery_failed")

    def test_failed_codex_handoff_cleans_up_only_the_created_worktree(self):
        self.engine.scan()
        created = {"ok": True, "created": True, "cwd": os.path.join(self.tmp.name, "wt"),
                   "root": self.cwd, "branch": "fleet/fail", "worktree_name": "fail"}
        self.engine._create_codex_worktree = lambda cwd, name="": created
        cleaned = []
        self.engine._remove_failed_codex_worktree = lambda value: cleaned.append(value) or None
        self.codex.start_thread = mock.Mock(side_effect=RuntimeError("app-server stopped"))
        result = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "cwd": self.cwd, "preview": "Continue",
            "worktree": True, "worktree_name": "fail", "mode": "plan"})
        self.assertFalse(result["ok"])
        self.assertEqual(cleaned, [created])

    def test_handoff_retry_requires_recorded_exact_destination(self):
        self.engine.scan()
        stale = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "preview": "retry", "destination_session_id": "codex:nope"})
        self.assertFalse(stale["ok"])
        self.assertIn("stale", stale["error"])
        self.engine._record_handoff_link("same", "claude", "codex:retry", "codex",
                                         "delivery_failed", "hash", "old failure")
        retried = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "preview": "retry exact",
            "destination_session_id": "codex:retry"})
        self.assertTrue(retried["ok"])
        self.assertFalse(retried["created"])
        self.assertEqual(self.codex.actions[-1]["session_id"], "codex:retry")

    def test_codex_worktree_creation_uses_argv_and_validates_name(self):
        os.makedirs(os.path.join(self.cwd, ".git"), exist_ok=True)
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(engine_module.subprocess, "run", return_value=completed) as run:
            made = self.engine._create_codex_worktree(self.cwd, "handoff-ui")
        self.assertTrue(made["ok"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ["git", "-C", os.path.realpath(self.cwd),
                                    "worktree", "add"])
        self.assertEqual(argv[-2:], [made["cwd"], "HEAD"])
        invalid = self.engine._create_codex_worktree(self.cwd, "bad name")
        self.assertFalse(invalid["ok"])

    def test_plain_prose_reply_detection_ignores_examples_and_finds_requests(self):
        self.assertTrue(requests_reply("Which option should I implement?"))
        self.assertTrue(requests_reply(
            "### Scope\n\nAnswer both before I continue.\n\nSome background follows."))
        self.assertFalse(requests_reply(
            "The parser handles `value?` and this quoted example: \"Continue?\""))
        self.assertFalse(requests_reply(
            "> Should this quoted requirement count?\n\nImplementation is complete."))
        self.assertFalse(requests_reply(
            "Why did the cache miss? The path changed, so I rebuilt the index."))
        self.assertFalse(requests_reply(
            "I will check whether the provider recovered, then rerun the test."))

    def test_pure_classifier_explains_priority_without_mutating_input(self):
        session = codex_session()
        session.update(state="running", quiet_s=12, agents_running=1,
                       pending={"kind": "question", "nonce": "q-1"},
                       _latest_prose={"role": "assistant", "text": "Which path?"})
        before = json.dumps(session, sort_keys=True)
        result = classify_placement(session, 100)
        self.assertEqual(json.dumps(session, sort_keys=True), before)
        self.assertEqual((result["ui_group"], result["winning_rule"],
                          result["state_confidence"]),
                         ("needs_you", "placement.pending.question", "confirmed"))
        self.assertIn("placement.state.running", result["suppressed_rules"])
        self.assertEqual([fact["kind"] for fact in result["state_evidence"]][:3],
                         ["provider_signal", "pending_request", "transcript_event"])

    def test_external_completion_is_available_before_it_ages_into_history(self):
        session = codex_session()
        session.update(state="turn_done", headless=True, read_only=True,
                       read_only_reason="Desktop-owned thread", quiet_s=15,
                       _latest_prose={"role": "assistant", "text": "Finished."})
        current = self.engine.organize_session(session, 100)
        self.assertEqual((current["ui_group"], current["reason_label"],
                          current["access"], current["primary_action"]),
                         ("available", "Completed elsewhere", "view_only", "view"))
        self.assertTrue(current["new_response"])

        older = codex_session()
        older.update(state="idle", headless=True, read_only=True, quiet_s=120,
                     _latest_prose={"role": "assistant", "text": "Finished."})
        historical = self.engine.organize_session(older, 200)
        self.assertEqual((historical["ui_group"], historical["reason_label"]),
                         ("history", "External"))

    def test_stale_snapshot_keeps_last_placement_with_explicit_stale_evidence(self):
        session = codex_session()
        session.update(state="stale", stale=True, stale_previous_state="running",
                       stale_reason="App Server stopped", quiet_s=8)
        organized = self.engine.organize_session(session, 100)
        self.assertEqual((organized["state"], organized["normalized_state"],
                          organized["ui_group"], organized["state_confidence"],
                          organized["access"]),
                         ("stale", "running", "working", "stale", "view_only"))
        self.assertIn("stale", [fact["kind"] for fact in organized["state_evidence"]])

    def test_state_journal_deduplicates_recovers_and_pages(self):
        session = codex_session()
        session.update(_latest_prose={"role": "assistant", "text": "Done."})
        available = self.engine.organize_session(session, 100)
        self.engine.record_state_events([available], 100)
        self.engine.record_state_events([available], 102)
        session = codex_session()
        session.update(state="running", reg_status="running", quiet_s=1,
                       _latest_prose={"role": "user", "text": "Continue"})
        running = self.engine.organize_session(session, 110)
        self.engine.record_state_events([running], 110)
        session = codex_session()
        session.update(state="stale", stale=True, stale_previous_state="running",
                       stale_reason="App Server stopped", quiet_s=3)
        stale = self.engine.organize_session(session, 115)
        self.engine.record_state_events([stale], 115)
        session = codex_session()
        session.update(state="running", reg_status="running", quiet_s=1)
        recovered = self.engine.organize_session(session, 120)
        self.engine.record_state_events([recovered], 120)

        count = self.engine.ensure_db().execute(
            "SELECT count(*) FROM state_events WHERE session_id='codex:same'").fetchone()[0]
        self.assertEqual(count, 4)
        first = self.engine.state_history("codex:same", limit=2)
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["events"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(first["events"][0]["winning_rule"], "placement.state.running")
        self.assertEqual(first["events"][1]["confidence"], "stale")
        second = self.engine.state_history(
            "codex:same", cursor=first["next_cursor"], limit=2)
        self.assertEqual(len(second["events"]), 2)
        self.assertIsNone(second["next_cursor"])
        self.assertFalse(self.engine.state_history("bad\nvalue")["ok"])
        self.assertFalse(self.engine.state_history("codex:same", limit=101)["ok"])

    def test_transient_claude_waiting_between_tools_remains_working(self):
        """A progress note followed by another tool is not a request for input."""
        with open(self.transcript, "a") as handle:
            handle.write(json.dumps({
                "type": "user", "timestamp": "2026-07-16T04:54:21Z",
                "message": {"role": "user", "content": "Add the test."},
            }) + "\n")
            handle.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-16T04:54:43Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "tool_use", "content": [{"type": "text", "text":
                        "Good call — the current fix lives inline in "
                        "`classify_one_worktree`. Let me extract it into a pure "
                        "classifier, then add the test."}]},
            }) + "\n")
        registry = os.path.join(self.sessions, "same.json")
        with open(registry, "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "waiting", "name": "Claude"}, handle)
        self.engine.codex = FakeCodex()
        now = os.path.getmtime(self.transcript) + 1
        with mock.patch.object(engine_module.time, "time", return_value=now):
            session = self.engine.scan()["sessions"][0]
        self.assertEqual((session["state"], session["ui_group"],
                          session["reason_label"]),
                         ("running", "working", "Working"))
        self.assertFalse(session["pending"])

    def test_uncorroborated_claude_waiting_must_persist_but_hook_is_immediate(self):
        now = 100.0
        self.assertFalse(self.engine.waiting_confirmed("plain", "waiting", now))
        self.assertFalse(self.engine.waiting_confirmed(
            "plain", "waiting", now + WAITING_CONFIRM_SECONDS - 0.01))
        self.assertTrue(self.engine.waiting_confirmed(
            "plain", "waiting", now + WAITING_CONFIRM_SECONDS))
        self.assertTrue(self.engine.waiting_confirmed(
            "hooked", "waiting", now,
            pending={"kind": "question", "nonce": "q1"}))
        self.assertFalse(self.engine.waiting_confirmed("plain", "busy", now + 10))

    def test_session_organization_maps_every_user_facing_group(self):
        now = 10_000

        def organized(**updates):
            session = codex_session()
            session.update(convo_v="revision:1", quiet_s=20,
                           _latest_prose={"role": "assistant", "text": "Finished."})
            session.update(updates)
            return self.engine.organize_session(session, now)

        question = organized(state="needs_you", pending={"kind": "question"})
        self.assertEqual((question["ui_group"], question["reason_label"],
                          question["primary_action"]),
                         ("needs_you", "Question waiting", "respond"))
        command = organized(state="needs_you", pending={"kind": "permission",
                            "approval_kind": "command"})
        self.assertEqual((command["reason_label"], command["primary_action"]),
                         ("Command approval", "review"))
        file_change = organized(state="needs_you", pending={"kind": "permission",
                                "approval_kind": "file_change"})
        self.assertEqual(file_change["reason_label"], "File approval")
        form = organized(state="needs_you", pending={"kind": "elicitation"})
        self.assertEqual(form["reason_label"], "Form waiting")
        provider_error = organized(state="error", error="protocol failed")
        self.assertEqual((provider_error["ui_group"], provider_error["reason_label"],
                          provider_error["winning_rule"]),
                         ("needs_you", "Fix needed", "placement.provider.error"))

        reply = organized(state="turn_done", _latest_prose={"role": "assistant",
                           "text": "Which layout should I use?"})
        self.assertEqual((reply["ui_group"], reply["reason_label"]),
                         ("needs_you", "Reply requested"))
        running = organized(state="running")
        self.assertEqual((running["ui_group"], running["reason_label"]),
                         ("working", "Working"))
        external = organized(state="running", headless=True, read_only=True)
        self.assertEqual((external["ui_group"], external["reason_label"],
                          external["primary_action"], external["access"]),
                         ("working", "Working elsewhere", "view", "view_only"))
        slow = organized(state="stalled")
        self.assertEqual((slow["ui_group"], slow["reason_label"]),
                         ("working", "Slow"))
        compacting = organized(state="running", compacting=4)
        self.assertEqual((compacting["ui_group"], compacting["reason_label"]),
                         ("working", "Compacting"))
        available = organized(state="idle")
        self.assertEqual((available["ui_group"], available["reason_label"]),
                         ("available", "Available"))
        inactive = organized(state="dormant")
        self.assertEqual((inactive["ui_group"], inactive["reason_label"],
                          inactive["primary_action"]),
                         ("history", "Inactive", "continue"))
        historical = organized(state="idle", headless=True, read_only=True)
        self.assertEqual((historical["ui_group"], historical["reason_label"]),
                         ("history", "External"))
        reopenable = organized(state="reopenable", capabilities={"reopen": True})
        self.assertEqual((reopenable["ui_group"], reopenable["primary_action"],
                          reopenable["access"]),
                         ("history", "reopen", "reopen"))
        unknown = organized(state="future_protocol_state")
        self.assertEqual((unknown["ui_group"], unknown["state_confidence"]),
                         ("available", "unknown"))

        closed = self.engine.organize_closed({
            "session_id": "codex:closed", "provider": "codex", "closed_at": 9,
            "last_seen": 8, "can_reopen": False})
        self.assertEqual((closed["state"], closed["ui_group"], closed["winning_rule"],
                          closed["state_confidence"]),
                         ("closed", "history", "placement.ledger.closed", "confirmed"))

    def test_pin_reply_dismissal_and_read_markers_persist(self):
        pinned = self.engine.update_settings({"pin_session": "codex:same",
                                              "pinned": True})
        self.assertEqual(pinned["pinned_sessions"], ["codex:same"])
        dismissed = self.engine.update_settings({
            "mark_available_session": "codex:same", "revision": "reply:2"})
        self.assertEqual(dismissed["reply_available"]["codex:same"], "reply:2")
        read = self.engine.update_settings({
            "mark_read_session": "codex:same", "revision": "response:3"})
        self.assertEqual(read["read_sessions"]["codex:same"], "response:3")

        with open(os.path.join(self.base, "config.json")) as handle:
            saved = json.load(handle)
        self.assertEqual(saved["pinned_sessions"], ["codex:same"])
        self.assertEqual(saved["reply_available"]["codex:same"], "reply:2")
        self.assertEqual(saved["read_sessions"]["codex:same"], "response:3")

        session = codex_session()
        session.update(state="turn_done", convo_v="reply:2",
                       _latest_prose={"role": "assistant",
                                      "text": "Should I continue?"})
        organized = self.engine.organize_session(session, time.time())
        self.assertEqual(organized["ui_group"], "available")
        self.assertTrue(organized["pinned"])

    def test_digest_and_budget_settings_persist_without_copying_budgets_to_config(self):
        self.engine.cfg["notify"] = {"needs_you": True}
        migrated = self.engine.scan()["notify"]
        self.assertFalse(migrated["scheduled_digest"])
        self.assertTrue(migrated["stall"])
        saved = self.engine.update_settings({
            "notify": {"scheduled_digest": True},
            "digest_schedule_time": "08:30",
            "digest_schedule_zone": "America/New_York",
            "budgets": [{"id": "fleet-token", "scope_type": "fleet",
                         "metric": "tokens", "limit_value": 50000,
                         "block_spawns": False}],
        })
        self.assertTrue(saved["ok"])
        self.assertTrue(saved["notify"]["scheduled_digest"])
        self.assertEqual(saved["budgets"][0]["id"], "fleet-token")
        with open(os.path.join(self.base, "config.json")) as handle:
            config = json.load(handle)
        self.assertEqual(config["digest_schedule_time"], "08:30")
        self.assertEqual(config["digest_schedule_zone"], "America/New_York")
        self.assertNotIn("budgets", config)
        self.engine.scan()
        budget = self.engine.budgets_snapshot()["budgets"][0]
        self.assertEqual((budget["metric"], budget["measurement_scope"]),
                         ("tokens", "partial"))

        invalid = self.engine.update_settings({"digest_schedule_zone": "Not/AZone"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(self.engine.cfg["digest_schedule_zone"], "America/New_York")
        overlong = self.engine.update_settings({"digest_schedule_zone": "A" * 121})
        self.assertFalse(overlong["ok"])
        self.assertEqual(self.engine.cfg["digest_schedule_zone"], "America/New_York")
        self.assertFalse(self.engine.update_settings(
            {"mute_session": "", "muted": True})["ok"])

    def test_explicit_hard_budget_blocks_new_spawns_but_not_existing_work(self):
        self.engine.update_settings({"budgets": [{
            "id": "hard-fleet", "scope_type": "fleet", "metric": "tokens",
            "limit_value": 1, "block_spawns": True,
        }]})
        snapshot = self.engine.scan()
        self.assertGreater(snapshot["sessions"][0].get("total_tokens") or 0, 1)
        budget_action = next(item for item in snapshot["actions"]
                             if item.get("kind") == "budget")
        self.assertIsNone(budget_action["session_id"])
        self.assertEqual(budget_action["primary_action"], "view_budget")
        self.assertEqual(budget_action["safe_bulk"], [])
        self.assertEqual(budget_action["delivery_state"], "Future spawns blocked")
        result = self.engine.spawn_codex_session({"cwd": self.cwd, "model": "gpt-5.4",
                                                  "effort": "high", "mode": "plan"})
        self.assertFalse(result["ok"])
        self.assertIn("blocked by an exceeded budget", result["error"])
        self.assertEqual(result["budget_blockers"][0]["id"], "hard-fleet")
        self.assertTrue(any(item.get("ui_group") == "available"
                            for item in self.engine.snapshot_cache.get("sessions", []) or
                            snapshot["sessions"]))

    def test_explicit_spawn_limit_fails_closed_when_budget_check_breaks(self):
        self.engine.operations.has_spawn_limits = lambda: True
        self.engine.operations.spawn_blockers = mock.Mock(
            side_effect=RuntimeError("ledger unavailable"))
        blockers = self.engine._spawn_budget_blockers("codex", self.cwd)
        self.assertEqual(blockers[0]["id"], "budget-check-unavailable")
        self.assertEqual(blockers[0]["measurement_scope"], "unavailable")

    def test_action_records_are_stable_deduplicated_and_bulk_triage_is_safe(self):
        session = codex_session()
        session.update(state="needs_you", convo_v="revision:7", quiet_s=5,
                       pending={"kind": "question", "nonce": "question:7",
                                "questions": [{"header": "Scope",
                                               "question": "Which scope?"}]},
                       last_msg={"role": "assistant", "text": "Choose one."})
        organized = self.engine.organize_session(session, 100)
        first = self.engine.action_records([organized, dict(organized)])
        second = self.engine.action_records([organized])
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["action_id"], second[0]["action_id"])
        self.assertEqual((first[0]["kind"], first[0]["request"],
                          first[0]["delivery_state"]),
                         ("question", "Which scope?", "Awaiting response"))
        self.assertNotIn("approve", first[0]["safe_bulk"])
        self.assertNotIn("dismiss", first[0]["safe_bulk"])

        rejected = self.engine.update_settings({"bulk_triage": {
            "operation": "approve", "items": [{"session_id": "codex:same",
                                                   "action_id": first[0]["action_id"]}]}})
        self.assertFalse(rejected["ok"])
        self.engine.snapshot_cache = {"actions": first}
        dismissed = self.engine.update_settings({"bulk_triage": {
            "operation": "dismiss", "items": [{"session_id": "codex:same",
                                                  "action_id": first[0]["action_id"]}]}})
        self.assertFalse(dismissed["ok"])
        self.assertEqual(len(self.engine.action_records([organized])), 1)

    def test_completed_and_reply_actions_expose_only_valid_bulk_operations(self):
        reply = codex_session()
        reply.update(ui_group="needs_you", reason_label="Reply requested",
                     primary_action="respond", primary_action_label="Respond",
                     access="interactive", access_label="Interactive",
                     activity_at=90, reply_requested=True, new_response=False,
                     last_msg={"role": "assistant", "text": "Which layout?"})
        outcome = codex_session()
        outcome.update(session_id="codex:other", native_session_id="other",
                       ui_group="available", reason_label="Available",
                       primary_action="continue", primary_action_label="Continue",
                       access="interactive", access_label="Interactive",
                       activity_at=95, reply_requested=False, new_response=True,
                       last_msg={"role": "assistant", "text": "Done."})
        records = {item["kind"]: item for item in self.engine.action_records([reply, outcome])}
        self.assertIn("mark_available", records["reply"]["safe_bulk"])
        self.assertNotIn("mark_read", records["reply"]["safe_bulk"])
        self.assertIn("mark_read", records["outcome"]["safe_bulk"])
        self.assertIn("dismiss", records["outcome"]["safe_bulk"])
        self.engine.snapshot_cache = {"actions": list(records.values())}
        dismissed = self.engine.update_settings({"bulk_triage": {
            "operation": "dismiss", "items": [{"session_id": "codex:other",
                "action_id": records["outcome"]["action_id"],
                "revision": records["outcome"]["revision"]}]}})
        self.assertTrue(dismissed["ok"])
        self.assertNotIn("outcome", {item["kind"] for item in
                                     self.engine.action_records([reply, outcome])})

    def test_workstream_identity_rolls_linked_worktrees_into_main_repository(self):
        main = os.path.join(self.tmp.name, "main-repo")
        linked = os.path.join(self.tmp.name, "linked-worktree")
        gitdir = os.path.join(main, ".git")
        linked_gitdir = os.path.join(gitdir, "worktrees", "linked")
        os.makedirs(linked_gitdir)
        os.makedirs(linked)
        with open(os.path.join(linked, ".git"), "w") as handle:
            handle.write("gitdir: " + linked_gitdir + "\n")
        with open(os.path.join(linked_gitdir, "commondir"), "w") as handle:
            handle.write("../..\n")
        main_identity = self.engine.workstream_identity(main)
        linked_identity = self.engine.workstream_identity(linked)
        self.assertEqual(main_identity["kind"], "git")
        self.assertEqual(main_identity["root"], os.path.realpath(main))
        self.assertEqual(linked_identity["root"], main_identity["root"])
        self.assertEqual(linked_identity["workstream_id"], main_identity["workstream_id"])
        self.assertEqual(linked_identity["worktree"], os.path.realpath(linked))

    def test_secondary_worktree_close_preview_and_cleanup_are_revision_checked(self):
        main = os.path.join(self.tmp.name, "close-main")
        os.makedirs(main)

        def git(*args, cwd=main):
            return subprocess.run(["git", *args], cwd=cwd, check=True,
                                  capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.email", "fleet@example.test")
        git("config", "user.name", "Fleet Test")
        with open(os.path.join(main, ".gitignore"), "w") as handle:
            handle.write("build/\n")
        with open(os.path.join(main, "tracked.txt"), "w") as handle:
            handle.write("base\n")
        git("add", ".gitignore", "tracked.txt")
        git("commit", "-m", "base")

        dirty = os.path.join(self.tmp.name, "close-dirty")
        git("worktree", "add", "-b", "feature/dirty", dirty)
        with open(os.path.join(dirty, "tracked.txt"), "a") as handle:
            handle.write("unstaged\n")
        with open(os.path.join(dirty, "staged.txt"), "w") as handle:
            handle.write("staged\n")
        git("add", "staged.txt", cwd=dirty)
        with open(os.path.join(dirty, "untracked.txt"), "w") as handle:
            handle.write("untracked\n")
        os.makedirs(os.path.join(dirty, "build"))
        with open(os.path.join(dirty, "build", "cache.bin"), "w") as handle:
            handle.write("ignored\n")

        session = {"session_id": "same", "provider": "codex", "cwd": dirty}
        preview = self.engine.close_worktree_preview(session)
        self.assertTrue(preview["secondary_worktree"])
        self.assertTrue(preview["inspect_ok"])
        self.assertFalse(preview["remove_allowed"])
        self.assertTrue(preview["force_remove_allowed"])
        self.assertEqual(preview["dirty_counts"], {
            "staged": 1, "unstaged": 1, "untracked": 1, "conflicts": 0})
        self.assertEqual(preview["ignored_files"], ["build/cache.bin"])
        self.assertEqual({item["path"] for item in preview["dirty_files"]},
                         {"tracked.txt", "staged.txt", "untracked.txt"})

        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [{"session_id": "codex:other",
                "provider": "codex", "title": "Other", "cwd": dirty}]}
        shared = self.engine.close_worktree_preview(session)
        self.assertFalse(shared["force_remove_allowed"])
        self.assertEqual(shared["shared_sessions"][0]["session_id"], "codex:other")
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": []}

        self.engine._mark_cleanup_ticket_closed(preview["cleanup_ticket"], "same")
        removed = self.engine.cleanup_closed_worktree({"session_id": "same",
            "cleanup_ticket": preview["cleanup_ticket"], "force": True})
        self.assertTrue(removed["ok"])
        self.assertTrue(removed["branch_preserved"])
        self.assertFalse(os.path.exists(dirty))
        self.assertEqual(git("show-ref", "--verify", "refs/heads/feature/dirty").returncode, 0)

        clean = os.path.join(self.tmp.name, "close-clean")
        git("worktree", "add", "-b", "feature/clean", clean)
        clean_session = {"session_id": "same", "provider": "codex", "cwd": clean}
        clean_preview = self.engine.close_worktree_preview(clean_session)
        self.assertTrue(clean_preview["remove_allowed"])
        self.engine._mark_cleanup_ticket_closed(clean_preview["cleanup_ticket"], "same")
        clean_removed = self.engine.cleanup_closed_worktree({"session_id": "same",
            "cleanup_ticket": clean_preview["cleanup_ticket"], "force": False})
        self.assertTrue(clean_removed["ok"])
        self.assertFalse(os.path.exists(clean))
        self.assertEqual(git("show-ref", "--verify", "refs/heads/feature/clean").returncode, 0)

        stale = os.path.join(self.tmp.name, "close-stale")
        git("worktree", "add", "-b", "feature/stale", stale)
        stale_session = {"session_id": "same", "provider": "codex", "cwd": stale}
        stale_preview = self.engine.close_worktree_preview(stale_session)
        with open(os.path.join(stale, "after-preview.txt"), "w") as handle:
            handle.write("changed\n")
        self.engine._mark_cleanup_ticket_closed(stale_preview["cleanup_ticket"], "same")
        rejected = self.engine.cleanup_closed_worktree({"session_id": "same",
            "cleanup_ticket": stale_preview["cleanup_ticket"], "force": False})
        self.assertFalse(rejected["ok"])
        self.assertIn("changed after preview", rejected["error"])
        self.assertTrue(os.path.isdir(stale))

        primary = self.engine.close_worktree_preview(
            {"session_id": "same", "provider": "codex", "cwd": main})
        self.assertFalse(primary["secondary_worktree"])
        self.assertFalse(primary["remove_allowed"])

    def test_workstreams_keep_missing_and_unrelated_folders_separate(self):
        one = codex_session()
        two = codex_session()
        one.update(session_id="codex:one", native_session_id="one",
                   cwd=os.path.join(self.tmp.name, "missing-one"), ui_group="available",
                   reason_label="Available", activity_at=2)
        two.update(session_id="codex:two", native_session_id="two",
                   cwd=os.path.join(self.tmp.name, "missing-two"), ui_group="history",
                   reason_label="External", activity_at=1)
        records = self.engine.workstream_records([one], [two])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item["missing"] for item in records))
        self.assertNotEqual(records[0]["workstream_id"], records[1]["workstream_id"])
        self.assertEqual(sum(item["counts"]["available"] for item in records), 1)
        self.assertEqual(sum(item["counts"]["history"] for item in records), 1)

    def test_workstreams_cache_ignores_poll_timestamps_but_invalidates_on_state(self):
        session = codex_session()
        session.update(cwd=self.cwd, ui_group="available", reason_label="Available",
                       activity_at=2)
        with self.engine.lock:
            self.engine.snapshot_cache = {"t": 1, "sessions": [session], "closed": []}
        with mock.patch.object(self.engine.operations, "budgets_snapshot",
                               return_value={"budgets": []}), mock.patch.object(
                                   self.engine, "workstream_records",
                                   wraps=self.engine.workstream_records) as records:
            first = self.engine.workstreams_snapshot()
            with self.engine.lock:
                self.engine.snapshot_cache = {"t": 2, "sessions": [dict(session)],
                                              "closed": []}
            second = self.engine.workstreams_snapshot()
            self.assertIs(first, second)
            self.assertEqual(records.call_count, 1)

            changed = dict(session, ui_group="working", state="running",
                           reason_label="Working")
            with self.engine.lock:
                self.engine.snapshot_cache = {"t": 3, "sessions": [changed], "closed": []}
            third = self.engine.workstreams_snapshot()
            self.assertIsNot(third, second)
            self.assertEqual(records.call_count, 2)
            self.assertEqual(third["workstreams"][0]["counts"]["working"], 1)

    def test_workstream_identity_prefers_nested_repo_and_resolves_symlinks(self):
        outer = os.path.join(self.tmp.name, "outer")
        inner = os.path.join(outer, "packages", "inner")
        cwd = os.path.join(inner, "src")
        os.makedirs(os.path.join(outer, ".git"))
        os.makedirs(os.path.join(inner, ".git"))
        os.makedirs(cwd)
        link = os.path.join(self.tmp.name, "inner-link")
        os.symlink(inner, link)
        nested = self.engine.workstream_identity(cwd)
        linked = self.engine.workstream_identity(link)
        self.assertEqual(nested["root"], os.path.realpath(inner))
        self.assertEqual(linked["workstream_id"], nested["workstream_id"])

    def test_workstream_identity_invalidates_a_renamed_root_and_flags_bad_metadata(self):
        repo = os.path.join(self.tmp.name, "rename-me")
        os.makedirs(os.path.join(repo, ".git"))
        before = self.engine.workstream_identity(repo)
        renamed = os.path.join(self.tmp.name, "renamed")
        os.rename(repo, renamed)
        after = self.engine.workstream_identity(repo)
        moved = self.engine.workstream_identity(renamed)
        self.assertTrue(after["missing"])
        self.assertNotEqual(before["workstream_id"], moved["workstream_id"])

        broken = os.path.join(self.tmp.name, "broken-worktree")
        os.makedirs(broken)
        with open(os.path.join(broken, ".git"), "w") as handle:
            handle.write("not git metadata\n")
        identity = self.engine.workstream_identity(broken)
        self.assertTrue(identity["stale"])
        self.assertIn("unreadable", identity["error"])

    def test_workstream_records_preserve_detached_branch_and_provider_outage_shape(self):
        session = codex_session()
        session.update(branch="HEAD", ui_group="working", reason_label="Working",
                       activity_at=9, cwd=self.cwd)
        records = self.engine.workstream_records([session], [])
        self.assertEqual(records[0]["branches"], ["HEAD"])
        self.assertEqual(records[0]["providers"], ["codex"])
        self.assertEqual(records[0]["counts"]["working"], 1)
        self.assertEqual(records[0]["repo_summary"]["tests"], "not_observed")

    def test_workstreams_do_not_merge_sessions_whose_locations_are_unknown(self):
        one = codex_session()
        two = codex_session()
        one.update(session_id="codex:one", cwd=None, ui_group="available", activity_at=2)
        two.update(session_id="codex:two", cwd="", ui_group="available", activity_at=1)
        records = self.engine.workstream_records([one, two], [])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item["kind"] == "unknown" for item in records))
        self.assertTrue(all(item["root"] == "Location unavailable" for item in records))
        self.assertNotEqual(records[0]["workstream_id"], records[1]["workstream_id"])

    def test_closed_history_is_filtered_paginated_and_keeps_pins_out_of_listing(self):
        rows = [
            {"session_id": "codex:one", "provider": "codex", "title": "Parser audit",
             "project": "fleet", "primary_action": "view", "pinned": False},
            {"session_id": "claude-two", "provider": "claude", "title": "Parser fix",
             "project": "fleet", "primary_action": "reopen", "pinned": False},
            {"session_id": "claude-pin", "provider": "claude", "title": "Pinned parser",
             "project": "fleet", "primary_action": "view", "pinned": True},
        ]
        with self.engine.lock:
            self.engine.snapshot_cache = {"closed": rows}
        first = self.engine.history_snapshot(limit=1, query="parser")
        self.assertTrue(first["ok"])
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["next_cursor"], 1)
        second = self.engine.history_snapshot(cursor=1, limit=1, query="parser")
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        codex = self.engine.history_snapshot(provider="codex", access="view")
        self.assertEqual([item["session_id"] for item in codex["items"]], ["codex:one"])
        exact = self.engine.history_snapshot(sid="claude-pin")
        self.assertEqual(exact["item"]["title"], "Pinned parser")
        self.assertFalse(self.engine.history_snapshot(provider="future")["ok"])

    def test_repository_routes_are_confined_to_observed_worktrees_and_audit_actions(self):
        os.makedirs(os.path.join(self.cwd, ".git"))
        fleet = self.engine.scan()
        self.assertTrue(fleet["sessions"])

        class FakeCenter:
            def snapshot(inner, root, worktree, test_outcome=None, force=False):
                return {"ok": True, "state": "ok", "root": root, "worktree": worktree,
                    "branch": "feature", "dirty": True, "files": [{"path": "engine.py"}],
                    "revision": "rev-1", "actions": {"commit": {"enabled": True}},
                    "pr": {"state": "none"}, "tests": test_outcome or
                    {"state": "not_observed"}}

            def perform(inner, kind, snapshot, payload):
                return {"ok": True, "kind": kind, "summary": "committed",
                        "snapshot": {**snapshot, "dirty": False, "files": [],
                                     "revision": "rev-2"}}

        self.engine.repo_center = FakeCenter()
        snapshot = self.engine.repository_snapshot("", self.cwd)
        self.assertTrue(snapshot["ok"])
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(os.path.join(outside, ".git"))
        rejected = self.engine.repository_snapshot("", outside)
        self.assertIn("current Fleet", rejected["error"])
        result = self.engine.repository_action({"type": "git_commit", "root": self.cwd,
            "worktree": self.cwd, "revision": "rev-1", "paths": ["engine.py"],
            "message": "Commit"})
        self.assertTrue(result["ok"])
        row = self.engine.ensure_db().execute(
            "SELECT kind,status,summary FROM repo_actions WHERE action_id=?",
            (result["action_id"],)).fetchone()
        self.assertEqual(row, ("git_commit", "succeeded", "committed"))

    def test_workstream_uses_newest_transcript_test_outcome(self):
        one = codex_session()
        one.update(cwd=self.cwd, repo_outcome={"state": "failed", "at": 4,
            "command": "npm test", "provider": "codex"})
        two = codex_session()
        two.update(session_id="codex:two", cwd=self.cwd,
                   repo_outcome={"state": "passed", "at": 8,
                                 "command": "python3 -m unittest", "provider": "claude"})
        records = self.engine.workstream_records([one, two], [])
        self.assertEqual(records[0]["test_outcome"]["state"], "passed")
        self.assertEqual(records[0]["test_outcome"]["command"],
                         "python3 -m unittest")

    def test_claude_peek_preserves_markdown_blocks(self):
        tail = Tail(self.transcript)
        text = "### Default width\n\nUse **Fit the screen**."
        tail.convo.append({"role": "assistant", "text": text})
        self.assertEqual(tail.last_message(500), {"role": "assistant", "text": text})
        long_text = "x" * 600
        tail.convo.append({"role": "assistant", "text": long_text})
        preview = tail.last_message(500)["text"]
        self.assertEqual(len(preview), 500)
        self.assertTrue(preview.endswith("…"))

    def test_tail_tracks_only_known_claude_permission_modes(self):
        path = os.path.join(self.tmp.name, "permission-mode.jsonl")
        rows = [
            {"type": "permission-mode", "permissionMode": "acceptEdits"},
            {"type": "permission-mode", "permissionMode": "invented"},
            {"type": "user", "permissionMode": "plan",
             "message": {"role": "user", "content": "continue"}},
        ]
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        tail = Tail(path)
        self.assertTrue(tail.poll())
        self.assertEqual(tail.permission_mode, "plan")


if __name__ == "__main__":
    unittest.main()
