"""Reading a permission prompt's own option rows off the pane.

All three variants captured live on Claude Code v2.1.220 put Yes/always/No in
rows 1/2/3 — so Fleet's keys are right for every one — but row 2's wording, and
the power it actually grants, differ sharply. A single fixed "always allow"
label described all three and was honest about none.
"""
import unittest
from unittest import mock

from fleetdash import screen
from fleetdash.engine import Engine

BASH = """\
 Bash command

   touch probe.txt
   Create probe.txt file

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to fleet-screen-rig/ from this project
   3. No

 Esc to cancel · Tab to amend · ctrl+e to explain
"""

READ = """\
 Read file

  Read(/Users/test/.claude/metrics/.session-unlock)

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow reading from metrics/ during this session
   3. No

 Esc to cancel · Tab to amend
"""


# Captured live from `tmux capture-pane -p` on 2026-07-25. The notice above the
# options is Claude Code explaining why there is no persistent grant to offer:
# it cannot derive a rule pattern from a command it cannot statically analyze.
NO_ALWAYS = """\
 Bash command

   echo "$(date)" > /Users/test/.claude/metrics/.session-unlock
   Run shell command

 Contains shell syntax (string) that cannot be statically analyzed

 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel · Tab to amend · ctrl+e to explain
"""


class PromptOptionsTest(unittest.TestCase):
    def test_bash_variant(self):
        self.assertEqual(
            screen.prompt_options(BASH.splitlines()),
            ["Yes",
             "Yes, and always allow access to fleet-screen-rig/ from this project",
             "No"])

    def test_read_variant_grants_something_different(self):
        """The whole reason this exists: same key positions, different power."""
        options = screen.prompt_options(READ.splitlines())
        self.assertEqual(options[1],
                         "Yes, allow reading from metrics/ during this session")
        self.assertNotEqual(options[1], screen.prompt_options(BASH.splitlines())[1])

    def test_the_live_prompt_wins_over_an_answered_one_above_it(self):
        """A pane still holds the rows of a prompt that was already answered."""
        rows = (READ + "\n" + BASH).splitlines()
        self.assertIn("fleet-screen-rig", screen.prompt_options(rows)[1])

    def test_no_option_list_is_an_empty_list(self):
        self.assertEqual(screen.prompt_options(["─" * 40, "❯", "─" * 40]), [])

    def test_rows_are_bounded(self):
        rows = [" ❯ 1. " + "x" * 500]
        self.assertEqual(len(screen.prompt_options(rows)[0]), 160)

    def test_the_option_index_is_capped(self):
        rows = [f"   {n}. option {n}" for n in range(1, 10)]
        self.assertEqual(len(screen.prompt_options(rows, limit=3)), 3)


class AlwaysOptionTest(unittest.TestCase):
    """Which key actually grants persistent access — read, never assumed.

    Fleet hardwired "2" for `always allow`. On NO_ALWAYS below, captured live on
    v2.1.220 on 2026-07-25, row 2 is "No": the button denied the request it was
    meant to grant forever.
    """

    def test_the_three_grant_variants_all_resolve_to_row_two(self):
        for frame in (BASH, READ):
            digit, text = screen.always_option(frame.splitlines())
            self.assertEqual(digit, 2)
            self.assertTrue(text.lower().startswith("yes,"))

    def test_a_prompt_with_no_persistent_grant_resolves_to_nothing(self):
        self.assertIsNone(screen.always_option(NO_ALWAYS.splitlines()))
        # …and row 2 there is exactly the key the old constant would have sent
        self.assertEqual(screen.prompt_options(NO_ALWAYS.splitlines())[1], "No")

    def test_the_trust_dialog_offers_no_always(self):
        """Its row 1 is "Yes, I trust this folder" — a yes, but not a grant row,
        and Fleet must never answer that dialog at all (invariant 21)."""
        trust = ("❯ 1. Yes, I trust this folder\n"
                 "  2. No, exit\n\n Enter to confirm · Esc to cancel")
        self.assertIsNone(screen.always_option(trust.splitlines()))

    def test_the_digit_comes_from_the_screen_not_the_position(self):
        """A pane whose first row is numbered 2 still yields the real key."""
        rows = ["  2. Yes", "  3. Yes, and always allow access to x/", "  4. No"]
        self.assertEqual(screen.always_option(rows), (3, "Yes, and always allow access to x/"))

    def test_no_options_at_all(self):
        self.assertIsNone(screen.always_option(["─" * 20, "❯", "─" * 20]))


class EnginePromptOptionsTest(unittest.TestCase):
    def test_it_reuses_every_refusal_session_screen_makes(self):
        """Same capture, same boundaries — a Codex thread, a background job, a
        production session read from staging must all still be refused."""
        engine = mock.Mock(spec=Engine)
        engine.session_screen.return_value = {
            "ok": False, "code": "screen_unavailable", "error": "nope"}
        out = Engine.prompt_options(engine, "s1")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "nope")

    def test_it_returns_the_kind_alongside_the_rows(self):
        engine = mock.Mock(spec=Engine)
        engine.session_screen.return_value = {"ok": True, "lines": BASH.splitlines()}
        out = Engine.prompt_options(engine, "s1")
        self.assertEqual(out["kind"], "permission")
        self.assertEqual(len(out["options"]), 3)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
