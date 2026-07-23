"""Coverage for fleetdash/engine.py — the Engine __init__ Codex runtime branch
paths (staging / migration-needed / committed / construction failure) plus the
one-shot CLI helpers (find_session_for_cwd, spend_table, main)."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))   # sibling test_cov_common import

from fleetdash import engine as engine_mod
from fleetdash import codex_runtime as codex_runtime_mod
from fleetdash.config import DEFAULT_CONFIG
from fleetdash.engine import Engine, find_session_for_cwd, spend_table, main
from test_cov_common import EngineCovBase, project_dir_for


class EngineInitCodexTest(EngineCovBase):
    """Exercise the Codex runtime selection branches in Engine.__init__.

    Each constructs a fresh Engine against the already-patched temp paths. No
    scan() is called, so the lazy Codex client never opens a socket."""

    def _cfg(self, **over):
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({"act_token": "secret", "ntfy_topic": ""})
        cfg.update(over)
        return cfg

    def test_staging_runtime_branch(self):
        eng = Engine(self._cfg(instance_mode="staging", codex_enabled=True))
        self.assertIsNotNone(eng.codex)
        # staging never installs the launcher
        self.assertIsNone(eng.codex_launcher_status)

    def test_committed_runtime_branch_sets_launcher_status(self):
        with mock.patch.object(codex_runtime_mod,
                               "codex_runtime_migration_needed", return_value=False), \
             mock.patch.object(codex_runtime_mod, "migrate_codex_runtime_metadata"):
            eng = Engine(self._cfg(codex_enabled=True))
        self.assertEqual(eng.codex_launcher_status["state"], "waiting_runtime")

    def test_migration_needed_branch(self):
        with mock.patch.object(codex_runtime_mod,
                               "codex_runtime_migration_needed", return_value=True):
            eng = Engine(self._cfg(codex_enabled=True))
        self.assertIsNotNone(eng.codex)
        # invoke the migration's target_factory closure (built in __init__): it
        # returns a lazy App Server client without opening any socket.
        client = eng.codex.runtime_migration.target_factory()
        self.assertIsNotNone(client)

    def test_codex_construction_failure_disables_adapter(self):
        with mock.patch.object(codex_runtime_mod, "codex_command",
                               side_effect=RuntimeError("no codex binary")):
            eng = Engine(self._cfg(codex_enabled=True))
        self.assertIsNone(eng.codex_observer)
        self.assertFalse(eng.codex.enabled)
        self.assertIn("no codex binary", eng.codex.error)


class EngineCliTest(EngineCovBase):
    def _make_subagents(self, sid, cwd):
        proj = project_dir_for(self.projects, cwd)
        subdir = os.path.join(proj, sid, "subagents")
        os.makedirs(subdir, exist_ok=True)
        aid = "agent-cli01"
        with open(os.path.join(subdir, f"{aid}.meta.json"), "w") as h:
            json.dump({"agentType": "Explore", "description": "search stuff"}, h)
        with open(os.path.join(subdir, f"{aid}.jsonl"), "w") as h:
            h.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-15T00:00:00Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 20, "output_tokens": 8},
                            "content": [{"type": "text", "text": "done"}]}}) + "\n")
        # a stray meta.json whose jsonl is missing is skipped
        with open(os.path.join(subdir, "agent-orphan.meta.json"), "w") as h:
            json.dump({"agentType": "x"}, h)
        return subdir

    # --------------------------------------------------- find_session_for_cwd
    def test_find_session_for_cwd_matches_live(self):
        hits = find_session_for_cwd(self.cwd)
        self.assertEqual([h["sessionId"] for h in hits], [self.sid])

    def test_find_session_for_cwd_skips_dead_pid(self):
        self.write_registry("dead-sid", cwd=self.cwd, pid=2_000_000_000)
        hits = find_session_for_cwd(self.cwd)
        self.assertNotIn("dead-sid", [h["sessionId"] for h in hits])

    # ---------------------------------------------------------- spend_table
    def test_spend_table_prints_rows(self):
        self._make_subagents(self.sid, self.cwd)
        buf = io.StringIO()
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg), \
             redirect_stdout(buf):
            spend_table(self.sid, self.cwd)
        out = buf.getvalue()
        self.assertIn("Explore", out)
        self.assertIn("TOTAL subagent spend", out)

    def test_spend_table_no_subagents(self):
        buf = io.StringIO()
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg), \
             redirect_stdout(buf):
            spend_table(self.sid, self.cwd)
        self.assertIn("no subagents", buf.getvalue())

    # ---------------------------------------------------------------- main()
    def _run_main(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["engine.py", *argv]), \
             redirect_stdout(buf):
            main()
        return buf.getvalue()

    def test_main_snapshot(self):
        fake = SimpleNamespace(scan=lambda: {"sessions": []})
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg), \
             mock.patch.object(engine_mod, "Engine", return_value=fake):
            out = self._run_main([])          # no args -> snapshot
            self.assertIn("sessions", out)
            out2 = self._run_main(["snapshot"])
            self.assertIn("sessions", out2)

    def test_main_spend_by_session(self):
        self._make_subagents(self.sid, self.cwd)
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg):
            out = self._run_main(["spend", "--session", self.sid[:8]])
        self.assertIn("Explore", out)

    def test_main_spend_by_cwd(self):
        self._make_subagents(self.sid, self.cwd)
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg):
            out = self._run_main(["spend", "--cwd", self.cwd])
        self.assertIn("TOTAL subagent spend", out)

    def test_main_spend_cwd_no_live_session_exits(self):
        empty = os.path.join(self.tmp.name, "empty-repo")
        os.makedirs(empty)
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg):
            with self.assertRaises(SystemExit):
                self._run_main(["spend", "--cwd", empty])

    def test_main_spend_cwd_multiple_hits(self):
        self._make_subagents(self.sid, self.cwd)
        self.write_registry("bbbbbbbb-cccc-dddd-eeee-ffffffffffff", cwd=self.cwd)
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg):
            out = self._run_main(["spend", "--cwd", self.cwd])
        self.assertIn("live sessions share this cwd", out)

    def test_main_spend_by_session_skips_corrupt_registry(self):
        self._make_subagents(self.sid, self.cwd)
        with open(os.path.join(self.sessions, "corrupt.json"), "w") as h:
            h.write("{ not valid json")   # the --session scan skips it
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg):
            out = self._run_main(["spend", "--session", self.sid[:8]])
        self.assertIn("Explore", out)

    def test_main_spend_unresolvable_session_usage_exit(self):
        with mock.patch.object(engine_mod, "load_config", return_value=self.cfg):
            with self.assertRaises(SystemExit):
                self._run_main(["spend", "--session", "zzzzzzzz-no-match"])

    def test_main_unknown_command_exits(self):
        with self.assertRaises(SystemExit):
            self._run_main(["frobnicate"])


if __name__ == "__main__":
    unittest.main()
