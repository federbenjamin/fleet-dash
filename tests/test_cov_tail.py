"""Coverage for fleetdash/tail.py — the incremental JSONL fold. Every case is
driven by writing synthetic transcript rows to a temp file and calling poll(),
exercising the fold edge cases the invariants describe (isMeta/system rows,
queued_command attachments, out-of-order compact timestamps, api_error
collapsing, qa events, usage/cache-bust accounting, effort rows, delivered
paths)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from fleetdash import paths as engine_paths
from fleetdash.config import DEFAULT_CONFIG
from fleetdash.tail import Tail


class TailCovTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "t.jsonl")
        self.cfg = dict(DEFAULT_CONFIG)
        # _tool_arg substitutes pathcfg.HOME -> "~"; pin it deterministically.
        p = mock.patch.object(engine_paths, "HOME", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, rows, mode="w"):
        with open(self.path, mode) as h:
            for r in rows:
                h.write(json.dumps(r) + "\n")

    def _tail(self, rows):
        self._write(rows)
        t = Tail(self.path)
        t.poll()
        return t

    def _append_raw(self, text):
        with open(self.path, "a") as h:
            h.write(text)

    def assistant(self, ts, text=None, usage=None, model="claude-sonnet",
                  stop="end_turn", content=None, effort=None):
        msg = {"role": "assistant", "model": model, "stop_reason": stop}
        if usage is not None:
            msg["usage"] = usage
        msg["content"] = content if content is not None else (
            [{"type": "text", "text": text or ""}])
        row = {"type": "assistant", "timestamp": ts, "message": msg}
        if effort is not None:
            row["effort"] = effort
        return row

    # ---------------------------------------------------------------- poll()
    def test_poll_missing_file_returns_false(self):
        t = Tail(os.path.join(self.tmp.name, "nope.jsonl"))
        self.assertFalse(t.poll())

    def test_poll_no_change_and_no_newline(self):
        self._write([self.assistant("2026-07-15T00:00:00Z", "hi",
                                    usage={"input_tokens": 1})])
        t = Tail(self.path)
        self.assertTrue(t.poll())
        self.assertFalse(t.poll())            # size == offset
        self._append_raw("{partial line no newline")
        self.assertFalse(t.poll())            # nl < 0 -> nothing consumed

    def test_poll_truncation_reinits(self):
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "hi",
                                       usage={"input_tokens": 5})])
        self.assertEqual(t.ti, 5)
        with open(self.path, "w") as h:       # rotate/truncate to smaller
            h.write("")
        self.assertFalse(t.poll())            # size < offset -> re-init, then empty
        self.assertEqual(t.offset, 0)
        self.assertEqual(t.ti, 0)

    def test_poll_skips_malformed_json_line(self):
        self._append_raw("not json at all\n")
        self._write([self.assistant("2026-07-15T00:00:02Z", "ok",
                                    usage={"input_tokens": 3})], mode="a")
        t = Tail(self.path)
        t.poll()
        self.assertEqual(t.ti, 3)

    # ------------------------------------------- interruption + activity clock
    def test_interrupted_tail_tracks_only_the_newest_row(self):
        rows = [{"type": "user", "timestamp": "2026-07-15T00:00:00Z",
                 "message": {"role": "user", "content": "the task"}}]
        t = self._tail(rows)
        self.assertFalse(t.interrupted_tail)
        for marker in ("[Request interrupted by user]",
                       "[Request interrupted by user for tool use]"):
            self._write([{"type": "user", "timestamp": "2026-07-15T00:00:01Z",
                          "message": {"role": "user", "content": [
                              {"type": "text", "text": marker}]}}], mode="a")
            t.poll()
            self.assertTrue(t.interrupted_tail, marker)
        # A resumed agent keeps working: any later row clears it.
        self._write([self.assistant("2026-07-15T00:00:02Z", "back")], mode="a")
        t.poll()
        self.assertFalse(t.interrupted_tail)
        # And the marker must be the row, not a mention inside one.
        self._write([{"type": "user", "timestamp": "2026-07-15T00:00:03Z",
                      "message": {"role": "user", "content":
                                  "why did [Request interrupted by user] appear?"}}],
                    mode="a")
        t.poll()
        self.assertFalse(t.interrupted_tail)

    def test_activity_clock_is_the_newest_row_not_the_mtime(self):
        """Quiet time rides the fold (invariant 81)."""
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "hi",
                                       usage={"input_tokens": 1})])
        first = t.activity_ep
        self.assertAlmostEqual(first, 1784073600.0, delta=1)   # the row's own time
        os.utime(self.path, None)                              # phantom touch
        self.assertFalse(t.poll())
        self.assertEqual(t.activity_ep, first)
        # Compaction appends rows carrying EARLIER timestamps: the clock is a max,
        # and real growth is activity even when the row predates it.
        self._write([self.assistant("2026-07-14T00:00:00Z", "older")], mode="a")
        self.assertTrue(t.poll())
        self.assertGreater(t.activity_ep, first)

    def test_first_read_of_an_old_file_is_not_activity(self):
        """A daemon restart folds a day-old transcript; it is still a day old."""
        self._write([self.assistant("2026-07-15T00:00:00Z", "hi")])
        t = Tail(self.path)
        self.assertTrue(t.poll())
        self.assertAlmostEqual(t.activity_ep, 1784073600.0, delta=1)

    def test_plain_text_of_an_unusable_content_shape(self):
        self.assertEqual(Tail._plain_text(None), "")
        self.assertEqual(Tail._plain_text([{"type": "tool_result"}]), "")

    # ---------------------------------------------------------- fold branches
    def test_compact_summary_flag(self):
        t = self._tail([{"type": "user", "timestamp": "2026-07-15T00:00:00Z",
                         "isCompactSummary": True,
                         "message": {"role": "user", "content": "x"}}])
        self.assertTrue(t.saw_compaction)

    def test_permission_mode_records(self):
        t = self._tail([
            {"type": "permission-mode", "timestamp": "2026-07-15T00:00:00Z",
             "permissionMode": "plan"},
            {"type": "user", "timestamp": "2026-07-15T00:00:01Z",
             "permissionMode": "acceptEdits",
             "message": {"role": "user", "content": "go"}},
        ])
        self.assertEqual(t.permission_mode, "acceptEdits")
        self.assertGreater(t.permission_mode_evidence_offset, 0)

    def test_unaccepted_prompt_branch_is_removed_when_next_prompt_bypasses_it(self):
        t = self._tail([
            {"type": "user", "promptId": "cancelled", "uuid": "u-cancelled",
             "parentUuid": "base", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "cancelled prompt"}},
            {"type": "user", "promptId": "kept", "uuid": "u-kept",
             "parentUuid": "base", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "user", "content": "kept prompt"}},
        ])
        self.assertEqual([row.get("text") for row in t.convo
                          if row.get("role") == "user"], ["kept prompt"])

    def test_accepted_prompt_branch_remains_after_later_interruption(self):
        t = self._tail([
            {"type": "user", "promptId": "accepted", "uuid": "u-accepted",
             "parentUuid": "base", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "accepted prompt"}},
            {**self.assistant("2026-07-15T00:00:01Z", "started"),
             "promptId": "accepted", "uuid": "a-accepted",
             "parentUuid": "u-accepted"},
            {"type": "user", "promptId": "later", "uuid": "u-later",
             "parentUuid": "base", "timestamp": "2026-07-15T00:00:02Z",
             "message": {"role": "user", "content": "later prompt"}},
        ])
        self.assertEqual([row.get("text") for row in t.convo
                          if row.get("role") == "user"],
                         ["accepted prompt", "later prompt"])

    def test_file_history_snapshot_backup_mapping(self):
        t = self._tail([{"type": "file-history-snapshot",
                         "timestamp": "2026-07-15T00:00:00Z",
                         "snapshot": {"trackedFileBackups": {
                             "/work/a.py": {"backupFileName": "abcdef01@v3"},
                             "/work/bad.py": {"backupFileName": "NOTVALID"},
                             "/work/x.py": "notadict"}}}])
        self.assertEqual(t.file_backups, {"/work/a.py": "abcdef01@v3"})

    def test_gitbranch_and_ai_title(self):
        t = self._tail([
            {"type": "x", "timestamp": "2026-07-15T00:00:00Z", "gitBranch": "feat"},
            {"type": "ai-title", "timestamp": "2026-07-15T00:00:01Z",
             "aiTitle": "My Title"},
        ])
        self.assertEqual(t.git_branch, "feat")
        self.assertEqual(t.ai_title, "My Title")

    def test_non_dict_message_ignored(self):
        t = self._tail([{"type": "x", "timestamp": "2026-07-15T00:00:00Z",
                         "message": "just a string"}])
        self.assertEqual(list(t.convo), [])

    def test_effort_folds_from_assistant_rows(self):
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "hi",
                                       usage={"input_tokens": 1}, effort="max")])
        self.assertEqual(t.effort, "max")
        self.assertGreater(t.effort_evidence_offset, 0)

    # --------------------------------------------------- attachment folding
    def test_queued_command_attachment_becomes_user_row(self):
        t = self._tail([{"type": "attachment", "timestamp": "2026-07-15T00:00:00Z",
                         "attachment": {"type": "queued_command",
                                        "origin": {"kind": "human"},
                                        "prompt": [{"type": "text", "text": "mid turn"}]}}])
        self.assertEqual([e["text"] for e in t.convo if e["role"] == "user"],
                         ["mid turn"])

    def test_queued_command_dedup_against_twin(self):
        t = self._tail([
            {"type": "user", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "dup me"}},
            {"type": "attachment", "timestamp": "2026-07-15T00:00:01Z",
             "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                            "prompt": "dup me"}},
        ])
        users = [e for e in t.convo if e["role"] == "user"]
        self.assertEqual(len(users), 1)

    def test_attachment_slash_prefixed_skipped(self):
        t = self._tail([{"type": "attachment", "timestamp": "2026-07-15T00:00:00Z",
                         "attachment": {"type": "queued_command",
                                        "origin": {"kind": "human"},
                                        "prompt": "/status"}}])
        self.assertEqual([e for e in t.convo if e["role"] == "user"], [])

    def test_queue_operation_terminal_event(self):
        raw = ("<task-notification><task-id>agent-qq</task-id>"
               "<status>failed</status></task-notification>")
        t = self._tail([{"type": "queue-operation",
                         "timestamp": "2026-07-15T00:00:00Z", "content": raw}])
        self.assertEqual(t.agent_terminals["agent-qq"]["status"], "failed")

    def test_prompt_content_as_block_list(self):
        t = self._tail([{"type": "user", "timestamp": "2026-07-15T00:00:00Z",
                         "message": {"role": "user", "content": [
                             {"type": "text", "text": "block one"},
                             {"type": "text", "text": "block two"}]}}])
        user = next(e for e in t.convo if e["role"] == "user")
        self.assertIn("block one", user["text"])

    def test_qa_add_trim_over_60(self):
        rows = []
        for i in range(70):
            rows.append(self.assistant(
                f"2026-07-15T00:{i//60:02d}:{i%60:02d}Z", content=[
                    {"type": "tool_use", "id": f"qq{i}", "name": "AskUserQuestion",
                     "input": {"questions": [{"header": "H", "question": f"Q{i}?"}]}}],
                stop="tool_use", usage={"input_tokens": 1}))
        t = self._tail(rows)
        self.assertLessEqual(len(t._qa_refs), 60)

    def test_qa_resolve_content_as_list(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "ql", "name": "AskUserQuestion",
                 "input": {"questions": [{"header": "H", "question": "Q?"}]}}],
                stop="tool_use", usage={"input_tokens": 1}),
            {"type": "user", "timestamp": "2026-07-15T00:00:02Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "ql",
                  "content": [{"type": "text", "text": '"Q?"="Yes"'}]}]}},
        ])
        qa = next(e for e in t.convo if e.get("kind") == "qa")
        self.assertEqual(qa["qa"][0]["a"], "Yes")

    def test_file_add_redelivery_inherits_caption(self):
        t = Tail(self.path)
        t._file_add("/w/r.txt", "orig caption", "2026-07-15T00:00:00Z")
        t._file_add("/w/r.txt", "", "2026-07-15T00:00:05Z")   # re-deliver, no caption
        matches = [f for f in t.files if f["path"] == "/w/r.txt"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["caption"], "orig caption")

    def test_attachment_task_notification(self):
        raw = ("<task-notification><task-id>abc123</task-id>"
               "<status>completed</status></task-notification>")
        t = self._tail([{"type": "attachment", "timestamp": "2026-07-15T00:00:00Z",
                         "attachment": {"commandMode": "task-notification",
                                        "prompt": raw}}])
        self.assertEqual(t.agent_terminals["agent-abc123"]["status"], "completed")

    # ------------------------------------------------- assistant usage/tools
    def test_skill_attribution_to_active_skill(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "tu1", "name": "Skill",
                 "input": {"skill": "handoff"}}], stop="tool_use",
                usage={"input_tokens": 4}),
            self.assistant("2026-07-15T00:00:01Z", "done",
                           usage={"input_tokens": 7, "output_tokens": 2}),
        ])
        skills = [k for k in t.stats if k[1] == "skill" and k[2] == "handoff"]
        self.assertTrue(skills)
        # active skill got the second turn's spend attributed
        row = t.stats[skills[0]]
        self.assertGreaterEqual(row[2], 7)

    def test_senduserfile_tool_adds_file(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "f1", "name": "SendUserFile",
                 "input": {"files": ["/work/out.txt", 5], "caption": "report"}}],
                stop="tool_use", usage={"input_tokens": 1}),
            {"type": "user", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "f1",
                  "content": "Files sent successfully"}]}}
        ])
        self.assertEqual([f["path"] for f in t.files], ["/work/out.txt"])
        self.assertIn("/work/out.txt", t.delivered_paths)
        self.assertEqual(t.file_deliveries[0]["tool_id"], "f1")

    def test_senduserfile_error_is_not_a_delivery(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "f1", "name": "SendUserFile",
                 "input": {"files": ["/work/out.txt"], "caption": "report"}}],
                stop="tool_use", usage={"input_tokens": 1}),
            {"type": "user", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "f1",
                  "is_error": True, "content": "cancelled"}]}}
        ])
        self.assertEqual(list(t.files), [])
        self.assertEqual(t.file_deliveries, [])
        tool = next(row for row in t.convo if row.get("name") == "SendUserFile")
        self.assertNotIn("files", tool)

    def test_askuserquestion_qa_event_and_resolve(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "q1", "name": "AskUserQuestion",
                 "input": {"questions": [
                     {"header": "H1", "question": "Pick?"},
                     {"header": "H2", "question": "Other?"}]}}],
                stop="tool_use", usage={"input_tokens": 1}),
            {"type": "user", "timestamp": "2026-07-15T00:00:02Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "q1",
                  "content": 'answered: "Pick?"="A", "drifted"="B"'}]}},
        ])
        qa = next(e for e in t.convo if e.get("kind") == "qa")
        self.assertEqual(qa["qa"][0]["a"], "A")
        self.assertEqual(qa["qa"][1]["a"], "B")   # leftover filled in order

    def test_askuserquestion_error_removes_unanswered_event(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "q1", "name": "AskUserQuestion",
                 "input": {"questions": [
                     {"header": "H1", "question": "Pick?"}]}}],
                stop="tool_use", usage={"input_tokens": 1}),
            {"type": "user", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "q1",
                  "is_error": True, "content": "cancelled"}]}}
        ])
        self.assertFalse(any(row.get("kind") == "qa" for row in t.convo))

    def test_qa_resolve_declined(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "q9", "name": "AskUserQuestion",
                 "input": {"questions": [{"header": "H", "question": "Q?"}]}}],
                stop="tool_use", usage={"input_tokens": 1}),
            {"type": "user", "timestamp": "2026-07-15T00:00:02Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "q9",
                  "content": "User declined to answer questions"}]}},
        ])
        qa = next(e for e in t.convo if e.get("kind") == "qa")
        self.assertEqual(qa["qa"][0]["a"], "(declined to answer)")

    def test_key_tool_bash_and_result_and_errored(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", content=[
                {"type": "tool_use", "id": "b1", "name": "Bash",
                 "input": {"command": "ls -la\nsecond", "description": "list"}}],
                stop="tool_use", usage={"input_tokens": 1}),
            {"type": "user", "timestamp": "2026-07-15T00:00:02Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "b1",
                  "is_error": True, "content": "boom"}]}},
        ])
        entry = next(e for e in t.convo if e.get("role") == "tool")
        self.assertEqual(entry["command"], "ls -la\nsecond")
        self.assertTrue(entry["failed"])
        self.assertIn("b1", t.errored_tools)

    def test_result_summary_edit_updated_created(self):
        for verb, marker in (("updated", "updated ✓"), ("created", "created ✓")):
            b = {"content": f"The file was {verb} successfully.", "is_error": False}
            self.assertEqual(Tail._result_summary(b, "Edit"), marker)
        listy = {"content": [{"type": "text", "text": "line one\nline two"}],
                 "is_error": True}
        self.assertTrue(Tail._result_summary(listy).startswith("✗ "))
        self.assertEqual(Tail._result_summary({"content": ""}), "")

    def test_tool_arg_branches(self):
        self.assertEqual(Tail._tool_arg("Agent", {"subagent_type": "Explore"}),
                         "Explore")
        self.assertEqual(Tail._tool_arg("Skill", {"skill": "handoff"}), "handoff")
        long = "x" * 200
        self.assertTrue(Tail._tool_arg("Read", {"file_path": long}).endswith("…"))
        homey = os.path.join(self.tmp.name, "f.txt")
        self.assertTrue(Tail._tool_arg("Read", {"path": homey}).startswith("~"))
        self.assertEqual(Tail._tool_peek_arg(
            "Write", {"file_path": "/very/long/private/path/report.md"}), "report.md")
        self.assertEqual(Tail._tool_peek_arg(
            "Bash", {"description": "Run focused tests"}), "Run focused tests")

    def test_tool_refs_trim_over_300(self):
        rows = []
        for i in range(320):
            rows.append(self.assistant(
                f"2026-07-15T00:{i//60:02d}:{i%60:02d}Z", content=[
                    {"type": "tool_use", "id": f"t{i}", "name": "Bash",
                     "input": {"command": f"echo {i}"}}],
                stop="tool_use", usage={"input_tokens": 1}))
        t = self._tail(rows)
        self.assertLessEqual(len(t._tool_refs), 300)

    # ---------------------------------------------------- user prompt folding
    def test_prompt_filters_system_reminder_and_continuation(self):
        t = self._tail([{"type": "user", "timestamp": "2026-07-15T00:00:00Z",
                         "message": {"role": "user", "content":
                             "<system-reminder>noise</system-reminder>"
                             "This session is being continued from a prior one"}}])
        self.assertTrue(t.saw_compaction)
        self.assertEqual([e for e in t.convo if e["role"] == "user"], [])

    def test_prompt_command_name_event(self):
        t = self._tail([{"type": "user", "timestamp": "2026-07-15T00:00:00Z",
                         "message": {"role": "user", "content":
                             "<command-name>/deploy</command-name>"
                             "<command-args>prod now</command-args>"}}])
        ev = next(e for e in t.convo if e.get("kind") == "command")
        self.assertEqual(ev["title"], "/deploy")
        self.assertEqual(ev["detail"], "prod now")

    def test_ismeta_prompt_skipped(self):
        t = self._tail([{"type": "user", "timestamp": "2026-07-15T00:00:00Z",
                         "isMeta": True,
                         "message": {"role": "user", "content": "meta text"}}])
        self.assertEqual([e for e in t.convo if e["role"] == "user"], [])

    # ------------------------------------------------ agent terminal events
    def test_agent_terminal_guards(self):
        t = Tail(self.path)
        t._agent_terminal_event(None, "2026-07-15T00:00:00Z")           # not str
        t._agent_terminal_event("no marker here", "2026-07-15T00:00:00Z")
        t._agent_terminal_event("<task-notification>missing bits"
                                "</task-notification>", "2026-07-15T00:00:00Z")
        self.assertEqual(t.agent_terminals, {})
        good = ("<task-notification><task-id>agent-zz</task-id>"
                "<status>killed</status></task-notification>")
        t._agent_terminal_event(good, "2026-07-15T00:00:05Z")
        # older ts must not overwrite the newer terminal notice
        t._agent_terminal_event(good.replace("killed", "completed"),
                                "2026-07-15T00:00:01Z")
        self.assertEqual(t.agent_terminals["agent-zz"]["status"], "killed")

    # ---------------------------------------------------------- system events
    def test_system_compact_boundary_out_of_order_insert(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:05Z", "later text",
                           usage={"input_tokens": 1}),
            {"type": "system", "subtype": "compact_boundary",
             "timestamp": "2026-07-15T00:00:03Z",
             "compactMetadata": {"trigger": "auto", "preTokens": 120000,
                                 "postTokens": 40000, "durationMs": 5000}},
        ])
        kinds = [e.get("kind") for e in t.convo if e.get("role") == "event"]
        self.assertIn("compact", kinds)
        self.assertTrue(t.saw_compaction)
        self.assertGreater(t.last_compact_ep, 0)

    def test_system_model_refusal_and_api_error_collapse(self):
        t = self._tail([
            {"type": "system", "subtype": "model_refusal_fallback",
             "timestamp": "2026-07-15T00:00:00Z", "originalModel": "fable-5",
             "fallbackModel": "opus-4.8", "content": "safeguards flagged"},
            {"type": "system", "subtype": "api_error",
             "timestamp": "2026-07-15T00:00:01Z",
             "error": {"message": "overloaded"}, "retryAttempt": 1,
             "maxRetries": 5},
            {"type": "system", "subtype": "api_error",
             "timestamp": "2026-07-15T00:00:02Z",
             "error": {"message": "overloaded"}, "retryAttempt": 2,
             "maxRetries": 5},
        ])
        model_ev = next(e for e in t.convo if e.get("kind") == "model")
        self.assertEqual(model_ev["title"], "fable-5 → opus-4.8")
        api_ev = next(e for e in t.convo if e.get("kind") == "api_error")
        self.assertEqual(api_ev["n"], 2)      # retry storm collapsed

    def test_system_model_refusal_default_title(self):
        t = self._tail([{"type": "system", "subtype": "model_refusal_fallback",
                         "timestamp": "2026-07-15T00:00:00Z", "content": "x"}])
        self.assertEqual(next(e for e in t.convo if e.get("kind") == "model")["title"],
                         "Switched to another model")

    def test_system_local_command_attaches_to_command(self):
        t = self._tail([
            {"type": "user", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content":
                 "<command-name>/status</command-name><command-args></command-args>"}},
            {"type": "system", "subtype": "local_command",
             "timestamp": "2026-07-15T00:00:01Z",
             "content": "<local-command-stdout>all good</local-command-stdout>"},
        ])
        cmd = next(e for e in t.convo if e.get("kind") == "command")
        self.assertEqual(cmd["detail"], "all good")

    def test_command_event_blank_name_ignored(self):
        t = Tail(self.path)
        t._command_event("<command-name></command-name>", "2026-07-15T00:00:00Z")
        self.assertEqual([e for e in t.convo if e.get("kind") == "command"], [])

    def test_event_add_full_deque_appends(self):
        t = Tail(self.path)
        for i in range(t.convo.maxlen):
            t.convo.append({"role": "assistant", "text": str(i),
                            "ts": "2026-07-15T00:00:00Z"})
        t._event_add("command", "/x", "", "2026-07-15T00:00:09Z")
        self.assertEqual(len(t.convo), t.convo.maxlen)
        self.assertTrue(any(e.get("kind") == "command" for e in t.convo))

    # ----------------------------------------------------------- cache track
    def _bust_tail(self, since_setup):
        """Two API calls where the second re-pays a big prefix -> a bust row."""
        rows = [
            self.assistant("2026-07-15T00:00:00Z", "first",
                           usage={"input_tokens": 100,
                                  "cache_read_input_tokens": 50000,
                                  "cache_creation_input_tokens": 0,
                                  "output_tokens": 10}),
        ]
        rows.extend(since_setup)
        rows.append(self.assistant(
            "2026-07-15T02:00:00Z", "second",
            usage={"input_tokens": 60000, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0, "output_tokens": 10}))
        return self._tail(rows)

    def test_cache_bust_idle_ttl(self):
        t = self._bust_tail([])
        causes = [k[2] for k in t.stats if k[1] == "cache"]
        self.assertTrue(any("idle" in c for c in causes))

    def test_cache_bust_compaction(self):
        t = self._bust_tail([{"type": "system", "subtype": "compact_boundary",
                              "timestamp": "2026-07-15T01:00:00Z",
                              "compactMetadata": {"trigger": "auto"}}])
        self.assertIn("compaction", [k[2] for k in t.stats if k[1] == "cache"])

    def test_cache_bust_model_switch(self):
        rows = [
            self.assistant("2026-07-15T00:00:00Z", "a", model="claude-opus",
                           usage={"input_tokens": 100,
                                  "cache_read_input_tokens": 50000,
                                  "output_tokens": 5}),
            self.assistant("2026-07-15T00:05:00Z", "b", model="claude-sonnet",
                           usage={"input_tokens": 60000,
                                  "cache_read_input_tokens": 0,
                                  "output_tokens": 5}),
        ]
        t = self._tail(rows)
        self.assertIn("model switch", [k[2] for k in t.stats if k[1] == "cache"])

    def test_cache_bust_skill(self):
        # A Skill invoked between calls (its own row carries no usage, so
        # _cache_track isn't called there) sets skill_since_usage; the next
        # billed call within the hour attributes the bust to "skill ...".
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", "a",
                           usage={"input_tokens": 100,
                                  "cache_read_input_tokens": 50000,
                                  "output_tokens": 5}),
            {"type": "assistant", "timestamp": "2026-07-15T00:02:00Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "tool_use", "content": [
                             {"type": "tool_use", "id": "sk", "name": "Skill",
                              "input": {"skill": "handoff"}}]}},
            self.assistant("2026-07-15T00:04:00Z", "b",
                           usage={"input_tokens": 60000,
                                  "cache_read_input_tokens": 0,
                                  "output_tokens": 5}),
        ])
        self.assertTrue(any(c.startswith("skill ")
                            for c in [k[2] for k in t.stats if k[1] == "cache"]))

    def test_cache_bust_idle_5m_to_1h(self):
        # gap between 330s and 3900s -> "idle 5m-1h (ttl?)"
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", "a",
                           usage={"input_tokens": 100,
                                  "cache_read_input_tokens": 50000,
                                  "output_tokens": 5}),
            self.assistant("2026-07-15T00:20:00Z", "b",
                           usage={"input_tokens": 60000,
                                  "cache_read_input_tokens": 0,
                                  "output_tokens": 5}),
        ])
        self.assertIn("idle 5m–1h (ttl?)",
                      [k[2] for k in t.stats if k[1] == "cache"])

    def test_cache_bust_tail_rewrite_breakpoint(self):
        # read stays >= 50% of prev prefix -> "tail rewrite"
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", "a",
                           usage={"input_tokens": 100,
                                  "cache_read_input_tokens": 50000,
                                  "output_tokens": 5}),
            self.assistant("2026-07-15T00:04:00Z", "b",
                           usage={"input_tokens": 30000,
                                  "cache_read_input_tokens": 30000,
                                  "output_tokens": 5}),
        ])
        self.assertIn("tail rewrite (breakpoint drift)",
                      [k[2] for k in t.stats if k[1] == "cache"])

    def test_cache_bust_deep_unattributed(self):
        t = self._tail([
            self.assistant("2026-07-15T00:00:00Z", "a",
                           usage={"input_tokens": 100,
                                  "cache_read_input_tokens": 50000,
                                  "output_tokens": 5}),
            self.assistant("2026-07-15T00:04:00Z", "b",
                           usage={"input_tokens": 60000,
                                  "cache_read_input_tokens": 100,
                                  "output_tokens": 5}),
        ])
        self.assertIn("deep bust (unattributed)",
                      [k[2] for k in t.stats if k[1] == "cache"])

    # ------------------------------------------------------ status/metrics
    def test_status_cache_track_bad_value_and_spike(self):
        t = Tail(self.path)
        t._status_cache_track({"cache_creation_input_tokens": "oops"},
                              "2026-07-15T00:00:00Z")      # exception -> 0
        self.assertEqual(t.cache_write_previous, 0)
        t._status_cache_track({"cache_creation_input_tokens": 25000},
                              "2026-07-15T00:00:01Z")
        self.assertEqual(t.cache_write_spikes, 1)
        self.assertEqual(t.cache_write_last_spike_value, 25000)

    def test_status_metrics_bad_usage_values(self):
        t = Tail(self.path)
        t.last_usage = {"input_tokens": "bad", "cache_read_input_tokens": None}
        metrics = t.status_metrics(self.cfg)
        self.assertIsNone(metrics["cache_read_pct"])
        self.assertEqual(metrics["cache_write"], 0)

    def test_status_metrics_turn_cost(self):
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "hi",
                                       usage={"input_tokens": 1000,
                                              "output_tokens": 500})])
        metrics = t.status_metrics(self.cfg)
        self.assertIsNotNone(metrics["turn_cost"])

    # ------------------------------------------------------------ misc utils
    def test_chars_list_and_str(self):
        self.assertEqual(Tail._chars({"content": [{"type": "text", "text": "abc"}]}),
                         3)
        self.assertEqual(Tail._chars({"content": "hello"}), 5)

    def test_delivered_paths_cap(self):
        t = Tail(self.path)
        for i in range(1100):
            t._file_add(f"/w/f{i}.txt", "", "2026-07-15T00:00:00Z")
        self.assertLessEqual(len(t.delivered_paths), 1024)

    def test_last_message_and_latest_prose_and_none(self):
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "x" * 300,
                                       usage={"input_tokens": 1})])
        self.assertTrue(t.last_message(50)["text"].endswith("…"))
        self.assertEqual(t.latest_prose()["role"], "assistant")
        empty = Tail(self.path)
        self.assertIsNone(empty.last_message())
        self.assertIsNone(empty.latest_prose())

    def test_context_tokens_and_cost_and_total(self):
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "hi",
                                       usage={"input_tokens": 10,
                                              "cache_creation_input_tokens": 2,
                                              "cache_read_input_tokens": 3,
                                              "output_tokens": 4})])
        self.assertEqual(t.context_tokens(), 15)
        self.assertEqual(t.total_tokens, 19)
        self.assertGreater(t.cost(self.cfg), 0)

    def test_turn_state_all_shapes(self):
        self.assertEqual(Tail(self.path).turn_state(), "unknown")
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "done",
                                       usage={"input_tokens": 1})])
        self.assertEqual(t.turn_state(), "awaiting_input")
        running = self._tail([self.assistant("2026-07-15T00:00:00Z", content=[
            {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "y"}}],
            stop="tool_use", usage={"input_tokens": 1})])
        self.assertEqual(running.turn_state(), "running")
        userlast = self._tail([{"type": "user", "timestamp": "2026-07-15T00:00:00Z",
                                "message": {"role": "user", "content": "hey"}}])
        self.assertEqual(userlast.turn_state(), "running")

    def test_assistant_mid_stream_none_stop(self):
        t = self._tail([self.assistant("2026-07-15T00:00:00Z", "thinking",
                                       stop=None, usage={"input_tokens": 1})])
        self.assertEqual(t.turn_state(), "running")


if __name__ == "__main__":
    unittest.main()
