import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import engine as engine_module
from engine import DEFAULT_CONFIG, Engine, Tail, requests_reply
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

    def test_codex_failure_does_not_remove_claude(self):
        self.codex.fail_sessions = True
        fleet = self.engine.scan()
        self.assertEqual([item["provider"] for item in fleet["sessions"]], ["claude"])
        self.assertFalse(fleet["providers"]["codex"]["ok"])
        self.assertIn("Codex crashed", fleet["providers"]["codex"]["error"])

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
                "effort": "high", "worktree": True, "worktree_name": "live-e2e"})
        self.assertTrue(spawned["ok"])
        command = writes[-1][1][0][0]
        self.assertIn("claude --model sonnet --effort high --worktree live-e2e", command)
        self.assertFalse(spawned["trust_prompt"])

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

    def test_plain_prose_reply_detection_ignores_examples_and_finds_requests(self):
        self.assertTrue(requests_reply("Which option should I implement?"))
        self.assertTrue(requests_reply(
            "### Scope\n\nAnswer both before I continue.\n\nSome background follows."))
        self.assertFalse(requests_reply(
            "The parser handles `value?` and this quoted example: \"Continue?\""))
        self.assertFalse(requests_reply(
            "> Should this quoted requirement count?\n\nImplementation is complete."))

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

    def test_claude_peek_preserves_markdown_blocks(self):
        tail = Tail(self.transcript)
        text = "### Default width\n\nUse **Fit the screen**."
        tail.convo.append({"role": "assistant", "text": text})
        self.assertEqual(tail.last_message(500), {"role": "assistant", "text": text})


if __name__ == "__main__":
    unittest.main()
