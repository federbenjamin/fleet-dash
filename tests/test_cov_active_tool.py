"""The transcript-derived open-tool label (`active_tool`).

"stalled" has always meant frozen mid-TOOL (invariant 7), but the projection
never said WHICH tool, so a wedged session and a slow one looked identical. The
fold already tracked unanswered tool_uses for usage accounting; these tests pin
the parts that were missing — the start time, the newest-wins rule, and the
states where reporting it would be a lie.
"""
import time
import unittest
from unittest import mock

from fleetdash.engine import Engine
from fleetdash.tail import Tail

from test_cov_common import EngineCovBase


def _assistant(tool_id, name, ts):
    return {"type": "assistant", "timestamp": ts, "uuid": f"u-{tool_id}",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": {}}]}}


def _result(tool_id, ts):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


class OpenToolTest(EngineCovBase):
    def _tail(self, rows):
        self.write_transcript(rows)
        tail = Tail(self.transcript)
        tail.poll()
        return tail

    def test_open_tool_is_none_until_a_tool_is_in_flight(self):
        self.assertIsNone(self._tail([]).open_tool())

    def test_open_tool_reports_name_and_start(self):
        tail = self._tail([_assistant("t1", "Bash", "2026-07-25T00:00:00.000Z")])
        self.assertEqual(tail.open_tool(),
                         {"name": "Bash", "ts": "2026-07-25T00:00:00.000Z", "count": 1})

    def test_a_result_closes_it(self):
        tail = self._tail([_assistant("t1", "Bash", "2026-07-25T00:00:00.000Z"),
                           _result("t1", "2026-07-25T00:00:05.000Z")])
        self.assertIsNone(tail.open_tool())

    def test_newest_wins_and_count_reports_the_rest(self):
        """Parallel tool calls: name the most recent, say how many are open."""
        tail = self._tail([_assistant("t1", "Read", "2026-07-25T00:00:00.000Z"),
                           _assistant("t2", "Bash", "2026-07-25T00:00:01.000Z")])
        open_tool = tail.open_tool()
        self.assertEqual(open_tool["name"], "Bash")
        self.assertEqual(open_tool["count"], 2)

    def test_it_is_not_filtered_by_key_tools(self):
        """KEY_TOOLS decides what the CONVERSATION shows. A session wedged on a
        tool nobody wants in the transcript is exactly the one worth naming."""
        tail = self._tail([_assistant("t1", "Glob", "2026-07-25T00:00:00.000Z")])
        self.assertEqual(tail.open_tool()["name"], "Glob")

    def test_end_turn_clears_it(self):
        rows = [_assistant("t1", "Bash", "2026-07-25T00:00:00.000Z"),
                {"type": "assistant", "timestamp": "2026-07-25T00:00:02.000Z",
                 "uuid": "u-end",
                 "message": {"role": "assistant", "stop_reason": "end_turn",
                             "content": [{"type": "text", "text": "done"}]}}]
        self.assertIsNone(self._tail(rows).open_tool())


class ActiveToolProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tail = mock.Mock()
        self.tail.open_tool.return_value = {
            "name": "Bash", "ts": "2026-07-25T00:00:00.000Z", "count": 1}

    def test_idle_states_report_nothing(self):
        """An idle session can still hold a stale pending entry from a turn that
        ended without a result row; calling that live work would be a lie."""
        for state in ("idle", "turn_done", "needs_you", "dormant"):
            self.assertIsNone(Engine._active_tool(self.tail, state), state)

    def test_working_states_report_the_tool(self):
        with mock.patch("fleetdash.engine_scan.time.time", return_value=1e9):
            with mock.patch("fleetdash.engine_scan.iso_epoch", return_value=1e9 - 240):
                out = Engine._active_tool(self.tail, "stalled")
        self.assertEqual(out, {"name": "Bash", "count": 1, "seconds": 240})

    def test_an_unparsable_timestamp_leaves_the_age_unknown(self):
        """Absent evidence is rendered as absent, never as a fabricated zero."""
        with mock.patch("fleetdash.engine_scan.iso_epoch", return_value=0):
            self.assertIsNone(Engine._active_tool(self.tail, "running")["seconds"])

    def test_a_future_timestamp_clamps_to_zero(self):
        """Compaction appends rows carrying earlier timestamps (invariant 17), so
        clock arithmetic here must never produce a negative age."""
        with mock.patch("fleetdash.engine_scan.time.time", return_value=1e9):
            with mock.patch("fleetdash.engine_scan.iso_epoch", return_value=1e9 + 60):
                self.assertEqual(
                    Engine._active_tool(self.tail, "running")["seconds"], 0)

    def test_a_long_tool_name_is_bounded(self):
        self.tail.open_tool.return_value = {"name": "x" * 200, "ts": None, "count": 1}
        self.assertEqual(len(Engine._active_tool(self.tail, "running")["name"]), 40)

    def test_no_open_tool_reports_nothing(self):
        self.tail.open_tool.return_value = None
        self.assertIsNone(Engine._active_tool(self.tail, "running"))


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
