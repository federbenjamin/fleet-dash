"""Coverage for fleetdash/engine_context.py — conversation/file/context
projections, hook pending, effort resolution, slash-command catalog, and the
opaque-file whitelist (invariants 1, 10, 11, 22, 43, 65)."""
import io
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))   # sibling test_cov_common import

from fleetdash import engine_context as ctx_mod
from fleetdash.tail import Tail
from test_cov_common import EngineCovBase


TODAY = time.strftime("%Y-%m-%d")


class ContextCovTest(EngineCovBase):
    # ------------------------------------------------------------- ledger seed
    def _seed_ledger(self):
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO agent_runs(agent_id, session_id, project, agent_type, "
            "model, description, in_tok, cw_tok, cr_tok, out_tok, cost, started, "
            "ended) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("agent-1", self.sid, "repo", "Explore", "claude-sonnet", "d",
             1000, 200, 5000, 300, 0.42, TODAY, TODAY))
        db.execute(
            "INSERT INTO session_runs(session_id, name, project, cwd, branch, "
            "model, cost, agent_cost, agents_total, bridge_url, first_seen, "
            "last_seen, closed_at, title, provider, transcript_path) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.sid, "Sess", "repo", self.cwd, "main", "claude-opus", 1.5, 0.42,
             1, None, int(time.time()) - 10, int(time.time()), None, "My Session",
             "claude", self.transcript))
        for kind, name, fam in (("skill", "handoff", "sonnet"),
                                ("tool", "Bash", "sonnet"),
                                ("cache", "compaction", "sonnet"),
                                ("tokens", "all", "sonnet")):
            db.execute(
                "INSERT INTO usage_stats(path, day, kind, name, uses, chars, "
                "t_in, t_cw, t_cr, t_out, fam) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.transcript, TODAY, kind, name, 3, 4000, 500, 100, 200, 50,
                 fam))
        db.commit()

    def test_insights_full_aggregation(self):
        self._seed_ledger()
        out = self.engine.insights(days=7)
        self.assertTrue(out["ok"])
        self.assertTrue(any(a["name"] == "Explore" for a in out["agents"]))
        self.assertTrue(any(s["name"] == "handoff" for s in out["skills"]))
        self.assertTrue(any(t["name"] == "Bash" for t in out["tools"]))
        self.assertTrue(out["cache_busts"])
        self.assertTrue(out["token_mix"])
        self.assertTrue(out["models"])
        self.assertIn("agent_cost", out["totals"])

    def test_insights_error_path(self):
        with mock.patch.object(self.engine, "ledger_reader",
                               side_effect=RuntimeError("db down")):
            out = self.engine.insights(days=7)
        self.assertFalse(out["ok"])
        self.assertIn("db down", out["error"])

    # ------------------------------------------------------------ hook_pending
    def _pending_path(self, sid):
        d = os.path.join(self.base, "pending")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{sid}.json")

    def test_hook_pending_missing_returns_none(self):
        self.assertIsNone(self.engine.hook_pending(self.sid, "idle"))

    def test_hook_pending_question_valid(self):
        with open(self._pending_path(self.sid), "w") as h:
            json.dump({"kind": "question", "nonce": "n1", "questions": [{"q": "x"}],
                       "ts": time.time()}, h)
        got = self.engine.hook_pending(self.sid, "waiting")
        self.assertEqual(got["kind"], "question")
        self.assertEqual(got["nonce"], "n1")

    def test_hook_pending_expires_stale_permission(self):
        path = self._pending_path(self.sid)
        with open(path, "w") as h:
            json.dump({"kind": "permission", "nonce": "n", "ts": time.time() - 100}, h)
        self.assertIsNone(self.engine.hook_pending(self.sid, "idle"))
        self.assertFalse(os.path.exists(path))       # removed

    def test_hook_pending_question_ghost_guard(self):
        """The guard is a grace period for Claude's status flicker, not a decision.

        It was 5s while a transcript fallback could re-surface a dropped capture
        under a second identity. That fallback is gone (invariant 1), so firing
        early now loses the question outright — hence the wider grace. act() still
        refuses to answer a prompt whose registry status is not `waiting`.
        """
        path = self._pending_path(self.sid)
        write = lambda age: json.dump(
            {"kind": "question", "nonce": "n", "questions": [], "ts": time.time() - age},
            open(path, "w"))

        write(8)     # would have been dropped by the old 5s guard
        self.assertIsNotNone(self.engine.hook_pending(self.sid, "idle"))
        write(self.engine.GHOST_QUESTION_GRACE + 2)
        self.assertIsNone(self.engine.hook_pending(self.sid, "idle"))
        self.assertTrue(os.path.exists(path))        # hidden, not removed
        # a waiting session keeps its question at any age
        write(self.engine.GHOST_QUESTION_GRACE + 2)
        self.assertIsNotNone(self.engine.hook_pending(self.sid, "waiting"))

    def test_hook_pending_collects_a_capture_left_by_a_dead_session(self):
        """PostToolUse clears a question capture, so only a dead session leaves one."""
        path = self._pending_path(self.sid)
        with open(path, "w") as handle:
            json.dump({"kind": "question", "nonce": "n", "questions": [],
                       "ts": time.time() - self.engine.STALE_CAPTURE_SECONDS - 60}, handle)
        self.assertIsNone(self.engine.hook_pending(self.sid, "waiting"))
        self.assertFalse(os.path.exists(path))

    def test_hook_pending_permission_valid(self):
        with open(self._pending_path(self.sid), "w") as h:
            json.dump({"kind": "permission", "nonce": "p1", "message": "rm -rf",
                       "ts": time.time()}, h)
        got = self.engine.hook_pending(self.sid, "waiting")
        self.assertEqual(got["kind"], "permission")
        self.assertEqual(got["input_summary"], "rm -rf")

    def test_hook_pending_unknown_kind(self):
        with open(self._pending_path(self.sid), "w") as h:
            json.dump({"kind": "mystery", "nonce": "z", "ts": time.time()}, h)
        self.assertIsNone(self.engine.hook_pending(self.sid, "waiting"))

    # ------------------------------------------------------------ _paired_files
    def test_paired_files_within_window(self):
        img = os.path.join(self.cwd, "shot.png")
        with open(img, "wb") as h:
            h.write(b"\x89PNG")
        txt = os.path.join(self.cwd, "notes.txt")   # missing on disk intentionally
        mt = Tail(self.transcript)
        q_ep = time.time()
        q_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(q_ep))
        mt.files.append({"path": img, "caption": "cap", "ts": q_iso})
        mt.files.append({"path": txt, "caption": "", "ts": q_iso})
        mt.files.append({"path": "/x/old.txt", "caption": "",
                         "ts": "2000-01-01T00:00:00Z"})   # out of window
        paired = self.engine._paired_files(mt, q_ep)
        names = {p["name"] for p in paired}
        self.assertIn("shot.png", names)
        self.assertNotIn("old.txt", names)
        self.assertTrue(next(p for p in paired if p["name"] == "shot.png")
                        ["kind"] == "image")

    # -------------------------------------------------------------- effort
    def test_reg_main_path_unknown(self):
        self.assertEqual(self.engine._reg_main_path("no-such"), (None, None))

    def test_transcript_effort_no_tail(self):
        self.assertEqual(self.engine._transcript_effort(self.sid), (None, 0))

    def test_transcript_effort_present(self):
        self.engine.tail_for(self.transcript).poll()
        value, offset = self.engine._transcript_effort(self.sid)
        self.assertEqual(value, "high")
        self.assertGreater(offset, 0)

    def test_agent_effort_plugin_type_uses_parent(self):
        self.assertEqual(self.engine.agent_effort("plugin:x", self.cwd, "high"),
                         "high")

    def test_agent_effort_reads_frontmatter_and_caches(self):
        agents = os.path.join(self.cwd, ".claude", "agents")
        os.makedirs(agents)
        with open(os.path.join(agents, "grader.md"), "w") as h:
            h.write("---\neffort: low\n---\nbody")
        self.assertEqual(self.engine.agent_effort("grader", self.cwd, "high"), "low")
        # second call hits the mtime cache branch
        self.assertEqual(self.engine.agent_effort("grader", self.cwd, "high"), "low")

    def test_agent_effort_no_pin_returns_parent(self):
        agents = os.path.join(self.cwd, ".claude", "agents")
        os.makedirs(agents)
        with open(os.path.join(agents, "plain.md"), "w") as h:
            h.write("---\nname: plain\n---\nbody")
        self.assertEqual(self.engine.agent_effort("plain", self.cwd, "max"), "max")

    def test_effort_for_transcript_retires_override(self):
        self.engine.tail_for(self.transcript).poll()
        _, off = self.engine._transcript_effort(self.sid)
        self.engine._claude_effort_overrides[self.sid] = ("low", time.time())
        self.engine._claude_control_overrides[self.sid] = {
            "effort": {"baseline": 1, "value": "low", "accepted_at": time.time()}}
        with mock.patch.object(self.engine, "_retire_claude_control_override"):
            self.assertEqual(self.engine.effort_for(self.sid), "high")

    def test_effort_for_statusline_retires_override(self):
        effdir = os.path.join(self.base, "effort")
        os.makedirs(effdir, exist_ok=True)
        with open(os.path.join(effdir, self.sid), "w") as h:
            h.write("max")
        self.engine._claude_effort_overrides[self.sid] = ("low", 0.0)
        self.engine._claude_control_overrides[self.sid] = {
            "effort": {"baseline": 0, "value": "low", "accepted_at": 0.0}}
        with mock.patch.object(self.engine, "_retire_claude_control_override"):
            self.assertEqual(self.engine.effort_for(self.sid), "max")

    def test_effort_for_override_held(self):
        self.engine._claude_effort_overrides[self.sid] = ("low", time.time() + 1000)
        self.engine._claude_control_overrides[self.sid] = {
            "effort": {"baseline": 10 ** 9, "value": "low",
                       "accepted_at": time.time() + 1000}}
        self.assertEqual(self.engine.effort_for(self.sid), "low")

    # ------------------------------------------------------- compacting_secs
    def _checkpoint(self):
        cdir = os.path.join(self.tmp.name, ".claude", "compaction",
                            os.path.basename(self.project_dir))
        os.makedirs(cdir, exist_ok=True)
        return os.path.join(cdir, f"checkpoint-{self.sid}.md")

    def test_compacting_secs_none_when_no_file(self):
        mt = SimpleNamespace(last_compact_ep=0)
        self.assertIsNone(self.engine.compacting_secs(self.sid, self.cwd, mt))

    def test_compacting_secs_running(self):
        p = self._checkpoint()
        with open(p, "w") as h:
            h.write("x")
        mt = SimpleNamespace(last_compact_ep=0)
        self.assertIsInstance(self.engine.compacting_secs(self.sid, self.cwd, mt),
                              int)

    def test_compacting_secs_already_landed(self):
        p = self._checkpoint()
        with open(p, "w") as h:
            h.write("x")
        mt = SimpleNamespace(last_compact_ep=time.time() + 1000)
        self.assertIsNone(self.engine.compacting_secs(self.sid, self.cwd, mt))

    def test_compacting_secs_stale_checkpoint(self):
        p = self._checkpoint()
        with open(p, "w") as h:
            h.write("x")
        os.utime(p, (time.time() - 2000, time.time() - 2000))
        mt = SimpleNamespace(last_compact_ep=0)
        self.assertIsNone(self.engine.compacting_secs(self.sid, self.cwd, mt))

    # -------------------------------------------------------------- commands
    def test_commands_full_catalog(self):
        proj = os.path.join(self.cwd, ".claude")
        os.makedirs(os.path.join(proj, "commands"))
        os.makedirs(os.path.join(proj, "skills", "myskill"))
        with open(os.path.join(proj, "commands", "deploy.md"), "w") as h:
            h.write("---\ndescription: Deploy it\n---\nrun deploy")
        with open(os.path.join(proj, "skills", "myskill", "SKILL.md"), "w") as h:
            h.write("---\ndescription: A skill\n---\nx")
        user = os.path.join(self.tmp.name, ".claude")
        os.makedirs(os.path.join(user, "commands"))
        with open(os.path.join(user, "commands", "note.md"), "w") as h:
            h.write("# Note command\nbody line")     # no frontmatter -> body desc
        # a plugin
        plug_root = os.path.join(self.tmp.name, "plug")
        os.makedirs(os.path.join(plug_root, "commands"))
        with open(os.path.join(plug_root, "commands", "go.md"), "w") as h:
            h.write("just go")
        os.makedirs(os.path.join(user, "plugins"))
        with open(os.path.join(user, "plugins", "installed_plugins.json"), "w") as h:
            json.dump({"plugins": {"myplug@1": [{"installPath": plug_root}]}}, h)
        out = self.engine.commands(self.sid)
        names = {c["name"] for c in out["commands"]}
        self.assertIn("/deploy", names)
        self.assertIn("/myskill", names)
        self.assertIn("/note", names)
        self.assertIn("/myplug:go", names)
        deploy = next(c for c in out["commands"] if c["name"] == "/deploy")
        self.assertEqual(deploy["desc"], "Deploy it")

    def test_commands_codex_delegates(self):
        self.engine.codex = SimpleNamespace(
            commands=lambda sid, cwd: {"ok": True, "commands": []})
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": "codex:z", "cwd": "/w"}]}
        self.assertTrue(self.engine.commands("codex:z")["ok"])

    def test_codex_commands_unavailable(self):
        self.engine.snapshot_cache = {"sessions": []}
        out = self.engine.codex_commands("codex:missing")
        self.assertFalse(out["ok"])

    def test_desc_of_unreadable_returns_blank(self):
        # a command dir with a directory named like a .md (open raises) is folded
        proj = os.path.join(self.cwd, ".claude", "commands")
        os.makedirs(os.path.join(proj, "weird.md"))   # directory, not a file
        out = self.engine.commands(self.sid)
        self.assertTrue(out["ok"])

    # ---------------------------------------------------- _claude_file_backup
    def test_claude_file_backup_invalid_inputs(self):
        self.assertIsNone(ctx_mod.ContextOps._claude_file_backup("not-a-uuid", "x"))
        self.assertIsNone(ctx_mod.ContextOps._claude_file_backup(
            self.sid, "not-valid-name"))
        # valid shape but the backup file does not exist
        self.assertIsNone(ctx_mod.ContextOps._claude_file_backup(
            self.sid, "abcdef01@v1"))

    def test_claude_file_backup_resolves(self):
        hist = os.path.join(self.tmp.name, ".claude", "file-history", self.sid)
        os.makedirs(hist)
        with open(os.path.join(hist, "abcdef01@v2"), "w") as h:
            h.write("backup bytes")
        got = ctx_mod.ContextOps._claude_file_backup(self.sid, "abcdef01@v2")
        self.assertTrue(got.endswith("abcdef01@v2"))

    # ------------------------------------------------- small remaining branches
    def test_hook_pending_remove_oserror_is_swallowed(self):
        with open(self._pending_path(self.sid), "w") as h:
            json.dump({"kind": "permission", "nonce": "n", "ts": time.time() - 100}, h)
        with mock.patch.object(ctx_mod.os, "remove", side_effect=OSError("busy")):
            self.assertIsNone(self.engine.hook_pending(self.sid, "idle"))

    def test_agent_effort_no_file_returns_parent(self):
        # no .claude/agents dir anywhere -> getmtime OSError, loop exhausts
        self.assertEqual(self.engine.agent_effort("ghost", self.cwd, "medium"),
                         "medium")

    def test_agent_effort_unreadable_file(self):
        agents = os.path.join(self.cwd, ".claude", "agents")
        os.makedirs(agents)
        os.makedirs(os.path.join(agents, "dir.md"))   # dir: getmtime ok, open fails
        self.assertEqual(self.engine.agent_effort("dir", self.cwd, "high"), "high")

    def test_transcript_effort_unknown_sid_no_path(self):
        self.assertEqual(self.engine._transcript_effort("nope"), (None, 0))

    def test_effort_for_no_override_prefers_transcript(self):
        self.engine.tail_for(self.transcript).poll()
        self.assertEqual(self.engine.effort_for(self.sid), "high")

    def test_commands_unknown_session_empty_cwd(self):
        out = self.engine.commands("unknown-sid")
        self.assertTrue(out["ok"])

    # ----------------------------------------------------- session_context
    def _deliver_file(self, name="out.txt", body="hello file"):
        fpath = os.path.join(self.cwd, name)
        with open(fpath, "w") as h:
            h.write(body)
        rows = [
            {"type": "user", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "tool_use",
                         "usage": {"input_tokens": 3},
                         "content": [{"type": "tool_use", "id": "sf1",
                                      "name": "SendUserFile",
                                      "input": {"files": [fpath],
                                                "caption": "the file"}}]}},
        ]
        self.write_transcript(rows)
        return fpath

    def test_session_context_fallback_poll(self):
        fpath = self._deliver_file()
        out = self.engine.session_context(self.sid)
        self.assertTrue(out["ok"])
        self.assertEqual(out["files"][0]["name"], "out.txt")
        # inline tool row files were enriched to fmeta dicts
        tool_rows = [m for m in out["messages"] if m.get("role") == "tool"]
        self.assertTrue(tool_rows and isinstance(tool_rows[0]["files"][0], dict))

    def test_session_context_not_live(self):
        out = self.engine.session_context("dddddddd-eeee-ffff-0000-111111111111")
        self.assertFalse(out["ok"])

    def test_session_context_starting_when_transcript_missing(self):
        sid = "cccccccc-dddd-eeee-ffff-000000000000"
        self.write_registry(sid, cwd=self.cwd)
        out = self.engine.session_context(sid)
        self.assertTrue(out["starting"])

    def test_session_context_codex_known_and_unknown(self):
        self.engine.codex = SimpleNamespace(context=lambda sid: {
            "ok": True, "messages": [], "files": [{"path": "/w/a.txt"}]})
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": "codex:c", "provider": "codex"}]}
        out = self.engine.session_context("codex:c")
        self.assertTrue(out["ok"])
        self.engine.snapshot_cache = {"sessions": []}
        self.assertFalse(self.engine.session_context("codex:x")["ok"])

    def test_session_context_uses_published_snapshot(self):
        self.engine._claude_context_snapshots[self.sid] = {
            "messages": [{"role": "assistant", "text": "cached"}],
            "files": [], "file_backups": {}}
        out = self.engine.session_context(self.sid)
        self.assertEqual(out["messages"][0]["text"], "cached")

    # ------------------------------------------------------- _agent_paths
    def test_agent_paths_branches(self):
        self.assertEqual(self.engine._agent_paths(self.sid, "bad id"), (None, None))
        self.assertEqual(self.engine._agent_paths("unknown", "agent-x"),
                         (None, None))
        self.assertEqual(self.engine._agent_paths(self.sid, "agent-missing"),
                         (None, None))

    # ------------------------------------------------------- agent_context
    def _make_subagent(self, aid="agent-x"):
        subdir = os.path.join(self.project_dir, self.sid, "subagents")
        os.makedirs(subdir, exist_ok=True)
        with open(os.path.join(subdir, f"{aid}.meta.json"), "w") as h:
            json.dump({"agentType": "Explore", "description": "look",
                       "spawnDepth": 1}, h)
        with open(os.path.join(subdir, f"{aid}.jsonl"), "w") as h:
            h.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-15T00:00:00Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 10, "output_tokens": 4},
                            "content": [{"type": "text", "text": "found"}]}}) + "\n")
        return subdir

    def test_agent_context_live_poll(self):
        self._make_subagent("agent-x")
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": self.sid, "agents": [
                {"agent_id": "agent-x", "state": "running"}]}]}
        out = self.engine.agent_context(self.sid, "agent-x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["info"]["agent_type"], "Explore")
        self.assertIn("status_line", out["info"])

    def test_agent_context_uses_snapshot(self):
        self._make_subagent("agent-s")
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": self.sid, "agents": [
                {"agent_id": "agent-s", "state": "running",
                 "description": "snap"}]}]}
        self.engine._claude_agent_context_snapshots[(self.sid, "agent-s")] = {
            "messages": [{"role": "assistant", "text": "snapped"}],
            "context_tokens": 42, "status_metrics": {}}
        out = self.engine.agent_context(self.sid, "agent-s")
        self.assertEqual(out["messages"][0]["text"], "snapped")
        self.assertEqual(out["info"]["ctx_tokens"], 42)

    def test_agent_context_no_such_agent(self):
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": self.sid, "agents": []}]}
        out = self.engine.agent_context(self.sid, "agent-nope")
        self.assertFalse(out["ok"])

    def test_agent_context_agent_present_but_transcript_gone(self):
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": self.sid, "agents": [
                {"agent_id": "agent-gone", "state": "running"}]}]}
        out = self.engine.agent_context(self.sid, "agent-gone")
        self.assertFalse(out["ok"])

    def test_agent_context_closed_claude_saved(self):
        subdir = os.path.join(self.project_dir, self.sid, "subagents")
        os.makedirs(subdir, exist_ok=True)
        with open(os.path.join(subdir, "agent-c.meta.json"), "w") as h:
            json.dump({"agentType": "grader", "description": "d"}, h)
        with open(os.path.join(subdir, "agent-c.jsonl"), "w") as h:
            h.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-15T00:00:00Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 1},
                            "content": [{"type": "text", "text": "graded"}]}}) + "\n")
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO session_runs(session_id, provider, transcript_path, "
            "closed_at, last_seen) VALUES(?,?,?,?,?)",
            (self.sid, "claude", self.transcript, int(time.time()),
             int(time.time())))
        db.commit()
        # remove the live registry so parent is not found in snapshot
        os.remove(os.path.join(self.sessions, f"{self.sid}.json"))
        self.engine.snapshot_cache = {"sessions": []}
        self.engine._closed_sessions_cache = None
        out = self.engine.agent_context(self.sid, "agent-c")
        self.assertTrue(out["ok"])
        self.assertTrue(out["closed"])

    def test_agent_context_codex_closed(self):
        self.engine.codex = SimpleNamespace(
            agent_context=lambda sid, aid: {"ok": True, "messages": [],
                                            "info": {"agent_id": aid}},
            resume_capability=lambda sid: (False, "closed"))
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO session_runs(session_id, provider, closed_at, last_seen) "
            "VALUES(?,?,?,?)", ("codex:cc", "codex", int(time.time()),
                                int(time.time())))
        db.commit()
        self.engine.snapshot_cache = {"sessions": []}
        self.engine._closed_sessions_cache = None
        out = self.engine.agent_context("codex:cc", "agent-z")
        self.assertTrue(out["ok"])

    def test_agent_context_codex_not_closed(self):
        self.engine.snapshot_cache = {"sessions": []}
        self.engine._closed_sessions_cache = None
        out = self.engine.agent_context("codex:unknown", "agent-z")
        self.assertFalse(out["ok"])
        self.assertIn("no such subagent", out["error"])

    def test_agent_context_codex_live(self):
        self.engine.codex = SimpleNamespace(
            agent_context=lambda sid, aid: {"ok": True, "messages": [],
                                            "info": {}})
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": "codex:live", "provider": "codex", "agents": [
                {"agent_id": "agent-q", "state": "running", "model": "gpt",
                 "agent_type": "sub"}]}]}
        with mock.patch.object(self.engine, "agent_status_line",
                               return_value={"ok": True}):
            out = self.engine.agent_context("codex:live", "agent-q")
        self.assertTrue(out["ok"])
        self.assertEqual(out["info"]["model"], "gpt")

    # -------------------------------------------------- file_selector_for_path
    def test_file_selector_claude_live(self):
        fpath = self._deliver_file("sel.txt")
        selector = self.engine.file_id(self.sid, fpath)
        self.assertEqual(self.engine.file_selector_for_path(self.sid, fpath),
                         selector)

    def test_file_selector_unknown_path(self):
        self.assertIsNone(self.engine.file_selector_for_path(
            self.sid, "/nowhere/x.txt"))

    def test_file_selector_codex_known(self):
        fpath = "/w/c.txt"
        self.engine.codex = SimpleNamespace(context=lambda sid: {
            "files": [{"path": fpath}]})
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": "codex:s", "provider": "codex"}]}
        selector = self.engine.file_id("codex:s", fpath)
        self.assertEqual(self.engine.file_selector_for_path("codex:s", fpath),
                         selector)

    def test_file_selector_codex_unknown(self):
        self.engine.snapshot_cache = {"sessions": []}
        self.engine._closed_sessions_cache = None
        self.assertIsNone(self.engine.file_selector_for_path("codex:x", "/w/a"))

    def test_file_selector_closed_claude(self):
        fpath = self._deliver_file("closed-sel.txt")
        self.engine.tail_for(self.transcript).poll()
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO session_runs(session_id, provider, transcript_path, "
            "closed_at, last_seen) VALUES(?,?,?,?,?)",
            (self.sid, "claude", self.transcript, int(time.time()),
             int(time.time())))
        db.commit()
        os.remove(os.path.join(self.sessions, f"{self.sid}.json"))
        self.engine._closed_sessions_cache = None
        selector = self.engine.file_id(self.sid, fpath)
        self.assertEqual(self.engine.file_selector_for_path(self.sid, fpath),
                         selector)

    # ---------------------------------------------------------- file_content
    def test_file_content_invalid_selector(self):
        ctype, data, err = self.engine.file_content(self.sid, "nope")
        self.assertIsNone(data)
        self.assertIn("invalid", err)

    def test_file_content_claude_live_text(self):
        fpath = self._deliver_file("fc.txt", "the body here")
        selector = self.engine.file_id(self.sid, fpath)
        ctype, data, err = self.engine.file_content(self.sid, selector)
        self.assertIsNone(err)
        self.assertEqual(data, b"the body here")
        self.assertIn("text/plain", ctype)

    def test_file_content_not_delivered(self):
        selector = "0" * 24
        ctype, data, err = self.engine.file_content(self.sid, selector)
        self.assertIn("not a file this session delivered", err)

    def test_file_content_too_large(self):
        fpath = self._deliver_file("big.bin", "x")
        with open(fpath, "wb") as h:
            h.write(b"\0" * 8_000_001)
        selector = self.engine.file_id(self.sid, fpath)
        ctype, data, err = self.engine.file_content(self.sid, selector)
        self.assertIn("too large", err)

    def test_file_content_unreadable(self):
        fpath = self._deliver_file("ur.txt", "data")
        selector = self.engine.file_id(self.sid, fpath)
        real_open = open

        def boom(path, *a, **k):
            if path == fpath:
                raise OSError("permission denied")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", side_effect=boom):
            ctype, data, err = self.engine.file_content(self.sid, selector)
        self.assertIn("unreadable", err)

    def test_file_content_backup_when_original_gone(self):
        fpath = self._deliver_file("gen.html", "<h1>hi</h1>")
        self.engine.tail_for(self.transcript).poll()
        # register a file-history backup, then delete the original
        hist = os.path.join(self.tmp.name, ".claude", "file-history", self.sid)
        os.makedirs(hist)
        with open(os.path.join(hist, "abcdef01@v9"), "w") as h:
            h.write("<h1>backup</h1>")
        self.engine.tails[self.transcript].file_backups[fpath] = "abcdef01@v9"
        os.remove(fpath)
        selector = self.engine.file_id(self.sid, fpath)
        ctype, data, err = self.engine.file_content(self.sid, selector)
        self.assertIsNone(err)
        self.assertEqual(data, b"<h1>backup</h1>")
        self.assertIn("text/plain", ctype)   # HTML stays text/plain

    def test_file_content_codex(self):
        fpath = "/w/codex-out.json"
        self.engine.codex = SimpleNamespace(
            context=lambda sid: {"files": [{"path": fpath}]},
            file_content=lambda sid, p: ("application/json", b"{}", None))
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": "codex:f", "provider": "codex"}]}
        selector = self.engine.file_id("codex:f", fpath)
        ctype, data, err = self.engine.file_content("codex:f", selector)
        self.assertEqual(data, b"{}")

    def test_file_content_codex_unknown_and_no_file(self):
        self.engine.snapshot_cache = {"sessions": []}
        self.engine._closed_sessions_cache = None
        _, _, err = self.engine.file_content("codex:x", "a" * 24)
        self.assertIn("unknown session", err)
        # known codex but selector matches no file
        self.engine.codex = SimpleNamespace(context=lambda sid: {"files": []})
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": "codex:g", "provider": "codex"}]}
        _, _, err2 = self.engine.file_content("codex:g", "b" * 24)
        self.assertIn("not a file this Codex thread", err2)

    def test_file_content_closed_claude(self):
        fpath = self._deliver_file("cc.txt", "closed body")
        self.engine.tail_for(self.transcript).poll()
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO session_runs(session_id, provider, transcript_path, "
            "closed_at, last_seen) VALUES(?,?,?,?,?)",
            (self.sid, "claude", self.transcript, int(time.time()),
             int(time.time())))
        db.commit()
        os.remove(os.path.join(self.sessions, f"{self.sid}.json"))
        self.engine._closed_sessions_cache = None
        selector = self.engine.file_id(self.sid, fpath)
        ctype, data, err = self.engine.file_content(self.sid, selector)
        self.assertEqual(data, b"closed body")

    def test_file_content_closed_unavailable(self):
        os.remove(os.path.join(self.sessions, f"{self.sid}.json"))
        self.engine._closed_sessions_cache = None
        _, _, err = self.engine.file_content(self.sid, "c" * 24)
        self.assertIn("unavailable", err)

    # ------------------------------------------------- final narrow branches
    def test_commands_duplicate_name_skipped(self):
        # a project command whose name duplicates a built-in exercises the
        # already-seen short-circuit in add()
        proj = os.path.join(self.cwd, ".claude", "commands")
        os.makedirs(proj)
        with open(os.path.join(proj, "status.md"), "w") as h:
            h.write("dup of built-in /status")
        out = self.engine.commands(self.sid)
        self.assertEqual(sum(1 for c in out["commands"] if c["name"] == "/status"),
                         1)

    def test_agent_context_closed_claude_agent_not_in_catalog(self):
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO session_runs(session_id, provider, transcript_path, "
            "closed_at, last_seen) VALUES(?,?,?,?,?)",
            (self.sid, "claude", self.transcript, int(time.time()),
             int(time.time())))
        db.commit()
        os.remove(os.path.join(self.sessions, f"{self.sid}.json"))
        self.engine.snapshot_cache = {"sessions": []}
        self.engine._closed_sessions_cache = None
        with mock.patch.object(self.engine, "_closed_claude_agents",
                               return_value=[]):
            out = self.engine.agent_context(self.sid, "agent-missing")
        self.assertIn("no such saved subagent", out["error"])

    def test_agent_context_closed_claude_transcript_vanished(self):
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO session_runs(session_id, provider, transcript_path, "
            "closed_at, last_seen) VALUES(?,?,?,?,?)",
            (self.sid, "claude", self.transcript, int(time.time()),
             int(time.time())))
        db.commit()
        os.remove(os.path.join(self.sessions, f"{self.sid}.json"))
        self.engine.snapshot_cache = {"sessions": []}
        self.engine._closed_sessions_cache = None
        # catalog names an agent whose jsonl is not actually on disk
        with mock.patch.object(self.engine, "_closed_claude_agents",
                               return_value=[{"agent_id": "agent-ghost"}]):
            out = self.engine.agent_context(self.sid, "agent-ghost")
        self.assertIn("saved subagent transcript is gone", out["error"])

    def test_agent_context_live_meta_unreadable(self):
        subdir = os.path.join(self.project_dir, self.sid, "subagents")
        os.makedirs(subdir, exist_ok=True)
        with open(os.path.join(subdir, "agent-m.jsonl"), "w") as h:
            h.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-15T00:00:00Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 1},
                            "content": [{"type": "text", "text": "x"}]}}) + "\n")
        os.makedirs(os.path.join(subdir, "agent-m.meta.json"))   # dir -> load fails
        self.engine.snapshot_cache = {"sessions": [
            {"session_id": self.sid, "agents": [
                {"agent_id": "agent-m", "state": "running"}]}]}
        out = self.engine.agent_context(self.sid, "agent-m")
        self.assertTrue(out["ok"])
        self.assertEqual(out["info"]["agent_type"], "?")   # meta defaulted

    def test_file_selector_snapshot_present(self):
        fpath = self._deliver_file("snap-sel.txt")
        self.engine.tail_for(self.transcript).poll()
        tail = self.engine.tails[self.transcript]
        self.engine._claude_context_snapshots[self.sid] = {
            "files": [dict(f) for f in tail.files],
            "messages": [dict(m) for m in tail.convo],
            "delivered_paths": dict(tail.delivered_paths)}
        selector = self.engine.file_id(self.sid, fpath)
        self.assertEqual(self.engine.file_selector_for_path(self.sid, fpath),
                         selector)

    def test_file_selector_codex_ledger_exception(self):
        self.engine.snapshot_cache = {"sessions": []}
        with mock.patch.object(self.engine, "ledger_reader",
                               side_effect=RuntimeError("db")):
            self.assertIsNone(
                self.engine.file_selector_for_path("codex:err", "/w/a"))

    def test_file_selector_closed_bad_transcript(self):
        db = self.engine.ensure_db()
        db.execute(
            "INSERT INTO session_runs(session_id, provider, transcript_path, "
            "closed_at, last_seen) VALUES(?,?,?,?,?)",
            (self.sid, "claude", "/not/under/projects.jsonl", int(time.time()),
             int(time.time())))
        db.commit()
        os.remove(os.path.join(self.sessions, f"{self.sid}.json"))
        self.engine._closed_sessions_cache = None
        self.assertIsNone(self.engine.file_selector_for_path(self.sid, "/w/a"))

    def test_file_content_snapshot_present(self):
        fpath = self._deliver_file("snap-fc.txt", "snap content")
        self.engine.tail_for(self.transcript).poll()
        tail = self.engine.tails[self.transcript]
        self.engine._claude_context_snapshots[self.sid] = {
            "files": [dict(f) for f in tail.files],
            "messages": [dict(m) for m in tail.convo],
            "file_backups": dict(tail.file_backups),
            "delivered_paths": dict(tail.delivered_paths)}
        selector = self.engine.file_id(self.sid, fpath)
        _, data, err = self.engine.file_content(self.sid, selector)
        self.assertEqual(data, b"snap content")

    def test_file_content_codex_ledger_exception(self):
        self.engine.snapshot_cache = {"sessions": []}
        with mock.patch.object(self.engine, "ledger_reader",
                               side_effect=RuntimeError("db")):
            _, _, err = self.engine.file_content("codex:err", "a" * 24)
        self.assertIn("unknown session", err)

    def test_file_content_delivered_but_gone_no_backup(self):
        fpath = self._deliver_file("vanish.txt", "temp")
        self.engine.tail_for(self.transcript).poll()
        os.remove(fpath)   # gone, and no file-history backup mapping
        selector = self.engine.file_id(self.sid, fpath)
        _, _, err = self.engine.file_content(self.sid, selector)
        self.assertIn("delivered file and Claude backup are gone", err)

    # ------------------------------------------------------ terminal screen
    def _screen_pane(self, lines="❯ hello\n"):
        """Put this session's tty on a fake tmux pane returning `lines`."""
        self.engine._tty_cache[os.getpid()] = "ttys009"
        return (mock.patch.object(self.engine, "_tmux_target_for_tty",
                                  return_value={"socket": "/s", "pane_id": "%1",
                                                "session": "fleet", "attached": True}),
                mock.patch.object(self.engine, "_tmux_capture",
                                  return_value={"ok": True,
                                                "lines": lines.splitlines(),
                                                "truncated": False}))

    def test_session_screen_reads_a_tmux_pane(self):
        target, capture = self._screen_pane()
        with target, capture:
            out = self.engine.session_screen(self.sid)
        self.assertTrue(out["ok"])
        self.assertEqual(out["lines"], ["❯ hello"])
        self.assertEqual(out["transport"], "tmux")
        self.assertEqual(out["session_id"], self.sid)
        self.assertFalse(out["truncated"])
        self.assertGreater(out["captured_at"], 0)

    def test_session_screen_needs_a_tmux_pane(self):
        self.engine._tty_cache[os.getpid()] = "ttys009"
        with mock.patch.object(self.engine, "_tmux_target_for_tty", return_value=None):
            out = self.engine.session_screen(self.sid)
        self.assertEqual(out["code"], "screen_unavailable")
        self.assertIn("not running in a tmux pane", out["error"])

    def test_session_screen_refuses_unreadable_sessions(self):
        self.assertIn("no session", self.engine.session_screen("")["error"])
        self.assertIn("Codex", self.engine.session_screen("codex:t1")["error"])
        self.assertIn("not live", self.engine.session_screen("missing")["error"])
        # a VS Code / headless session has no tty to read
        self.engine._tty_cache[os.getpid()] = ""
        self.assertIn("no terminal", self.engine.session_screen(self.sid)["error"])

    def test_session_screen_refuses_a_background_job(self):
        with mock.patch.object(self.engine, "_is_background_claude", return_value=True):
            out = self.engine.session_screen(self.sid)
        self.assertIn("background Claude job", out["error"])

    def test_session_screen_refuses_production_sessions_in_staging(self):
        self.engine.cfg["instance_mode"] = "staging"
        out = self.engine.session_screen(self.sid)
        self.assertIn("sessions it started", out["error"])

    def test_session_screen_surfaces_a_capture_failure(self):
        target, _ = self._screen_pane()
        with target, mock.patch.object(
                self.engine, "_tmux_capture",
                return_value={"ok": False, "error": "can't find pane"}):
            out = self.engine.session_screen(self.sid)
        self.assertEqual(out["code"], "screen_unavailable")
        self.assertIn("can't find pane", out["error"])


class ScreenPromptKindTest(EngineCovBase):
    """Direct screen evidence for the act() freshness gate (invariant 77)."""

    ASK = ["❯ 1. Red", "  2. Green", "Enter to select · ↑/↓ to navigate · Esc to cancel"]

    def _reg(self):
        return {"pid": 4242, "sessionId": self.sid}

    def test_no_tty_yields_no_evidence(self):
        self.assertIsNone(self.engine.screen_prompt_kind(self._reg(), ""))

    def test_a_background_job_is_never_read(self):
        """Its PTY bytes are readiness evidence that must not cross an API."""
        with mock.patch.object(self.engine, "_is_background_claude", return_value=True):
            self.assertIsNone(self.engine.screen_prompt_kind(self._reg(), "ttys1"))

    def test_a_session_outside_tmux_yields_no_evidence(self):
        with mock.patch.object(self.engine, "_is_background_claude", return_value=False), \
                mock.patch.object(self.engine, "_tmux_target_for_tty", return_value=None):
            self.assertIsNone(self.engine.screen_prompt_kind(self._reg(), "ttys1"))

    def test_an_unreadable_pane_yields_no_evidence(self):
        with mock.patch.object(self.engine, "_is_background_claude", return_value=False), \
                mock.patch.object(self.engine, "_tmux_target_for_tty", return_value="%1"), \
                mock.patch.object(self.engine, "_tmux_capture",
                                  return_value={"ok": False, "error": "gone"}):
            self.assertIsNone(self.engine.screen_prompt_kind(self._reg(), "ttys1"))

    def test_an_unrecognized_screen_is_no_evidence_not_no_prompt(self):
        with mock.patch.object(self.engine, "_is_background_claude", return_value=False), \
                mock.patch.object(self.engine, "_tmux_target_for_tty", return_value="%1"), \
                mock.patch.object(self.engine, "_tmux_capture",
                                  return_value={"ok": True, "lines": ["… working"],
                                                "truncated": False}):
            self.assertIsNone(self.engine.screen_prompt_kind(self._reg(), "ttys1"))

    def test_a_visible_question_is_reported(self):
        with mock.patch.object(self.engine, "_is_background_claude", return_value=False), \
                mock.patch.object(self.engine, "_tmux_target_for_tty",
                                  return_value="%1") as target, \
                mock.patch.object(self.engine, "_tmux_capture",
                                  return_value={"ok": True, "lines": self.ASK,
                                                "truncated": False}):
            self.assertEqual(self.engine.screen_prompt_kind(self._reg(), "ttys1"), "question")
        target.assert_called_once_with("/dev/ttys1")

    def test_an_absolute_tty_is_not_double_prefixed(self):
        with mock.patch.object(self.engine, "_is_background_claude", return_value=False), \
                mock.patch.object(self.engine, "_tmux_target_for_tty",
                                  return_value=None) as target:
            self.engine.screen_prompt_kind(self._reg(), "/dev/ttys1")
        target.assert_called_once_with("/dev/ttys1")


class RequestIdentityTest(EngineCovBase):
    """Server-owned prompt identity and the answered fence (invariant 75)."""

    def _question(self, nonce, header="Pick", label="one"):
        return {"kind": "question", "nonce": nonce,
                "questions": [{"question": header,
                               "options": [{"label": label}]}]}

    def _permission(self, nonce, tool="Bash", summary="npm test"):
        return {"kind": "permission", "nonce": nonce, "tool": tool,
                "input_summary": summary}

    def test_one_prompt_keeps_one_id_across_repeated_scans(self):
        first = self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 100.0)
        again = self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 102.0)
        self.assertTrue(first["request_id"].startswith("req-"))
        self.assertEqual(first["request_id"], again["request_id"])

    def test_a_permission_survives_the_hook_to_transcript_nonce_flip(self):
        """The capture expires at 15s and the transcript fallback takes over with
        a different nonce. Same prompt, so the same identity — otherwise an
        answered permission reappears under a second id."""
        hook = self.engine._apply_request_identity(
            self.sid, self._permission("hook-9", tool="requested tool",
                                       summary="Claude needs permission"), 100.0)
        transcript = self.engine._apply_request_identity(
            self.sid, self._permission("toolu_abc"), 120.0)
        self.assertEqual(hook["request_id"], transcript["request_id"])

    def test_a_different_question_from_the_same_source_mints_a_new_id(self):
        first = self.engine._apply_request_identity(
            self.sid, self._question("hook-1", header="Ship it?"), 100.0)
        second = self.engine._apply_request_identity(
            self.sid, self._question("hook-2", header="Delete it?"), 101.0)
        self.assertNotEqual(first["request_id"], second["request_id"])

    def test_a_second_permission_from_the_same_source_mints_a_new_id(self):
        first = self.engine._apply_request_identity(
            self.sid, self._permission("toolu_1", summary="npm test"), 100.0)
        second = self.engine._apply_request_identity(
            self.sid, self._permission("toolu_2", summary="rm -rf /"), 101.0)
        self.assertNotEqual(first["request_id"], second["request_id"])

    def test_a_resolved_prompt_retires_its_identity(self):
        first = self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 100.0)
        self.assertIsNone(self.engine._apply_request_identity(self.sid, None, 101.0))
        # the very same question asked again is a NEW request
        reasked = self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 102.0)
        self.assertNotEqual(first["request_id"], reasked["request_id"])

    def test_an_answered_prompt_stops_rendering_until_the_provider_catches_up(self):
        pending = self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 100.0)
        self.assertTrue(self.engine._record_answered_request(self.sid, "hook-1"))
        self.assertTrue(self.engine._request_answered(self.sid, "hook-1"))
        self.assertIsNone(self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 101.0))
        # …and the fence follows the identity across a nonce flip
        self.assertIsNone(self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 102.0))
        self.assertEqual(pending["request_id"],
                         self.engine._request_ids[self.sid]["request_id"])

    def test_a_new_question_is_never_fenced_by_the_previous_answer(self):
        self.engine._apply_request_identity(
            self.sid, self._question("hook-1", header="Ship it?"), 100.0)
        self.engine._record_answered_request(self.sid, "hook-1")
        fresh = self.engine._apply_request_identity(
            self.sid, self._question("hook-2", header="Delete it?"), 101.0)
        self.assertIsNotNone(fresh)

    def test_the_fence_expires_so_an_unresolved_prompt_returns(self):
        self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 100.0)
        self.engine._record_answered_request(self.sid, "hook-1")
        with mock.patch.object(self.engine, "ANSWERED_FENCE_SECONDS", 0):
            back = self.engine._apply_request_identity(
                self.sid, self._question("hook-1"), time.time() + 5)
        self.assertIsNotNone(back)
        self.assertFalse(self.engine._request_answered(self.sid, "hook-1"))

    def test_an_expired_fence_stops_refusing_answers(self):
        """act() must not refuse forever a prompt the provider never resolved."""
        self.engine._apply_request_identity(
            self.sid, self._question("hook-1"), 100.0)
        self.engine._record_answered_request(self.sid, "hook-1")
        self.assertTrue(self.engine._request_answered(self.sid, "hook-1"))
        self.engine._answered_requests[self.sid]["at"] -= (
            self.engine.ANSWERED_FENCE_SECONDS + 5)
        self.assertFalse(self.engine._request_answered(self.sid, "hook-1"))
        self.assertNotIn(self.sid, self.engine._answered_requests)

    def test_an_unseen_nonce_is_never_fenced(self):
        """Without a record there is no identity to fence, and refusing an answer
        on a guess would strand a genuinely open prompt."""
        self.assertFalse(self.engine._record_answered_request(self.sid, "hook-9"))
        self.assertFalse(self.engine._request_answered(self.sid, "hook-9"))
        self.engine._apply_request_identity(self.sid, self._question("hook-1"), 100.0)
        self.assertFalse(self.engine._request_answered(self.sid, "hook-2"))

    def test_the_fence_is_dropped_when_its_prompt_is_replaced(self):
        self.engine._apply_request_identity(
            self.sid, self._question("hook-1", header="Ship it?"), 100.0)
        self.engine._record_answered_request(self.sid, "hook-1")
        self.engine._apply_request_identity(
            self.sid, self._question("hook-2", header="Delete it?"), 101.0)
        self.assertFalse(self.engine._request_answered(self.sid, "hook-1"))

    def test_identity_records_stay_bounded(self):
        self.engine.REQUEST_IDENTITY_LIMIT = 3
        for index in range(5):
            self.engine._apply_request_identity(
                f"s{index}", self._question(f"hook-{index}"), 100.0)
        self.assertLessEqual(len(self.engine._request_ids),
                             self.engine.REQUEST_IDENTITY_LIMIT)

    def test_a_malformed_question_shape_still_yields_a_signature(self):
        pending = {"kind": "question", "nonce": "hook-1",
                   "questions": ["raw", {"question": "ok", "options": "nope"}]}
        self.assertIsNotNone(
            self.engine._apply_request_identity(self.sid, pending, 100.0))

    def test_nonce_history_per_prompt_is_capped(self):
        for index in range(20):
            self.engine._apply_request_identity(
                self.sid, self._permission(f"toolu_{index}", summary="same"), 100.0)
        self.assertLessEqual(len(self.engine._request_ids[self.sid]["nonces"]), 8)


if __name__ == "__main__":
    unittest.main()
