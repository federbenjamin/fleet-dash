"""A permission prompt read off the pane, before its hook fires.

Measured on a staging rig 2026-07-25: the prompt is visible on the terminal
seconds before Claude's permission Notification hook lands, and the transcript
holds nothing at all while it is open — so for those seconds Fleet knew a session
was waiting and could not say what for.
"""
import time
import unittest
from unittest import mock

from fleetdash import screen as screenlib
from test_cov_common_ops import EngineFixture

PERMISSION_FRAME = [
    " Bash command",
    "",
    "   touch probe.txt",
    "",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. No",
    "",
    " Esc to cancel · Tab to amend",
]


class ScreenPermissionTest(EngineFixture):
    def _observed(self, label):
        self.engine._screen_states = (
            {"same": {"state": label, "at": time.time()}} if label else {})

    def test_a_rendered_permission_becomes_a_pending_request(self):
        self._observed(screenlib.PERMISSION)
        pending = self.engine._screen_permission("same", time.time())
        self.assertEqual(pending["kind"], "permission")
        self.assertEqual(pending["source"], "screen")
        self.assertTrue(pending["nonce"].startswith("screen-"))

    def test_no_observation_is_no_prompt(self):
        self._observed(None)
        self.assertIsNone(self.engine._screen_permission("same", time.time()))

    def test_another_surface_is_not_a_permission(self):
        for label in (screenlib.QUESTION, screenlib.TRUST, screenlib.INPUT,
                      screenlib.UNKNOWN):
            self._observed(label)
            self.assertIsNone(self.engine._screen_permission("same", time.time()),
                              label)

    def test_the_nonce_is_stable_while_the_prompt_stays_up(self):
        self._observed(screenlib.PERMISSION)
        now = time.time()
        first = self.engine._screen_permission("same", now)
        again = self.engine._screen_permission("same", now + 3)
        self.assertEqual(first["nonce"], again["nonce"],
                         "a re-render must not mint a second identity")

    def test_the_prompt_going_away_retires_the_nonce(self):
        self._observed(screenlib.PERMISSION)
        first = self.engine._screen_permission("same", time.time())
        self._observed(screenlib.INPUT)
        self.assertIsNone(self.engine._screen_permission("same", time.time()))
        self._observed(screenlib.PERMISSION)
        second = self.engine._screen_permission("same", time.time())
        self.assertNotEqual(first["nonce"], second["nonce"])

    def test_the_registry_is_bounded(self):
        self._observed(screenlib.PERMISSION)
        self.engine._screen_prompts = {
            f"s{index}": {"nonce": "n", "at": time.time()}
            for index in range(self.engine.REQUEST_IDENTITY_LIMIT + 5)}
        self.engine._screen_permission("same", time.time())
        self.assertLessEqual(len(self.engine._screen_prompts),
                             self.engine.REQUEST_IDENTITY_LIMIT)


class ScreenPromptActTest(EngineFixture):
    def _arm(self, screen_kind=screenlib.PERMISSION):
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[self.engine and __import__("os").getpid()] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})
        self.engine._screen_states = {"same": {"state": screenlib.PERMISSION,
                                               "at": time.time()}}
        pending = self.engine._screen_permission("same", time.time())
        self.engine.screen_prompt_kind = lambda reg, tty: screen_kind
        return pending["nonce"]

    def test_a_screen_nonce_is_answerable(self):
        nonce = self._arm()
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": nonce, "choice": "deny"})
        self.assertTrue(out["ok"], out)

    def test_a_nonce_the_server_never_minted_is_refused(self):
        self._arm()
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": "screen-9999999999999", "choice": "deny"})
        self.assertFalse(out["ok"])
        self.assertIn("stale", out["error"])

    def test_an_unreadable_terminal_refuses_a_screen_derived_answer(self):
        """The classifier is not a corroborating check here — it is the only
        evidence the prompt exists."""
        nonce = self._arm(screen_kind=None)
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": nonce, "choice": "deny"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "screen_unreadable")

    def test_a_different_surface_refuses_it(self):
        nonce = self._arm(screen_kind=screenlib.TRUST)
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": nonce, "choice": "deny"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "screen_mismatch")

    def test_a_screen_nonce_cannot_answer_a_question(self):
        """Questions keep their hook: a PreToolUse capture is immediate, so they
        have no gap to close, and their key recipe depends on shape the screen
        cannot be trusted to describe (invariant 4)."""
        nonce = self._arm(screen_kind=screenlib.QUESTION)
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": nonce, "digits": [1], "n_options": 1})
        self.assertFalse(out["ok"])
        self.assertIn("stale", out["error"])

    def test_a_session_no_longer_waiting_refuses_it(self):
        nonce = self._arm()
        self.write_registry(status="idle")
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": nonce, "choice": "deny"})
        self.assertFalse(out["ok"])


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
