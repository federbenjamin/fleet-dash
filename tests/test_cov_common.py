"""Shared setUp for coverage tests (test_cov_*). Mirrors the paths.BASE/CAPTURE
patching pattern from test_engine_providers.py so an Engine can be constructed
against isolated temp dirs with a fake transcript + registry."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from fleetdash import paths as engine_paths
from fleetdash.config import DEFAULT_CONFIG
from fleetdash.engine import Engine


def project_dir_for(projects_root, cwd):
    return os.path.join(projects_root, cwd.replace("/", "-").replace(".", "-"))


class EngineCovBase(unittest.TestCase):
    """Engine wired to isolated temp state, one live Claude session + transcript."""

    codex_enabled = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.base = os.path.join(root, "fleet")
        self.sessions = os.path.join(root, "sessions")
        self.projects = os.path.join(root, "projects")
        self.claude_account = os.path.join(root, ".claude.json")
        self.claude_usage = os.path.join(self.base, "usage.json")
        self.claude_stats = os.path.join(root, "stats-cache.json")
        self.claude_history = os.path.join(root, "history.jsonl")
        self.claude_settings = os.path.join(root, "settings.json")
        self.claude_usage_prefs = os.path.join(root, "claude-usage.plist")
        os.makedirs(self.base)
        os.makedirs(self.sessions)
        os.makedirs(self.projects)
        patchers = [
            mock.patch.object(engine_paths, "HOME", root),
            mock.patch.object(engine_paths, "BASE", self.base),
            mock.patch.object(engine_paths, "CAPTURE_BASE", self.base),
            mock.patch.object(engine_paths, "SESSIONS", self.sessions),
            mock.patch.object(engine_paths, "PROJECTS", self.projects),
            mock.patch.object(engine_paths, "CLAUDE_ACCOUNT", self.claude_account),
            mock.patch.object(engine_paths, "CLAUDE_USAGE", self.claude_usage),
            mock.patch.object(engine_paths, "CLAUDE_STATS", self.claude_stats),
            mock.patch.object(engine_paths, "CLAUDE_HISTORY", self.claude_history),
            mock.patch.object(engine_paths, "CLAUDE_SETTINGS", self.claude_settings),
            mock.patch.object(engine_paths, "CLAUDE_USAGE_PREFS",
                              self.claude_usage_prefs),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.cwd = os.path.join(root, "repo")
        os.makedirs(self.cwd)
        self.project_dir = project_dir_for(self.projects, self.cwd)
        os.makedirs(self.project_dir)
        self.transcript = os.path.join(self.project_dir, f"{self.sid}.jsonl")
        self.write_transcript([
            {"type": "user", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "timestamp": "2026-07-15T00:00:01Z", "effort": "high",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "end_turn",
                         "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": "hi"}]}},
        ])
        self.write_registry(self.sid, status="idle")
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({"codex_enabled": self.codex_enabled, "act_token": "secret",
                    "ntfy_topic": ""})
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.addCleanup(self._close_engine)

    def _close_engine(self):
        if getattr(self.engine, "db", None):
            self.engine.db.close()

    def write_transcript(self, rows, path=None):
        path = path or self.transcript
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def write_registry(self, sid, status="idle", pid=None, cwd=None, name="Claude"):
        pid = pid if pid is not None else os.getpid()
        cwd = cwd or self.cwd
        with open(os.path.join(self.sessions, f"{sid}.json"), "w") as handle:
            json.dump({"sessionId": sid, "pid": pid, "cwd": cwd,
                       "status": status, "name": name, "startedAt": 1}, handle)
