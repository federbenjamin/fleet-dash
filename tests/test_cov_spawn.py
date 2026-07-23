"""Coverage for fleetdash.engine_spawn: settings validation, spawn/handoff
composition, the applet exchange, and the legacy ntfy test route."""
import os
import subprocess
import unittest
import uuid
from unittest import mock

from fleetdash import engine_spawn as spawn_module
from tests.test_cov_common import EngineFixture


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


class UpdateSettingsTests(EngineFixture):
    def test_rejects_non_dict(self):
        self.assertFalse(self.engine.update_settings("x")["ok"])

    def test_unknown_field(self):
        out = self.engine.update_settings({"nope": 1})
        self.assertIn("unknown settings field", out["error"])

    def test_dependent_fields(self):
        self.assertIn("muted requires",
                      self.engine.update_settings({"muted": True})["error"])
        self.assertIn("pinned requires",
                      self.engine.update_settings({"pinned": True})["error"])
        self.assertIn("revision requires",
                      self.engine.update_settings({"revision": "r"})["error"])

    def test_numeric_validation(self):
        self.assertIn("bad value", self.engine.update_settings(
            {"stall_seconds": True})["error"])
        self.assertIn("bad value", self.engine.update_settings(
            {"stall_seconds": "abc"})["error"])
        self.assertIn("must be", self.engine.update_settings(
            {"stall_seconds": 5})["error"])
        self.assertTrue(self.engine.update_settings({"stall_seconds": 700})["ok"])

    def test_bool_validation(self):
        self.assertIn("must be boolean", self.engine.update_settings(
            {"preview_sessions": "yes"})["error"])
        self.assertTrue(self.engine.update_settings(
            {"preview_sessions": False})["ok"])

    def test_reader_width(self):
        self.assertIn("fit or centered", self.engine.update_settings(
            {"reader_width": "wide"})["error"])
        self.assertTrue(self.engine.update_settings(
            {"reader_width": "centered"})["ok"])

    def test_mute_session(self):
        self.assertIn("invalid mute_session", self.engine.update_settings(
            {"mute_session": "", "muted": True})["error"])
        self.assertIn("muted must be boolean", self.engine.update_settings(
            {"mute_session": "s1", "muted": "x"})["error"])
        out = self.engine.update_settings({"mute_session": "s1", "muted": True})
        self.assertIn("s1", out["muted_sessions"])
        out = self.engine.update_settings({"mute_session": "s1", "muted": False})
        self.assertNotIn("s1", out["muted_sessions"])

    def test_pin_session(self):
        self.assertIn("valid pin_session", self.engine.update_settings(
            {"pin_session": "", "pinned": True})["error"])
        self.assertIn("pinned must be boolean", self.engine.update_settings(
            {"pin_session": "s1", "pinned": "x"})["error"])
        out = self.engine.update_settings({"pin_session": "s1", "pinned": True})
        self.assertIn("s1", out["pinned_sessions"])

    def test_mark_available_and_read(self):
        self.assertIn("required", self.engine.update_settings(
            {"mark_available_session": "s1", "revision": ""})["error"])
        out = self.engine.update_settings(
            {"mark_available_session": "s1", "revision": "r1"})
        self.assertEqual(out["reply_available"]["s1"], "r1")
        out = self.engine.update_settings(
            {"mark_read_session": "s1", "revision": "r2"})
        self.assertEqual(out["read_sessions"]["s1"], "r2")

    def test_bulk_triage_validation(self):
        self.assertIn("must be an object", self.engine.update_settings(
            {"bulk_triage": "x"})["error"])
        self.assertIn("unsupported bulk", self.engine.update_settings(
            {"bulk_triage": {"operation": "nope", "items": []}})["error"])
        self.assertIn("1–100 items", self.engine.update_settings(
            {"bulk_triage": {"operation": "mute", "items": []}})["error"])
        self.assertIn("session and action", self.engine.update_settings(
            {"bulk_triage": {"operation": "mute",
                             "items": [{"session_id": "", "action_id": ""}]}})["error"])
        self.assertIn("revision is required", self.engine.update_settings(
            {"bulk_triage": {"operation": "mark_read",
                             "items": [{"session_id": "s", "action_id": "a"}]}})["error"])

    def test_bulk_triage_mute_and_dismiss(self):
        out = self.engine.update_settings({"bulk_triage": {
            "operation": "mute",
            "items": [{"session_id": "s1", "action_id": "a1"}]}})
        self.assertIn("s1", out["muted_sessions"])
        with self.engine.lock:
            self.engine.snapshot_cache = {"actions": [
                {"action_id": "a1", "session_id": "s1", "revision": "",
                 "safe_bulk": ["dismiss"]}]}
        out = self.engine.update_settings({"bulk_triage": {
            "operation": "dismiss",
            "items": [{"session_id": "s1", "action_id": "a1"}]}})
        self.assertIn("a1", out["dismissed_actions"])

    def test_bulk_triage_mark_read_requires_eligible_action(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"actions": [
                {"action_id": "a1", "session_id": "s1", "revision": "r1",
                 "safe_bulk": ["mark_read"]}]}
        out = self.engine.update_settings({"bulk_triage": {
            "operation": "mark_read",
            "items": [{"session_id": "s1", "action_id": "a1", "revision": "r1"}]}})
        self.assertEqual(out["read_sessions"]["s1"], "r1")
        # A stale/ineligible action is rejected.
        bad = self.engine.update_settings({"bulk_triage": {
            "operation": "mark_read",
            "items": [{"session_id": "s1", "action_id": "missing",
                       "revision": "r1"}]}})
        self.assertIn("stale or ineligible", bad["error"])

    def test_bulk_item_not_dict(self):
        out = self.engine.update_settings({"bulk_triage": {
            "operation": "mute", "items": ["notadict"]}})
        self.assertIn("invalid bulk triage item", out["error"])

    def test_mute_too_many_sessions(self):
        self.engine.cfg["muted_sessions"] = {f"s{i}": 1 for i in range(5001)}
        out = self.engine.update_settings({"mute_session": "extra", "muted": True})
        self.assertIn("too many muted", out["error"])

    def test_bulk_mute_too_many_sessions(self):
        self.engine.cfg["muted_sessions"] = {f"s{i}": 1 for i in range(5001)}
        out = self.engine.update_settings({"bulk_triage": {
            "operation": "mute",
            "items": [{"session_id": "extra", "action_id": "a"}]}})
        self.assertIn("too many muted", out["error"])

    def test_budgets_update_and_error(self):
        from fleetdash.briefing import OperationsError
        with mock.patch.object(self.engine.operations, "replace_budgets",
                               return_value=[{"id": "b1"}]):
            out = self.engine.update_settings({"budgets": [{"id": "b1"}]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["budgets"], [{"id": "b1"}])
        with mock.patch.object(self.engine.operations, "replace_budgets",
                               side_effect=OperationsError("bad budget")):
            out = self.engine.update_settings({"budgets": [{"id": "b1"}]})
        self.assertIn("bad budget", out["error"])

    def test_nothing_to_update(self):
        self.assertIn("nothing to update", self.engine.update_settings({})["error"])


class SpawnSessionTests(EngineFixture):
    def test_no_such_directory(self):
        out = self.engine.spawn_session({"cwd": "/no/such/dir"})
        self.assertIn("no such directory", out["error"])

    def test_outside_home(self):
        out = self.engine.spawn_session({"cwd": "/"})
        self.assertIn("under your home folder", out["error"])

    def test_unknown_model_and_effort(self):
        self.assertIn("unknown model", self.engine.spawn_session(
            {"cwd": self.cwd, "model": "gpt"})["error"])
        self.assertIn("unknown effort", self.engine.spawn_session(
            {"cwd": self.cwd, "effort": "turbo"})["error"])

    def test_unknown_permission_mode(self):
        out = self.engine.spawn_session(
            {"cwd": self.cwd, "permission_mode": "bogus"})
        self.assertIn("unknown Claude permission mode", out["error"])

    def test_bad_worktree_name(self):
        out = self.engine.spawn_session(
            {"cwd": self.cwd, "worktree_name": "bad name!"})
        self.assertIn("worktree name", out["error"])

    def test_worktree_requires_repo(self):
        out = self.engine.spawn_session({"cwd": self.cwd, "worktree": True})
        self.assertIn("not a git repo", out["error"])

    def test_invalid_reserved_sid(self):
        out = self.engine.spawn_session({"cwd": self.cwd}, reserved_sid="not-a-uuid")
        self.assertIn("invalid Claude session id", out["error"])

    def test_successful_spawn_composes_command(self):
        with mock.patch.object(self.engine, "_iterm_write",
                               return_value={"ok": True}):
            out = self.engine.spawn_session(
                {"cwd": self.cwd, "model": "sonnet", "effort": "high",
                 "permission_mode": "plan"})
        self.assertTrue(out["ok"])
        self.assertIn("--model sonnet", out["command"])
        self.assertIn("--effort high", out["command"])
        self.assertIn("--permission-mode plan", out["command"])
        self.assertEqual(out["provider"], "claude")

    def test_spawn_with_worktree(self):
        os.makedirs(os.path.join(self.cwd, ".git"), exist_ok=True)
        with mock.patch.object(self.engine, "_iterm_write",
                               return_value={"ok": True}):
            out = self.engine.spawn_session(
                {"cwd": self.cwd, "worktree": True, "worktree_name": "wt1"})
        self.assertIn("--worktree wt1", out["command"])


class SpawnCodexTests(EngineFixture):
    def test_no_such_directory(self):
        out = self.engine.spawn_codex_session({"cwd": "/no/such"})
        self.assertIn("no such directory", out["error"])

    def test_outside_home(self):
        self.assertIn("under your home folder",
                      self.engine.spawn_codex_session({"cwd": "/"})["error"])

    def test_initial_text_bounds(self):
        self.assertIn("1–2,000", self.engine.spawn_codex_session(
            {"cwd": self.cwd, "initial_text": "x" * 2001})["error"])

    def test_success(self):
        out = self.engine.spawn_codex_session(
            {"cwd": self.cwd, "initial_text": "build it"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["session_id"], "codex:new-thread")

    def test_start_thread_exception(self):
        self.codex.start_thread = mock.Mock(side_effect=RuntimeError("codex down"))
        out = self.engine.spawn_codex_session({"cwd": self.cwd})
        self.assertIn("codex down", out["error"])

    def test_budget_blocked(self):
        with mock.patch.object(self.engine.operations, "has_spawn_limits",
                               return_value=True), \
             mock.patch.object(self.engine.operations, "spawn_blockers",
                               return_value=[{"id": "b", "label": "L",
                                              "scope_type": "fleet"}]):
            out = self.engine.spawn_codex_session({"cwd": self.cwd})
        self.assertIn("blocked by an exceeded budget", out["error"])


class StagingDispatchTests(EngineFixture):
    def setUp(self):
        super().setUp()
        self.engine.cfg["instance_mode"] = "staging"

    def test_claude_spawn_dispatches_to_staging(self):
        with mock.patch.object(self.engine, "_spawn_staging_session",
                               return_value={"ok": True, "staging": True}) as sp:
            out = self.engine.spawn_session({"cwd": self.cwd})
        self.assertTrue(out["staging"])
        self.assertEqual(sp.call_args[0][1], "claude")

    def test_codex_spawn_dispatches_to_staging(self):
        with mock.patch.object(self.engine, "_spawn_staging_session",
                               return_value={"ok": True, "staging": True}) as sp:
            out = self.engine.spawn_codex_session({"cwd": self.cwd})
        self.assertTrue(out["staging"])
        self.assertEqual(sp.call_args[0][1], "codex")


class CodexWorktreeTests(EngineFixture):
    def test_requires_git_repo(self):
        out = self.engine._create_codex_worktree(self.tmp.name)
        self.assertIn("requires a Git repository", out["error"])

    def test_bad_name(self):
        root = os.path.join(self.tmp.name, "gitrepo")
        os.makedirs(root)
        git("init", "-q", root)
        out = self.engine._create_codex_worktree(root, "bad name!")
        self.assertIn("worktree name", out["error"])

    def test_create_and_remove(self):
        root = os.path.join(self.tmp.name, "gitrepo2")
        os.makedirs(root)
        git("init", "-q", root)
        git("-C", root, "config", "user.email", "a@b.c")
        git("-C", root, "config", "user.name", "T")
        with open(os.path.join(root, "f.txt"), "w") as handle:
            handle.write("hi\n")
        git("-C", root, "add", "f.txt")
        git("-C", root, "commit", "-qm", "first")
        with mock.patch.object(spawn_module.pathcfg, "HOME", self.tmp.name):
            created = self.engine._create_codex_worktree(root, "wtname")
        self.assertTrue(created["ok"], created)
        self.assertTrue(os.path.isdir(created["cwd"]))
        # Removing it again succeeds (no error string).
        self.assertIsNone(self.engine._remove_failed_codex_worktree(created))

    def test_remove_ignores_non_created(self):
        self.assertIsNone(self.engine._remove_failed_codex_worktree(None))
        self.assertIsNone(self.engine._remove_failed_codex_worktree(
            {"created": False}))

    def test_worktree_path_already_exists(self):
        root = os.path.join(self.tmp.name, "gitrepo3")
        os.makedirs(root)
        git("init", "-q", root)
        with mock.patch.object(spawn_module.pathcfg, "HOME", self.tmp.name), \
             mock.patch.object(spawn_module.os.path, "lexists",
                               return_value=True):
            out = self.engine._create_codex_worktree(root, "dup")
        self.assertIn("already exists", out["error"])

    def test_worktree_add_returncode_failure(self):
        root = os.path.join(self.tmp.name, "gitrepo4")
        os.makedirs(root)
        git("init", "-q", root)
        with mock.patch.object(spawn_module.pathcfg, "HOME", self.tmp.name), \
             mock.patch.object(spawn_module.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 1, "", "worktree add failed")):
            out = self.engine._create_codex_worktree(root, "wt")
        self.assertIn("worktree add failed", out["error"])

    def test_worktree_add_exception(self):
        root = os.path.join(self.tmp.name, "gitrepo5")
        os.makedirs(root)
        git("init", "-q", root)
        with mock.patch.object(spawn_module.pathcfg, "HOME", self.tmp.name), \
             mock.patch.object(spawn_module.subprocess, "run",
                               side_effect=OSError("git missing")):
            out = self.engine._create_codex_worktree(root, "wt")
        self.assertIn("could not create worktree", out["error"])

    def test_remove_failed_returncode_and_exception(self):
        created = {"created": True, "root": "/r", "cwd": "/r/wt"}
        with mock.patch.object(spawn_module.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 1, "", "cannot remove")):
            self.assertIn("cannot remove",
                          self.engine._remove_failed_codex_worktree(created))
        with mock.patch.object(spawn_module.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertIn("boom",
                          self.engine._remove_failed_codex_worktree(created))


class HandoffTests(EngineFixture):
    def test_missing_source(self):
        out = self.engine.execute_handoff({"session_id": "nope"})
        self.assertIn("source session is unavailable", out["error"])

    def _seed_source(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [
                {"session_id": "same", "provider": "claude", "cwd": self.cwd,
                 "title": "S"}]}

    def test_bad_provider(self):
        self._seed_source()
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "gemini", "preview": "x"})
        self.assertIn("provider must be", out["error"])

    def test_empty_and_too_long_preview(self):
        self._seed_source()
        self.assertIn("is empty", self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex", "preview": "  "})["error"])
        self.assertIn("too long", self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex",
             "preview": "x" * 30001})["error"])

    def test_codex_handoff_creates_thread(self):
        self._seed_source()
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex", "preview": "do the work",
             "cwd": self.cwd, "mode": "plan"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["destination_session_id"], "codex:new-thread")

    def test_codex_handoff_bad_mode(self):
        self._seed_source()
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex", "preview": "x",
             "cwd": self.cwd, "mode": "nope"})
        self.assertIn("mode must be", out["error"])

    def test_codex_handoff_no_thread_id(self):
        self._seed_source()
        self.codex.start_thread = mock.Mock(return_value={})
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex", "preview": "x",
             "cwd": self.cwd})
        self.assertIn("did not return a thread id", out["error"])

    def test_handoff_bad_cwd(self):
        self._seed_source()
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex", "preview": "x",
             "cwd": "/no/such/dir"})
        self.assertIn("no such directory", out["error"])

    def test_handoff_cwd_outside_home(self):
        self._seed_source()
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex", "preview": "x",
             "cwd": "/"})
        self.assertIn("under your home folder", out["error"])

    def test_codex_handoff_start_thread_exception_cleans_worktree(self):
        self._seed_source()
        self.codex.start_thread = mock.Mock(side_effect=RuntimeError("nope"))
        with mock.patch.object(self.engine, "_create_codex_worktree",
                               return_value={"ok": True, "created": True,
                                             "cwd": self.cwd, "root": self.cwd}), \
             mock.patch.object(self.engine, "_remove_failed_codex_worktree",
                               return_value="cleanup problem"):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "codex", "preview": "x",
                 "cwd": self.cwd, "worktree": True})
        self.assertIn("nope", out["error"])
        self.assertEqual(out["cleanup_error"], "cleanup problem")

    def test_codex_handoff_no_thread_id_cleans_worktree(self):
        self._seed_source()
        self.codex.start_thread = mock.Mock(return_value={})
        with mock.patch.object(self.engine, "_create_codex_worktree",
                               return_value={"ok": True, "created": True,
                                             "cwd": self.cwd, "root": self.cwd}), \
             mock.patch.object(self.engine, "_remove_failed_codex_worktree",
                               return_value="cleanup issue"):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "codex", "preview": "x",
                 "cwd": self.cwd, "worktree": True})
        self.assertIn("did not return a thread id", out["error"])
        self.assertEqual(out["cleanup_error"], "cleanup issue")

    def test_codex_handoff_worktree_create_fails(self):
        self._seed_source()
        with mock.patch.object(self.engine, "_create_codex_worktree",
                               return_value={"ok": False, "error": "no repo"}):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "codex", "preview": "x",
                 "cwd": self.cwd, "worktree": True})
        self.assertIn("no repo", out["error"])

    def test_claude_handoff_retry_delivers_through_act(self):
        self._seed_source()
        self.engine._record_handoff_link(
            "same", "claude", "claude-dest", "claude", "delivered", "hash")
        with mock.patch.object(self.engine, "act",
                               return_value={"ok": True}) as act:
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "claude", "preview": "retry",
                 "destination_session_id": "claude-dest"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(act.call_args[0][0]["type"], "handoff_text")


class HandoffAdvancedTests(EngineFixture):
    def _seed_source(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [
                {"session_id": "same", "provider": "claude", "cwd": self.cwd,
                 "title": "S"}]}

    def test_claude_handoff_full_path(self):
        self._seed_source()
        fixed = uuid.UUID("11111111-2222-3333-4444-555555555555")
        with mock.patch.object(spawn_module.uuid, "uuid4", return_value=fixed), \
             mock.patch.object(self.engine, "is_trusted", return_value=True), \
             mock.patch.object(self.engine, "spawn_session",
                               return_value={"ok": True}), \
             mock.patch.object(self.engine, "live_sessions",
                               return_value=[{"sessionId": str(fixed)}]), \
             mock.patch.object(self.engine, "act",
                               return_value={"ok": True}):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "claude",
                 "preview": "continue the work", "cwd": self.cwd})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["destination_session_id"], str(fixed))

    def test_claude_handoff_untrusted_dir(self):
        self._seed_source()
        with mock.patch.object(self.engine, "is_trusted", return_value=False):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "claude",
                 "preview": "x", "cwd": self.cwd})
        self.assertIn("has not trusted", out["error"])

    def test_claude_handoff_spawn_fails(self):
        self._seed_source()
        with mock.patch.object(self.engine, "is_trusted", return_value=True), \
             mock.patch.object(self.engine, "spawn_session",
                               return_value={"ok": False, "error": "boom"}):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "claude",
                 "preview": "x", "cwd": self.cwd})
        self.assertIn("boom", out["error"])

    def test_claude_handoff_not_attachable(self):
        self._seed_source()
        fixed = uuid.UUID("66666666-2222-3333-4444-555555555555")
        with mock.patch.object(spawn_module.uuid, "uuid4", return_value=fixed), \
             mock.patch.object(self.engine, "is_trusted", return_value=True), \
             mock.patch.object(self.engine, "spawn_session",
                               return_value={"ok": True}), \
             mock.patch.object(self.engine, "live_sessions", return_value=[]), \
             mock.patch.object(spawn_module.time, "sleep"), \
             mock.patch.object(spawn_module.time, "monotonic",
                               side_effect=[0, 0, 100]):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "claude",
                 "preview": "x", "cwd": self.cwd})
        self.assertIn("did not become attachable", out["error"])
        self.assertTrue(out["retryable"])

    def test_handoff_retry_existing_codex_link(self):
        self._seed_source()
        self.engine._record_handoff_link(
            "same", "claude", "codex:dest", "codex", "delivered", "hash")
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex",
             "preview": "retry this", "destination_session_id": "codex:dest"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["created"])
        self.assertEqual(self.codex.actions[-1]["type"], "text")

    def test_handoff_retry_mismatched_destination(self):
        self._seed_source()
        out = self.engine.execute_handoff(
            {"session_id": "same", "provider": "codex", "preview": "x",
             "destination_session_id": "codex:unknown"})
        self.assertIn("stale or mismatched", out["error"])

    def test_codex_handoff_with_worktree(self):
        root = os.path.join(self.tmp.name, "hrepo")
        os.makedirs(root)
        git("init", "-q", root)
        git("-C", root, "config", "user.email", "a@b.c")
        git("-C", root, "config", "user.name", "T")
        with open(os.path.join(root, "f.txt"), "w") as handle:
            handle.write("hi\n")
        git("-C", root, "add", "f.txt")
        git("-C", root, "commit", "-qm", "first")
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [
                {"session_id": "same", "provider": "claude", "cwd": root,
                 "title": "S"}]}
        with mock.patch.object(spawn_module.pathcfg, "HOME", self.tmp.name):
            out = self.engine.execute_handoff(
                {"session_id": "same", "provider": "codex", "preview": "work",
                 "cwd": root, "worktree": True, "worktree_name": "hwt"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["worktree"]["created"])


class CodexFocusTests(EngineFixture):
    def test_view_only_thread(self):
        self.codex.session = {"session_id": "codex:t1", "read_only": True}
        out = self.engine.focus_codex_terminal({"session_id": "codex:t1"})
        self.assertIn("view only", out["error"])

    def test_no_terminal_route(self):
        self.codex.session = {"session_id": "codex:t1"}
        with mock.patch.object(self.engine, "_codex_terminal_route",
                               return_value=None):
            out = self.engine.focus_codex_terminal({"session_id": "codex:t1"})
        self.assertIn("no attached Codex terminal", out["error"])

    def test_focus_success(self):
        self.codex.session = {"session_id": "codex:t1"}
        with mock.patch.object(self.engine, "_codex_terminal_route",
                               return_value={"tty": "ttys009"}), \
             mock.patch.object(self.engine, "_iterm_write",
                               return_value={"ok": True}):
            out = self.engine.focus_codex_terminal({"session_id": "codex:t1"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["focused"])


class BudgetBlockerTests(EngineFixture):
    def test_spawn_blocked_by_budget(self):
        with mock.patch.object(self.engine.operations, "has_spawn_limits",
                               return_value=True), \
             mock.patch.object(self.engine.operations, "spawn_blockers",
                               return_value=[{"id": "b1", "label": "Cap",
                                              "scope_type": "fleet"}]):
            out = self.engine.spawn_session({"cwd": self.cwd})
        self.assertIn("blocked by an exceeded budget", out["error"])
        self.assertEqual(out["budget_blockers"][0]["id"], "b1")

    def test_budget_check_exception_is_conservative(self):
        with mock.patch.object(self.engine.operations, "has_spawn_limits",
                               return_value=True), \
             mock.patch.object(self.engine.operations, "spawn_blockers",
                               side_effect=RuntimeError("db down")):
            blockers = self.engine._spawn_budget_blockers("claude", self.cwd)
        self.assertEqual(blockers[0]["id"], "budget-check-unavailable")


class LegacyNtfyTests(EngineFixture):
    def test_disabled(self):
        out = self.engine.legacy_ntfy_test()
        self.assertIn("disabled", out["error"])

    def test_not_configured(self):
        self.engine.cfg["legacy_ntfy_enabled"] = True
        self.engine.cfg["ntfy_topic"] = ""
        out = self.engine.legacy_ntfy_test()
        self.assertIn("not configured", out["error"])

    def test_queued_runs_synchronously(self):
        self.engine.cfg["legacy_ntfy_enabled"] = True
        self.engine.cfg["ntfy_topic"] = "mytopic"
        self.engine.cfg["ntfy_server"] = "https://ntfy.example"

        class SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()
        with mock.patch.object(spawn_module.threading, "Thread", SyncThread), \
             mock.patch.object(spawn_module.urllib.request, "urlopen"):
            out = self.engine.legacy_ntfy_test()
        self.assertTrue(out["ok"])
        self.assertTrue(out["queued"])

    def test_claim_failure(self):
        self.engine.cfg["legacy_ntfy_enabled"] = True
        self.engine.cfg["ntfy_topic"] = "mytopic"
        self.engine.cfg["ntfy_server"] = "https://ntfy.example"
        with mock.patch.object(self.engine.operations, "notification_claim",
                               return_value=False):
            out = self.engine.legacy_ntfy_test()
        self.assertIn("could not be queued", out["error"])

    def test_send_disabled_topic_marks_status(self):
        with mock.patch.object(self.engine.operations,
                               "notification_status") as status:
            self.engine.cfg["ntfy_topic"] = ""
            self.engine._send_legacy_ntfy_test("k1")
        status.assert_called_with("k1", "disabled")

    def test_post_failure_marks_failed(self):
        with mock.patch.object(spawn_module.urllib.request, "urlopen",
                               side_effect=OSError("network")), \
             mock.patch.object(self.engine.operations,
                               "notification_status") as status:
            self.engine._post("k2", mock.Mock())
        self.assertEqual(status.call_args[0][1], "failed")


class ItermWriteTests(EngineFixture):
    def _fake_open(self, verdict="ok"):
        def run(argv, **kwargs):
            req_path = os.path.join(self.base, "inject-request.txt")
            res_path = os.path.join(self.base, "inject-result.txt")
            with open(req_path) as handle:
                req_id = handle.read().splitlines()[1]
            with open(res_path, "w") as handle:
                handle.write(f"{req_id} {verdict}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run

    def test_successful_write(self):
        with mock.patch.object(spawn_module.subprocess, "run",
                               side_effect=self._fake_open("ok")):
            out = self.engine._iterm_write("/dev/ttys001",
                                           [("hello", True)], step_delay=0.05)
        self.assertTrue(out["ok"])

    def test_focus_and_uncertain_verdict(self):
        with mock.patch.object(spawn_module.subprocess, "run",
                               side_effect=self._fake_open("lost")):
            out = self.engine._iterm_write("/dev/ttys001",
                                           [("__FOCUS__", False)], step_delay=0.05)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "delivery_uncertain")

    def test_open_launch_failure(self):
        with mock.patch.object(spawn_module.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 1, "", "launch denied")):
            out = self.engine._iterm_write("/dev/ttys001", [("hi", True)])
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "injector_not_launched")

    def test_open_raises(self):
        with mock.patch.object(spawn_module.subprocess, "run",
                               side_effect=OSError("open missing")):
            out = self.engine._iterm_write("/dev/ttys001", [("hi", True)])
        self.assertEqual(out["code"], "injector_not_launched")

    def test_result_never_matches_times_out(self):
        def run(argv, **kwargs):
            # Never write the result file, so the read raises (OSError branch)
            # and the poll loop exhausts its deadline.
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(spawn_module.subprocess, "run", side_effect=run), \
             mock.patch.object(spawn_module.time, "sleep"), \
             mock.patch.object(spawn_module.time, "time",
                               side_effect=[0, 0, 100]):
            out = self.engine._iterm_write("/dev/ttys001", [("hi", True)])
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "delivery_uncertain")
        self.assertIn("was lost", out["error"])


if __name__ == "__main__":
    unittest.main()
