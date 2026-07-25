"""Coverage for fleetdash.engine_act: the act() dispatcher — early routing,
Claude state gates, question/permission answer building, control changes, and
direct text/relay (all with the applet exchange mocked)."""
import os
import time
import unittest
from unittest import mock

from tests.test_cov_common_ops import EngineFixture

PID = os.getpid()


class EarlyDispatchTests(EngineFixture):
    def test_non_dict_action(self):
        self.assertIn("must be an object", self.engine.act("nope")["error"])

    def test_ping(self):
        self.assertTrue(self.engine.act({"type": "ping"})["ok"])

    def test_resume_and_send_dispatch(self):
        with mock.patch.object(self.engine, "resume_and_send",
                               return_value={"ok": True, "r": 1}) as m:
            self.assertTrue(self.engine.act(
                {"type": "resume_and_send", "session_id": "x"})["ok"])
        m.assert_called_once()

    def test_briefing_review_dispatch(self):
        with mock.patch.object(self.engine, "briefing_action",
                               return_value={"ok": True}) as m:
            self.engine.act({"type": "briefing_review"})
        m.assert_called_once()

    def test_outbox_dispatch(self):
        with mock.patch.object(self.engine, "outbox_action",
                               return_value={"ok": True}) as m:
            self.engine.act({"type": "outbox_cancel", "id": 1})
        m.assert_called_once()

    def test_handoff_dispatch(self):
        with mock.patch.object(self.engine, "execute_handoff",
                               return_value={"ok": True}) as m:
            self.engine.act({"type": "handoff", "session_id": "x"})
        m.assert_called_once()

    def test_repository_action_dispatch(self):
        with mock.patch.object(self.engine, "repository_action",
                               return_value={"ok": True}) as m:
            self.engine.act({"type": "git_commit", "root": "/r"})
        m.assert_called_once()

    def test_worktree_cleanup_dispatch(self):
        with mock.patch.object(self.engine, "cleanup_closed_worktree",
                               return_value={"ok": True}) as m:
            self.engine.act({"type": "worktree_cleanup", "cleanup_ticket": "t"})
        m.assert_called_once()

    def test_spawn_dispatch(self):
        with mock.patch.object(self.engine, "spawn_session",
                               return_value={"ok": True}) as claude, \
             mock.patch.object(self.engine, "spawn_codex_session",
                               return_value={"ok": True}) as codex:
            self.engine.act({"type": "spawn", "provider": "claude", "cwd": "/x"})
            self.engine.act({"type": "spawn", "provider": "codex", "cwd": "/x"})
        claude.assert_called_once()
        codex.assert_called_once()

    def test_image_resolve_error(self):
        out = self.engine.act({"type": "image_text", "session_id": "same",
                               "upload_ids": "notalist"})
        self.assertIn("attach between", out["error"])

    def test_session_not_live(self):
        out = self.engine.act({"type": "text", "session_id": "ghost",
                               "text": "hi"})
        self.assertIn("not live", out["error"])


class CodexDispatchTests(EngineFixture):
    def _codex_snapshot(self, **caps):
        session = {"session_id": "codex:t1", "provider": "codex",
                   "capabilities": caps}
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [session]}
        return session

    def test_codex_close_preview_no_session(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": []}
        out = self.engine.act({"type": "close_preview", "session_id": "codex:t1"})
        self.assertIn("not live", out["error"])

    def test_codex_close_preview_with_session(self):
        self._codex_snapshot()
        with mock.patch.object(self.engine, "close_worktree_preview",
                               return_value={"ok": True}) as m:
            self.engine.act({"type": "close_preview", "session_id": "codex:t1"})
        m.assert_called_once()

    def test_codex_close_stale_ticket(self):
        self._codex_snapshot()
        out = self.engine.act({"type": "close", "session_id": "codex:t1",
                               "cleanup_ticket": "bad"})
        self.assertIn("cleanup preview expired", out["error"])

    def test_codex_text_via_terminal_route(self):
        self._codex_snapshot()
        with mock.patch.object(self.engine, "_codex_terminal_route",
                               return_value={"tty": "ttys1"}), \
             mock.patch.object(self.engine, "_write_codex_terminal",
                               return_value={"ok": True, "route": True}) as m:
            out = self.engine.act({"type": "text", "session_id": "codex:t1",
                                   "text": "steer"})
        self.assertTrue(out["route"])
        m.assert_called_once()

    def test_codex_text_queue_submit(self):
        self._codex_snapshot(queue_submit=True)
        with mock.patch.object(self.engine, "_codex_terminal_route",
                               return_value=None), \
             mock.patch.object(self.engine, "_queue_codex_recovery",
                               return_value={"ok": True, "queued": True}) as m:
            out = self.engine.act({"type": "text", "session_id": "codex:t1",
                                   "text": "later"})
        self.assertTrue(out["queued"])
        m.assert_called_once()

    def test_codex_act_queueable_result(self):
        self._codex_snapshot()
        self.codex.act = lambda action: {"ok": False, "queueable": True}
        with mock.patch.object(self.engine, "_codex_terminal_route",
                               return_value=None), \
             mock.patch.object(self.engine, "_queue_codex_recovery",
                               return_value={"ok": True, "queued": True}) as m:
            out = self.engine.act({"type": "text", "session_id": "codex:t1",
                                   "text": "x"})
        m.assert_called_once()

    def test_codex_close_marks_ticket(self):
        self._codex_snapshot()
        self.codex.act = lambda action: {"ok": True}
        with mock.patch.object(self.engine, "_mark_cleanup_ticket_closed") as m:
            self.engine.act({"type": "close", "session_id": "codex:t1"})
        m.assert_called_once()


class ClaudeGateTests(EngineFixture):
    def test_interrupt_not_mid_turn(self):
        self.write_registry(status="idle")
        out = self.engine.act({"type": "interrupt", "session_id": "same"})
        self.assertIn("nothing to interrupt", out["error"])

    def test_relay_refused_while_waiting(self):
        self.write_registry(status="waiting")
        out = self.engine.act({"type": "relay", "session_id": "same",
                               "agent_id": "agent-x", "text": "hi"})
        self.assertIn("waiting on a prompt", out["error"])

    def test_permission_mode_requires_idle(self):
        self.write_registry(status="busy")
        out = self.engine.act({"type": "permission_mode", "session_id": "same",
                               "mode": "plan"})
        self.assertIn("only while Claude is idle", out["error"])

    def test_session_settings_requires_idle(self):
        self.write_registry(status="busy")
        out = self.engine.act({"type": "session_settings", "session_id": "same",
                               "model": "sonnet", "effort": "high",
                               "expected_model": "claude-sonnet",
                               "expected_effort": "high"})
        self.assertIn("only while Claude is idle", out["error"])

    def test_text_refused_when_not_idle(self):
        self.write_registry(status="busy")
        out = self.engine.act({"type": "text", "session_id": "same", "text": "hi"})
        self.assertTrue(out["queueable"])

    def test_prompt_answer_requires_waiting(self):
        self.write_registry(status="idle")
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1]})
        self.assertIn("isn't waiting on a prompt", out["error"])

    def test_close_preview_claude(self):
        with mock.patch.object(self.engine, "close_worktree_preview",
                               return_value={"ok": True}) as m:
            self.engine.act({"type": "close_preview", "session_id": "same"})
        m.assert_called_once()

    def test_close_stale_ticket(self):
        out = self.engine.act({"type": "close", "session_id": "same",
                               "cleanup_ticket": "bad"})
        self.assertIn("cleanup preview expired", out["error"])


class QuestionAnswerTests(EngineFixture):
    def _arm(self, questions):
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: {
            "kind": "question", "nonce": "q1", "questions": questions}
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.writes = []
        self.engine._iterm_write = mock.Mock(
            side_effect=lambda tty, steps, step_delay=None:
            self.writes.append((tty, steps, step_delay)) or {"ok": True})

    def test_single_select_option(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}, {"label": "B"}],
                    "multiSelect": False, "allowOther": True}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1]})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.writes[0][2], 0.4)

    def test_option_other_text(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}],
                    "multiSelect": False, "allowOther": True}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "other": "something else"})
        self.assertTrue(out["ok"], out)

    def test_multi_select_option(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}, {"label": "B"}],
                    "multiSelect": True, "allowOther": True}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1, 2]})
        self.assertTrue(out["ok"], out)

    def test_multi_select_option_with_other(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}],
                    "multiSelect": True, "allowOther": True}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1], "other": "extra"})
        self.assertTrue(out["ok"], out)

    def test_dismiss(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}]}])
        out = self.engine.act({"type": "dismiss", "session_id": "same",
                               "nonce": "q1"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.writes[0][1], [("\x1b", False)])

    def test_multiq_single_and_multi(self):
        self._arm([
            {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}],
             "multiSelect": False, "allowOther": False},
            {"question": "Q2", "options": [{"label": "C"}, {"label": "D"}],
             "multiSelect": True, "allowOther": True}])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [
                                   {"digits": [1]},
                                   {"digits": [1], "other": "more"}]})
        self.assertTrue(out["ok"], out)

    def test_stale_nonce(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}]}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "wrong", "digits": [1]})
        self.assertIn("stale", out["error"])

    def test_invalid_digits(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}, {"label": "B"}],
                    "multiSelect": False, "allowOther": False}])
        # out-of-range digit
        self.assertIn("invalid option", self.engine.act({
            "type": "option", "session_id": "same", "nonce": "q1",
            "digits": [9]})["error"])
        # boolean digit
        self.assertIn("invalid option", self.engine.act({
            "type": "option", "session_id": "same", "nonce": "q1",
            "digits": [True]})["error"])
        # too many for single-select
        self.assertIn("choose one", self.engine.act({
            "type": "option", "session_id": "same", "nonce": "q1",
            "digits": [1, 2]})["error"])

    def test_no_option_chosen(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}],
                    "multiSelect": False, "allowOther": False}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": []})
        self.assertIn("no option chosen", out["error"])

    def test_option_other_not_allowed(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}],
                    "multiSelect": False, "allowOther": False}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "other": "x"})
        self.assertIn("Other is unavailable", out["error"])

    def test_wrong_action_for_prompt_kind(self):
        # A permission pending but an option answer is refused.
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: {
            "kind": "permission", "nonce": "q1", "tool": "Bash"}
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1]})
        self.assertIn("not a question", out["error"])


class QuestionShapeTests(EngineFixture):
    def _arm(self, questions):
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: {
            "kind": "question", "nonce": "q1", "questions": questions}
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})

    def test_questions_not_a_list(self):
        self._arm("notalist")
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1]})
        self.assertIn("question shape is unavailable", out["error"])

    def test_options_bad(self):
        self._arm([{"question": "Q", "options": "nope"}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1]})
        self.assertIn("options are unavailable", out["error"])

    def test_too_many_options_for_other(self):
        opts = [{"label": str(i)} for i in range(9)]
        self._arm([{"question": "Q", "options": opts, "allowOther": True}])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1]})
        self.assertIn("too many options", out["error"])

    def test_answer_digits_string_and_invalid(self):
        self._arm([{"question": "Q", "options": [{"label": "A"}, {"label": "B"}],
                    "multiSelect": False, "allowOther": False}])
        # String digit is parsed.
        self.assertTrue(self.engine.act({
            "type": "option", "session_id": "same", "nonce": "q1",
            "digits": ["2"]})["ok"])
        # Non-list digits.
        self.assertIn("invalid option", self.engine.act({
            "type": "option", "session_id": "same", "nonce": "q1",
            "digits": "notalist"})["error"])

    def test_transcript_pending_question(self):
        self.append_transcript({"type": "assistant",
            "timestamp": "2026-07-15T00:00:05Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tool-q", "name": "AskUserQuestion",
                 "input": {"questions": [{"question": "Q",
                     "options": [{"label": "A"}], "multiSelect": False,
                     "allowOther": False}]}}]}})
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "tool-q", "digits": [1]})
        self.assertTrue(out["ok"], out)


class MultiqEdgeTests(EngineFixture):
    def _arm(self, questions):
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: {
            "kind": "question", "nonce": "q1", "questions": questions}
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})

    def _q(self, multi=False, other=False, n=2):
        return {"question": "Q", "multiSelect": multi, "allowOther": other,
                "options": [{"label": str(i)} for i in range(n)]}

    def test_answer_count_mismatch(self):
        self._arm([self._q(), self._q()])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [{"digits": [1]}]})
        self.assertIn("answer count does not match", out["error"])

    def test_answer_not_dict(self):
        self._arm([self._q()])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": ["notadict"]})
        self.assertIn("invalid question answer", out["error"])

    def test_answer_invalid_digits(self):
        self._arm([self._q()])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [{"digits": [9]}]})
        self.assertIn("invalid option selection", out["error"])

    def test_answer_empty(self):
        self._arm([self._q()])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [{"digits": []}]})
        self.assertIn("needs an answer", out["error"])

    def test_answer_other_not_allowed(self):
        self._arm([self._q(other=False)])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [{"other": "x"}]})
        self.assertIn("Other is unavailable", out["error"])

    def test_answer_single_two_digits(self):
        self._arm([self._q(multi=False)])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [{"digits": [1, 2]}]})
        self.assertIn("choose one option", out["error"])

    def test_answer_single_option_and_other(self):
        self._arm([self._q(multi=False, other=True)])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1",
                               "answers": [{"digits": [1], "other": "x"}]})
        self.assertIn("an option or Other", out["error"])

    def test_multi_select_no_other(self):
        self._arm([self._q(multi=True, other=False, n=2)])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [{"digits": [1, 2]}]})
        self.assertTrue(out["ok"], out)

    def test_single_select_other(self):
        self._arm([self._q(multi=False, other=True)])
        out = self.engine.act({"type": "multiq", "session_id": "same",
                               "nonce": "q1", "answers": [{"other": "typed"}]})
        self.assertTrue(out["ok"], out)

    def test_option_refuses_multi_question_pending(self):
        self._arm([self._q(), self._q()])
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1]})
        self.assertIn("answer all questions together", out["error"])

    def test_option_single_option_and_other(self):
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: {
            "kind": "question", "nonce": "q1",
            "questions": [self._q(multi=False, other=True)]}
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1], "other": "x"})
        self.assertIn("an option or Other", out["error"])


class PermissionAnswerTests(EngineFixture):
    def _arm_permission(self):
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: {
            "kind": "permission", "nonce": "p1", "tool": "Bash"}
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})

    def test_permission_allow(self):
        self._arm_permission()
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": "p1", "choice": "allow"})
        self.assertTrue(out["ok"], out)

    def test_permission_deny_is_escape(self):
        self._arm_permission()
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": "p1", "choice": "deny"})
        self.assertTrue(out["ok"], out)

    def test_permission_unknown_choice(self):
        self._arm_permission()
        out = self.engine.act({"type": "permission", "session_id": "same",
                               "nonce": "p1", "choice": "maybe"})
        self.assertIn("unknown choice", out["error"])

    def test_an_accepted_answer_fences_the_prompt_against_a_second_device(self):
        """Two devices can render the same prompt; only one may answer it
        (invariant 75)."""
        self._arm_permission()
        self.engine._apply_request_identity(
            "same", {"kind": "permission", "nonce": "p1", "tool": "Bash",
                     "input_summary": ""}, time.time())
        first = self.engine.act({"type": "permission", "session_id": "same",
                                 "nonce": "p1", "choice": "allow"})
        self.assertTrue(first["ok"], first)
        second = self.engine.act({"type": "permission", "session_id": "same",
                                  "nonce": "p1", "choice": "deny"})
        self.assertEqual(second["code"], "duplicate")
        self.assertEqual(self.engine._iterm_write.call_count, 1)

    def test_a_possibly_delivered_answer_also_fences_the_prompt(self):
        """Uncertainty is not permission to try again from another device."""
        self._arm_permission()
        self.engine._iterm_write = mock.Mock(
            return_value={"ok": False, "error": "no result file"})
        self.engine._apply_request_identity(
            "same", {"kind": "permission", "nonce": "p1", "tool": "Bash",
                     "input_summary": ""}, time.time())
        first = self.engine.act({"type": "permission", "session_id": "same",
                                 "nonce": "p1", "choice": "allow"})
        self.assertEqual(first["code"], "delivery_uncertain")
        self.assertTrue(self.engine._request_answered("same", "p1"))

    def test_a_proven_pre_delivery_failure_leaves_the_prompt_answerable(self):
        self._arm_permission()
        self.engine._iterm_write = mock.Mock(
            return_value={"ok": False, "code": "terminal_not_available",
                          "error": "no pane"})
        self.engine._apply_request_identity(
            "same", {"kind": "permission", "nonce": "p1", "tool": "Bash",
                     "input_summary": ""}, time.time())
        self.engine.act({"type": "permission", "session_id": "same",
                         "nonce": "p1", "choice": "allow"})
        self.assertFalse(self.engine._request_answered("same", "p1"))


class ScreenGateTests(EngineFixture):
    """act() refuses to type at a widget that is not on screen (invariant 77)."""

    def _arm_question(self, screen_kind):
        self.write_registry(status="waiting")
        self.engine.hook_pending = lambda sid, status: {
            "kind": "question", "nonce": "q1",
            "questions": [{"question": "Pick", "options": [{"label": "Red"}]}]}
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})
        self.engine.screen_prompt_state = lambda reg, tty: (
            {"kind": screen_kind, "always": None} if screen_kind else None)

    def test_a_visible_question_is_answered(self):
        self._arm_question("question")
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1], "n_options": 1})
        self.assertTrue(out["ok"], out)

    def test_no_screen_evidence_keeps_the_previous_behaviour(self):
        self._arm_question(None)
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1], "n_options": 1})
        self.assertTrue(out["ok"], out)

    def test_an_ordinary_input_box_refuses_the_keys(self):
        """The exact failure invariant 5 exists to prevent: digits typed into the
        main input become a message."""
        self._arm_question("input")
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1], "n_options": 1})
        self.assertEqual(out["code"], "screen_mismatch")
        self.assertIn("ordinary input box", out["error"])
        self.engine._iterm_write.assert_not_called()

    def test_the_folder_trust_dialog_is_never_answered(self):
        """Digits there would accept trust on the user's behalf (invariant 21)."""
        self._arm_question("trust")
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1], "n_options": 1})
        self.assertEqual(out["code"], "screen_mismatch")
        self.assertIn("folder-trust", out["error"])
        self.engine._iterm_write.assert_not_called()

    def test_a_permission_screen_refuses_a_question_answer(self):
        self._arm_question("permission")
        out = self.engine.act({"type": "option", "session_id": "same",
                               "nonce": "q1", "digits": [1], "n_options": 1})
        self.assertEqual(out["code"], "screen_mismatch")
        self.engine._iterm_write.assert_not_called()

    def test_dismiss_is_accepted_on_either_modal_screen(self):
        for kind in ("question", "permission"):
            self._arm_question(kind)
            out = self.engine.act({"type": "dismiss", "session_id": "same",
                                   "nonce": "q1"})
            self.assertTrue(out["ok"], (kind, out))

    def test_dismiss_is_refused_at_the_input_box(self):
        self._arm_question("input")
        out = self.engine.act({"type": "dismiss", "session_id": "same", "nonce": "q1"})
        self.assertEqual(out["code"], "screen_mismatch")


class ActReceiptTests(EngineFixture):
    """The receipt binding around act() itself (invariant 76)."""

    def _arm_text(self):
        self.write_registry(status="idle")
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})

    def test_a_delivered_action_records_and_reports_its_receipt(self):
        self._arm_text()
        out = self.engine.act({"type": "text", "session_id": "same", "text": "hi",
                               "client_request_id": "act-abcdef123456"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["receipt_state"], "delivered")
        self.assertEqual(
            self.engine.act_receipt("act-abcdef123456")["receipt"]["state"], "delivered")

    def test_the_same_request_id_never_types_twice(self):
        """This is the whole point: a browser that lost its response may retry."""
        self._arm_text()
        first = self.engine.act({"type": "text", "session_id": "same", "text": "hi",
                                 "client_request_id": "act-abcdef123456"})
        second = self.engine.act({"type": "text", "session_id": "same", "text": "hi",
                                  "client_request_id": "act-abcdef123456"})
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["replayed"])
        self.assertEqual(self.engine._iterm_write.call_count, 1)

    def test_a_pre_delivery_refusal_is_recorded_as_failed_not_uncertain(self):
        """act() refuses an empty text before touching the transport; recording
        that as uncertain would tell the user to go check a terminal that never
        received anything."""
        self._arm_text()
        out = self.engine.act({"type": "text", "session_id": "same", "text": "",
                               "client_request_id": "act-abcdef123456"})
        self.assertFalse(out["ok"])
        self.assertEqual(
            self.engine.act_receipt("act-abcdef123456")["receipt"]["state"], "failed")

    def test_an_uncertain_delivery_is_recorded_as_uncertain(self):
        self._arm_text()
        self.engine._iterm_write = mock.Mock(
            return_value={"ok": False, "error": "no result file"})
        self.engine.act({"type": "option", "session_id": "same", "nonce": "n",
                         "client_request_id": "act-abcdef123456"})
        state = self.engine.act_receipt("act-abcdef123456")["receipt"]["state"]
        self.assertIn(state, ("uncertain", "failed"))

    def test_a_nested_send_does_not_collide_with_the_outer_receipt(self):
        """`send_message` forwards its client_request_id to a nested direct send,
        and act() re-enters itself under the Claude mutation lock. Only the
        outermost call owns the receipt."""
        self._arm_text()
        self.engine.scan()          # _send_now_or_queue reads the snapshot cache
        out = self.engine.act({"type": "send_message", "session_id": "same",
                               "text": "hi", "client_request_id": "act-abcdef123456"})
        self.assertNotEqual(out.get("code"), "in_flight", out)
        self.assertTrue(out.get("ok") or out.get("queued"), out)

    def test_a_raising_dispatcher_records_uncertainty_and_still_raises(self):
        self._arm_text()
        with mock.patch.object(self.engine, "_act_dispatch",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.engine.act({"type": "text", "session_id": "same", "text": "hi",
                                 "client_request_id": "act-abcdef123456"})
        self.assertEqual(
            self.engine.act_receipt("act-abcdef123456")["receipt"]["state"], "uncertain")

    def test_a_non_dict_action_is_still_refused(self):
        self.assertFalse(self.engine.act("not an action")["ok"])


class ControlChangeTests(EngineFixture):
    def _arm_idle(self):
        self.write_registry(status="idle")
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.engine.scan()

    def test_session_settings_unknown_model(self):
        self._arm_idle()
        out = self.engine.act({"type": "session_settings", "session_id": "same",
                               "model": "gpt", "effort": "high",
                               "expected_model": "claude-sonnet",
                               "expected_effort": ""})
        self.assertIn("unknown Claude model", out["error"])

    def test_session_settings_unknown_effort(self):
        self._arm_idle()
        out = self.engine.act({"type": "session_settings", "session_id": "same",
                               "model": "sonnet", "effort": "turbo",
                               "expected_model": "claude-sonnet",
                               "expected_effort": ""})
        self.assertIn("unsupported effort", out["error"])

    def test_session_settings_missing_expected(self):
        self._arm_idle()
        out = self.engine.act({"type": "session_settings", "session_id": "same",
                               "model": "sonnet", "effort": "high"})
        self.assertEqual(out["code"], "stale_settings")

    def test_session_settings_stale_expected(self):
        self._arm_idle()
        out = self.engine.act({"type": "session_settings", "session_id": "same",
                               "model": "sonnet", "effort": "high",
                               "expected_model": "wrong", "expected_effort": "x"})
        self.assertEqual(out["code"], "stale_settings")

    def test_session_settings_partial_failure(self):
        self._arm_idle()
        results = [{"ok": True}, {"ok": False, "code": "delivery_uncertain",
                                  "error": "lost"}]
        self.engine._iterm_write = mock.Mock(side_effect=lambda *a, **k:
                                             results.pop(0))
        out = self.engine.act({"type": "session_settings", "session_id": "same",
                               "model": "opus", "effort": "high",
                               "expected_model": "claude-sonnet",
                               "expected_effort": ""})
        self.assertTrue(out["partial"])
        self.assertEqual(out["model"], "opus")

    def test_permission_mode_success(self):
        self._arm_idle()
        # Make the current permission mode 'default' and target 'plan'.
        tail = self.engine.tail_for(self.transcript)
        tail.permission_mode = "default"
        with mock.patch.object(self.engine, "_claude_permission_modes",
                               return_value=["default", "acceptEdits", "plan"]):
            self.engine._iterm_write = mock.Mock(return_value={"ok": True})
            out = self.engine.act({"type": "permission_mode",
                                   "session_id": "same", "mode": "plan"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mode"], "plan")

    def test_permission_mode_no_change(self):
        self._arm_idle()
        tail = self.engine.tail_for(self.transcript)
        tail.permission_mode = "plan"
        with mock.patch.object(self.engine, "_claude_permission_modes",
                               return_value=["default", "acceptEdits", "plan"]):
            out = self.engine.act({"type": "permission_mode",
                                   "session_id": "same", "mode": "plan"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "plan")

    def test_permission_mode_unavailable_target(self):
        self._arm_idle()
        tail = self.engine.tail_for(self.transcript)
        tail.permission_mode = "default"
        with mock.patch.object(self.engine, "_claude_permission_modes",
                               return_value=["default", "acceptEdits", "plan"]):
            out = self.engine.act({"type": "permission_mode",
                                   "session_id": "same", "mode": "auto"})
        self.assertIn("Auto is unavailable", out["error"])

    def test_permission_mode_unknown(self):
        self._arm_idle()
        out = self.engine.act({"type": "permission_mode", "session_id": "same",
                               "mode": "nonsense"})
        self.assertIn("unknown Claude permission mode", out["error"])

    def test_permission_mode_current_dontask(self):
        self._arm_idle()
        tail = self.engine.tail_for(self.transcript)
        tail.permission_mode = "dontAsk"
        with mock.patch.object(self.engine, "_claude_permission_modes",
                               return_value=["default", "plan"]):
            out = self.engine.act({"type": "permission_mode",
                                   "session_id": "same", "mode": "plan"})
        self.assertIn("startup-only", out["error"])

    def test_permission_mode_current_not_reported(self):
        self._arm_idle()
        tail = self.engine.tail_for(self.transcript)
        tail.permission_mode = "default"
        with mock.patch.object(self.engine, "_claude_permission_modes",
                               return_value=["plan", "acceptEdits"]):
            out = self.engine.act({"type": "permission_mode",
                                   "session_id": "same", "mode": "plan"})
        self.assertIn("not reported a live permission", out["error"])

    def test_permission_mode_bypass_unavailable(self):
        self._arm_idle()
        tail = self.engine.tail_for(self.transcript)
        tail.permission_mode = "default"
        with mock.patch.object(self.engine, "_claude_permission_modes",
                               return_value=["default", "plan"]):
            out = self.engine.act({"type": "permission_mode",
                                   "session_id": "same",
                                   "mode": "bypassPermissions"})
        self.assertIn("Bypass was not enabled", out["error"])

    def test_session_settings_no_change(self):
        # A transcript effort row makes the current effort a valid catalog value.
        self.append_transcript({"type": "assistant", "effort": "high",
            "timestamp": "2026-07-15T00:00:05Z",
            "message": {"role": "assistant", "model": "claude-sonnet",
                        "stop_reason": "end_turn", "usage": {"input_tokens": 1},
                        "content": [{"type": "text", "text": "row"}]}})
        self._arm_idle()
        tail = self.engine.tail_for(self.transcript)
        tail.model = "sonnet"
        self.assertEqual(self.engine.effort_for("same"), "high")
        out = self.engine.act({"type": "session_settings", "session_id": "same",
                               "model": "sonnet", "effort": "high",
                               "expected_model": "sonnet",
                               "expected_effort": "high"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["model"], "sonnet")

    def test_no_tty_is_rejected(self):
        self._arm_idle()
        with mock.patch.object(self.engine, "_tty_for_pid", return_value=None), \
             mock.patch.object(self.engine, "_is_background_claude",
                               return_value=False):
            out = self.engine.act({"type": "text", "session_id": "same",
                                   "text": "hi"})
        self.assertIn("no terminal", out["error"])


class TextRelayTests(EngineFixture):
    def _arm_idle(self):
        self.write_registry(status="idle")
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda *a, **k: None
        self.engine._tty_cache[PID] = "ttys-test"
        self.writes = []
        self.engine._iterm_write = mock.Mock(
            side_effect=lambda tty, steps, step_delay=None:
            self.writes.append((tty, steps, step_delay)) or {"ok": True})

    def test_text_with_slash_gets_space(self):
        self._arm_idle()
        out = self.engine.act({"type": "text", "session_id": "same",
                               "text": "/status"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.writes[0][1], [("/status ", True)])

    def test_empty_text(self):
        self._arm_idle()
        out = self.engine.act({"type": "text", "session_id": "same", "text": "   "})
        self.assertIn("empty text", out["error"])

    def test_noop(self):
        self._arm_idle()
        out = self.engine.act({"type": "noop", "session_id": "same"})
        self.assertTrue(out["ok"], out)

    def test_relay_to_subagent(self):
        subdir = os.path.join(os.path.dirname(self.transcript), "same", "subagents")
        os.makedirs(subdir)
        aid = "agent-r1"
        with open(os.path.join(subdir, aid + ".meta.json"), "w") as handle:
            handle.write('{"description": "a child"}')
        with open(os.path.join(subdir, aid + ".jsonl"), "w") as handle:
            handle.write("{}\n")
        self._arm_idle()
        out = self.engine.act({"type": "relay", "session_id": "same",
                               "agent_id": aid, "text": "forward this"})
        self.assertTrue(out["ok"], out)
        self.assertIn("relay to subagent", self.writes[0][1][0][0])

    def test_relay_no_such_agent(self):
        self._arm_idle()
        out = self.engine.act({"type": "relay", "session_id": "same",
                               "agent_id": "agent-missing", "text": "x"})
        self.assertIn("no such subagent", out["error"])

    def test_relay_empty_text(self):
        subdir = os.path.join(os.path.dirname(self.transcript), "same", "subagents")
        os.makedirs(subdir)
        aid = "agent-r2"
        with open(os.path.join(subdir, aid + ".meta.json"), "w") as handle:
            handle.write("{}")
        with open(os.path.join(subdir, aid + ".jsonl"), "w") as handle:
            handle.write("{}\n")
        self._arm_idle()
        out = self.engine.act({"type": "relay", "session_id": "same",
                               "agent_id": aid, "text": "   "})
        self.assertIn("empty text", out["error"])

    def test_image_text_no_images(self):
        self._arm_idle()
        # image_text with resolved empty paths -> "no images" after resolution;
        # here upload resolution fails first, so exercise the empty-path guard
        # by passing image_paths that get stripped and no upload_ids.
        out = self.engine.act({"type": "image_text", "session_id": "same",
                               "text": "look", "upload_ids": ["missing"]})
        self.assertIn("missing, expired", out["error"])


if __name__ == "__main__":
    unittest.main()
