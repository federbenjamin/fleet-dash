"""The screen-derived compaction label (invariants 17, 77, 78).

A compaction writes NOTHING to the transcript while it runs, so a project with
no PreCompact hook had no live evidence at all. The pane has some: 46 frames of
a real `/compact` were captured on Claude Code v2.1.220, and 43 of them carry
`Compacting conversation…`.
"""
import time
import unittest
from unittest import mock

from fleetdash import screen

FRAME = """\
⏺ Write(~/.claude/metrics/.session-unlock)
  ⎿  User rejected update to ../.claude/metrics/.session-unlock

✻ Cooked for 42s

❯ /compact

· Compacting conversation…
  ▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 5%
  ⎿  Tip: Run tasks in the cloud while you keep coding locally · clau.de/web

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
"""


class CompactingClassifierTest(unittest.TestCase):
    def test_a_real_compacting_frame(self):
        self.assertEqual(screen.classify_screen(FRAME.splitlines()), screen.COMPACTING)

    def test_it_beats_the_input_box_below_it(self):
        """This surface is NOT modal: the empty input box is still on screen, so
        an ordering mistake would report a compacting pane as idle."""
        self.assertIn("❯", FRAME)
        self.assertEqual(screen.classify_screen(FRAME.splitlines()), screen.COMPACTING)

    def test_the_rotating_spinner_glyph_does_not_matter(self):
        for glyph in ("·", "✽", "✻", "✢", "✳", "✶"):
            rows = FRAME.replace("· Compacting", f"{glyph} Compacting").splitlines()
            self.assertEqual(screen.classify_screen(rows), screen.COMPACTING, glyph)

    def test_a_modal_prompt_still_wins(self):
        """A prompt owns the keyboard; compaction does not. If both somehow
        rendered, refusing to call it compacting is the safe order."""
        rows = FRAME.splitlines() + ["", " Do you want to proceed?", " ❯ 1. Yes",
                                     " Esc to cancel · Tab to amend"]
        self.assertEqual(screen.classify_screen(rows), screen.PERMISSION)

    def test_an_ordinary_idle_pane_is_unaffected(self):
        rows = ["─" * 60, "❯", "─" * 60]
        self.assertEqual(screen.classify_screen(rows), screen.INPUT)


class ObservedSecondsTest(unittest.TestCase):
    """`since` must survive a re-observation, or the age resets every window."""

    def test_none_when_unobserved_or_showing_something_else(self):
        from fleetdash.engine import Engine
        engine = mock.Mock(spec=Engine)
        engine._screen_states = {}
        self.assertIsNone(Engine.observed_screen_seconds(engine, "s1", "compacting"))
        engine._screen_states = {"s1": {"state": "input", "at": time.time(),
                                        "since": time.time()}}
        self.assertIsNone(Engine.observed_screen_seconds(engine, "s1", "compacting"))

    def test_age_is_measured_from_first_sighting_not_last(self):
        from fleetdash.engine import Engine
        engine = mock.Mock(spec=Engine)
        now = time.time()
        engine._screen_states = {"s1": {"state": "compacting", "at": now,
                                        "since": now - 42}}
        self.assertEqual(
            Engine.observed_screen_seconds(engine, "s1", "compacting"), 42)

    def test_it_falls_back_to_at_when_since_is_missing(self):
        """Records written before `since` existed must not crash a live scan."""
        from fleetdash.engine import Engine
        engine = mock.Mock(spec=Engine)
        engine._screen_states = {"s1": {"state": "compacting",
                                        "at": time.time() - 7}}
        self.assertEqual(
            Engine.observed_screen_seconds(engine, "s1", "compacting"), 7)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
