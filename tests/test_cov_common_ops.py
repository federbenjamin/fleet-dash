"""Shared Engine fixture for coverage tests (test_cov_*).

Mirrors the canonical fixture in test_engine_providers.py: temp state dir,
patched fleetdash.paths, a fake Claude registry + transcript, and a FakeCodex.
No test methods live here — it is imported by the per-module coverage suites.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from fleetdash import paths as engine_paths
from fleetdash.config import DEFAULT_CONFIG
from fleetdash.engine import Engine


class FakeCodex:
    def __init__(self, session=None):
        self.error = None
        self.models = [{"id": "gpt-5.4", "name": "GPT-5.4", "efforts": ["high"]}]
        self.session = session
        self.actions = []
        self.fail_sessions = False
        self.started_thread = None

    def sessions(self):
        if self.fail_sessions:
            raise RuntimeError("Codex crashed")
        return [dict(self.session)] if self.session else []

    def account_usage(self):
        return {"provider": "codex", "buckets": []}

    def act(self, action):
        self.actions.append(action)
        return {"ok": True, "provider": "codex"}

    def context(self, sid):
        return {"ok": True, "messages": [{"role": "assistant", "text": "cached"}],
                "files": [], "closed": True}

    def agent_context(self, sid, aid):
        return {"ok": True, "messages": [], "info": {"agent_id": aid}}

    def file_content(self, sid, path):
        return "text/plain", b"codex", None

    def commands(self, sid, cwd):
        return {"ok": True, "commands": [{"name": "/compact"}]}

    def resume_capability(self, sid):
        return True, None

    @staticmethod
    def native(sid):
        return sid.split(":", 1)[1]

    @staticmethod
    def key(tid):
        return "codex:" + tid

    def start_thread(self, cwd, model=None, effort=None, mode="plan",
                     initial_text=None):
        self.started_thread = {"cwd": cwd, "model": model, "effort": effort,
                               "mode": mode, "initial_text": initial_text}
        return {"id": "new-thread"}


class EngineFixture(unittest.TestCase):
    """Base TestCase with a working Engine, patched paths, and a live Claude
    session (`same`) whose transcript already has one completed assistant turn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "fleet")
        self.sessions = os.path.join(self.tmp.name, "sessions")
        self.projects = os.path.join(self.tmp.name, "projects")
        self.claude_account = os.path.join(self.tmp.name, ".claude.json")
        self.claude_usage = os.path.join(self.base, "usage.json")
        self.claude_stats = os.path.join(self.tmp.name, "stats-cache.json")
        self.claude_history = os.path.join(self.tmp.name, "history.jsonl")
        self.claude_settings = os.path.join(self.tmp.name, "settings.json")
        self.claude_usage_prefs = os.path.join(self.tmp.name, "claude-usage.plist")
        self.tmux_sockets = os.path.join(self.tmp.name, "tmux-sockets")
        os.makedirs(self.base)
        os.makedirs(self.sessions)
        os.makedirs(self.projects)
        os.makedirs(self.tmux_sockets)
        self.patchers = [
            mock.patch.object(engine_paths, "TMUX_SOCKETS", self.tmux_sockets),
            mock.patch.object(engine_paths, "HOME", self.tmp.name),
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
        for patcher in self.patchers:
            patcher.start()
        self.cwd = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.cwd)
        project_dir = os.path.join(self.projects,
                                   self.cwd.replace("/", "-").replace(".", "-"))
        os.makedirs(project_dir)
        self.transcript = os.path.join(project_dir, "same.jsonl")
        rows = [
            {"type": "user", "timestamp": "2026-07-15T00:00:00Z",
             "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "timestamp": "2026-07-15T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "end_turn", "usage": {"input_tokens": 10,
                         "output_tokens": 5}, "content": [{"type": "text",
                                                            "text": "hi"}]}},
        ]
        with open(self.transcript, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        with open(os.path.join(self.sessions, "same.json"), "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "idle", "name": "Claude", "startedAt": 1}, handle)
        cfg = dict(DEFAULT_CONFIG)
        # The legacy applet keeps these fixtures on the transport the existing
        # `_iterm_write` patches assert against; the tmux dispatcher has its own
        # tests that opt in explicitly (tests/test_cov_tmux.py).
        cfg.update({"codex_enabled": False, "act_token": "secret", "ntfy_topic": "",
                    "terminal_transport": "applet"})
        self.engine = Engine(cfg)
        self.codex = FakeCodex()
        self.engine.codex = self.codex

    def tearDown(self):
        if self.engine.db:
            self.engine.db.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def write_registry(self, status="idle", **extra):
        payload = {"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                   "status": status, "name": "Claude", "startedAt": 1}
        payload.update(extra)
        with open(os.path.join(self.sessions, "same.json"), "w") as handle:
            json.dump(payload, handle)

    def append_transcript(self, row):
        with open(self.transcript, "a") as handle:
            handle.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    unittest.main()
