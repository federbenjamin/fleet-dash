"""Coverage for fleetdash.engine_ledger: ledger preparation, path validation,
edge JSON reads, history metadata, closed-session context, and handoff preview."""
import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from fleetdash import engine_ledger as ledger_module
from fleetdash import paths as engine_paths
from fleetdash.engine import Engine
from tests.test_cov_common_ops import EngineFixture

UUID = "11111111-2222-3333-4444-555555555555"


class PrepareLedgerTests(unittest.TestCase):
    def test_missing_is_ok(self):
        self.assertEqual(Engine._prepare_ledger("/no/such/ledger.db"),
                         {"ok": True, "recovered": False})

    def test_valid_db(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.db")
            sqlite3.connect(path).close()
            self.assertEqual(Engine._prepare_ledger(path),
                             {"ok": True, "recovered": False})

    def test_locked_db_defers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.db")
            holder = sqlite3.connect(path)
            holder.execute("CREATE TABLE t(x)")
            holder.commit()
            holder.execute("BEGIN EXCLUSIVE")
            try:
                with mock.patch.object(ledger_module.sqlite3, "connect") as conn:
                    conn.return_value.execute.side_effect = \
                        sqlite3.OperationalError("database is locked")
                    out = Engine._prepare_ledger(path)
                self.assertTrue(out.get("check_deferred"))
            finally:
                holder.close()

    def test_corrupt_db_is_quarantined(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.db")
            with open(path, "wb") as handle:
                handle.write(b"not a sqlite database at all")
            out = Engine._prepare_ledger(path)
            self.assertTrue(out["recovered"])
            self.assertIn("quarantine", out)

    def test_quarantine_failure_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.db")
            with open(path, "wb") as handle:
                handle.write(b"corrupt bytes")
            with mock.patch.object(ledger_module.os, "replace",
                                   side_effect=OSError("cannot move")):
                with self.assertRaises(RuntimeError):
                    Engine._prepare_ledger(path)


class SafePathTests(EngineFixture):
    def _uuid_transcript(self):
        path = os.path.join(os.path.dirname(self.transcript), f"{UUID}.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "user"}) + "\n")
        return path

    def test_safe_transcript_validation(self):
        self.assertIsNone(self.engine._safe_claude_transcript("not-a-uuid", "/x"))
        self.assertIsNone(self.engine._safe_claude_transcript(UUID, ""))
        # Right shape but wrong location.
        self.assertIsNone(self.engine._safe_claude_transcript(
            UUID, os.path.join(self.tmp.name, f"{UUID}.jsonl")))
        # A real UUID transcript under projects resolves.
        path = self._uuid_transcript()
        self.assertEqual(self.engine._safe_claude_transcript(UUID, path),
                         os.path.realpath(path))

    def test_safe_reopen_cwd(self):
        self.assertIsNone(self.engine._safe_reopen_cwd(""))
        self.assertIsNone(self.engine._safe_reopen_cwd("/no/such/dir"))
        self.assertIsNone(self.engine._safe_reopen_cwd("/"))
        self.assertEqual(self.engine._safe_reopen_cwd(self.cwd),
                         os.path.realpath(self.cwd))


class EdgeJsonTests(EngineFixture):
    def test_large_file_reads_head_and_tail(self):
        path = os.path.join(self.tmp.name, "big.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"head": True, "i": 0}) + "\n")
            handle.write(("x" * 5_000_000) + "\n")  # oversized line -> skipped
            handle.write("{ not json\n")             # bad json -> skipped
            for i in range(1, 6000):
                handle.write(json.dumps({"mid": i}) + "\n")
            handle.write(json.dumps({"tail": True}) + "\n")
        objects = self.engine._edge_json_objects(path)
        self.assertTrue(any(o.get("head") for o in objects))
        self.assertTrue(any(o.get("tail") for o in objects))

    def test_small_file_reads_whole(self):
        path = os.path.join(self.tmp.name, "small.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"only": 1}) + "\n")
        objects = self.engine._edge_json_objects(path)
        self.assertEqual(objects, [{"only": 1}])


class HistoryTitlesTests(EngineFixture):
    def test_reads_valid_rows_only(self):
        with open(self.claude_history, "w") as handle:
            handle.write(json.dumps({"sessionId": UUID, "display": "My prompt",
                                     "project": self.cwd,
                                     "timestamp": 1_000_000}) + "\n")
            handle.write("{ not json\n")
            handle.write(json.dumps({"sessionId": "not-a-uuid"}) + "\n")
        titles = self.engine._history_titles()
        self.assertIn(UUID, titles)
        self.assertEqual(titles[UUID]["title"], "My prompt")

    def test_missing_history_file(self):
        os.unlink(self.claude_history) if os.path.exists(self.claude_history) else None
        self.assertEqual(self.engine._history_titles(), {})


class TranscriptMetadataTests(EngineFixture):
    def test_extracts_titles_and_first_prompt(self):
        path = os.path.join(os.path.dirname(self.transcript), f"{UUID}.jsonl")
        rows = [
            {"type": "custom-title", "customTitle": "Custom",
             "timestamp": "2026-07-15T00:00:00Z", "cwd": self.cwd},
            {"type": "ai-title", "aiTitle": "AI title"},
            {"type": "user", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "the very first prompt"}]}},
            {"type": "assistant",
             "message": {"role": "assistant", "model": "claude-opus"}},
        ]
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        meta = self.engine._claude_transcript_metadata(path, {"title": "hist"})
        self.assertEqual(meta["title"], "Custom")
        self.assertEqual(meta["model"], "claude-opus")

    def test_backfill_and_history_snapshot(self):
        # backfill indexes only UUID-named top-level transcripts.
        path = os.path.join(os.path.dirname(self.transcript), f"{UUID}.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "user", "cwd": self.cwd,
                "timestamp": "2026-07-15T00:00:00Z",
                "message": {"role": "user", "content": "hi"}}) + "\n")
        imported = self.engine.backfill_claude_history()
        self.assertGreaterEqual(imported, 1)


class DrainStatsTests(EngineFixture):
    def test_drain_stats_clean_is_noop(self):
        tail = self.engine.tail_for(self.transcript)
        tail.poll()
        tail.stats_dirty = set()
        self.engine.drain_stats(tail)  # early return, no db work

    def test_drain_stats_exception_is_swallowed(self):
        tail = self.engine.tail_for(self.transcript)
        tail.poll()
        tail.stats_dirty = {("2026-07-15", "kind", "name")}
        tail.stats = {("2026-07-15", "kind", "name"): (1, 1, 1, 1, 1, 1)}
        with mock.patch.object(self.engine, "ensure_db",
                               side_effect=RuntimeError("db gone")):
            self.engine.drain_stats(tail)  # must not raise


class RecordSessionsTests(EngineFixture):
    def test_oversized_status_line_is_dropped(self):
        session = {"session_id": "s1", "name": "n", "project": "p", "cwd": self.cwd,
                   "branch": "b", "model": "m", "cost": 1.0, "agent_cost": 0.0,
                   "agents_total": 0, "bridge_url": None, "title": "t",
                   "provider": "claude",
                   "status_line": {"blob": "x" * 200_000}}
        self.engine.record_sessions([session], time.time())
        db = self.engine.ledger_reader()
        row = db.execute("SELECT status_line_json FROM session_runs "
                         "WHERE session_id='s1'").fetchone()
        db.close()
        self.assertIsNone(row[0])

    def test_record_sessions_exception_swallowed(self):
        with mock.patch.object(self.engine, "ensure_db",
                               side_effect=RuntimeError("no db")):
            self.engine.record_sessions([{"session_id": "s"}], time.time())

    def test_unchanged_signature_and_departure(self):
        session = {"session_id": "s2", "name": "n", "project": "p",
                   "cwd": self.cwd, "branch": "b", "model": "m", "cost": 1.0,
                   "agent_cost": 0.0, "agents_total": 0, "bridge_url": None,
                   "title": "t", "provider": "claude", "status_line": {}}
        now = time.time()
        self.engine.record_sessions([session], now)
        # Same signature within 30s -> skipped.
        self.engine.record_sessions([session], now + 1)
        # Session departs -> its closed_at is stamped.
        self.engine.record_sessions([], now + 2)
        db = self.engine.ledger_reader()
        row = db.execute("SELECT closed_at FROM session_runs "
                         "WHERE session_id='s2'").fetchone()
        db.close()
        self.assertIsNotNone(row[0])


class StateHistoryTests(EngineFixture):
    def test_invalid_inputs(self):
        self.assertIn("invalid session id",
                      self.engine.state_history("\x01bad")["error"])
        self.assertIn("invalid evidence",
                      self.engine.state_history("s", cursor="x")["error"])
        self.assertIn("1–100",
                      self.engine.state_history("s", limit=500)["error"])

    def test_history_reads_recorded_events(self):
        session = {"session_id": "sid-a", "provider": "claude", "state": "idle",
                   "normalized_state": "idle", "ui_group": "available",
                   "reason_label": "Available", "convo_v": "1",
                   "state_evidence": [{"kind": "k", "label": "L", "value": "v",
                                       "confidence": "confirmed"}],
                   "suppressed_rules": ["r1"]}
        self.engine.record_state_events([session], time.time())
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [session]}
        out = self.engine.state_history("sid-a")
        self.assertTrue(out["ok"])
        self.assertTrue(out["events"])
        self.assertEqual(out["current"]["session_id"], "sid-a")

    def test_history_db_exception(self):
        with mock.patch.object(ledger_module.sqlite3, "connect",
                               side_effect=sqlite3.OperationalError("x")):
            out = self.engine.state_history("sid-a")
        self.assertIn("unavailable", out["error"])


class RecordStateEventsTests(EngineFixture):
    def test_skips_already_closed_and_swallows_errors(self):
        closed = {"session_id": "sc", "normalized_state": "closed",
                  "provider": "claude"}
        self.engine.record_state_events([closed], time.time())
        # Second pass with the same closed session is skipped (no new row).
        self.engine.record_state_events([closed], time.time())
        with mock.patch.object(self.engine, "ensure_db",
                               side_effect=RuntimeError("boom")):
            self.engine._state_event_signatures = None
            self.engine.record_state_events([closed], time.time())

    def test_skips_session_without_id(self):
        self.engine.record_state_events([{"provider": "claude"}], time.time())

    def test_unchanged_signature_is_skipped(self):
        session = {"session_id": "sid-x", "provider": "claude", "state": "idle",
                   "normalized_state": "idle", "convo_v": "1"}
        now = time.time()
        self.engine.record_state_events([session], now)
        # Same signature -> no new row inserted.
        self.engine.record_state_events([session], now + 1)


class ClosedSessionTests(EngineFixture):
    def _closed_uuid_session(self):
        path = os.path.join(os.path.dirname(self.transcript), f"{UUID}.jsonl")
        rows = [
            {"type": "user", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "end_turn", "usage": {"input_tokens": 1},
                         "content": [{"type": "text", "text": "hi"}]}},
        ]
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        db = self.engine.ensure_db()
        db.execute("INSERT INTO session_runs(session_id,provider,closed_at,cwd,"
                   "title,transcript_path) VALUES(?,?,?,?,?,?)",
                   (UUID, "claude", int(time.time()), self.cwd, "T", path))
        db.commit()
        self.engine._closed_sessions_cache = None
        return path

    def test_closed_sessions_lists_reopenable(self):
        self._closed_uuid_session()
        rows = self.engine.closed_sessions()
        row = next(r for r in rows if r["session_id"] == UUID)
        self.assertTrue(row["can_reopen"])

    def test_closed_context_unknown(self):
        self.assertIn("unknown session",
                      self.engine.closed_context("never")["error"])

    def test_closed_context_reads_transcript(self):
        self._closed_uuid_session()
        out = self.engine.closed_context(UUID)
        self.assertTrue(out["ok"])
        self.assertTrue(out["closed"])
        self.assertEqual(out["messages"][-1]["text"], "hi")

    def test_closed_context_freezes_status_line(self):
        path = self._closed_uuid_session()
        db = self.engine.ensure_db()
        db.execute("UPDATE session_runs SET status_line_json=? WHERE session_id=?",
                   (json.dumps({"model": "m", "cost": 1}), UUID))
        db.commit()
        out = self.engine.closed_context(UUID)
        self.assertTrue(out["info"]["status_line"]["frozen"])

    def test_closed_sessions_uses_cache(self):
        self._closed_uuid_session()
        first = self.engine.closed_sessions()
        second = self.engine.closed_sessions()
        self.assertEqual([r["session_id"] for r in first],
                         [r["session_id"] for r in second])

    def test_closed_sessions_bad_status_line_json(self):
        path = self._closed_uuid_session()
        db = self.engine.ensure_db()
        db.execute("UPDATE session_runs SET status_line_json=? WHERE session_id=?",
                   ("{ not json", UUID))
        db.commit()
        self.engine._closed_sessions_cache = None
        rows = self.engine.closed_sessions()
        row = next(r for r in rows if r["session_id"] == UUID)
        self.assertNotIn("status_line", row)

    def test_closed_context_codex_delegates(self):
        db = self.engine.ensure_db()
        db.execute("INSERT INTO session_runs(session_id,provider,closed_at,cwd) "
                   "VALUES('codex:c1','codex',?, ?)",
                   (int(time.time()), self.cwd))
        db.commit()
        out = self.engine.closed_context("codex:c1")
        self.assertTrue(out["ok"])
        self.assertTrue(out["closed"])

    def test_closed_resume_capability_variants(self):
        # Codex delegates to the adapter.
        self.assertEqual(self.engine.closed_resume_capability("codex:x"),
                         (True, None))
        # Unknown claude row (no transcript).
        allowed, reason = self.engine.closed_resume_capability(
            {"session_id": "gone", "provider": "claude"})
        self.assertFalse(allowed)
        self.assertIn("transcript", reason)

    def test_closed_resume_bad_cwd(self):
        path = os.path.join(os.path.dirname(self.transcript), f"{UUID}.jsonl")
        with open(path, "w") as handle:
            handle.write("{}\n")
        allowed, reason = self.engine.closed_resume_capability(
            {"session_id": UUID, "provider": "claude",
             "transcript_path": path, "cwd": "/no/such/dir"})
        self.assertFalse(allowed)
        self.assertIn("working directory", reason)

    def test_closed_resume_by_sid_lookup(self):
        # row is None -> looks up in closed_sessions.
        allowed, reason = self.engine.closed_resume_capability("unknown-sid")
        self.assertFalse(allowed)

    def test_closed_sessions_exception_returns_empty(self):
        with mock.patch.object(self.engine, "ledger_reader",
                               side_effect=RuntimeError("db down")):
            self.assertEqual(self.engine.closed_sessions(), [])


class ClosedAgentsTests(EngineFixture):
    def test_closed_agents_reads_subagents(self):
        subdir = os.path.join(os.path.dirname(self.transcript), "same", "subagents")
        os.makedirs(subdir)
        aid = "agent-child1"
        with open(os.path.join(subdir, aid + ".meta.json"), "w") as handle:
            json.dump({"agentType": "quick", "description": "d"}, handle)
        with open(os.path.join(subdir, aid + ".jsonl"), "w") as handle:
            handle.write(json.dumps({"type": "assistant",
                "timestamp": "2026-07-16T00:00:00Z", "message": {
                    "role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "end_turn", "usage": {"input_tokens": 1},
                    "content": [{"type": "text", "text": "done"}]}}) + "\n")
        agents = self.engine._closed_claude_agents("same", self.transcript)
        self.assertEqual(agents[0]["agent_id"], aid)
        self.assertEqual(agents[0]["state"], "done")

    def test_no_subagents_dir(self):
        self.assertEqual(
            self.engine._closed_claude_agents("same", self.transcript), [])

    def test_closed_agents_skips_bad_meta_and_missing_transcript(self):
        subdir = os.path.join(os.path.dirname(self.transcript), "same", "subagents")
        os.makedirs(subdir)
        # Glob-matching id that fails the strict regex -> skipped.
        with open(os.path.join(subdir, "agent-bad!char.meta.json"), "w") as handle:
            handle.write("{}")
        # Valid id but no transcript file -> skipped.
        with open(os.path.join(subdir, "agent-notrans.meta.json"), "w") as handle:
            handle.write("{}")
        # Valid id, unreadable meta json, but a real transcript -> meta {}.
        aid = "agent-badmeta"
        with open(os.path.join(subdir, aid + ".meta.json"), "w") as handle:
            handle.write("{ not json")
        with open(os.path.join(subdir, aid + ".jsonl"), "w") as handle:
            handle.write(json.dumps({"type": "assistant",
                "timestamp": "2026-07-16T00:00:00Z", "message": {
                    "role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "end_turn", "content": [
                        {"type": "text", "text": "x"}]}}) + "\n")
        agents = self.engine._closed_claude_agents("same", self.transcript,
                                                   lock_held=True)
        ids = [a["agent_id"] for a in agents]
        self.assertEqual(ids, [aid])


class LedgerFinalizeTests(EngineFixture):
    def test_finalize_writes_agent_row(self):
        subdir = os.path.join(os.path.dirname(self.transcript), "same", "subagents")
        os.makedirs(subdir)
        aid = "agent-fin"
        jl = os.path.join(subdir, aid + ".jsonl")
        with open(jl, "w") as handle:
            handle.write(json.dumps({"type": "assistant",
                "timestamp": "2026-07-16T00:00:00Z", "message": {
                    "role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "end_turn", "usage": {"input_tokens": 5},
                    "content": [{"type": "text", "text": "done"}]}}) + "\n")
        tail = self.engine.tail_for(jl)
        tail.poll()
        self.engine.ledger_finalize(subdir, aid, {"agentType": "quick",
                                                  "description": "d"}, tail)
        db = self.engine.ledger_reader()
        row = db.execute("SELECT agent_type FROM agent_runs WHERE agent_id=?",
                         (aid,)).fetchone()
        db.close()
        self.assertEqual(row[0], "quick")


class HandoffLinkTests(EngineFixture):
    def test_record_read_and_map(self):
        self.engine._record_handoff_link(
            "src", "claude", "dst", "codex", "delivered", "hash", error=None)
        link = self.engine._handoff_link("src", "dst")
        self.assertEqual(link["status"], "delivered")
        self.assertIsNone(self.engine._handoff_link("src", "missing"))
        mapping = self.engine.handoff_link_map(["src", "dst"])
        self.assertIn("src", mapping)
        self.assertIn("dst", mapping)
        # Cached second read.
        self.assertEqual(self.engine.handoff_link_map(["src"]),
                         {"src": mapping["src"]})

    def test_map_empty(self):
        self.assertEqual(self.engine.handoff_link_map([]), {})

    def test_artifact_section(self):
        self.assertIn("(none)", self.engine._handoff_artifact_section([]))
        section = self.engine._handoff_artifact_section(
            [{"path": "/a/b.md", "caption": "cap"}])
        self.assertIn("/a/b.md", section)


class HandoffPreviewTests(EngineFixture):
    def _seed(self, **extra):
        session = {"session_id": "same", "provider": "claude", "cwd": self.cwd,
                   "title": "Src", "project": "repo", "branch": "feature",
                   "model": "claude-sonnet", "effort": "high"}
        session.update(extra)
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [session], "closed": []}

    def test_bad_provider(self):
        self.assertIn("provider must be",
                      self.engine.handoff_preview("same", "gemini")["error"])

    def test_missing_source(self):
        self.assertIn("unavailable",
                      self.engine.handoff_preview("nope", "codex")["error"])

    def test_preview_for_claude(self):
        self.engine.scan()
        self._seed(pending={"kind": "question", "questions": [
            {"question": "Which?"}]}, reply_requested=True, error="an error")
        out = self.engine.handoff_preview("same", "claude")
        self.assertTrue(out["ok"])
        self.assertIn("Continue this work", out["preview"])
        self.assertEqual(out["target_provider"], "claude")

    def test_preview_with_artifacts_and_search_fallback(self):
        artifact = os.path.join(self.cwd, "plan.md")
        with open(artifact, "w") as handle:
            handle.write("plan")
        self._seed()

        class Search:
            def handoff_material(self, sid, n):
                raise RuntimeError("index down")
        self.engine.search = Search()
        with mock.patch.object(self.engine, "session_context", return_value={
                "ok": True,
                "messages": [{"role": "user", "text": "the objective"},
                             {"role": "assistant", "text": "working"}],
                "files": [{"path": artifact, "caption": "the plan"}]}):
            out = self.engine.handoff_preview("same", "claude")
        self.assertTrue(out["ok"])
        self.assertEqual(out["artifacts"][0]["name"], "plan.md")
        self.assertIn("the objective", out["preview"])

    def test_preview_uses_indexed_material_and_permission_pending(self):
        outside = os.path.join(self.tmp.name, "..", "outside.md")
        self._seed(pending={"kind": "permission", "tool": "Bash",
                            "input_summary": "run tests"})

        class Search:
            def handoff_material(self, sid, n):
                return {"ok": True,
                        "recent": [{"role": "assistant", "text": "indexed reply"},
                                   {"role": "event", "text": "skip me"}],
                        "first_user": "indexed objective",
                        "todos": ["finish it"],
                        "artifacts": [{"path": outside, "caption": "outside home"}]}
        self.engine.search = Search()
        with mock.patch.object(self.engine, "session_context",
                               return_value={"ok": True, "messages": [],
                                             "files": []}):
            out = self.engine.handoff_preview("same", "codex")
        self.assertTrue(out["ok"])
        self.assertIn("indexed objective", out["preview"])
        self.assertIn("finish it", out["preview"])
        # An artifact outside HOME is filtered out.
        self.assertEqual(out["artifacts"], [])

    def test_preview_effort_not_in_catalog_is_cleared(self):
        self.engine.scan()
        self._seed(model="claude-sonnet", effort="weird-effort")
        out = self.engine.handoff_preview("same", "claude")
        self.assertEqual(out["defaults"]["effort"], "")
        self.assertEqual(out["defaults"]["model"], "sonnet")

    def test_preview_for_codex_from_closed(self):
        db = self.engine.ensure_db()
        db.execute("INSERT INTO session_runs(session_id,provider,closed_at,cwd,"
                   "title,transcript_path) VALUES('same','claude',?,?,?,?)",
                   (int(time.time()), self.cwd, "T", self.transcript))
        db.commit()
        self.engine._closed_sessions_cache = None
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [], "closed": [
                {"session_id": "same", "provider": "claude", "cwd": self.cwd,
                 "closed_at": time.time(), "normalized_state": "closed",
                 "title": "T"}]}
        out = self.engine.handoff_preview("same", "codex")
        self.assertTrue(out["ok"])
        self.assertEqual(out["defaults"]["mode"], "plan")


class ProjectFileIdsTests(EngineFixture):
    def test_projects_files_to_opaque_ids(self):
        context = {"files": [{"path": "/a/b.md", "caption": "c"}],
                   "messages": [{"role": "tool",
                                 "files": [{"path": "/a/d.md"}]}]}
        out = self.engine._project_file_ids("same", context)
        self.assertNotIn("path", out["files"][0])
        self.assertIn("file_id", out["files"][0])
        self.assertIn("file_id", out["messages"][0]["files"][0])

    def test_non_dict_passthrough(self):
        self.assertEqual(self.engine._project_file_ids("same", None), None)


class TrustedDirTests(EngineFixture):
    def test_trusted_dirs_reads_claude_json(self):
        with open(self.claude_account, "w") as handle:
            json.dump({"projects": {self.cwd: {"hasTrustDialogAccepted": True},
                                    "/other": {"hasTrustDialogAccepted": False}}},
                      handle)
        trusted = self.engine.trusted_dirs()
        self.assertIn(self.cwd, trusted)

    def test_trusted_dirs_missing_file(self):
        if os.path.exists(self.claude_account):
            os.unlink(self.claude_account)
        self.assertEqual(self.engine.trusted_dirs(), set())

    def test_is_trusted_walks_ancestors(self):
        real_cwd = os.path.realpath(self.cwd)
        with open(self.claude_account, "w") as handle:
            json.dump({"projects": {real_cwd: {"hasTrustDialogAccepted": True}}},
                      handle)
        child = os.path.join(self.cwd, "sub", "deep")
        os.makedirs(child)
        self.assertTrue(self.engine.is_trusted(child))
        self.assertFalse(self.engine.is_trusted(self.tmp.name))

    def test_recent_dirs(self):
        self.engine.scan()
        dirs = self.engine.recent_dirs()
        self.assertTrue(any(d["path"] == self.cwd for d in dirs))

    def test_recent_dirs_ledger_exception(self):
        with mock.patch.object(self.engine, "ledger_reader",
                               side_effect=RuntimeError("db down")):
            dirs = self.engine.recent_dirs()
        self.assertIsInstance(dirs, list)


if __name__ == "__main__":
    unittest.main()
