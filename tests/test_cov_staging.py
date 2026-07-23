"""Coverage for fleetdash/engine_staging.py — staging-instance isolation
(invariant 56): codex launcher install, owned-session registration/trim,
staging workspace creation, spawn wrapping, and view-only masking."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))   # sibling test_cov_common import

from fleetdash import engine_staging as staging_mod
from test_cov_common import EngineCovBase


class StagingCovTest(EngineCovBase):
    # -------------------------------------------------- _ensure_codex_launcher
    def test_launcher_skips_when_staging(self):
        self.engine.cfg["instance_mode"] = "staging"
        self.engine.codex_launcher_status = None
        self.engine._ensure_codex_launcher()
        self.assertIsNone(self.engine.codex_launcher_status)

    def test_launcher_skips_when_already_ready(self):
        self.engine.cfg["codex_enabled"] = True
        self.engine.codex_launcher_status = {"state": "ready"}
        self.engine._ensure_codex_launcher()
        self.assertEqual(self.engine.codex_launcher_status["state"], "ready")

    def test_launcher_waits_for_committed_runtime(self):
        self.engine.cfg["codex_enabled"] = True
        self.engine.codex_launcher_status = {"state": "waiting_runtime"}
        self.engine.codex = SimpleNamespace(
            runtime_status=lambda: {"phase": "pending"},
            client=SimpleNamespace(connection_state="ready"))
        self.engine._ensure_codex_launcher()
        self.assertEqual(self.engine.codex_launcher_status["state"], "waiting_runtime")

    def test_launcher_installs_when_runtime_ready(self):
        self.engine.cfg["codex_enabled"] = True
        self.engine.codex_launcher_status = {"state": "waiting_runtime"}
        self.engine.codex = SimpleNamespace(
            runtime_status=lambda: {"phase": "committed"},
            client=SimpleNamespace(connection_state="ready"))
        with mock.patch("fleetdash.codex_launcher.install_launcher",
                        return_value={"installed": True, "state": "ready"}) as inst:
            self.engine._ensure_codex_launcher()
        self.assertTrue(inst.called)
        self.assertEqual(self.engine.codex_launcher_status["state"], "ready")

    def test_launcher_install_exception_marks_repair(self):
        self.engine.cfg["codex_enabled"] = True
        self.engine.codex_launcher_status = {"state": "waiting_runtime"}
        self.engine.codex = SimpleNamespace(
            runtime_status=lambda: {"phase": "committed"},
            client=SimpleNamespace(connection_state="ready"))
        with mock.patch("fleetdash.codex_launcher.install_launcher",
                        side_effect=RuntimeError("boom")):
            self.engine._ensure_codex_launcher()
        self.assertEqual(self.engine.codex_launcher_status["state"], "repair_needed")
        self.assertIn("boom", self.engine.codex_launcher_status["error"])

    # ------------------------------------------------- register/owned helpers
    def test_register_staging_session_persists(self):
        self.engine.cfg["instance_mode"] = "staging"
        self.engine._register_staging_session("sid-1", "claude", self.cwd)
        self.assertTrue(self.engine._staging_owns("sid-1"))
        self.assertEqual(self.engine._staging_owned()["sid-1"]["provider"], "claude")

    def test_register_staging_session_trims_over_500(self):
        self.engine.cfg["instance_mode"] = "staging"
        seed = {f"old-{i}": {"provider": "claude", "cwd": self.cwd,
                             "created_at": float(i)} for i in range(501)}
        self.engine.cfg["staging_owned_sessions"] = seed
        self.engine._register_staging_session("newest", "claude", self.cwd)
        records = self.engine._staging_owned()
        self.assertLessEqual(len(records), 500)
        self.assertIn("newest", records)
        self.assertNotIn("old-0", records)   # oldest by created_at dropped

    def test_register_ignored_when_not_staging(self):
        self.engine.cfg.pop("instance_mode", None)
        self.engine._register_staging_session("x", "claude", self.cwd)
        self.assertFalse(self.engine._staging_owns("x"))

    def test_staging_owned_non_dict_config(self):
        self.engine.cfg["staging_owned_sessions"] = ["not", "a", "dict"]
        self.assertEqual(self.engine._staging_owned(), {})

    # --------------------------------------------------- _staging_source_root
    def test_source_root_none_when_missing_dir(self):
        missing = os.path.join(self.tmp.name, "does-not-exist")
        with mock.patch.dict(os.environ, {"FLEET_DASH_STAGING_SOURCE": missing},
                             clear=False):
            self.assertIsNone(self.engine._staging_source_root())

    def test_source_root_resolves_git_checkout(self):
        src = os.path.join(self.tmp.name, "src")
        os.makedirs(os.path.join(src, ".git"))
        with mock.patch.dict(os.environ, {"FLEET_DASH_STAGING_SOURCE": src},
                             clear=False):
            self.assertEqual(self.engine._staging_source_root(),
                             os.path.realpath(src))

    def test_source_root_none_when_not_git(self):
        src = os.path.join(self.tmp.name, "plain")
        os.makedirs(src)
        with mock.patch.dict(os.environ, {"FLEET_DASH_STAGING_SOURCE": src},
                             clear=False):
            self.assertIsNone(self.engine._staging_source_root())

    # ------------------------------------------------ _create_staging_workspace
    def _with_source(self):
        src = os.path.join(self.tmp.name, "src")
        os.makedirs(os.path.join(src, ".git"), exist_ok=True)
        return mock.patch.dict(os.environ, {"FLEET_DASH_STAGING_SOURCE": src},
                               clear=False)

    def test_create_workspace_success(self):
        with self._with_source(), mock.patch.object(
                staging_mod.subprocess, "run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            result = self.engine._create_staging_workspace("feature")
        self.assertTrue(result["ok"])
        self.assertTrue(result["worktree_name"].startswith("feature-"))
        self.assertTrue(result["branch"].startswith("fleet-staging/"))

    def test_create_workspace_no_source(self):
        with mock.patch.object(self.engine, "_staging_source_root",
                               return_value=None):
            result = self.engine._create_staging_workspace("x")
        self.assertFalse(result["ok"])
        self.assertIn("unavailable", result["error"])

    def test_create_workspace_bad_name(self):
        with self._with_source():
            result = self.engine._create_staging_workspace("bad name!")
        self.assertFalse(result["ok"])
        self.assertIn("worktree name", result["error"])

    def test_create_workspace_git_failure(self):
        with self._with_source(), mock.patch.object(
                staging_mod.subprocess, "run",
                return_value=SimpleNamespace(returncode=1, stdout="",
                                             stderr="fatal: already exists")):
            result = self.engine._create_staging_workspace("f")
        self.assertFalse(result["ok"])
        self.assertIn("already exists", result["error"])

    def test_create_workspace_subprocess_exception(self):
        with self._with_source(), mock.patch.object(
                staging_mod.subprocess, "run",
                side_effect=OSError("no git")):
            result = self.engine._create_staging_workspace("f")
        self.assertFalse(result["ok"])
        self.assertIn("could not create", result["error"])

    # -------------------------------------------------- _spawn_staging_session
    def test_spawn_staging_workspace_failure_short_circuits(self):
        with mock.patch.object(self.engine, "_create_staging_workspace",
                               return_value={"ok": False, "error": "nope"}):
            result = self.engine._spawn_staging_session({"type": "spawn"}, "claude")
        self.assertFalse(result["ok"])

    def test_spawn_staging_registers_on_success(self):
        self.engine.cfg["instance_mode"] = "staging"
        ws = {"ok": True, "cwd": self.cwd, "branch": "fleet-staging/x",
              "worktree_name": "x"}
        with mock.patch.object(self.engine, "_create_staging_workspace",
                               return_value=ws), \
             mock.patch.object(self.engine, "spawn_session",
                               return_value={"ok": True, "session_id": "spawned"}):
            result = self.engine._spawn_staging_session({"type": "spawn"}, "claude")
        self.assertTrue(result["staging_owned"])
        self.assertEqual(result["staging_workspace"], ws)
        self.assertTrue(self.engine._staging_owns("spawned"))

    def test_spawn_staging_codex_failure_keeps_workspace(self):
        ws = {"ok": True, "cwd": self.cwd, "branch": "b", "worktree_name": "x"}
        with mock.patch.object(self.engine, "_create_staging_workspace",
                               return_value=ws), \
             mock.patch.object(self.engine, "spawn_codex_session",
                               return_value={"ok": False, "error": "denied"}):
            result = self.engine._spawn_staging_session({"type": "spawn"}, "codex")
        self.assertFalse(result["ok"])
        self.assertEqual(result["staging_workspace"], ws)

    # --------------------------------------------------- _staging_mask_session
    def test_mask_clears_can_reopen(self):
        masked = self.engine._staging_mask_session({
            "session_id": "prod", "can_reopen": True,
            "capabilities": {"submit": True}})
        self.assertFalse(masked["can_reopen"])
        self.assertEqual(masked["access"], "view_only")
        self.assertFalse(masked["capabilities"]["submit"])

    def test_mask_owned_session_untouched(self):
        self.engine.cfg["staging_owned_sessions"] = {
            "mine": {"provider": "claude", "cwd": self.cwd, "created_at": 1}}
        masked = self.engine._staging_mask_session({
            "session_id": "mine", "capabilities": {"submit": True}})
        self.assertTrue(masked["staging_owned"])
        self.assertTrue(masked["capabilities"]["submit"])

    # ------------------------------------------ operations fleet / action gate
    def test_operations_fleet_filters_to_owned(self):
        self.engine.cfg["instance_mode"] = "staging"
        self.engine.cfg["staging_owned_sessions"] = {
            "mine": {"provider": "claude", "cwd": self.cwd, "created_at": 1}}
        fleet = {
            "sessions": [{"session_id": "mine", "staging_owned": True},
                         {"session_id": "prod", "staging_owned": False}],
            "closed": [{"session_id": "prod2", "staging_owned": False}],
            "actions": [{"session_id": "mine"}, {"session_id": "prod"}],
            "providers": {"codex": {"ok": False, "error": "prod outage"}},
        }
        projected = self.engine._staging_operations_fleet(fleet)
        self.assertEqual([s["session_id"] for s in projected["sessions"]], ["mine"])
        self.assertEqual(projected["closed"], [])
        self.assertEqual([a["session_id"] for a in projected["actions"]], ["mine"])
        self.assertEqual(projected["providers"]["codex"], {"ok": True})

    def test_operations_fleet_passthrough_when_not_staging(self):
        fleet = {"sessions": []}
        self.assertIs(self.engine._staging_operations_fleet(fleet), fleet)

    def test_action_error_none_when_not_staging(self):
        self.assertIsNone(self.engine._staging_action_error({"type": "text"}))

    def test_action_error_allows_whitelisted_and_owned(self):
        self.engine.cfg["instance_mode"] = "staging"
        self.engine.cfg["staging_owned_sessions"] = {
            "mine": {"provider": "claude", "cwd": self.cwd, "created_at": 1}}
        self.assertIsNone(self.engine._staging_action_error({"type": "ping"}))
        self.assertIsNone(self.engine._staging_action_error({"type": "outbox_delete"}))
        self.assertIsNone(self.engine._staging_action_error(
            {"type": "text", "session_id": "mine"}))
        denied = self.engine._staging_action_error(
            {"type": "text", "session_id": "prod"})
        self.assertIn("view only", denied["error"])


if __name__ == "__main__":
    unittest.main()
