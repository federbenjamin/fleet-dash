"""Every tool call reaches the conversation, and its output is reachable.

`KEY_TOOLS` used to decide which tool calls became conversation rows, so Read,
Grep, Glob, WebFetch and every MCP tool were not hidden by the browser — they
never reached it. It now decides prominence only.
"""
import json
import os
import unittest
from unittest import mock

from fleetdash.engine import Engine
from fleetdash.tail import Tail
from test_cov_common import EngineCovBase


def assistant(blocks, ts="2026-07-25T10:00:00.000Z", uuid="u1"):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts,
            "message": {"role": "assistant", "content": blocks}}


def result(tool_use_id, text, is_error=False, ts="2026-07-25T10:00:01.000Z"):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id,
                 "content": text, "is_error": is_error}]}}


class QuietToolsAreFoldedTest(EngineCovBase):
    def _fold(self, rows):
        self.write_transcript(rows)
        tail = Tail(self.transcript)
        tail.poll()
        return list(tail.convo)

    def test_a_read_becomes_a_row(self):
        rows = self._fold([
            assistant([{"type": "tool_use", "id": "t1", "name": "Read",
                        "input": {"file_path": "/tmp/x.py"}}]),
            result("t1", "line one\nline two\nline three"),
        ])
        tools = [r for r in rows if r["role"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "Read")
        self.assertTrue(tools[0]["quiet"], "a read is quiet, never absent")
        self.assertEqual(tools[0]["arg"], "/tmp/x.py")

    def test_a_key_tool_is_not_quiet(self):
        rows = self._fold([
            assistant([{"type": "tool_use", "id": "t1", "name": "Write",
                        "input": {"file_path": "/tmp/x.py"}}])])
        self.assertNotIn("quiet", [r for r in rows if r["role"] == "tool"][0])

    def test_an_unknown_tool_is_quiet_without_a_list_to_maintain(self):
        rows = self._fold([
            assistant([{"type": "tool_use", "id": "t1",
                        "name": "mcp__linear__save_issue", "input": {"id": "ENG-1"}}])])
        entry = [r for r in rows if r["role"] == "tool"][0]
        self.assertTrue(entry["quiet"])
        self.assertEqual(entry["arg"], "ENG-1", "the one scalar it was given")

    def test_a_multi_scalar_unknown_tool_guesses_nothing(self):
        rows = self._fold([
            assistant([{"type": "tool_use", "id": "t1", "name": "mcp__x__y",
                        "input": {"a": "one", "b": "two"}}])])
        self.assertEqual([r for r in rows if r["role"] == "tool"][0]["arg"], "")

    def test_search_tools_get_a_subject_line(self):
        rows = self._fold([
            assistant([{"type": "tool_use", "id": "t1", "name": "Grep",
                        "input": {"pattern": "def poll", "path": "/tmp"}}]),
            assistant([{"type": "tool_use", "id": "t2", "name": "WebFetch",
                        "input": {"url": "https://example.test/a"}}], uuid="u2"),
        ])
        args = [r["arg"] for r in rows if r["role"] == "tool"]
        self.assertEqual(args, ["/tmp", "https://example.test/a"])

    def test_the_row_records_how_much_result_there_is(self):
        rows = self._fold([
            assistant([{"type": "tool_use", "id": "t1", "name": "Read",
                        "input": {"file_path": "/tmp/x.py"}}]),
            result("t1", "a\nb\nc"),
        ])
        entry = [r for r in rows if r["role"] == "tool"][0]
        self.assertEqual(entry["result_lines"], 3)
        self.assertEqual(entry["result_chars"], 5)
        self.assertEqual(entry["tool_id"], "t1")
        self.assertEqual(entry["result"], "a", "the preview stays one line")

    def test_a_call_with_no_result_yet_offers_no_expander(self):
        rows = self._fold([
            assistant([{"type": "tool_use", "id": "t1", "name": "Read",
                        "input": {"file_path": "/tmp/x.py"}}])])
        self.assertNotIn("result_chars", [r for r in rows if r["role"] == "tool"][0])


class ToolResultRouteTest(EngineCovBase):
    def _armed(self, text, is_error=False):
        self.write_transcript([
            assistant([{"type": "tool_use", "id": "t1", "name": "Read",
                        "input": {"file_path": "/tmp/x.py"}}]),
            result("t1", text, is_error),
        ])
        self.engine._reg_main_path = lambda sid: ({"pid": 1}, self.transcript)

    def test_it_returns_the_full_output(self):
        self._armed("line one\nline two")
        out = self.engine.tool_result("s1", "t1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "line one\nline two")
        self.assertFalse(out["truncated"])

    def test_it_is_bounded_and_says_so(self):
        self._armed("x" * 9000)
        out = self.engine.tool_result("s1", "t1")
        self.assertEqual(len(out["text"]), Engine.TOOL_RESULT_INLINE)
        self.assertEqual(out["chars"], 9000)
        self.assertTrue(out["truncated"])

    def test_a_failed_result_says_so(self):
        self._armed("boom", is_error=True)
        self.assertTrue(self.engine.tool_result("s1", "t1")["failed"])

    def test_the_id_is_shape_checked_before_anything_is_read(self):
        """It is only ever compared, never used to build a path — but a
        client-supplied identifier gets checked regardless."""
        self.engine._reg_main_path = lambda sid: ({"pid": 1}, self.transcript)
        for bad in ("../../etc/passwd", "a b", "", "x" * 200, None):
            out = self.engine.tool_result("s1", bad)
            self.assertFalse(out["ok"])
            self.assertEqual(out["error"], "invalid tool reference")

    def test_an_unknown_call_is_not_an_error_page(self):
        self._armed("hi")
        out = self.engine.tool_result("s1", "nosuchid")
        self.assertFalse(out["ok"])
        self.assertIn("no result recorded", out["error"])

    def test_a_session_that_is_not_live_is_refused(self):
        self.engine._reg_main_path = lambda sid: (None, None)
        self.assertFalse(self.engine.tool_result("s1", "t1")["ok"])

    def test_an_unreadable_transcript_is_reported_not_raised(self):
        self.engine._reg_main_path = lambda sid: ({"pid": 1}, self.transcript)
        self.write_transcript([])
        with mock.patch("builtins.open", side_effect=OSError("gone")):
            out = self.engine.tool_result("s1", "t1")
        self.assertFalse(out["ok"])
        self.assertIn("unreadable", out["error"])


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
