"""Line-coverage tests for fleetdash.search_index.

These target parser edge cases (malformed/oversized/unknown records, every row
shape), per-source error state, the query/context/handoff readers, rebuild, and
the worker/CLI lifecycle. Companion to tests.test_search_index, which owns the
happy-path integration flows; this file drives the remaining branches directly.

Deterministic, stdlib-only, no network, no real ~/.claude access: every index is
built against explicit temp roots passed to the SearchIndex constructor.
"""
import argparse
import os
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from fleetdash import search_index
from fleetdash.search_index import (
    SearchIndex,
    _candidate_paths,
    _claude_row,
    _codex_row,
    _content_text,
    _epoch,
    _json_text,
    _main,
    _parent_alive,
    _result_text,
    _worker,
)


CLAUDE_SID = "11111111-2222-3333-4444-555555555555"
CODEX_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class PureHelperTest(unittest.TestCase):
    """Module-level parsing helpers, exercised without any database."""

    def test_epoch_variants(self):
        # Millisecond epoch (> 10_000_000_000) is divided by 1000 (line 40).
        self.assertEqual(_epoch(20_000_000_000), 20_000_000.0)
        # Plain second epoch stays as float.
        self.assertEqual(_epoch(1000), 1000.0)
        # ISO string parses.
        self.assertAlmostEqual(_epoch("1970-01-01T00:00:01Z"), 1.0)
        # Unparseable string -> None (lines 45-46).
        self.assertIsNone(_epoch("not-a-timestamp"))
        # Neither number nor string -> None (line 47).
        self.assertIsNone(_epoch(None))
        self.assertIsNone(_epoch([1, 2]))

    def test_content_text(self):
        self.assertEqual(_content_text("plain"), "plain")           # line 57
        self.assertEqual(_content_text(42), "")                       # line 59
        # Non-dict block is skipped (line 63); dict text blocks join.
        self.assertEqual(
            _content_text([7, {"type": "text", "text": "a"},
                           {"type": "output_text", "text": "b"}]),
            "a\n\nb")

    def test_result_text(self):
        self.assertEqual(_result_text("raw"), "raw")                 # line 71-72
        self.assertEqual(_result_text(5), "")                         # line 73-74
        # Non-dict skipped; "text" then "content" fields both collected.
        self.assertEqual(
            _result_text([9, {"text": "one"}, {"content": "two"},
                          {"other": "ignored"}]),
            "one\ntwo")

    def test_json_text_falls_back_to_str(self):
        self.assertEqual(_json_text({"b": 1, "a": 2}), '{"a": 2, "b": 1}')
        # Unserializable object hits the except branch (lines 88-89).
        self.assertTrue(_json_text(object.__new__(object)))
        self.assertTrue(_json_text({1, 2, 3}))

    def test_candidate_paths_walks_all_shapes(self):
        found = _candidate_paths({
            "file_path": "/abs/one.py",           # line 99-100 (path field)
            "nested": {"path": "/abs/two.py"},     # line 103-104 (dict recurse)
            "arr": [{"path": "/abs/three.py"}],    # line 103-107 (list recurse)
            "rel": {"file_path": "relfile.txt"},   # line 113-114 (relative join)
        }, cwd="/base")
        self.assertIn(os.path.realpath("/abs/one.py"), found)
        self.assertIn(os.path.realpath("/abs/two.py"), found)
        self.assertIn(os.path.realpath("/abs/three.py"), found)
        self.assertIn(os.path.realpath("/base/relfile.txt"), found)

    def test_parent_alive(self):
        self.assertTrue(_parent_alive(os.getpid()))
        # A pid that (almost certainly) does not exist -> OSError -> False.
        self.assertFalse(_parent_alive(2_000_000_000))

    def test_match_query_empty(self):
        self.assertEqual(SearchIndex._match_query("!!! ??"), "")     # line 902-903

    def test_contained_valueerror(self):
        # Mixing absolute and relative raises ValueError -> False (lines 576-577).
        self.assertFalse(SearchIndex._contained("/root", "relative/path"))


class ClaudeRowTest(unittest.TestCase):
    def test_custom_title(self):
        docs, arts, meta, known, cut = _claude_row(
            {"type": "custom-title", "customTitle": "My Title"}, {})
        self.assertEqual(meta["title"], "My Title")                   # lines 137-138
        self.assertEqual(docs, [])

    def test_attachment_queued_human_message(self):
        row = {"type": "attachment", "timestamp": "t",
               "attachment": {"type": "queued_command",
                              "origin": {"kind": "human"},
                              "prompt": "queued human text"}}
        docs, arts, meta, known, cut = _claude_row(row, {})           # lines 140-148
        self.assertEqual(docs[0][0], "user")
        self.assertEqual(docs[0][3], "queued human text")

    def test_system_event_row(self):
        docs, arts, meta, known, cut = _claude_row(
            {"type": "system", "timestamp": "t", "content": "compact boundary"}, {})
        self.assertEqual(docs[0][:2], ("system", "event"))            # lines 150-155

    def test_assistant_thinking_and_nondict_block(self):
        row = {"type": "assistant", "timestamp": "t",
               "message": {"role": "assistant", "model": "m", "content": [
                   7,                                                  # line 175 skip
                   {"type": "thinking", "thinking": "deep reasoning"},  # 178-181
                   {"type": "text", "text": "answer"}]}}
        docs, arts, meta, known, cut = _claude_row(row, {})
        kinds = {(d[0], d[1]) for d in docs}
        self.assertIn(("assistant", "reasoning"), kinds)
        self.assertIn(("assistant", "message"), kinds)

    def test_user_list_content_with_tool_result(self):
        row = {"type": "user", "timestamp": "t",
               "message": {"role": "user", "content": [
                   {"type": "tool_result", "content": "tool output text"}]}}
        docs, arts, meta, known, cut = _claude_row(row, {})           # lines 193-199
        self.assertTrue(any(d[1] == "tool" and d[3] == "tool output text"
                            for d in docs))

    def test_unknown_role_marks_unknown(self):
        docs, arts, meta, known, cut = _claude_row(
            {"type": "user", "timestamp": "t",
             "message": {"role": "tool-daemon", "content": "x"}}, {})
        self.assertFalse(known)                                       # line 209


class CodexRowTest(unittest.TestCase):
    def test_session_meta_subagent(self):
        row = {"type": "session_meta",
               "payload": {"id": "child-1", "thread_source": "subagent"}}
        docs, arts, meta, known, cut = _codex_row(row, {})            # lines 228-229
        self.assertEqual(meta["source_kind"], "subagent")
        self.assertEqual(meta["agent_id"], "child-1")

    def test_turn_context(self):
        row = {"type": "turn_context",
               "payload": {"cwd": "/work/proj", "model": "gpt"}}
        docs, arts, meta, known, cut = _codex_row(row, {})            # lines 233-238
        self.assertEqual(meta["cwd"], "/work/proj")
        self.assertEqual(meta["project"], "proj")
        self.assertEqual(meta["model"], "gpt")

    def test_event_msg_lifecycle_artifact_and_unknown(self):
        started = _codex_row(
            {"type": "event_msg", "timestamp": "t",
             "payload": {"type": "task_started"}}, {})               # lines 251-254
        self.assertEqual(started[0][0][:2], ("system", "event"))

        arts = _codex_row(
            {"type": "event_msg", "timestamp": "t",
             "payload": {"type": "token_count", "path": "/abs/a.py"}},
            {"cwd": "/base"})                                         # lines 255-257
        self.assertIn(os.path.realpath("/abs/a.py"), arts[1])

        unknown = _codex_row(
            {"type": "event_msg", "payload": {"type": "brand_new"}}, {})
        self.assertFalse(unknown[3])                                  # lines 258-259

    def test_response_item_reasoning_summary_forms(self):
        as_list = _codex_row(
            {"type": "response_item", "timestamp": "t",
             "payload": {"type": "reasoning",
                         "summary": [{"type": "summary_text", "text": "s"}]}}, {})
        self.assertTrue(any(d[1] == "reasoning" for d in as_list[0]))  # 269-276
        as_str = _codex_row(
            {"type": "response_item", "timestamp": "t",
             "payload": {"type": "reasoning", "summary": "plain summary"}}, {})
        self.assertTrue(any(d[3] == "plain summary" for d in as_str[0]))

    def test_response_item_function_call_and_output(self):
        call = _codex_row(
            {"type": "response_item", "timestamp": "t",
             "payload": {"type": "function_call", "name": "Edit",
                         "arguments": {"file_path": "/abs/x.py"}}},
            {"cwd": "/base"})                                         # 277-284
        self.assertEqual(call[0][0][:2], ("tool", "tool"))
        self.assertIn(os.path.realpath("/abs/x.py"), call[1])

        out = _codex_row(
            {"type": "response_item", "timestamp": "t",
             "payload": {"type": "function_call_output", "output": "cmd out"}}, {})
        self.assertEqual(out[0][0][3], "cmd out")                     # 285-290

        unknown = _codex_row(
            {"type": "response_item", "payload": {"type": "mystery_item"}}, {})
        self.assertFalse(unknown[3])                                  # 291-292

    def test_top_level_state_and_unknown_types(self):
        ignored = _codex_row({"type": "world_state", "payload": {}}, {})
        self.assertTrue(ignored[3])                                   # lines 294-295
        self.assertEqual(ignored[0], [])
        unknown = _codex_row({"type": "utterly_new_type", "payload": {}}, {})
        self.assertFalse(unknown[3])                                  # line 296


class IndexBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.project = os.path.join(self.claude, "-Users-test-repo")
        os.makedirs(self.project)
        os.makedirs(self.codex)
        self.db_path = os.path.join(self.tmp.name, "search.db")
        self.index = SearchIndex(self.db_path, self.claude, self.codex,
                                 discover_seconds=.01, batch_rows=250)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.index.close)

    def main_path(self):
        return os.path.join(self.project, CLAUDE_SID + ".jsonl")

    def write_rows(self, path, rows, final_newline=True):
        import json
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            for i, row in enumerate(rows):
                raw = row if isinstance(row, bytes) else json.dumps(row).encode()
                handle.write(raw)
                if final_newline or i < len(rows) - 1:
                    handle.write(b"\n")

    def drain(self, attempts=500):
        idle = 0
        for _ in range(attempts):
            if self.index.run_once():
                idle = 0
            else:
                idle += 1
                if idle >= 2:
                    return
            time.sleep(.001)
        self.fail("did not settle")


class SearchReaderTest(IndexBaseTest):
    def _seed(self):
        import json
        rows = [
            {"type": "user", "timestamp": "2026-07-16T00:00:00Z",
             "cwd": os.path.join(self.tmp.name, "repo"),
             "message": {"role": "user", "content": "alpha browse token"}},
            {"type": "assistant", "timestamp": "2026-07-16T00:00:01Z",
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "text", "text": "beta answer"}]}},
        ]
        self.write_rows(self.main_path(), rows)
        self.drain()

    def test_search_input_validation(self):
        self.assertFalse(self.index.search(provider="bogus")["ok"])    # line 920
        self.assertFalse(self.index.search(kind="bogus")["ok"])        # line 922
        self.assertFalse(self.index.search(cursor="notint")["ok"])     # 926-927

    def test_browse_and_project_filter(self):
        self._seed()
        browse = self.index.search("")                                 # 985-992
        self.assertTrue(browse["ok"])
        self.assertTrue(browse["results"])
        # Project filter clause (line 950): unknown project -> no rows.
        self.assertEqual(self.index.search("alpha", project="nope")["results"], [])

    def test_search_cache_eviction(self):
        self._seed()
        for i in range(140):                                           # line 1016
            self.index.search("alpha q%d" % i)
        with self.index.cache_lock:
            self.assertLessEqual(len(self.index.search_cache), 128)

    def test_context_validation_and_missing(self):
        self.assertFalse(self.index.context(document_id=object())["ok"])  # 1024-1025
        self.assertFalse(self.index.context(999999)["ok"])             # line 1035

    def test_handoff_validation_and_missing(self):
        self.assertFalse(
            self.index.handoff_material("s", recent_limit=object())["ok"])  # 1063-1064
        self.assertFalse(self.index.handoff_material("never-indexed")["ok"])  # 1074

    def test_handoff_todos_and_artifact_dedup(self):
        todo_text = "\n".join("TODO: task %d remaining" % i for i in range(9))
        rows = [
            {"type": "user", "timestamp": "2026-07-16T00:00:00Z",
             "message": {"role": "user", "content": "kick off"}},
            {"type": "assistant", "timestamp": "2026-07-16T00:00:01Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": todo_text}]}},
        ]
        self.write_rows(self.main_path(), rows)
        self.drain()

        # Two artifact source rows with the SAME path force the dedup skip (1109).
        with self.index.lock:
            db = self.index._db()
            src = db.execute("SELECT id FROM sources WHERE source_kind='session'"
                             " LIMIT 1").fetchone()["id"]
            for i, key in enumerate(("artdup-1", "artdup-2")):
                db.execute(
                    """INSERT INTO sources(source_key,path,provider,source_kind,
                       session_id,parser_version,title,complete)
                       VALUES(?,?,?,'artifact',?,1,?,1)""",
                    (key, "/same/artifact.md", "claude", CLAUDE_SID, "artifact.md"))
                aid = db.execute("SELECT id FROM sources WHERE source_key=?",
                                 (key,)).fetchone()["id"]
                db.execute(
                    """INSERT INTO documents(source_id,ordinal,position,role,kind,
                       timestamp_epoch,title,text,artifact_path)
                       VALUES(?, 'artifact', 0, 'artifact','artifact',?,?,?,?)""",
                    (aid, float(i), "artifact.md", "content", "/same/artifact.md"))
            db.commit()

        handoff = self.index.handoff_material(CLAUDE_SID)
        self.assertTrue(handoff["ok"])
        self.assertTrue(handoff["todos"])                              # 1100-1104
        self.assertGreaterEqual(len(handoff["todos"]), 1)
        self.assertEqual(len(handoff["artifacts"]), 1)                 # line 1109

    def test_rebuild_clears_sources(self):
        self._seed()
        self.assertTrue(self.index.search("alpha")["results"])
        result = self.index.rebuild()                                  # 1169-1178
        self.assertEqual(result["state"], "rebuilding")
        self.assertEqual(self.index.last_discovery, 0)
        with self.index.lock:
            count = self.index._db().execute(
                "SELECT COUNT(*) FROM sources").fetchone()[0]
        self.assertEqual(count, 0)


class ReadBatchAndIndexTest(IndexBaseTest):
    def test_oversized_and_nondict_rows(self):
        with mock.patch.object(search_index, "MAX_ROW_BYTES", 50):
            with open(self.main_path(), "wb") as handle:
                handle.write(b"x" * 60 + b"\n")     # no newline in window -> 733-737
                handle.write(b"y" * 50 + b"\n")     # ends w/ newline, >MAX -> 743-744
                handle.write(b"123\n")              # valid JSON, non-dict -> 751-752
            self.drain()
        status = self.index.status()
        self.assertGreaterEqual(status["malformed_rows"], 3)

    def test_index_transcript_read_oserror(self):
        self.write_rows(self.main_path(), [
            {"type": "user", "timestamp": "t",
             "message": {"role": "user", "content": "will fail"}}])
        self.index.discover()
        source = dict(self.index._next_source())
        with mock.patch.object(self.index, "_read_batch",
                               side_effect=OSError("read boom")):
            self.index._index_transcript(source)                       # 772-777
        with self.index.lock:
            row = self.index._db().execute(
                "SELECT error,deferred_until FROM sources WHERE id=?",
                (source["id"],)).fetchone()
        self.assertEqual(row["error"], "read boom")
        self.assertGreater(row["deferred_until"], 0)

    def test_index_transcript_generation_mismatch(self):
        self.write_rows(self.main_path(), [
            {"type": "user", "timestamp": "t",
             "message": {"role": "user", "content": "stale generation"}}])
        self.index.discover()
        source = dict(self.index._next_source())
        with self.index.lock:
            db = self.index._db()
            db.execute("UPDATE sources SET generation=generation+1 WHERE id=?",
                       (source["id"],))
            db.commit()
        self.index._index_transcript(source)                           # line 784
        # Nothing indexed for that stale generation.
        self.assertEqual(self.index.search("stale generation")["results"], [])

    def test_index_transcript_project_fallback_from_cwd(self):
        # Row carries no cwd/project; the DB row already has a cwd but no project,
        # so the merge fills project from basename(cwd) (lines 787-788).
        self.write_rows(self.main_path(), [
            {"type": "user", "timestamp": "t",
             "message": {"role": "user", "content": "fallback project row"}}])
        self.index.discover()
        source_row = self.index._next_source()
        with self.index.lock:
            db = self.index._db()
            db.execute("UPDATE sources SET cwd=?, project=NULL WHERE id=?",
                       ("/home/user/myproj", source_row["id"]))
            db.commit()
            source = dict(db.execute("SELECT * FROM sources WHERE id=?",
                                     (source_row["id"],)).fetchone())
        self.index._index_transcript(source)
        with self.index.lock:
            project = self.index._db().execute(
                "SELECT project FROM sources WHERE id=?",
                (source["id"],)).fetchone()["project"]
        self.assertEqual(project, "myproj")


class ArtifactTest(IndexBaseTest):
    def test_oversized_and_binary_artifacts(self):
        big = os.path.join(self.tmp.name, "big.md")
        with open(big, "w") as handle:
            handle.write("x" * 40)
        binary = os.path.join(self.tmp.name, "bin.txt")
        with open(binary, "wb") as handle:
            handle.write(b"a\x00b")   # <= patched limit, contains a NUL byte
        self.write_rows(self.main_path(), [
            {"type": "assistant", "timestamp": "t",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "SendUserFile", "id": "b1",
                  "input": {"files": [big]}},
                 {"type": "tool_use", "name": "SendUserFile", "id": "b2",
                  "input": {"files": [binary]}}]}}])
        with mock.patch.object(search_index, "ARTIFACT_MAX_BYTES", 10):
            self.drain()
        errors = {row["error"] for row in self.index.status()["warnings"]
                  if row["error"]}
        self.assertIn("artifact exceeds 1 MB search limit", errors)    # line 872
        self.assertIn("binary artifact", errors)                       # line 877

    def test_artifact_source_non_absolute_path_is_skipped(self):
        # realpath always returns an absolute path, so force the defensive guard
        # by patching isabs to report a relative result (lines 840-841).
        self.write_rows(self.main_path(), [
            {"type": "user", "timestamp": "t",
             "message": {"role": "user", "content": "seed"}}])
        self.drain()
        with self.index.lock:
            db = self.index._db()
            sid = db.execute("SELECT id FROM sources LIMIT 1").fetchone()["id"]
            before = db.execute(
                "SELECT COUNT(*) FROM sources WHERE source_kind='artifact'"
            ).fetchone()[0]
            with mock.patch.object(search_index.os.path, "isabs",
                                   return_value=False):
                self.index._artifact_source(db, sid, {"provider": "claude",
                                                      "session_id": CLAUDE_SID},
                                            "/tmp/whatever.md")
            after = db.execute(
                "SELECT COUNT(*) FROM sources WHERE source_kind='artifact'"
            ).fetchone()[0]
        self.assertEqual(before, after)

    def test_artifact_refresh_on_change(self):
        art = os.path.join(self.tmp.name, "live.md")
        with open(art, "w") as handle:
            handle.write("first content")
        self.write_rows(self.main_path(), [
            {"type": "assistant", "timestamp": "t",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "SendUserFile", "id": "a1",
                  "input": {"files": [art]}}]}}])
        self.drain()
        with self.index.lock:
            complete = self.index._db().execute(
                "SELECT complete FROM sources WHERE source_kind='artifact'"
            ).fetchone()["complete"]
        self.assertEqual(complete, 1)
        # Change the artifact; discovery must mark it pending again (674-676).
        time.sleep(.01)
        with open(art, "w") as handle:
            handle.write("second content is different and longer")
        self.index.last_discovery = 0
        self.index.discover()
        with self.index.lock:
            complete = self.index._db().execute(
                "SELECT complete FROM sources WHERE source_kind='artifact'"
            ).fetchone()["complete"]
        self.assertEqual(complete, 0)


class DiscoverAndFilesTest(IndexBaseTest):
    def test_files_skips_noise(self):
        outside = os.path.join(self.tmp.name, "outside_real.jsonl")
        with open(outside, "w") as handle:
            handle.write("{}\n")
        # Claude root noise.
        with open(os.path.join(self.project, "notes.txt"), "w") as handle:
            handle.write("x")                                          # line 592
        os.symlink(outside, os.path.join(self.project, "link.jsonl"))  # line 595
        self.write_rows(self.main_path(), [
            {"type": "user", "timestamp": "t",
             "message": {"role": "user", "content": "real"}}])
        # Codex root noise.
        with open(os.path.join(self.codex, "readme.txt"), "w") as handle:
            handle.write("x")                                          # line 611
        os.symlink(outside, os.path.join(self.codex, "clink.jsonl"))   # line 614
        with open(os.path.join(self.codex, "no-uuid-here.jsonl"), "w") as handle:
            handle.write("{}\n")                                       # line 617
        codex_valid = os.path.join(
            self.codex, "rollout-2026-07-16T00-00-00-" + CODEX_SID + ".jsonl")
        with open(codex_valid, "w") as handle:
            handle.write("{}\n")

        keys = {entry[0] for entry in self.index._files()}
        self.assertIn("claude:" + os.path.realpath(self.main_path()), keys)
        self.assertIn("codex:" + os.path.realpath(codex_valid), keys)
        # Neither the .txt files, the escaping symlinks, nor the non-UUID name
        # became sources.
        self.assertFalse(any("notes.txt" in k or "readme.txt" in k or
                             "no-uuid-here" in k or "outside_real" in k
                             for k in keys))

    def test_discover_stat_oserror_is_skipped(self):
        with mock.patch.object(self.index, "_files", return_value=[
                ("k", "/no/such/path.jsonl", "claude", "session", "sid", None)]):
            self.index.last_discovery = 0
            self.index.discover()                                      # 633-634
        with self.index.lock:
            count = self.index._db().execute(
                "SELECT COUNT(*) FROM sources").fetchone()[0]
        self.assertEqual(count, 0)


class DbFailureTest(IndexBaseTest):
    def test_signal_reader_oserror(self):
        self.index.reader_signal_path = os.path.join(
            self.tmp.name, "no", "such", "dir", "reader-active")
        self.index.last_reader_signal = -1.0
        # Must not raise even though the parent directory is missing (342-343).
        self.index._signal_reader()

    def test_db_reraises_unconfirmed_databaseerror(self):
        class Boom:
            def close(self):
                raise sqlite3.Error("close failed")   # exercises 383-384
        self.index.connection = Boom()
        with mock.patch.object(self.index, "_open_db",
                               side_effect=sqlite3.DatabaseError("disk I/O error")):
            with self.assertRaises(sqlite3.DatabaseError):
                self.index._db()                                       # 380-386
        self.assertIsNone(self.index.connection)

    def test_quarantine_tolerates_close_error(self):
        class Boom:
            def close(self):
                raise sqlite3.Error("cannot close")
        self.index.read_connection = Boom()
        self.index.connection = Boom()
        # Should swallow the close errors and complete quarantine (358-359).
        self.index._quarantine_corrupt_db(
            sqlite3.DatabaseError("database disk image is malformed"))
        self.assertIn("rebuilt", self.index.last_error)
        self.assertIsNone(self.index.connection)
        self.assertIsNone(self.index.read_connection)


class ProcessLifecycleTest(IndexBaseTest):
    def test_start_process_and_ensure_return_running(self):
        fake = types.SimpleNamespace(poll=lambda: None)
        self.index.process = fake
        self.assertIs(self.index.start_process(), fake)                # line 524
        self.assertIs(self.index.ensure_process(), fake)               # line 538
        self.index.process = None   # keep the close() cleanup off the fake

    def test_close_kills_on_timeout(self):
        events = {"terminated": False, "killed": False}

        class FakeProc:
            def poll(self_inner):
                return None

            def terminate(self_inner):
                events["terminated"] = True

            def wait(self_inner, timeout=None):
                raise search_index.subprocess.TimeoutExpired("x", timeout)

            def kill(self_inner):
                events["killed"] = True

        self.index.process = FakeProc()
        self.index.close()                                             # 545-551
        self.assertTrue(events["terminated"])
        self.assertTrue(events["killed"])

    def test_start_thread_runs_and_second_start_is_noop(self):
        self.index.start()                                             # 510-514
        self.assertTrue(self.index.thread.is_alive())
        self.index.start()                                             # 510-511 early return
        time.sleep(.05)
        self.index.close()                                             # 543-544 join
        self.assertFalse(self.index.thread.is_alive())

    def test_worker_thread_records_error(self):
        with mock.patch.object(self.index, "run_once",
                               side_effect=RuntimeError("worker boom")):
            self.index.start()                                         # 562-568
            deadline = time.time() + 3
            while time.time() < deadline and not self.index.last_error:
                time.sleep(.01)
            self.assertEqual(self.index.last_error, "worker boom")
        self.index.close()


class ModuleWorkerTest(IndexBaseTest):
    def _args(self):
        return types.SimpleNamespace(
            db=self.db_path, claude_root=self.claude, codex_root=self.codex,
            parent_pid=os.getpid(), discover_seconds=.01, batch_rows=250)

    def test_worker_reader_active_short_circuits(self):
        # A fresh reader-active signal file makes the loop yield without indexing.
        with open(self.db_path + ".reader-active", "w") as handle:
            handle.write("")
        os.utime(self.db_path + ".reader-active", None)
        # os.nice failing must be swallowed (lines 1192-1193).
        with mock.patch.object(search_index.os, "nice",
                               side_effect=OSError("no nice")), \
                mock.patch.object(search_index, "_parent_alive",
                                  side_effect=[True, False]):
            _worker(self._args())                                      # 1191-1214, 1232-1233

    def test_worker_indexing_error_then_clear(self):
        self.write_rows(self.main_path(), [
            {"type": "user", "timestamp": "t",
             "message": {"role": "user", "content": "worker corpus"}}])
        with mock.patch.object(search_index.time, "sleep", lambda *a, **k: None), \
                mock.patch.object(search_index, "_parent_alive",
                                  side_effect=[True, True, False]), \
                mock.patch.object(SearchIndex, "run_once",
                                  side_effect=[RuntimeError("boom"), False]):
            _worker(self._args())                                      # 1215-1231
        # The error, then its clearing, both round-tripped through search_meta.
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT value FROM search_meta WHERE key='last_worker_error'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)

    def test_main_requires_worker_args(self):
        with mock.patch.object(search_index.sys, "argv", ["prog"]):
            with self.assertRaises(SystemExit):
                _main()                                                # 1246-1248

    def test_main_dispatches_worker(self):
        argv = ["prog", "--worker", "--db", self.db_path,
                "--claude-root", self.claude, "--codex-root", self.codex,
                "--parent-pid", str(os.getpid())]
        with mock.patch.object(search_index.sys, "argv", argv), \
                mock.patch.object(search_index, "_worker") as worker:
            _main()                                                    # 1237-1245, 1249
        self.assertEqual(worker.call_count, 1)
        self.assertIsInstance(worker.call_args[0][0], argparse.Namespace)


if __name__ == "__main__":
    unittest.main()
