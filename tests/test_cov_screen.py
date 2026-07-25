"""Coverage for fleetdash/screen.py (invariant 77).

Every frame below is a verbatim tail of a real `capture-pane -p` read from Claude
Code v2.1.219 in a disposable tmux sandbox on 2026-07-24. They are fixtures
precisely because they were observed rather than imagined — the same discipline
invariant 4's key map was built with.
"""
import unittest

from fleetdash import screen


ASK = """
Which colour?

❯ 1. Red
     Red
  2. Green
     Green
  3. Blue
     Blue
  4. Type something.
────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

PERMISSION = """
 Create file
 .claude/metrics/.session-unlock
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 2026-07-24T00:00:00Z
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create .session-unlock?
 ❯ 1. Yes
   2. Yes, and allow Claude to edit its own settings for this session
   3. No

 Esc to cancel · Tab to amend
"""

TRUST = """
 Accessing workspace:

 /private/tmp/claude/sbx-tui

 Quick safety check: Is this a project you created or one you trust?

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

IDLE = """
                   tmux focus-events off · add 'set -g focus-events on'
────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────
  sbx-tui                                                            /rc
  ⠀⠀⠀⠀Haiku 4.5 │ Ctx: 0%  →130k │ Sess: 16% │ Wk: 84%
  ⠀⠀⠀⠀⠀⠀⠀⠀♻ 0% │ ✎ 0 │ $0.000  Δ+$0.000
  ⏸ manual mode on · ← for agents
"""

BUSY = """
❯ count from 1 to 40 slowly

  38
  39
  40

✻ Cogitated for 2s
"""


def lines(frame):
    return frame.splitlines()


class ClassifyTest(unittest.TestCase):
    def test_the_ask_selector_is_a_question(self):
        self.assertEqual(screen.classify_screen(lines(ASK)), screen.QUESTION)

    def test_a_tool_permission_prompt_is_a_permission(self):
        self.assertEqual(screen.classify_screen(lines(PERMISSION)), screen.PERMISSION)

    def test_the_folder_trust_dialog_is_its_own_kind(self):
        """It is a numbered selector like the others, and answering it is the one
        thing Fleet must never do (invariant 21) — so it can never be mistaken
        for a question."""
        self.assertEqual(screen.classify_screen(lines(TRUST)), screen.TRUST)

    def test_an_empty_prompt_between_two_rules_is_the_input_box(self):
        self.assertEqual(screen.classify_screen(lines(IDLE)), screen.INPUT)

    def test_a_mid_turn_screen_is_unknown_not_absent(self):
        self.assertEqual(screen.classify_screen(lines(BUSY)), screen.UNKNOWN)

    def test_no_frame_is_unknown(self):
        for value in (None, [], [""], ["   "]):
            self.assertEqual(screen.classify_screen(value), screen.UNKNOWN, value)

    def test_only_the_tail_is_read(self):
        """A question answered ten screens ago must not classify the pane."""
        stale = lines(ASK) + [""] * (screen.TAIL_LINES + 5) + lines(IDLE)
        self.assertEqual(screen.classify_screen(stale), screen.INPUT)

    def test_a_question_whose_footer_scrolled_off_still_classifies(self):
        body = [row for row in lines(ASK) if "Enter to select" not in row]
        self.assertEqual(screen.classify_screen(body), screen.QUESTION)

    def test_a_permission_whose_footer_scrolled_off_still_classifies(self):
        body = [row for row in lines(PERMISSION) if "Tab to amend" not in row]
        self.assertEqual(screen.classify_screen(body), screen.PERMISSION)

    def test_a_highlighted_option_row_is_not_the_input_box(self):
        """Both are `❯`; only the empty prompt sits directly under a rule."""
        self.assertEqual(screen.classify_screen(["────", "❯ 1. Red", "  2. Green"]),
                         screen.UNKNOWN)

    def test_non_string_rows_are_tolerated(self):
        self.assertEqual(screen.classify_screen([None, 7, "Enter to select · Esc"]),
                         screen.QUESTION)


if __name__ == "__main__":
    unittest.main()
