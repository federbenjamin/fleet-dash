import copy
import json
import os
import plistlib
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash import paths as engine_paths
from fleetdash import engine_uploads as engine_uploads_module
from fleetdash.config import (DEFAULT_CONFIG, IMAGE_UPLOAD_SESSION_COUNT,
                    WAITING_CONFIRM_SECONDS, load_config)
from fleetdash.engine import Engine
from fleetdash.placement import (classify_placement, completed_handoff,
                    redact_handoff_text, requests_reply)
from fleetdash.tail import Tail
from server import Handler


class FakeCodex:
    def __init__(self, session=None):
        self.error = None
        self.models = [{"id": "gpt-5.4", "name": "GPT-5.4", "efforts": ["high"]}]
        self.session = session
        self.actions = []
        self.fail_sessions = False
        self.muted_at_call = None
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


def codex_session():
    return {"session_id": "codex:same", "native_session_id": "same",
            "provider": "codex", "name": "Codex", "title": "Codex",
            "project": "repo", "cwd": "/work/repo", "branch": "feature",
            "model": "gpt-5.4", "family": "codex", "effort": "high",
            "collaboration_mode": "default", "running": None, "last_msg": None,
            "state": "idle", "reg_status": "idle", "quiet_s": 0,
            "ctx_tokens": 100, "ctx_pct": 1.0, "cost": None,
            "cost_source": "unavailable", "bridge_url": None, "started_ms": 1,
            "pending": None, "compacting": None, "muted": False, "convo_v": "1:1",
            "files_n": 0, "agents": [], "agents_running": 0, "agents_total": 0,
            "agent_cost": None, "capabilities": {"submit": True, "interrupt": False,
                "close": True, "exact_cost": False, "focus_terminal": False}}


class EngineProviderTest(unittest.TestCase):
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
        os.makedirs(self.base)
        os.makedirs(self.sessions)
        os.makedirs(self.projects)
        self.patchers = [
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
        cfg.update({"codex_enabled": False, "act_token": "secret", "ntfy_topic": "",
                    "muted_sessions": {"codex:same": time.time()}})
        self.engine = Engine(cfg)
        self.codex = FakeCodex(codex_session())
        self.engine.codex = self.codex

    def tearDown(self):
        if self.engine.db:
            self.engine.db.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_shared_fleet_keeps_provider_ids_independent_and_unknown_cost_partial(self):
        fleet = self.engine.scan()
        self.assertEqual({item["session_id"] for item in fleet["sessions"]},
                         {"same", "codex:same"})
        codex = next(item for item in fleet["sessions"] if item["provider"] == "codex")
        claude = next(item for item in fleet["sessions"] if item["provider"] == "claude")
        self.assertTrue(codex["muted"])
        self.assertFalse(claude["muted"])
        self.assertTrue(claude["capabilities"]["close"])
        self.assertTrue(claude["capabilities"]["model_effort_settings"])
        self.assertTrue(claude["capabilities"]["change_model_effort"])
        self.assertTrue(codex["capabilities"]["close"])
        self.assertTrue(fleet["totals"]["cost_partial"])
        self.assertGreaterEqual(fleet["totals"]["session_cost"], 0)
        self.assertGreaterEqual(fleet["diagnostics"]["scan_ms"], 0)
        self.assertEqual(fleet["diagnostics"]["scan_samples"], 1)
        second = self.engine.scan()
        self.assertEqual(second["diagnostics"]["scan_samples"], 2)
        self.assertGreaterEqual(second["diagnostics"]["scan_p95_ms"], 0)

    def test_staging_mirrors_production_sessions_read_only_and_enforces_owner_gate(self):
        self.engine.cfg.update(instance_mode="staging", instance_name="Fleet Staging",
                               staging_owned_sessions={"codex:same": {
                                   "provider": "codex", "cwd": self.cwd,
                                   "created_at": time.time()}})
        fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"]
                      if item["session_id"] == "same")
        codex = next(item for item in fleet["sessions"]
                     if item["session_id"] == "codex:same")
        self.assertTrue(claude["staging_observer"])
        self.assertEqual(claude["access"], "view_only")
        self.assertFalse(claude["capabilities"]["submit"])
        self.assertFalse(claude["capabilities"]["change_permission_mode"])
        self.assertFalse(claude["capabilities"]["model_effort_settings"])
        self.assertFalse(claude["capabilities"]["change_model_effort"])
        self.assertFalse(claude["capabilities"]["change_permission_mode"])
        masked_attention = self.engine._staging_mask_session({
            "session_id": "same", "reply_requested": True, "new_response": True,
            "capabilities": {"submit": True}})
        self.assertFalse(masked_attention["reply_requested"])
        self.assertFalse(masked_attention["new_response"])
        self.assertTrue(codex["staging_owned"])
        self.assertTrue(codex["capabilities"]["submit"])
        denied = self.engine.act({"type": "text", "session_id": "same",
                                  "text": "must not land"})
        self.assertFalse(denied["ok"])
        self.assertIn("view only", denied["error"])
        allowed = self.engine.act({"type": "text", "session_id": "codex:same",
                                   "text": "staging test"})
        self.assertTrue(allowed["ok"])

    def test_send_message_delivers_immediately_only_when_claude_is_available(self):
        self.engine.scan()
        writes = []
        with mock.patch.object(self.engine, "_tty_for_pid", return_value="ttys001"), \
             mock.patch.object(self.engine, "_iterm_write",
                side_effect=lambda tty, steps, step_delay=None:
                    writes.append((tty, steps, step_delay)) or {"ok": True}):
            result = self.engine.act({"type": "send_message", "session_id": "same",
                "text": "Deliver immediately", "client_request_id": "send-now-claude-0001"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["queued"])
        self.assertEqual(result["delivery"], "sent_now")
        self.assertEqual(writes, [
            ("/dev/ttys001", [("Deliver immediately", True)], 0.05)])
        self.assertEqual(self.engine.outbox.counts()["pending"], 0)

    def test_direct_claude_text_and_image_lost_ack_are_never_auto_retried(self):
        self.engine.scan()
        uncertain = {"ok": False, "code": "delivery_uncertain",
                     "error": "injector result was lost"}
        with mock.patch.object(self.engine, "_tty_for_pid", return_value="ttys001"), \
             mock.patch.object(self.engine, "_iterm_write", return_value=uncertain):
            text = self.engine.act({"type": "send_message", "session_id": "same",
                "text": "May already exist", "client_request_id": "lost-text-0001"})
        self.assertEqual(text.get("code"), "delivery_uncertain")
        self.assertEqual(self.engine.outbox.counts()["pending"], 0)

        with mock.patch.object(self.engine, "_resolve_image_uploads",
                               return_value=(["/private/tmp/image.png"], None)), \
             mock.patch.object(self.engine, "_tty_for_pid", return_value="ttys001"), \
             mock.patch.object(self.engine, "_iterm_write", return_value=uncertain):
            image = self.engine.act({"type": "send_message", "session_id": "same",
                "text": "Inspect", "upload_ids": ["upload-1"],
                "client_request_id": "lost-image-0001"})
        self.assertEqual(image.get("code"), "delivery_uncertain")
        self.assertEqual(self.engine.outbox.counts()["pending"], 0)

    def test_send_message_queues_busy_claude_and_dispatches_once_when_idle(self):
        registry = os.path.join(self.sessions, "same.json")
        with open(registry, "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "busy", "name": "Claude", "startedAt": 1}, handle)
        snapshot = self.engine.scan()
        claude = next(item for item in snapshot["sessions"]
                      if item["session_id"] == "same")
        self.assertEqual(claude["ui_group"], "working")
        writes = []
        with mock.patch.object(self.engine, "_tty_for_pid", return_value="ttys001"), \
             mock.patch.object(self.engine, "_iterm_write",
                side_effect=lambda tty, steps, step_delay=None:
                    writes.append((tty, steps, step_delay)) or {"ok": True}):
            queued = self.engine.act({"type": "send_message", "session_id": "same",
                "text": "Wait for this turn", "client_request_id": "send-busy-claude-0001"})
            self.assertTrue(queued["ok"], queued)
            self.assertTrue(queued["queued"])
            self.assertEqual(queued["message"], "Queued · waiting for session")
            self.assertFalse(writes)
            row = self.engine.outbox.get(queued["outbox_id"])
            self.assertEqual(row["kind"], "when_available")
            self.assertEqual(row["target_session_id"], "same")

            with open(registry, "w") as handle:
                json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                           "status": "idle", "name": "Claude", "startedAt": 1}, handle)
            self.engine.scan()
            self.engine.run_outbox()
            self.engine.run_outbox()
        self.assertEqual(self.engine.outbox.get(queued["outbox_id"])["state"], "sent")
        self.assertEqual(writes, [
            ("/dev/ttys001", [("Wait for this turn", True)], 0.05)])

    def test_send_message_revalidates_claude_before_terminal_injection(self):
        registry = os.path.join(self.sessions, "same.json")
        self.engine.scan()  # cached snapshot says idle
        with open(registry, "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "waiting", "name": "Claude", "startedAt": 1}, handle)
        writes = []
        with mock.patch.object(self.engine, "_tty_for_pid", return_value="ttys001"), \
             mock.patch.object(self.engine, "_iterm_write",
                side_effect=lambda tty, steps, step_delay=None:
                    writes.append((tty, steps, step_delay)) or {"ok": True}):
            result = self.engine.act({"type": "send_message", "session_id": "same",
                "text": "Do not type into the question", "client_request_id":
                    "send-stale-claude-0001"})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["queued"])
        self.assertFalse(writes)
        self.assertEqual(self.engine.outbox.get(result["outbox_id"])["state"],
                         "waiting_availability")

    def test_composer_followup_dismisses_claude_question_before_queueing_text(self):
        reg = {"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
               "status": "waiting", "name": "Claude", "startedAt": 1}
        pending = {"kind": "question", "nonce": "q-followup", "questions": [{
            "question": "Choose", "multiSelect": False, "allowOther": False,
            "options": [{"label": "One"}, {"label": "Two"}]}]}
        self.engine.live_sessions = lambda: [reg]
        self.engine.hook_pending = lambda sid, status: pending
        self.engine.compacting_secs = lambda sid, cwd, tail: None
        writes = []
        self.engine._tty_cache[os.getpid()] = "ttys-test"
        self.engine._iterm_write = mock.Mock(side_effect=lambda tty, steps,
            step_delay=None: writes.append((tty, steps, step_delay)) or {"ok": True})
        self.engine.scan()

        result = self.engine.act({"type": "dismiss_then_send",
            "session_id": "same", "nonce": "q-followup",
            "text": "Here is the context instead",
            "client_request_id": "question-followup-claude-0001"})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dismissed"])
        self.assertTrue(result["queued"])
        self.assertEqual(writes, [("/dev/ttys-test", [("\x1b", False)], 0.4)])
        row = self.engine.outbox.get(result["outbox_id"])
        self.assertEqual(row["message"], "Here is the context instead")
        self.assertEqual(row["target_session_id"], "same")

    def test_composer_followup_dismisses_codex_question_before_queueing_text(self):
        self.codex.session.update(state="needs_you", reg_status="waiting",
            pending={"kind": "question", "nonce": "q-codex", "questions": [{
                "question": "Choose", "options": [{"label": "One"}]}]})
        self.codex.session["capabilities"].update(
            submit=False, answer_structured=True, decide_approval=True)
        self.engine.scan()

        result = self.engine.act({"type": "dismiss_then_send",
            "session_id": "codex:same", "nonce": "q-codex",
            "text": "Follow this instruction instead",
            "client_request_id": "question-followup-codex-0001"})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dismissed"])
        self.assertTrue(result["queued"])
        self.assertEqual(self.codex.actions, [{"type": "dismiss",
            "session_id": "codex:same", "nonce": "q-codex"}])
        self.assertEqual(self.engine.outbox.get(result["outbox_id"])["message"],
                         "Follow this instruction instead")

    def test_composer_followup_refuses_changed_question_without_sending(self):
        self.codex.session.update(state="needs_you", reg_status="waiting",
            pending={"kind": "question", "nonce": "new-question", "questions": [{
                "question": "New", "options": [{"label": "One"}]}]})
        self.codex.session["capabilities"].update(
            submit=False, answer_structured=True, decide_approval=True)
        self.engine.scan()

        result = self.engine.act({"type": "dismiss_then_send",
            "session_id": "codex:same", "nonce": "old-question",
            "text": "Must not become an option",
            "client_request_id": "question-followup-stale-0001"})

        self.assertFalse(result["ok"])
        self.assertIn("changed", result["error"])
        self.assertEqual(self.codex.actions, [])
        self.assertEqual(self.engine.outbox.counts()["pending"], 0)

    def test_send_message_steers_fleet_owned_active_codex_turn_immediately(self):
        self.codex.session.update(state="running", reg_status="running",
                                  control_state="connected_active")
        self.codex.session["capabilities"].update(submit=True, queue_submit=False)
        self.engine.scan()
        self.engine._codex_terminal_route = lambda _sid, force=False: None
        result = self.engine.act({"type": "send_message", "session_id": "codex:same",
            "text": "Steer active work", "client_request_id": "send-codex-steer-0001"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["queued"])
        self.assertEqual(result["delivery"], "sent_now")
        self.assertEqual(self.codex.actions[-1], {"type": "text",
            "session_id": "codex:same", "text": "Steer active work",
            "client_request_id": "send-codex-steer-0001"})

    def test_send_message_replies_immediately_to_idle_needs_you_session(self):
        self.codex.session.update(
            state="idle", reg_status="idle", control_state="connected_idle",
            _latest_prose={"role": "assistant", "text": "What are you working on?"},
            last_msg={"role": "assistant", "text": "What are you working on?"})
        snapshot = self.engine.scan()
        current = next(item for item in snapshot["sessions"]
                       if item["session_id"] == "codex:same")
        self.assertEqual(current["state"], "idle")
        self.assertEqual(current["ui_group"], "needs_you")
        self.assertTrue(current["reply_requested"])

        result = self.engine.act({"type": "send_message", "session_id": "codex:same",
            "text": "Here is my reply", "client_request_id": "send-reply-codex-0001"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["queued"])
        self.assertEqual(result["delivery"], "sent_now")
        self.assertEqual(self.codex.actions[-1]["text"], "Here is my reply")
        self.assertEqual(self.engine.outbox.counts()["pending"], 0)

    def test_busy_claude_photo_is_copied_into_durable_outbox_storage(self):
        registry = os.path.join(self.sessions, "same.json")
        with open(registry, "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "busy", "name": "Claude", "startedAt": 1}, handle)
        self.engine.scan()
        upload_id = "phone-image-001"
        upload_root, image_path, meta_path = self.engine._image_upload_paths(upload_id)
        os.makedirs(upload_root, exist_ok=True)
        payload = b"normalized jpeg"
        with open(image_path, "wb") as image:
            image.write(payload)
        with open(meta_path, "w") as meta:
            json.dump({"session_id": "same", "size": len(payload),
                       "expires_at": time.time() + 300}, meta)
        queued = self.engine.act({"type": "send_message", "session_id": "same",
            "text": "Inspect the photo", "upload_ids": [upload_id],
            "client_request_id": "send-photo-claude-0001"})
        self.assertTrue(queued["queued"], queued)
        row = self.engine.outbox.get_internal(queued["outbox_id"])
        self.assertEqual(row["image_count"], 1)
        self.assertNotEqual(row["_image_paths"], [image_path])
        with open(row["_image_paths"][0], "rb") as image:
            self.assertEqual(image.read(), payload)

    def test_client_cannot_smuggle_image_paths_through_send_message(self):
        self.engine.scan()
        writes = []
        with mock.patch.object(self.engine, "_tty_for_pid", return_value="ttys001"), \
             mock.patch.object(self.engine, "_iterm_write",
                side_effect=lambda tty, steps, step_delay=None:
                    writes.append((tty, steps)) or {"ok": True}):
            result = self.engine.act({"type": "send_message", "session_id": "same",
                "text": "No attachment", "image_paths": [self.transcript],
                "client_request_id": "send-path-smuggle-0001"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(writes, [("/dev/ttys001", [("No attachment", True)])])
        self.assertNotIn(self.transcript, str(writes))

    def test_staging_spawn_forces_dedicated_worktree_and_records_exact_session(self):
        workspace = os.path.join(self.tmp.name, "staging-workspace")
        os.makedirs(workspace)
        self.engine.cfg.update(instance_mode="staging", instance_name="Fleet Staging",
                               staging_owned_sessions={})
        persisted = []
        with mock.patch.object(self.engine, "_create_staging_workspace", return_value={
                "ok": True, "cwd": workspace, "branch": "fleet-staging/test",
                "worktree_name": "test"}), \
             mock.patch.object(self.engine, "_iterm_write", return_value={"ok": True}), \
             mock.patch.object(self.engine, "_persist_config_fields",
                               side_effect=lambda fields: persisted.append(fields)):
            result = self.engine.act({"type": "spawn", "provider": "claude",
                "cwd": self.cwd, "worktree": False, "__staging_internal": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cwd"], os.path.realpath(workspace))
        self.assertNotIn(self.cwd, result["command"])
        self.assertTrue(result["staging_owned"])
        self.assertIn(result["session_id"], self.engine.cfg["staging_owned_sessions"])
        self.assertTrue(any("staging_owned_sessions" in fields for fields in persisted))

    def test_stall_default_migrates_exact_old_default_once(self):
        migration_base = os.path.join(self.tmp.name, "migration")
        os.makedirs(migration_base)
        path = os.path.join(migration_base, "config.json")
        with open(path, "w") as handle:
            json.dump({"stall_seconds": 240, "act_token": "existing"}, handle)
        with mock.patch.object(engine_paths, "BASE", migration_base):
            migrated = load_config()
        self.assertEqual(migrated["stall_seconds"], 600)
        self.assertTrue(migrated["_stall_default_v2"])

        with open(path, "w") as handle:
            json.dump({"stall_seconds": 900, "act_token": "existing"}, handle)
        with mock.patch.object(engine_paths, "BASE", migration_base):
            custom = load_config()
        self.assertEqual(custom["stall_seconds"], 900)

    def test_new_claude_registry_session_is_interactive_before_first_transcript(self):
        os.unlink(self.transcript)
        fleet = self.engine.scan()
        session = next(item for item in fleet["sessions"]
                       if item["provider"] == "claude")
        self.assertEqual((session["state"], session["ui_group"]),
                         ("idle", "available"))
        self.assertTrue(session["capabilities"]["submit"])
        self.assertIsNone(session["ctx_tokens"])
        self.assertIsNone(session["cost"])
        self.assertEqual(self.engine.session_context("same"), {
            "ok": True, "messages": [], "files": [], "starting": True})

    def test_private_image_upload_is_normalized_scoped_and_resolved_server_side(self):
        private = b"camera=private;gps=private"
        fake_jpeg = (b"\xff\xd8\xff\xe1" + (len(private) + 2).to_bytes(2, "big") +
                     private + b"\xff\xda\x00\x02\xff\xd9")
        self.assertNotIn(private, self.engine._strip_jpeg_metadata(fake_jpeg))
        self.engine.scan()
        image_path = os.path.join(os.path.dirname(__file__), "..", "static", "icons",
                                  "fleet-192.png")
        with open(image_path, "rb") as handle:
            data = handle.read()
        uploaded = self.engine.store_image_upload(
            "codex:same", "opaque_image_1", "../../phone.png", "image/png", data)
        self.assertTrue(uploaded["ok"], uploaded)
        self.assertEqual(uploaded["name"], "phone.png")
        paths, error = self.engine._resolve_image_uploads(
            "codex:same", ["opaque_image_1"])
        self.assertIsNone(error)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].startswith(os.path.join(self.base, "uploads") + os.sep))
        self.assertEqual(stat.S_IMODE(os.stat(paths[0]).st_mode), 0o600)
        denied, error = self.engine._resolve_image_uploads("same", ["opaque_image_1"])
        self.assertIsNone(denied)
        self.assertIn("another session", error)

        result = self.engine.act({"type": "image_text", "session_id": "codex:same",
                                  "text": "Inspect", "upload_ids": ["opaque_image_1"]})
        self.assertTrue(result["ok"])
        self.assertEqual(self.codex.actions[-1]["image_paths"], paths)
        claude_upload = self.engine.store_image_upload(
            "same", "opaque_image_3", "phone.png", "image/png", data)
        self.assertTrue(claude_upload["ok"], claude_upload)
        writes = []
        self.engine._tty_cache[os.getpid()] = "ttys999"
        with mock.patch.object(self.engine, "_iterm_write",
                               side_effect=lambda tty, steps, step_delay=None:
                               writes.append((tty, steps, step_delay)) or {"ok": True}):
            claude_result = self.engine.act({"type": "image_text", "session_id": "same",
                "text": "Inspect in Claude", "upload_ids": ["opaque_image_3"]})
        self.assertTrue(claude_result["ok"])
        self.assertIn("Inspect in Claude", writes[0][1][0][0])
        self.assertIn(os.path.join(self.base, "uploads", "opaque_image_3.jpg"),
                      writes[0][1][0][0])
        mismatch = self.engine.store_image_upload(
            "codex:same", "opaque_image_2", "fake.jpg", "image/jpeg", data)
        self.assertFalse(mismatch["ok"])

        collision = self.engine.store_image_upload(
            "same", "opaque_image_1", "replace.png", "image/png", data)
        self.assertFalse(collision["ok"])
        self.assertIn("already exists", collision["error"])
        paths_after, error = self.engine._resolve_image_uploads(
            "codex:same", ["opaque_image_1"])
        self.assertIsNone(error)
        self.assertEqual(paths_after, paths)

    def test_temporarily_missing_known_session_accepts_photo_and_queues_delivery(self):
        self.engine.scan()
        with self.engine.lock:
            self.engine.snapshot_cache["sessions"] = []
        image_path = os.path.join(os.path.dirname(__file__), "..", "static", "icons",
                                  "fleet-192.png")
        with open(image_path, "rb") as handle:
            data = handle.read()

        uploaded = self.engine.store_image_upload(
            "same", "missing-session-photo", "phone.png", "image/png", data)
        self.assertTrue(uploaded["ok"], uploaded)
        result = self.engine.act({
            "type": "send_message", "session_id": "same",
            "text": "Inspect this after reconnecting",
            "upload_ids": ["missing-session-photo"],
            "client_request_id": "missing-session-send-0001",
        })
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["queued"], result)
        queued = self.engine.outbox.get_internal(result["outbox_id"])
        self.assertEqual(queued["target_session_id"], "same")
        self.assertEqual(queued["target_provider"], "claude")
        self.assertEqual(queued["message"], "Inspect this after reconnecting")
        self.assertEqual(len(queued["_image_paths"]), 1)
        self.assertTrue(os.path.isfile(queued["_image_paths"][0]))

        unknown = self.engine.store_image_upload(
            "never-seen", "unknown-session-photo", "phone.png", "image/png", data)
        self.assertFalse(unknown["ok"])
        self.assertIn("not available", unknown["error"])

    def test_repeated_claude_file_delivery_becomes_newest_without_duplication(self):
        tail = Tail(self.transcript)
        tail._file_add("/work/first.md", "first", 1)
        tail._file_add("/work/second.md", "second", 2)
        tail._file_add("/work/first.md", "first again", 3)
        self.assertEqual([item["path"] for item in tail.files],
                         ["/work/second.md", "/work/first.md"])
        self.assertEqual(len(tail.files), 2)
        self.assertEqual(tail.files[-1]["caption"], "first again")

    def test_claude_file_preview_content_types_keep_html_inert(self):
        paths = {"preview.html": b"<h1>safe static preview</h1>",
                 "report.pdf": b"%PDF-1.4\n%%EOF\n",
                 "data.json": b'{"ok":true}'}
        delivered = []
        for name, data in paths.items():
            path = os.path.join(self.cwd, name)
            with open(path, "wb") as handle:
                handle.write(data)
            delivered.append(path)
        row = {"type": "assistant", "timestamp": "2026-07-15T00:00:02Z",
               "message": {"role": "assistant", "model": "claude-sonnet",
                   "stop_reason": "tool_use", "usage": {}, "content": [{
                       "type": "tool_use", "id": "send-preview-files",
                       "name": "SendUserFile",
                       "input": {"files": delivered, "caption": "Preview files"}}]}}
        with open(self.transcript, "a") as handle:
            handle.write(json.dumps(row) + "\n")
        self.engine.scan()
        expected = {"preview.html": "text/plain; charset=utf-8",
                    "report.pdf": "application/pdf",
                    "data.json": "application/json; charset=utf-8"}
        for path in delivered:
            ctype, data, error = self.engine.file_content(
                "same", self.engine.file_id("same", path))
            self.assertIsNone(error)
            self.assertEqual(ctype, expected[os.path.basename(path)])
            self.assertEqual(data, paths[os.path.basename(path)])

    def test_missing_claude_delivery_uses_confined_file_history_backup(self):
        sid = "11111111-2222-3333-4444-555555555555"
        delivered = os.path.join(self.tmp.name, "removed-scratch", "plan.md")
        backup_name = "abcdef1234567890@v2"
        transcript = os.path.join(os.path.dirname(self.transcript), sid + ".jsonl")
        rows = [
            {"type": "assistant", "timestamp": "2026-07-15T00:00:02Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "tool_use", "usage": {}, "content": [{
                             "type": "tool_use", "id": "send-file", "name": "SendUserFile",
                             "input": {"files": [delivered], "caption": "Durable plan"}}]}},
            {"type": "file-history-snapshot", "timestamp": "2026-07-15T00:00:03Z",
             "snapshot": {"trackedFileBackups": {
                 delivered: {"backupFileName": backup_name},
                 "/untrusted": {"backupFileName": "../../config.json"}}}},
        ]
        with open(transcript, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        with open(os.path.join(self.sessions, sid + ".json"), "w") as handle:
            json.dump({"sessionId": sid, "pid": os.getpid(), "cwd": self.cwd,
                       "status": "idle", "name": "Claude", "startedAt": 2}, handle)
        backup_dir = os.path.join(self.tmp.name, ".claude", "file-history", sid)
        os.makedirs(backup_dir)
        with open(os.path.join(backup_dir, backup_name), "wb") as handle:
            handle.write(b"# recovered plan\n")

        self.engine.scan()
        context = self.engine.session_context(sid)
        self.assertTrue(context["ok"])
        self.assertNotIn("path", context["files"][0])
        self.assertEqual(context["files"][0]["file_id"],
                         self.engine.file_id(sid, delivered))
        self.assertFalse(context["files"][0]["missing"])
        ctype, data, error = self.engine.file_content(
            sid, self.engine.file_id(sid, delivered))
        self.assertIsNone(error)
        self.assertEqual(ctype, "text/plain; charset=utf-8")
        self.assertEqual(data, b"# recovered plan\n")
        self.assertNotIn("/untrusted", self.engine.tail_for(transcript).file_backups)
        self.assertIsNone(self.engine._claude_file_backup(sid, "../../config.json"))

    def test_delivery_whitelist_outlives_ring_buffers(self):
        delivered = os.path.join(self.cwd, "old-delivery.md")
        with open(delivered, "w") as handle:
            handle.write("old delivery body")
        self.engine.scan()
        # The files deque and convo chips are ring buffers; simulate a delivery
        # that aged out of both — only the durable whitelist remembers it.
        self.engine._claude_context_snapshots["same"] = {
            "revision": 1, "messages": [], "files": [],
            "file_backups": {}, "delivered_paths": {delivered: 5.0}}
        fid = self.engine.file_id("same", delivered)
        self.assertEqual(self.engine.file_selector_for_path("same", delivered), fid)
        ctype, data, error = self.engine.file_content("same", fid)
        self.assertIsNone(error)
        self.assertEqual(data, b"old delivery body")
        # Tail keeps every delivered path (bounded), while files stays a ring.
        tail = self.engine.tail_for(self.transcript)
        for index in range(12):
            tail._file_add(f"/tmp/burst-{index}", "", index)
        self.assertEqual(len(tail.files), 10)
        for index in range(12):
            self.assertIn(f"/tmp/burst-{index}", tail.delivered_paths)
        # The backup mapping alone must never widen the whitelist.
        self.engine._claude_context_snapshots["same"] = {
            "revision": 2, "messages": [], "files": [],
            "file_backups": {delivered: "abcdef1234567890@v1"},
            "delivered_paths": {}}
        self.assertIsNone(self.engine.file_selector_for_path("same", delivered))
        _, _, error = self.engine.file_content("same", fid)
        self.assertIsNotNone(error)

    def test_transcript_effort_is_primary_and_retires_overrides(self):
        def effort_row(level):
            return json.dumps({"type": "assistant",
                "timestamp": "2026-07-15T00:00:05Z", "effort": level,
                "message": {"role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "end_turn", "usage": {"input_tokens": 1},
                    "content": [{"type": "text", "text": "row"}]}}) + "\n"
        with open(self.transcript, "a") as handle:
            handle.write(effort_row("high"))
        fleet = self.engine.scan()
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertEqual(session["effort"], "high")

        with open(self.transcript, "a") as handle:   # junk values never surface
            handle.write(effort_row("turbo"))
        fleet = self.engine.scan()
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertEqual(session["effort"], "high")

        pid = os.getpid()
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})
        accepted = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "sonnet", "effort": "low", "expected_model": "claude-sonnet",
            "expected_effort": "high"})
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(self.engine.effort_for("same"), "low")
        fleet = self.engine.scan()   # older transcript rows must not retire it
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertEqual(session["effort"], "low")

        with open(self.transcript, "a") as handle:   # newer native evidence wins
            handle.write(effort_row("medium"))
        fleet = self.engine.scan()
        session = next(s for s in fleet["sessions"] if s["session_id"] == "same")
        self.assertEqual(session["effort"], "medium")
        self.assertNotIn(
            "effort", self.engine._claude_control_overrides.get("same") or {})

    def test_image_upload_quota_rejects_before_conversion(self):
        self.engine.scan()
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        for index in range(IMAGE_UPLOAD_SESSION_COUNT):
            upload_id = f"quota-{index}"
            _, image_path, meta_path = self.engine._image_upload_paths(upload_id)
            with open(image_path, "wb") as image:
                image.write(b"x")
            with open(meta_path, "w") as meta:
                json.dump({"session_id": "codex:same", "size": 1,
                           "expires_at": time.time() + 300}, meta)
        image_path = os.path.join(os.path.dirname(__file__), "..", "static", "icons",
                                  "fleet-192.png")
        with open(image_path, "rb") as handle:
            data = handle.read()
        with mock.patch.object(subprocess, "run") as convert:
            rejected = self.engine.store_image_upload(
                "codex:same", "quota-overflow", "phone.png", "image/png", data)
        self.assertFalse(rejected["ok"])
        self.assertIn("limit is full", rejected["error"])
        convert.assert_not_called()

    def test_image_upload_quota_rechecks_normalized_size(self):
        self.engine.scan()
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        _, image_path, meta_path = self.engine._image_upload_paths("quota-existing")
        with open(image_path, "wb") as image:
            image.write(b"x" * 85)
        with open(meta_path, "w") as meta:
            json.dump({"session_id": "codex:same", "size": 85,
                       "expires_at": time.time() + 300}, meta)

        def convert(argv, **_kwargs):
            with open(argv[-1], "wb") as output:
                output.write(b"placeholder")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(engine_uploads_module, "IMAGE_UPLOAD_SESSION_BYTES", 100), \
             mock.patch.object(subprocess, "run", side_effect=convert), \
             mock.patch.object(self.engine, "_strip_jpeg_metadata", return_value=b"j" * 20):
            rejected = self.engine.store_image_upload(
                "codex:same", "quota-normalized", "phone.png", "image/png",
                b"\x89PNG\r\n\x1a\n")
        self.assertFalse(rejected["ok"])
        self.assertIn("limit is full", rejected["error"])
        _, final_image, final_meta = self.engine._image_upload_paths("quota-normalized")
        self.assertFalse(os.path.exists(final_image))
        self.assertFalse(os.path.exists(final_meta))

    def test_image_cleanup_rotates_beyond_first_batch(self):
        root = os.path.join(self.base, "uploads")
        os.makedirs(root, exist_ok=True)
        for index in range(2100):
            with open(os.path.join(root, f"expired-{index}.json"), "w") as meta:
                json.dump({"expires_at": 1}, meta)
        for _ in range(3):
            self.engine._cleanup_image_uploads(now=100)
        self.assertFalse(any(name.endswith(".json") for name in os.listdir(root)))
        self.assertEqual(self.engine._image_cleanup_skip, 0)

    def test_large_nul_path_probe_keeps_only_a_bounded_sample(self):
        probe = self.engine._bounded_nul_paths([
            sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(b\"ignored\\0\" * 70000)"],
            max_input=1_000_000, keep=3)
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["count"], 70000)
        self.assertEqual(probe["paths"], ["ignored", "ignored", "ignored"])
        self.assertEqual(len(probe["digest"]), 64)

    def test_claude_shell_status_defers_to_a_completed_transcript_turn(self):
        registry = os.path.join(self.sessions, "same.json")
        with open(registry, "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "shell", "name": "Claude", "startedAt": 1}, handle)
        settled = next(item for item in self.engine.scan()["sessions"]
                       if item["provider"] == "claude")
        self.assertEqual((settled["state"], settled["ui_group"],
                          settled["capabilities"]["interrupt"]),
                         ("turn_done", "available", False))

        with open(self.transcript, "a") as handle:
            handle.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-17T07:40:00Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "tool_use", "content": [{"type": "tool_use",
                        "id": "shell-active", "name": "Bash", "input": {}}]},
            }) + "\n")
        active = next(item for item in self.engine.scan()["sessions"]
                      if item["provider"] == "claude")
        self.assertEqual((active["state"], active["ui_group"],
                          active["capabilities"]["interrupt"]),
                         ("running", "working", True))

    def test_live_context_uses_published_scan_snapshot_without_scan_lock(self):
        fleet = self.engine.scan()
        revision = next(item["convo_v"] for item in fleet["sessions"]
                        if item["session_id"] == "same")

        class RefuseLock:
            def __enter__(self):
                raise AssertionError("routine context read waited for scan_lock")

            def __exit__(self, *_):
                return False

        self.engine.scan_lock = RefuseLock()
        context = self.engine.session_context("same")
        self.assertTrue(context["ok"])
        self.assertEqual(self.engine._claude_context_snapshots["same"]["revision"],
                         revision)
        self.assertEqual(context["messages"][-1]["text"], "hi")

    def test_live_agent_context_uses_parent_scoped_snapshot_without_scan_lock(self):
        subdir = os.path.join(os.path.dirname(self.transcript), "same", "subagents")
        os.makedirs(subdir)
        aid = "agent-shared123"
        with open(os.path.join(subdir, aid + ".meta.json"), "w") as handle:
            json.dump({"agentType": "quick-build", "description": "Scoped child",
                       "spawnDepth": 1, "toolUseId": "tool-child"}, handle)
        with open(os.path.join(subdir, aid + ".jsonl"), "w") as handle:
            handle.write(json.dumps({"type": "assistant",
                "timestamp": "2026-07-16T20:39:45.000Z", "message": {
                    "role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "end_turn", "usage": {"input_tokens": 100,
                        "cache_creation_input_tokens": 200,
                        "cache_read_input_tokens": 700, "output_tokens": 5},
                    "content": [{"type": "text", "text": "child complete"}]}}) + "\n")
        fleet = self.engine.scan()
        parent = next(item for item in fleet["sessions"] if item["session_id"] == "same")
        self.assertEqual(parent["agents"][0]["agent_id"], aid)

        class RefuseLock:
            def __enter__(self):
                raise AssertionError("routine agent context read waited for scan_lock")

            def __exit__(self, *_):
                return False

        self.engine.scan_lock = RefuseLock()
        context = self.engine.agent_context("same", aid)
        self.assertTrue(context["ok"])
        self.assertEqual(context["messages"][-1]["text"], "child complete")
        self.assertEqual(context["info"]["status_line"]["cache_write"], 200)
        self.assertIn(("same", aid), self.engine._claude_agent_context_snapshots)

    def test_codex_agent_context_requires_exact_parent_membership(self):
        calls = []
        self.codex.agent_context = lambda sid, aid: (
            calls.append((sid, aid)) or {"ok": True, "messages": []})
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [{
                "session_id": "codex:parent-one", "provider": "codex",
                "agents": [{"agent_id": "child-one", "state": "running"}]}, {
                "session_id": "codex:parent-two", "provider": "codex",
                "agents": [{"agent_id": "child-two", "state": "running"}]}]}

        denied = self.engine.agent_context("codex:parent-one", "child-two")
        missing = self.engine.agent_context("codex:missing", "child-one")
        allowed = self.engine.agent_context("codex:parent-one", "child-one")

        self.assertFalse(denied["ok"])
        self.assertFalse(missing["ok"])
        self.assertTrue(allowed["ok"])
        self.assertEqual(calls, [("codex:parent-one", "child-one")])

    def test_claude_usage_includes_email_and_all_local_transcript_token_types(self):
        with open(self.claude_account, "w") as handle:
            json.dump({"oauthAccount": {"emailAddress": "claude@example.com"}}, handle)
        with open(self.claude_usage, "w") as handle:
            json.dump({"five_hour_pct": 12, "seven_day_pct": 34}, handle)
        with open(self.claude_stats, "w") as handle:
            json.dump({"modelUsage": {
                "claude-sonnet": {"inputTokens": 10,
                    "cacheCreationInputTokens": 20, "cacheReadInputTokens": 30,
                    "outputTokens": 40},
                "claude-haiku": {"inputTokens": 1, "outputTokens": 2,
                    "cacheReadInputTokens": None}}}, handle)

        usage = self.engine.read_usage()
        self.assertEqual(usage["email"], "claude@example.com")
        self.assertEqual(usage["lifetime_tokens"], 103)
        self.assertEqual(usage["lifetime_scope"], "local_transcripts")
        self.assertEqual((usage["five_hour_pct"], usage["weekly_pct"]), (12, 34))

    def test_claude_usage_tracks_selected_profiles_and_live_file_changes(self):
        def write(active, first_pct):
            profiles = [
                {"id": "profile-one", "name": "first", "isSelectedForDisplay": True,
                 "refreshInterval": 30,
                 "apiSessionKey": "must-never-leave-the-plist",
                 "oauthAccountJSON": json.dumps({"emailAddress": "first@example.com"}),
                 "claudeUsage": {"sessionPercentage": first_pct,
                                  "weeklyPercentage": 22,
                                  "fableWeeklyPercentage": 7,
                                  "sessionResetTime": 800_000_000,
                                  "weeklyResetTime": 800_100_000,
                                  "fableWeeklyResetTime": 800_150_000}},
                {"id": "profile-two", "name": "second", "isSelectedForDisplay": True,
                 "refreshInterval": 30,
                 "oauthAccountJSON": json.dumps({"emailAddress": "second@example.com"}),
                 "claudeUsage": {"sessionPercentage": 33,
                                  "weeklyPercentage": 44,
                                  "fableWeeklyPercentage": 66,
                                  "sessionResetTime": 800_200_000,
                                  "weeklyResetTime": 800_300_000,
                                  "fableWeeklyResetTime": 800_350_000}},
            ]
            with open(self.claude_usage_prefs, "wb") as handle:
                plistlib.dump({"profiles_v3": json.dumps(profiles).encode(),
                    "multiProfileDisplayConfig": json.dumps({"showWeek": True,
                        "showActiveProfileIndicator": True}).encode(),
                    "activeProfileId": active, "profileDisplayMode": "multi"}, handle)

        write("profile-one", 11)
        usage = self.engine.read_usage()
        self.assertEqual(usage["source"], "claude_usage")
        self.assertEqual([item["email"] for item in usage["profiles"]],
                         ["first@example.com", "second@example.com"])
        self.assertEqual([item["active"] for item in usage["profiles"]], [True, False])
        self.assertEqual([item["fable_weekly_pct"] for item in usage["profiles"]],
                         [7, 66])
        self.assertIsNotNone(usage["profiles"][0]["fable_weekly_reset"])
        self.assertEqual(usage["five_hour_pct"], 11)
        self.assertEqual(usage["refresh_seconds"], 30)
        self.assertNotIn("must-never-leave-the-plist", json.dumps(usage))

        write("profile-two", 55)
        usage = self.engine.read_usage()
        self.assertEqual([item["active"] for item in usage["profiles"]], [False, True])
        self.assertEqual(usage["five_hour_pct"], 33)

    def test_working_order_is_entry_order_and_persists(self):
        def rows(*ids):
            return [{"session_id": sid, "ui_group": "working"} for sid in ids]

        first = self.engine.stable_working_order(rows("a", "b"))
        self.assertEqual(first, {"a": 0, "b": 1})
        self.engine.cfg["working_order"] = ["a", "a", "b"]
        self.assertEqual(self.engine.stable_working_order(rows("b", "a")),
                         {"a": 0, "b": 1})
        self.assertEqual(self.engine.stable_working_order(rows("c", "b", "a")),
                         {"a": 0, "b": 1, "c": 2})
        self.assertEqual(self.engine.stable_working_order(rows("c", "b")),
                         {"b": 0, "c": 1})
        self.assertEqual(self.engine.stable_working_order(rows("a", "c", "b")),
                         {"b": 0, "c": 1, "a": 2})
        with open(os.path.join(self.base, "config.json")) as handle:
            stored = json.load(handle)
        self.assertEqual(os.stat(os.path.join(self.base, "config.json")).st_mode & 0o777,
                         0o600)
        self.assertEqual(stored["working_order"], ["b", "c", "a"])
        self.assertTrue(self.engine.update_settings({"preview_agents": True})["ok"])
        with open(os.path.join(self.base, "config.json")) as handle:
            stored = json.load(handle)
        self.assertEqual(stored["working_order"], ["b", "c", "a"])
        self.assertTrue(stored["preview_agents"])
        restarted_cfg = dict(DEFAULT_CONFIG)
        restarted_cfg.update(stored)
        restarted_cfg["codex_enabled"] = False
        restarted = Engine(restarted_cfg)
        try:
            self.assertEqual(restarted.stable_working_order(rows("a", "c", "b")),
                             {"b": 0, "c": 1, "a": 2})
        finally:
            if restarted.db:
                restarted.db.close()

    def test_codex_failure_keeps_last_good_codex_snapshot_stale_without_removing_claude(self):
        healthy = self.engine.scan()
        self.assertEqual({item["provider"] for item in healthy["sessions"]},
                         {"claude", "codex"})
        self.codex.fail_sessions = True
        fleet = self.engine.scan()
        self.assertEqual({item["provider"] for item in fleet["sessions"]},
                         {"claude", "codex"})
        codex = next(item for item in fleet["sessions"] if item["provider"] == "codex")
        self.assertEqual((codex["state"], codex["provider_stale"],
                          codex["state_confidence"], codex["access"]),
                         ("stale", True, "stale", "interactive"))
        self.assertFalse(fleet["providers"]["codex"]["ok"])
        self.assertIn("Codex crashed", fleet["providers"]["codex"]["error"])

        self.codex.fail_sessions = False
        recovered = self.engine.scan()
        codex = next(item for item in recovered["sessions"] if item["provider"] == "codex")
        self.assertEqual((codex["state"], codex["provider_stale"],
                          recovered["providers"]["codex"]["ok"]),
                         ("idle", False, True))

    def test_corrupt_shared_ledger_is_quarantined_and_fleet_still_starts(self):
        recovery_base = os.path.join(self.tmp.name, "recovery")
        os.makedirs(recovery_base)
        ledger_path = os.path.join(recovery_base, "ledger.db")
        with open(ledger_path, "wb") as handle:
            handle.write(b"not a sqlite database")
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({"codex_enabled": False, "ntfy_topic": ""})
        with mock.patch.object(engine_paths, "BASE", recovery_base):
            recovered = Engine(cfg)
            try:
                fleet = recovered.scan()
                self.assertFalse(fleet["ledger"]["ok"])
                self.assertTrue(fleet["ledger"]["recovered"])
                self.assertNotIn("detail", fleet["ledger"])
                self.assertTrue(os.path.exists(ledger_path))
                quarantine = os.path.join(recovery_base, fleet["ledger"]["quarantine"])
                self.assertTrue(os.path.exists(quarantine))
                with sqlite3.connect(ledger_path) as db:
                    self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                if recovered.db:
                    recovered.db.close()

    def test_http_ledger_reads_do_not_share_the_scan_connection(self):
        original = self.engine.ensure_db()

        class PoisonedScanConnection:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.DatabaseError("file is not a database")

            def close(self):
                pass

        self.engine.db = PoisonedScanConnection()
        try:
            insights = self.engine.insights(7)
            self.assertTrue(insights["ok"], insights)
            self.assertEqual(self.engine.recent_dirs()[0]["path"], self.cwd)
        finally:
            self.engine.db = original

    def test_claude_history_backfill_is_viewable_reopenable_and_idempotent(self):
        sid = "11111111-2222-3333-4444-555555555555"
        path = os.path.join(os.path.dirname(self.transcript), sid + ".jsonl")
        rows = [
            {"type": "user", "sessionId": sid, "cwd": self.cwd,
             "gitBranch": "archive", "timestamp": "2026-07-14T00:00:00Z",
             "message": {"role": "user", "content": "Review the old parser"}},
            {"type": "ai-title", "sessionId": sid,
             "aiTitle": "Historical parser review"},
            {"type": "assistant", "sessionId": sid, "cwd": self.cwd,
             "gitBranch": "archive", "timestamp": "2026-07-14T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-sonnet",
                         "stop_reason": "end_turn", "usage": {"input_tokens": 2,
                         "output_tokens": 1}, "content": [{"type": "text",
                                                             "text": "Review complete"}]}},
        ]
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        subdir = os.path.join(os.path.dirname(self.transcript), sid, "subagents")
        os.makedirs(subdir)
        with open(os.path.join(subdir, "agent-child.jsonl"), "w") as handle:
            handle.write(json.dumps(rows[-1]) + "\n")

        fleet = self.engine.scan()

        archived = next(item for item in fleet["closed"] if item["session_id"] == sid)
        self.assertEqual(archived["title"], "Historical parser review")
        self.assertTrue(archived["can_reopen"])
        self.assertEqual((archived["primary_action"], archived["access"]),
                         ("reopen", "reopen"))
        stored = self.engine.ensure_db().execute(
            "SELECT cost, agent_cost, agents_total FROM session_runs WHERE session_id=?",
            (sid,)).fetchone()
        self.assertEqual(stored, (None, None, None))
        context = self.engine.closed_context(sid)
        self.assertTrue(context["ok"])
        self.assertEqual(context["messages"][-1]["text"], "Review complete")
        self.assertEqual(self.engine.backfill_claude_history(), 0)
        ids = {row[0] for row in self.engine.ensure_db().execute(
            "SELECT session_id FROM session_runs")}
        self.assertNotIn("agent-child", ids)

        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps)) or {"ok": True})
        reopened = self.engine.act({"type": "reopen", "session_id": sid})
        self.assertTrue(reopened["ok"])
        self.assertTrue(reopened["reopened"])
        self.assertEqual(writes[0][0], "SPAWN")
        self.assertIn(f"claude --resume {sid}", writes[0][1][0][0])

    def test_claude_reopen_rejects_unindexed_and_unsafe_transcripts(self):
        unknown = self.engine.act({"type": "reopen",
                                   "session_id": "11111111-2222-3333-4444-555555555555"})
        self.assertFalse(unknown["ok"])
        self.assertIn("closed Claude", unknown["error"])
        self.assertIsNone(self.engine._safe_claude_transcript(
            "11111111-2222-3333-4444-555555555555", self.transcript))

    def test_actions_context_commands_and_files_route_by_provider(self):
        action = {"type": "text", "session_id": "codex:same", "text": "go"}
        self.assertEqual(self.engine.act(action)["provider"], "codex")
        self.assertEqual(self.codex.actions, [action])
        self.engine.snapshot_cache = {"sessions": [codex_session()]}
        self.assertTrue(self.engine.session_context("codex:same")["closed"])
        self.assertEqual(self.engine.commands("codex:same")["commands"][0]["name"],
                         "/compact")
        self.codex.context = lambda sid: {
            "ok": True, "messages": [],
            "files": [{"path": "/work/repo/a.txt", "name": "a.txt"}], "closed": True}
        self.assertEqual(self.engine.file_content(
            "codex:same", self.engine.file_id("codex:same", "/work/repo/a.txt")),
                         ("text/plain", b"codex", None))

        self.codex.context = mock.Mock(side_effect=AssertionError(
            "unknown Codex IDs must not reach the provider"))
        self.codex.file_content = mock.Mock(side_effect=AssertionError(
            "unknown Codex IDs must not reach the provider"))
        self.assertEqual(self.engine.session_context("codex:unknown"),
                         {"ok": False, "error": "unknown session"})
        self.assertEqual(self.engine.closed_context("codex:unknown"),
                         {"ok": False, "error": "unknown session"})
        self.assertEqual(self.engine.file_content("codex:unknown", "0" * 24),
                         (None, None, "unknown session"))
        self.codex.context.assert_not_called()
        self.codex.file_content.assert_not_called()

    def test_closed_resume_request_is_durable_idempotent_and_exact(self):
        sid = "codex:closed-owned"
        self.engine.closed_resume_capability = mock.Mock(return_value=(True, None))
        self.codex.resume_owned_thread = mock.Mock(return_value={
            "ok": True, "session_id": sid, "resumed": True})
        self.engine.snapshot_cache = {"sessions": [], "providers": {
            "codex": {"ok": True}, "claude": {"ok": True}}}
        action = {"session_id": sid, "text": "continue the saved work",
                  "client_request_id": "resume-request-123"}

        first = self.engine.resume_and_send(action)
        second = self.engine.resume_and_send(action)
        self.assertTrue(first["ok"])
        self.assertEqual(second["outbox_id"], first["outbox_id"])
        self.assertEqual(first["queue_state"], "waiting_availability")
        self.codex.resume_owned_thread.assert_called_once_with(sid)
        row = self.engine.outbox.get(first["outbox_id"])
        self.assertEqual(row["target_session_id"], sid)
        self.assertEqual(row["origin"], "closed_resume")

        self.engine.closed_resume_capability = mock.Mock(
            return_value=(False, "external Codex thread is view only"))
        refused = self.engine.resume_and_send({**action,
            "session_id": "codex:external", "client_request_id": "resume-request-456"})
        self.assertFalse(refused["ok"])
        self.assertIn("view only", refused["error"])

    def test_codex_terminal_focus_requires_exact_existing_route(self):
        session = codex_session()
        session["cwd"] = self.cwd
        self.codex.session = session
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps)) or {"ok": True})
        self.engine._codex_terminal_route = lambda tid, force=False: (
            {"tty": "/dev/ttys007", "pid": 700} if tid == "same" and force else None)
        result = self.engine.act({"type": "focus", "session_id": "codex:same"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["shared_runtime"])
        self.assertTrue(result["focused"])
        self.assertEqual(writes, [
            ("/dev/ttys007", [("__FOCUS__", False)])])
        self.assertEqual(self.codex.actions, [])

    def test_codex_terminal_focus_never_spawns_when_route_is_missing(self):
        self.codex.session = codex_session()
        self.engine._codex_terminal_route = lambda tid, force=False: None
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps)) or {"ok": True})

        result = self.engine.act({"type": "focus", "session_id": "codex:same"})

        self.assertFalse(result["ok"])
        self.assertIn("no attached Codex terminal", result["error"])
        self.assertEqual(writes, [])
        self.assertEqual(self.codex.actions, [])

    def test_codex_terminal_discovery_requires_exact_socket_uuid_and_unique_tty(self):
        thread_id = "019f6bb5-1a72-7041-a7f2-afab571271d9"
        socket_path = os.path.join(self.tmp.name, "fleet-codex.sock")
        exact = (f" 101 ttys001 /opt/homebrew/bin/node /opt/codex resume --remote "
                 f"unix://{socket_path} {thread_id}\n")
        duplicate_child = (f" 102 ttys001 /opt/codex resume --remote "
                           f"unix://{socket_path} {thread_id}\n")
        wrong_socket = (f" 103 ttys002 /opt/codex resume --remote "
                        f"unix://{socket_path}-other {thread_id}\n")
        headless = (f" 104 ?? /opt/codex resume --remote "
                    f"unix://{socket_path} {thread_id}\n")
        self.engine._codex_terminal_routes_cache = (0.0, {})
        with mock.patch("fleetdash.codex_runtime.codex_control_socket", return_value=socket_path), \
             mock.patch.object(subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=exact + duplicate_child + wrong_socket + headless)):
            routes = self.engine._codex_terminal_routes(force=True)
        self.assertEqual(routes, {thread_id: {"tty": "/dev/ttys001", "pid": 102}})

        ambiguous = exact + (f" 105 ttys003 /opt/codex resume --remote "
                             f"unix://{socket_path} {thread_id}\n")
        self.engine._codex_terminal_routes_cache = (0.0, {})
        with mock.patch("fleetdash.codex_runtime.codex_control_socket", return_value=socket_path), \
             mock.patch.object(subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=ambiguous)):
            self.assertEqual(self.engine._codex_terminal_routes(force=True), {})

    def test_live_codex_terminal_flushes_recovery_queue_without_app_server_control(self):
        thread_id = "019f6bb5-1a72-7041-a7f2-afab571271d9"
        sid = "codex:" + thread_id
        session = codex_session()
        session.update(session_id=sid, native_session_id=thread_id, state="stalled",
                       control_state="reconnecting", queue_accepting=True,
                       read_only=False, headless=False, external=True)
        session["capabilities"].update(submit=False, queue_submit=True,
                                       focus_terminal=False)
        self.codex.session = session
        routes = {}
        self.engine._codex_terminal_routes = lambda force=False: dict(routes)

        disconnected = self.engine.scan()
        before = next(item for item in disconnected["sessions"]
                      if item["session_id"] == sid)
        self.assertTrue(before["capabilities"]["queue_submit"])
        queued = self.engine.act({"type": "text", "session_id": sid,
                                  "text": "Queued exact work",
                                  "client_request_id": "terminal-route-recovery"})
        self.assertTrue(queued["queued"])
        self.assertEqual(self.engine.outbox.get(queued["outbox_id"])["state"],
                         "waiting_provider")

        routes[thread_id] = {"tty": "/dev/ttys001", "pid": 101}
        connected = self.engine.scan()
        after = next(item for item in connected["sessions"]
                     if item["session_id"] == sid)
        self.assertTrue(after["terminal_attached"])
        self.assertTrue(after["capabilities"]["submit"])
        self.assertFalse(after["capabilities"]["queue_submit"])
        self.assertEqual(after["access"], "interactive")
        self.assertEqual(after["control_state"], "terminal_active")

        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        self.engine.run_outbox()
        row = self.engine.outbox.get(queued["outbox_id"])
        self.assertEqual(row["state"], "sent")
        self.assertEqual(writes, [
            ("/dev/ttys001", [("Queued exact work", True)], 0.05)])
        self.assertEqual(self.codex.actions, [])

    def test_connected_app_server_turn_stays_canonical_when_terminal_is_attached(self):
        thread_id = "019f6bb5-1a72-7041-a7f2-afab571271d9"
        sid = "codex:" + thread_id
        session = codex_session()
        session.update(session_id=sid, native_session_id=thread_id, state="running",
                       control_state="connected_active", queue_accepting=False)
        session["capabilities"].update(submit=True, queue_submit=False)
        self.codex.session = session
        self.engine._codex_terminal_routes = lambda force=False: {
            thread_id: {"tty": "/dev/ttys001", "pid": 101}}

        snapshot = self.engine.scan()
        active = next(item for item in snapshot["sessions"]
                      if item["session_id"] == sid)
        self.assertTrue(active["terminal_attached"])
        self.assertEqual(active["control_state"], "connected_active")

        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        result = self.engine.act({"type": "text", "session_id": sid,
                                  "text": "After compact"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.codex.actions[-1]["text"], "After compact")
        self.assertEqual(writes, [])

    def test_codex_focus_uses_existing_exact_terminal_route(self):
        thread_id = "019f6bb5-1a72-7041-a7f2-afab571271d9"
        sid = "codex:" + thread_id
        self.codex.session = {**codex_session(), "session_id": sid,
                              "native_session_id": thread_id}
        self.engine._codex_terminal_route = lambda value, force=False: {
            "tty": "/dev/ttys001", "pid": 101}
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        result = self.engine.act({"type": "focus", "session_id": sid})
        self.assertTrue(result["ok"])
        self.assertTrue(result["focused"])
        self.assertEqual(result["transport"], "codex_terminal")
        self.assertEqual(writes, [
            ("/dev/ttys001", [("__FOCUS__", False)], 0.05)])
        self.assertEqual(self.codex.actions, [])

    def test_unknown_codex_id_cannot_use_discovered_terminal_route(self):
        self.engine.scan()
        self.engine._codex_terminal_route = lambda value, force=False: {
            "tty": "/dev/ttys001", "pid": 101}
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        provider_calls = []
        self.codex.act = lambda action: (
            provider_calls.append(action) or {"ok": False, "error": "unknown Codex session"})

        result = self.engine.act({"type": "text", "session_id": "codex:forged",
                                  "text": "must not reach a terminal"})
        self.assertFalse(result["ok"])
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(writes, [])
        focused = self.engine.act({"type": "focus", "session_id": "codex:forged"})
        self.assertFalse(focused["ok"])
        self.assertEqual(writes, [])

    def test_codex_spawn_starts_visible_initial_hi(self):
        with mock.patch.object(engine_paths, "HOME", self.tmp.name):
            result = self.engine.spawn_codex_session({
                "provider": "codex", "cwd": self.cwd, "model": "gpt-5.4",
                "effort": "high", "mode": "plan"})
        cwd = os.path.realpath(self.cwd)
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "codex:new-thread")
        self.assertEqual(result["initial_message"], "hi")
        self.assertEqual(self.codex.started_thread, {
            "cwd": cwd, "model": "gpt-5.4", "effort": "high",
            "mode": "plan", "initial_text": "hi"})

    def test_codex_spawn_uses_explicit_initial_message_when_supplied(self):
        with mock.patch.object(engine_paths, "HOME", self.tmp.name):
            result = self.engine.spawn_codex_session({
                "provider": "codex", "cwd": self.cwd, "model": "gpt-5.4",
                "effort": "high", "mode": "default", "initial_text": "Start exact work"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["initial_message"], "Start exact work")
        self.assertEqual(self.codex.started_thread["initial_text"], "Start exact work")

    def test_outbox_action_dispatches_to_exact_available_provider_session(self):
        self.engine.scan()
        created = self.engine.act({"type": "outbox_create", "kind": "when_available",
            "message": "Queued exact work", "created_zone": "UTC",
            "target_provider": "codex", "target_session_id": "codex:same"})
        self.assertTrue(created["ok"])
        self.engine.run_outbox()
        row = self.engine.outbox.get(created["item"]["id"])
        self.assertEqual(row["state"], "sent")
        self.assertEqual(self.codex.actions[-1], {"type": "text",
            "session_id": "codex:same", "text": "Queued exact work"})
        self.assertNotIn("Queued exact work", json.dumps(row["provider_receipt"]))

    def test_outbox_rejects_view_only_target_before_persisting(self):
        self.engine.scan()
        with self.engine.lock:
            target = next(item for item in self.engine.snapshot_cache["sessions"]
                          if item["session_id"] == "codex:same")
            target.update(read_only=True, access="view_only",
                          read_only_reason="another runtime owns it")
        result = self.engine.act({"type": "outbox_create", "kind": "when_available",
            "message": "Do not redirect", "created_zone": "UTC",
            "target_provider": "codex", "target_session_id": "codex:same"})
        self.assertFalse(result["ok"])
        self.assertIn("another runtime", result["error"])
        self.assertEqual(self.engine.outbox.counts()["pending"], 0)

    def test_scheduled_new_codex_session_sends_message_in_exact_first_turn(self):
        self.engine.scan()
        created = self.engine.act({"type": "outbox_create", "kind": "new_session",
            "message": "Scheduled first turn", "created_zone": "UTC",
            "trigger_at": time.time() - 1, "spawn_spec": {"provider": "codex",
                "cwd": self.cwd, "model": "gpt-5.4", "effort": "high",
                "mode": "plan", "worktree": False, "worktree_name": ""}})
        self.assertTrue(created["ok"])
        with mock.patch.object(engine_paths, "HOME", self.tmp.name):
            self.engine.run_outbox()
        row = self.engine.outbox.get(created["item"]["id"])
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["destination_session_id"], "codex:new-thread")
        self.assertEqual(self.codex.started_thread["initial_text"], "Scheduled first turn")

    def test_arbitrary_claude_file_path_is_rejected(self):
        ctype, data, error = self.engine.file_content("same", self.transcript)
        self.assertIsNone(ctype)
        self.assertIsNone(data)
        self.assertEqual(error, "invalid file selector")

    def test_legacy_ntfy_is_manual_generic_and_disabled_by_default(self):
        self.engine.cfg["ntfy_topic"] = "private-topic"
        self.engine.cfg["ntfy_server"] = "https://ntfy.example.test"
        sent = []
        self.engine._send_legacy_ntfy_test = lambda key: sent.append(key)
        self.assertFalse(self.engine.legacy_ntfy_test()["ok"])
        self.assertEqual(sent, [])
        enabled = self.engine.update_settings({"legacy_ntfy_enabled": True})
        self.assertEqual(enabled, {"ok": True, "legacy_ntfy_enabled": True})
        queued = self.engine.legacy_ntfy_test()
        self.assertTrue(queued["ok"])
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("legacy-test:"))
        self.assertEqual(self.engine.operations.legacy_notification_diagnostics()
                         ["statuses"]["queued"], 1)
        fleet = self.engine.scan()
        self.assertTrue(fleet["settings"]["legacy_ntfy_enabled"])
        self.assertTrue(fleet["settings"]["legacy_ntfy_configured"])
        self.assertNotIn("notify", fleet)
        self.assertNotIn("dashboard_url", fleet["settings"])

    def test_token_cookie_requires_exact_cookie_name_and_value(self):
        def check(cookie="", header="", mode="production"):
            obj = SimpleNamespace(eng=SimpleNamespace(cfg={"act_token": "secret",
                                                           "instance_mode": mode}),
                                  headers={"Cookie": cookie, "X-Act-Token": header})
            return Handler.token_ok(obj)

        self.assertTrue(check(cookie="act_token_production=secret"))
        self.assertTrue(check(cookie="act_token_staging=secret", mode="staging"))
        self.assertTrue(check(header="secret"))
        self.assertFalse(check(cookie="act_token=secret"))
        self.assertFalse(check(cookie="act_token_staging=secret"))
        self.assertFalse(check(cookie="act_token_production=secret", mode="staging"))
        self.assertFalse(check(cookie="xact_token=secret"))
        self.assertFalse(check(cookie="act_token_production=secret-suffix"))

    def test_claude_actions_share_validated_engine_surface(self):
        reg = {"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[os.getpid()] = "ttys-test"
        tail = SimpleNamespace(pending={}, poll=lambda: None, turn_state=lambda: "running")
        self.engine.tail_for = lambda path: tail
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        self.assertTrue(self.engine.act({"type": "text", "session_id": "same",
                                         "text": "hello"})["ok"])
        self.assertEqual(writes[-1][1], [("hello", True)])
        self.assertTrue(self.engine.act({"type": "focus",
                                         "session_id": "same"})["ok"])
        self.assertEqual(writes[-1][1], [("__FOCUS__", False)])

        reg["status"] = "busy"
        # Observing the provider's active transition closes the post-send
        # registry-lag state machine once this session later returns idle.
        self.assertTrue(self.engine.act({"type": "text", "session_id": "same",
                                         "text": "too soon"})["queueable"])
        self.assertTrue(self.engine.act({"type": "interrupt",
                                         "session_id": "same"})["ok"])
        self.assertEqual(writes[-1][1], [("\x1b", False)])
        reg["status"] = "shell"
        self.assertTrue(self.engine.act({"type": "interrupt",
                                         "session_id": "same"})["ok"])
        tail.turn_state = lambda: "awaiting_input"
        self.assertIn("finished", self.engine.act({"type": "interrupt",
            "session_id": "same"})["error"])
        reg["status"] = "waiting"
        self.engine.hook_pending = lambda sid, status: {
            "kind": "question", "nonce": "q1", "questions": [{
                "question": "Choose", "multiSelect": False, "allowOther": True,
                "options": [{"label": "One"}, {"label": "Two"}]}]}
        answered = self.engine.act({"type": "option", "session_id": "same",
                                    "nonce": "q1", "digits": [1], "n_options": 2})
        self.assertTrue(answered["ok"])
        self.assertEqual(writes[-1][1], [("1", False), ("", True)])

        # Client counts and mode are hints only. Even a maliciously large count
        # cannot expand the key sequence beyond the authoritative hook shape.
        answered = self.engine.act({"type": "option", "session_id": "same",
                                    "nonce": "q1", "digits": [2],
                                    "n_options": 1_000_000_000, "multi": True})
        self.assertTrue(answered["ok"])
        self.assertEqual(writes[-1][1], [("2", False), ("", True)])
        rejected = self.engine.act({"type": "option", "session_id": "same",
                                    "nonce": "q1", "digits": [3],
                                    "n_options": 1_000_000_000})
        self.assertFalse(rejected["ok"])
        self.assertIn("invalid option", rejected["error"])
        forged_permission = self.engine.act({"type": "permission", "session_id": "same",
                                             "nonce": "q1", "choice": "allow"})
        self.assertFalse(forged_permission["ok"])
        self.assertIn("not a permission", forged_permission["error"])

        self.engine.hook_pending = lambda sid, status: {
            "kind": "permission", "nonce": "p1"}
        allowed = self.engine.act({"type": "permission", "session_id": "same",
                                   "nonce": "p1", "choice": "allow"})
        self.assertTrue(allowed["ok"])
        self.assertEqual(writes[-1][1], [("1", False), ("", True)])
        reg["status"] = "idle"
        self.engine.hook_pending = lambda sid, status: None
        tail.pending = {}
        self.engine._agent_paths = lambda sid, aid: (self.transcript,
                                                     os.path.join(self.tmp.name, "missing-meta"))
        relayed = self.engine.act({"type": "relay", "session_id": "same",
            "agent_id": "agent-child", "text": "report status"})
        self.assertTrue(relayed["ok"])
        self.assertIn("agent-child", writes[-1][1][0][0])

    def test_queued_claude_image_revalidates_idle_before_injection(self):
        self.engine.live_sessions = lambda: [{"sessionId": "same", "pid": os.getpid(),
                                               "cwd": self.cwd, "status": "waiting"}]
        self.engine._iterm_write = mock.Mock(side_effect=AssertionError(
            "a queued image must not be typed into a prompt"))
        result = self.engine._write_claude_queued_message({
            "session_id": "same", "text": "Inspect",
            "image_paths": [os.path.join(self.base, "outbox-images", "image.jpg")]})
        self.assertFalse(result["ok"])
        self.assertTrue(result["queueable"])
        self.assertEqual(result["code"], "provider_control_unavailable")
        self.engine._iterm_write.assert_not_called()

    def test_background_claude_session_uses_supported_attach_transport(self):
        pid = 424245
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude", "kind": "bg",
               "jobId": "1a2b3c4d"}
        self.engine.live_sessions = lambda: [reg]
        tail = SimpleNamespace(pending={}, poll=lambda: None,
                               turn_state=lambda: "awaiting_input")
        self.engine.tail_for = lambda path: tail
        attached = []
        transport = SimpleNamespace(
            write=lambda job, steps, step_delay=None:
                attached.append((job, steps, step_delay)) or
                {"ok": True, "transport": "claude_attach"},
            attach_command=lambda job, cwd:
                ("/usr/local/bin/claude", job, os.path.realpath(cwd)),
            stop=lambda job: {"ok": True, "transport": "claude_stop"})
        self.engine._claude_background = transport
        self.engine._iterm_write = mock.Mock(side_effect=AssertionError(
            "background sends must not use iTerm"))

        result = self.engine.act({"type": "text", "session_id": "same",
                                  "text": "hello"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["transport"], "claude_attach")
        self.assertEqual(attached, [("1a2b3c4d", [("hello", True)], 0.05)])
        self.assertNotIn(pid, self.engine._tty_cache)

    def test_background_prewrite_failure_keeps_prompt_actions_safely_retryable(self):
        reg = {"sessionId": "same", "pid": 424248, "cwd": self.cwd,
               "status": "waiting", "name": "Claude", "kind": "bg",
               "jobId": "1a2b3c4d"}
        self.engine.live_sessions = lambda: [reg]
        tail = SimpleNamespace(pending={}, poll=lambda: None,
                               model="sonnet", permission_mode="default",
                               model_evidence_offset=1,
                               permission_mode_evidence_offset=1)
        self.engine.tail_for = lambda path: tail
        failure = {"ok": False, "code": "background_connection_lost",
                   "error": "Claude background attachment was not ready"}
        success = {"ok": True, "transport": "claude_attach"}
        cases = [
            ({"kind": "question", "nonce": "single", "questions": [{
                "question": "One?", "multiSelect": False,
                "options": [{"label": "A"}, {"label": "B"}]}]},
             {"type": "option", "nonce": "single", "digits": [1]}),
            ({"kind": "question", "nonce": "multi", "questions": [{
                "question": "Many?", "multiSelect": True,
                "options": [{"label": "A"}, {"label": "B"}]}]},
             {"type": "option", "nonce": "multi", "digits": [1]}),
            ({"kind": "question", "nonce": "dismiss", "questions": [{
                "question": "Dismiss?", "multiSelect": False,
                "options": [{"label": "A"}]}]},
             {"type": "dismiss", "nonce": "dismiss"}),
            ({"kind": "permission", "nonce": "deny", "tool": "Bash"},
             {"type": "permission", "nonce": "deny", "choice": "deny"}),
        ]
        for pending, payload in cases:
            with self.subTest(nonce=pending["nonce"]):
                self.engine.hook_pending = lambda sid, status, value=pending: value
                writer = mock.Mock(side_effect=[failure, success])
                self.engine._claude_background = SimpleNamespace(write=writer)

                first = self.engine.act({"session_id": "same", **payload})
                self.assertEqual(first.get("code"), "background_connection_lost")
                self.assertNotIn("same", self.engine._claude_delivery_uncertain)
                retried = self.engine.act({"session_id": "same", **payload})
                self.assertTrue(retried["ok"], retried)
                self.assertEqual(writer.call_count, 2)

    def test_background_prewrite_failure_keeps_control_actions_safely_retryable(self):
        pid = 424249
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude", "kind": "bg",
               "jobId": "1a2b3c4d"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._claude_command_cache[pid] = "/usr/local/bin/claude --model sonnet"
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, tail: None
        self.engine.effort_for = lambda sid: "medium"
        tail = SimpleNamespace(pending={}, poll=lambda: None,
                               model="sonnet", permission_mode="default",
                               model_evidence_offset=1,
                               permission_mode_evidence_offset=1)
        self.engine.tail_for = lambda path: tail
        failure = {"ok": False, "code": "background_connection_lost",
                   "error": "Claude background attachment was not ready"}
        success = {"ok": True, "transport": "claude_attach"}
        cases = [
            ({"type": "session_settings", "model": "opus", "effort": "medium",
              "expected_model": "sonnet", "expected_effort": "medium"}, 1),
            ({"type": "session_settings", "model": "opus", "effort": "high",
              "expected_model": "sonnet", "expected_effort": "medium"}, 2),
            ({"type": "permission_mode", "mode": "acceptEdits"}, 1),
            ({"type": "permission_mode", "mode": "plan"}, 2),
        ]
        with mock.patch.object(self.engine, "_record_claude_control_overrides",
                               return_value=None):
            for payload, successful_writes in cases:
                with self.subTest(action=payload):
                    tail.model = "sonnet"
                    tail.permission_mode = "default"
                    self.engine._claude_control_uncertain.clear()
                    writer = mock.Mock(side_effect=[failure] +
                                      [success] * successful_writes)
                    self.engine._claude_background = SimpleNamespace(write=writer)

                    first = self.engine.act({"session_id": "same", **payload})
                    self.assertEqual(first.get("code"), "background_connection_lost")
                    self.assertNotIn("same", self.engine._claude_control_uncertain)
                    retried = self.engine.act({"session_id": "same", **payload})
                    self.assertTrue(retried["ok"], retried)
                    self.assertEqual(writer.call_count, 1 + successful_writes)

    def test_foreground_claude_tty_fallback_rejects_non_terminal_paths(self):
        pid = 424246
        ps_result = SimpleNamespace(stdout="??\n", returncode=0)
        lsof_result = SimpleNamespace(stdout=f"p{pid}\nf0\nn/private/tmp/input\n",
                                      returncode=0)
        with mock.patch.object(subprocess, "run",
                               side_effect=[ps_result, lsof_result]):
            self.assertEqual(self.engine._tty_for_pid(pid), "")
        self.assertNotIn(pid, self.engine._tty_cache)

    def test_background_claude_focus_opens_official_attach_and_close_uses_stop(self):
        pid = 424247
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude", "kind": "bg",
               "jobId": "1a2b3c4d"}
        self.engine.live_sessions = lambda: [reg]
        transport = SimpleNamespace(
            write=mock.Mock(return_value={"ok": True}),
            attach_command=lambda job, cwd:
                ("/usr/local/bin/claude", job, os.path.realpath(cwd)),
            stop=mock.Mock(return_value={"ok": True, "transport": "claude_stop"}))
        self.engine._claude_background = transport
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        focused = self.engine.act({"type": "focus", "session_id": "same"})
        self.assertTrue(focused["ok"])
        self.assertEqual(writes[0][0], "SPAWN")
        self.assertEqual(writes[0][1], [(f"cd {os.path.realpath(self.cwd)} && "
            "/usr/local/bin/claude attach 1a2b3c4d", False)])

        process = SimpleNamespace(stdout="/usr/local/bin/claude --bg-pty-host")
        with mock.patch.object(subprocess, "run", return_value=process), \
             mock.patch.object(os, "kill") as kill:
            closed = self.engine.act({"type": "close", "session_id": "same"})
        self.assertTrue(closed["ok"])
        transport.stop.assert_called_once_with("1a2b3c4d")
        kill.assert_not_called()

    def test_claude_permission_mode_uses_only_verified_native_cycle(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine._claude_command_cache[pid] = "/usr/local/bin/claude --model sonnet"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet-5", model_evidence_offset=1,
                               permission_mode_evidence_offset=1)
        self.engine.tail_for = lambda path: tail
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        changed = self.engine.act({"type": "permission_mode", "session_id": "same",
                                   "mode": "plan"})
        self.assertEqual(changed, {"ok": True, "mode": "plan"})
        self.assertEqual(writes[-2:], [
            ("/dev/ttys-test", [("\x1b[Z", False)], 0.4),
            ("/dev/ttys-test", [("\x1b[Z", False)], 0.4)])
        self.assertEqual(tail.permission_mode, "plan")

        self.engine._claude_command_cache[pid] = (
            "/usr/local/bin/claude --allow-dangerously-skip-permissions")
        tail.permission_mode = "plan"
        bypass = self.engine.act({"type": "permission_mode", "session_id": "same",
                                  "mode": "bypassPermissions"})
        self.assertTrue(bypass["ok"])
        self.assertEqual(writes[-1][1], [("\x1b[Z", False)])
        tail.permission_mode = "plan"
        auto = self.engine.act({"type": "permission_mode", "session_id": "same",
                                "mode": "auto"})
        self.assertTrue(auto["ok"])
        self.assertEqual([item[1] for item in writes[-2:]],
                         [[("\x1b[Z", False)], [("\x1b[Z", False)]])

        tail.permission_mode_evidence_offset += 1
        tail.permission_mode = "dontAsk"
        self.assertIn("startup-only", self.engine.act({"type": "permission_mode",
            "session_id": "same", "mode": "default"})["error"])
        reg["status"] = "busy"
        self.assertIn("idle", self.engine.act({"type": "permission_mode",
            "session_id": "same", "mode": "plan"})["error"])

    def test_claude_existing_session_settings_are_idle_allowlisted_and_failure_safe(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine._claude_command_cache[pid] = "/usr/local/bin/claude --model sonnet"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet")
        self.engine.tail_for = lambda path: tail
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        changed = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "opus", "effort": "high",
            "expected_model": "claude-sonnet", "expected_effort": ""})
        self.assertEqual(changed, {"ok": True, "model": "opus", "effort": "high"})
        self.assertEqual(writes[-2:], [
            ("/dev/ttys-test", [("/model opus", True)], 0.4),
            ("/dev/ttys-test", [("/effort high", True)], 0.4)])
        self.assertEqual(tail.model, "opus")
        self.assertEqual(self.engine.effort_for("same"), "high")

        effort_only = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "opus", "effort": "low",
            "expected_model": "opus", "expected_effort": "high"})
        self.assertTrue(effort_only["ok"], effort_only)
        self.assertEqual(writes[-1], ("/dev/ttys-test", [("/effort low", True)], 0.4))

        before = list(writes)
        invalid = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "/quit", "effort": "low"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(writes, before)
        stale = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "sonnet", "effort": "high",
            "expected_model": "opus", "expected_effort": "high"})
        self.assertEqual(stale.get("code"), "stale_settings")
        self.assertEqual(writes, before)

        self.engine._iterm_write = lambda tty, steps, step_delay=None: {
            "ok": False, "code": "injector_not_launched",
            "error": "provider rejected command"}
        failed = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "sonnet", "effort": "high",
            "expected_model": "opus", "expected_effort": "low"})
        self.assertFalse(failed["ok"])
        self.assertEqual(tail.model, "opus")
        self.assertEqual(self.engine.effort_for("same"), "low")

        reg["status"] = "busy"
        busy = self.engine.act({"type": "session_settings", "session_id": "same",
                                "model": "sonnet", "effort": "high"})
        self.assertFalse(busy["ok"])
        self.assertIn("idle", busy["error"])

    def test_claude_control_sequences_report_the_exact_accepted_prefix(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine._claude_command_cache[pid] = "/usr/local/bin/claude --model sonnet"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet")
        self.engine.tail_for = lambda path: tail
        results = iter(({"ok": True}, {"ok": False, "code": "injector_not_launched",
                                        "error": "second command did not launch"}))
        self.engine._iterm_write = lambda tty, steps, step_delay=None: next(results)
        with mock.patch.object(time, "sleep"):
            settings = self.engine.act({"type": "session_settings", "session_id": "same",
                "model": "opus", "effort": "high",
                "expected_model": "claude-sonnet", "expected_effort": ""})
        self.assertTrue(settings["ok"], settings)
        self.assertTrue(settings["partial"])
        self.assertEqual((settings["model"], settings["effort"]), ("opus", ""))
        self.assertEqual(tail.model, "opus")

        tail.permission_mode = "default"
        results = iter(({"ok": True}, {"ok": False, "code": "injector_not_launched",
                                        "error": "second key did not launch"}))
        self.engine._iterm_write = lambda tty, steps, step_delay=None: next(results)
        with mock.patch.object(time, "sleep"):
            permission = self.engine.act({"type": "permission_mode",
                "session_id": "same", "mode": "plan"})
        self.assertTrue(permission["ok"], permission)
        self.assertTrue(permission["partial"])
        self.assertEqual(permission["mode"], "acceptEdits")
        self.assertEqual(tail.permission_mode, "acceptEdits")

    def test_claude_accepted_controls_survive_restart_until_newer_native_evidence(self):
        pid = os.getpid()
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})

        accepted = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "opus", "effort": "high",
            "expected_model": "claude-sonnet", "expected_effort": ""})
        self.assertTrue(accepted["ok"], accepted)

        with open(os.path.join(self.base, "config.json")) as handle:
            restored_cfg = dict(DEFAULT_CONFIG)
            restored_cfg.update(json.load(handle))
        restored_cfg["codex_enabled"] = False
        restarted = Engine(restored_cfg)
        restarted.codex = FakeCodex(None)
        try:
            fleet = restarted.scan()
            session = next(item for item in fleet["sessions"]
                           if item["session_id"] == "same")
            self.assertEqual(session["model"], "opus")
            self.assertEqual(session["effort"], "high")

            # This row is appended later but deliberately carries an older
            # timestamp, matching Claude's compaction timestamp inversion.
            with open(self.transcript, "a") as handle:
                handle.write(json.dumps({"type": "assistant",
                    "timestamp": "2020-01-01T00:00:00Z",
                    "message": {"role": "assistant", "model": "claude-haiku",
                        "stop_reason": "end_turn", "usage": {"input_tokens": 1},
                        "content": [{"type": "text", "text": "native"}]}}) + "\n")
            effort_dir = os.path.join(self.base, "effort")
            os.makedirs(effort_dir, exist_ok=True)
            effort_path = os.path.join(effort_dir, "same")
            with open(effort_path, "w") as handle:
                handle.write("low")
            os.utime(effort_path, (time.time() + 5, time.time() + 5))
            fleet = restarted.scan()
            session = next(item for item in fleet["sessions"]
                           if item["session_id"] == "same")
            self.assertEqual(session["model"], "claude-haiku")
            self.assertEqual(session["effort"], "low")

            with open(self.transcript, "a") as handle:
                handle.write(json.dumps({"type": "permission-mode",
                    "timestamp": "2020-01-01T00:00:01Z",
                    "permissionMode": "default"}) + "\n")
            restarted.scan()
            restarted._tty_cache[pid] = "ttys-test"
            results = iter(({"ok": True}, {"ok": False,
                "code": "injector_not_launched", "error": "second key failed"}))
            restarted._iterm_write = lambda tty, steps, step_delay=None: next(results)
            with mock.patch.object(time, "sleep"):
                partial = restarted.act({"type": "permission_mode",
                    "session_id": "same", "mode": "plan"})
            self.assertTrue(partial["partial"], partial)
            self.assertEqual(partial["mode"], "acceptEdits")

            with open(os.path.join(self.base, "config.json")) as handle:
                third_cfg = dict(DEFAULT_CONFIG)
                third_cfg.update(json.load(handle))
            third_cfg["codex_enabled"] = False
            third = Engine(third_cfg)
            third.codex = FakeCodex(None)
            try:
                fleet = third.scan()
                session = next(item for item in fleet["sessions"]
                               if item["session_id"] == "same")
                self.assertEqual(session["permission_mode"], "acceptEdits")
                with open(self.transcript, "a") as handle:
                    handle.write(json.dumps({"type": "permission-mode",
                        "timestamp": "2019-01-01T00:00:00Z",
                        "permissionMode": "plan"}) + "\n")
                fleet = third.scan()
                session = next(item for item in fleet["sessions"]
                               if item["session_id"] == "same")
                self.assertEqual(session["permission_mode"], "plan")
            finally:
                if third.db:
                    third.db.close()
        finally:
            if restarted.db:
                restarted.db.close()

    def test_claude_lost_control_ack_fails_closed_across_restart(self):
        pid = os.getpid()
        effort_dir = os.path.join(self.base, "effort")
        os.makedirs(effort_dir, exist_ok=True)
        with open(os.path.join(effort_dir, "same"), "w") as handle:
            handle.write("high")
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        self.engine._iterm_write = mock.Mock(return_value={
            "ok": False, "code": "delivery_uncertain", "error": "lost result"})
        uncertain = self.engine.act({"type": "session_settings", "session_id": "same",
            "model": "opus", "effort": "high",
            "expected_model": "claude-sonnet", "expected_effort": "high"})
        self.assertEqual(uncertain.get("code"), "control_delivery_uncertain")

        with open(os.path.join(self.base, "config.json")) as handle:
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(json.load(handle))
        cfg["codex_enabled"] = False
        restarted = Engine(cfg)
        restarted.codex = FakeCodex(None)
        try:
            fleet = restarted.scan()
            session = next(item for item in fleet["sessions"]
                           if item["session_id"] == "same")
            self.assertTrue(session["control_delivery_uncertain"])
            self.assertFalse(session["capabilities"]["change_model_effort"])
            with open(self.transcript, "a") as handle:
                handle.write(json.dumps({"type": "assistant",
                    "timestamp": "2020-01-01T00:00:00Z",
                    "message": {"role": "assistant", "model": "opus",
                        "stop_reason": "end_turn", "usage": {"input_tokens": 1},
                        "content": []}}) + "\n")
            fleet = restarted.scan()
            session = next(item for item in fleet["sessions"]
                           if item["session_id"] == "same")
            self.assertFalse(session["control_delivery_uncertain"])

            with open(self.transcript, "a") as handle:
                handle.write(json.dumps({"type": "permission-mode",
                    "timestamp": "2020-01-01T00:00:01Z",
                    "permissionMode": "default"}) + "\n")
            restarted.scan()
            restarted._tty_cache[pid] = "ttys-test"
            restarted._iterm_write = mock.Mock(return_value={
                "ok": False, "code": "delivery_uncertain", "error": "lost result"})
            permission = restarted.act({"type": "permission_mode",
                "session_id": "same", "mode": "acceptEdits"})
            self.assertEqual(permission.get("code"), "control_delivery_uncertain")
            with open(os.path.join(self.base, "config.json")) as handle:
                final_cfg = dict(DEFAULT_CONFIG)
                final_cfg.update(json.load(handle))
            final_cfg["codex_enabled"] = False
            final = Engine(final_cfg)
            final.codex = FakeCodex(None)
            try:
                session = next(item for item in final.scan()["sessions"]
                               if item["session_id"] == "same")
                self.assertFalse(session["capabilities"]["change_permission_mode"])
            finally:
                if final.db:
                    final.db.close()
        finally:
            if restarted.db:
                restarted.db.close()

    def test_claude_prompt_lost_ack_blocks_every_retry_shape_across_restart(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "waiting", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine.compacting_secs = lambda sid, cwd, mt: None

        cases = [
            ({"kind": "question", "nonce": "single", "questions": [{
                "question": "One?", "multiSelect": False,
                "options": [{"label": "A"}, {"label": "B"}]}]},
             {"type": "option", "nonce": "single", "digits": [1]}),
            ({"kind": "question", "nonce": "multi", "questions": [{
                "question": "Many?", "multiSelect": True,
                "options": [{"label": "A"}, {"label": "B"}]}]},
             {"type": "option", "nonce": "multi", "digits": [1]}),
            ({"kind": "question", "nonce": "dismiss", "questions": [{
                "question": "Dismiss?", "multiSelect": False,
                "options": [{"label": "A"}]}]},
             {"type": "dismiss", "nonce": "dismiss"}),
            ({"kind": "permission", "nonce": "deny", "tool": "Bash"},
             {"type": "permission", "nonce": "deny", "choice": "deny"}),
        ]
        for pending, payload in cases:
            with self.subTest(nonce=pending["nonce"]):
                self.engine.hook_pending = lambda sid, status, value=pending: value
                writer = mock.Mock(return_value={"ok": False,
                    "code": "delivery_uncertain", "error": "lost result"})
                self.engine._iterm_write = writer
                result = self.engine.act({"session_id": "same", **payload})
                self.assertEqual(result.get("code"), "delivery_uncertain")
                calls = writer.call_count
                retry = self.engine.act({"session_id": "same", **payload})
                self.assertEqual(retry.get("code"), "delivery_uncertain")
                self.assertEqual(writer.call_count, calls)

        with open(os.path.join(self.base, "config.json")) as handle:
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(json.load(handle))
        cfg["codex_enabled"] = False
        restarted = Engine(cfg)
        restarted.codex = FakeCodex(None)
        restarted.live_sessions = lambda: [reg]
        restarted._tty_cache[pid] = "ttys-test"
        restarted.compacting_secs = lambda sid, cwd, mt: None
        try:
            # A transient unreadable/partial hook file must not erase the
            # durable nonce while the registry still reports waiting.
            restarted.hook_pending = lambda sid, status: None
            restarted.scan()
            self.assertEqual(restarted._claude_delivery_uncertain.get("same"), "deny")
            pending, payload = cases[-1]
            restarted.hook_pending = lambda sid, status: pending
            writer = mock.Mock(return_value={"ok": True})
            restarted._iterm_write = writer
            retry = restarted.act({"session_id": "same", **payload})
            self.assertEqual(retry.get("code"), "delivery_uncertain")
            writer.assert_not_called()

            # One registry transition is not enough while the same permission
            # capture remains valid. Permission captures have no clear hook and
            # can suppress the Notification for a newer prompt, then become
            # visible again when the registry returns to waiting.
            reg["status"] = "busy"
            restarted.scan()
            self.assertEqual(restarted._claude_delivery_uncertain.get("same"), "deny")
            reg["status"] = "waiting"
            restarted.scan()
            retry = restarted.act({"session_id": "same", **payload})
            self.assertEqual(retry.get("code"), "delivery_uncertain")
            writer.assert_not_called()

            # A different valid nonce is canonical evidence that the old
            # native surface has changed and retires the old fence.
            fresh = {"kind": "permission", "nonce": "fresh", "tool": "Bash"}
            restarted.hook_pending = lambda sid, status: fresh
            restarted.scan()
            self.assertNotIn("same", restarted._claude_delivery_uncertain)
        finally:
            if restarted.db:
                restarted.db.close()

    def test_hook_pending_closes_capture_file(self):
        pending_dir = os.path.join(self.base, "pending")
        os.makedirs(pending_dir)
        path = os.path.join(pending_dir, "same.json")
        with open(path, "w") as handle:
            json.dump({"kind": "permission", "nonce": "p-close",
                       "message": "Allow?", "ts": time.time()}, handle)

        real_open = open
        handles = []

        def tracked_open(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            handles.append(handle)
            return handle

        with mock.patch("builtins.open", side_effect=tracked_open):
            pending = self.engine.hook_pending("same", "waiting")
        self.assertEqual(pending["nonce"], "p-close")
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)

    def test_claude_pending_capabilities_match_native_registry_gate(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        pending = {"kind": "question", "nonce": "q-parity", "questions": [{
            "question": "Choose", "multiSelect": False,
            "options": [{"label": "One"}, {"label": "Two"}]}]}
        self.engine.live_sessions = lambda: [reg]
        self.engine.hook_pending = lambda sid, status: pending
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        self.engine._tty_cache[pid] = "ttys-test"
        self.engine._iterm_write = mock.Mock(return_value={"ok": True})

        idle = next(item for item in self.engine.scan()["sessions"]
                    if item["session_id"] == "same")
        self.assertFalse(idle["capabilities"]["answer_structured"])
        self.assertIn("native prompt", idle["capabilities"]["answer_reason"])
        rejected = self.engine.act({"type": "option", "session_id": "same",
                                    "nonce": "q-parity", "digits": [1]})
        self.assertFalse(rejected["ok"])
        self.engine._iterm_write.assert_not_called()

        reg["status"] = "waiting"
        waiting = next(item for item in self.engine.scan()["sessions"]
                       if item["session_id"] == "same")
        self.assertTrue(waiting["capabilities"]["answer_structured"])
        accepted = self.engine.act({"type": "option", "session_id": "same",
                                    "nonce": "q-parity", "digits": [1]})
        self.assertTrue(accepted["ok"], accepted)

    def test_claude_settings_block_fresh_hook_requests_and_compaction(self):
        pending = {"kind": "permission", "nonce": "pending-settings",
                   "tool": "Bash", "input_summary": "approval required"}
        with mock.patch.object(self.engine, "hook_pending", return_value=pending), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None):
            fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"]
                      if item["session_id"] == "same")
        self.assertFalse(claude["capabilities"]["change_model_effort"])
        self.assertFalse(claude["capabilities"]["change_permission_mode"])
        self.assertIn("request", claude["capabilities"]["change_model_effort_reason"])

        with mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=4):
            fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"]
                      if item["session_id"] == "same")
        self.assertFalse(claude["capabilities"]["change_model_effort"])
        self.assertFalse(claude["capabilities"]["change_permission_mode"])
        self.assertIn("compaction", claude["capabilities"]["change_model_effort_reason"])

        transcript_tail = self.engine.tail_for(self.transcript)
        transcript_tail.pending = {
            "transcript-request": {"name": "Bash", "input": {"command": "pwd"}}}
        with mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None):
            fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"]
                      if item["session_id"] == "same")
        self.assertFalse(claude["capabilities"]["change_model_effort"])
        self.assertFalse(claude["capabilities"]["change_permission_mode"])
        self.assertIn("request", claude["capabilities"]["change_model_effort_reason"])
        transcript_tail.pending.clear()

        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet", last_compact_ep=0)
        self.engine.tail_for = lambda path: tail
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        action = {"type": "session_settings", "session_id": "same",
                  "model": "opus", "effort": "high",
                  "expected_model": "claude-sonnet", "expected_effort": ""}
        with mock.patch.object(self.engine, "hook_pending", return_value=pending), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None):
            rejected = self.engine.act(action)
            permission_rejected = self.engine.act({"type": "permission_mode",
                "session_id": "same", "mode": "plan"})
        self.assertFalse(rejected["ok"])
        self.assertFalse(permission_rejected["ok"])
        self.assertIn("pending request", rejected["error"])
        with mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=2):
            rejected = self.engine.act(action)
            permission_rejected = self.engine.act({"type": "permission_mode",
                "session_id": "same", "mode": "plan"})
        self.assertFalse(rejected["ok"])
        self.assertFalse(permission_rejected["ok"])
        self.assertIn("compacting", rejected["error"])
        tail.pending = {"transcript-request": {"name": "Bash", "input": {}}}
        with mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None):
            rejected = self.engine.act(action)
            permission_rejected = self.engine.act({"type": "permission_mode",
                "session_id": "same", "mode": "plan"})
        self.assertFalse(rejected["ok"])
        self.assertFalse(permission_rejected["ok"])
        self.assertIn("pending request", rejected["error"])
        self.assertEqual(writes, [])

    def test_claude_session_mutation_lock_orders_settings_before_text(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet", last_compact_ep=0)
        self.engine.tail_for = lambda path: tail
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        entered = threading.Event()
        release = threading.Event()
        writes = []

        def blocked_write(tty, steps, step_delay=None):
            writes.append((tty, steps, step_delay))
            if steps and steps[0][0].startswith("/model"):
                entered.set()
                self.assertTrue(release.wait(2))
            return {"ok": True}

        self.engine._iterm_write = blocked_write
        results = {}
        setting = threading.Thread(target=lambda: results.setdefault("settings",
            self.engine.act({"type": "session_settings", "session_id": "same",
                "model": "opus", "effort": "high",
                "expected_model": "claude-sonnet", "expected_effort": ""})))
        text = threading.Thread(target=lambda: results.setdefault("text",
            self.engine.act({"type": "text", "session_id": "same",
                             "text": "use the accepted settings"})))
        setting.start()
        self.assertTrue(entered.wait(2))
        text.start()
        time.sleep(.03)
        self.assertEqual(len(writes), 1)
        release.set()
        setting.join(2)
        text.join(2)
        self.assertFalse(setting.is_alive())
        self.assertFalse(text.is_alive())
        self.assertTrue(results["settings"]["ok"], results)
        self.assertTrue(results["text"]["ok"], results)
        self.assertEqual(writes[0][1], [("/model opus", True)])
        self.assertEqual(writes[1][1], [("/effort high", True)])
        self.assertEqual(writes[2][1], [("use the accepted settings", True)])

    def test_claude_direct_text_and_image_never_enter_a_pending_or_compacting_tui(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        tail = SimpleNamespace(pending={"tool-1": {"name": "Bash", "input": {}}},
                               poll=lambda: None, permission_mode="default",
                               model="claude-sonnet", last_compact_ep=0)
        self.engine.tail_for = lambda path: tail
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        self.engine._resolve_image_uploads = lambda sid, ids: (
            ["/private/tmp/fleet-image.png"], None)
        self.engine._iterm_write = mock.Mock()

        text = self.engine.act({"type": "text", "session_id": "same", "text": "hello"})
        image = self.engine.act({"type": "image_text", "session_id": "same",
                                 "text": "inspect", "upload_ids": ["upload-1"]})
        self.assertFalse(text["ok"])
        self.assertTrue(text["queueable"])
        self.assertFalse(image["ok"])
        self.assertTrue(image["queueable"])
        self.engine._iterm_write.assert_not_called()

        tail.pending = {}
        self.engine.compacting_secs = lambda sid, cwd, mt: 1
        compacting = self.engine.act({"type": "text", "session_id": "same",
                                      "text": "still unsafe"})
        self.assertFalse(compacting["ok"])
        self.assertTrue(compacting["queueable"])
        self.engine._iterm_write.assert_not_called()

    def test_claude_busy_relay_ignores_transcript_tool_pending_but_idle_relay_does_not(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "busy", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        tail = SimpleNamespace(pending={"tool-1": {"name": "Bash", "input": {}}},
                               poll=lambda: None, permission_mode="default",
                               model="claude-sonnet", last_compact_ep=0)
        self.engine.tail_for = lambda path: tail
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        meta = os.path.join(self.tmp.name, "agent.meta.json")
        with open(meta, "w") as handle:
            json.dump({"description": "worker"}, handle)
        self.engine._agent_paths = lambda sid, aid: (self.transcript, meta)
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        relayed = self.engine.act({"type": "relay", "session_id": "same",
                                   "agent_id": "agent-worker", "text": "status?"})
        self.assertTrue(relayed["ok"], relayed)
        self.assertEqual(len(writes), 1)
        reg["status"] = "idle"
        rejected = self.engine.act({"type": "relay", "session_id": "same",
                                    "agent_id": "agent-worker", "text": "again"})
        self.assertFalse(rejected["ok"])
        self.assertEqual(len(writes), 1)

    def test_claude_session_mutation_lock_makes_second_settings_cas_stale(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet", last_compact_ep=0)
        self.engine.tail_for = lambda path: tail
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        entered = threading.Event()
        release = threading.Event()
        writes = []

        def blocked_write(tty, steps, step_delay=None):
            writes.append((tty, steps, step_delay))
            entered.set()
            self.assertTrue(release.wait(2))
            return {"ok": True}

        self.engine._iterm_write = blocked_write
        results = {}
        first = threading.Thread(target=lambda: results.setdefault("first",
            self.engine.act({"type": "session_settings", "session_id": "same",
                "model": "opus", "effort": "high",
                "expected_model": "claude-sonnet", "expected_effort": ""})))
        second = threading.Thread(target=lambda: results.setdefault("second",
            self.engine.act({"type": "session_settings", "session_id": "same",
                "model": "haiku", "effort": "low",
                "expected_model": "claude-sonnet", "expected_effort": ""})))
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        time.sleep(.03)
        self.assertEqual(len(writes), 1)
        release.set()
        first.join(2)
        second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(results["first"]["ok"], results)
        self.assertFalse(results["second"]["ok"], results)
        self.assertEqual(results["second"].get("code"), "stale_settings")
        self.assertEqual(len(writes), 2)

    def test_claude_queued_image_uses_same_session_mutation_lock(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet", last_compact_ep=0, convo_rev=1)
        self.engine.tail_for = lambda path: tail
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        entered = threading.Event()
        release = threading.Event()
        writes = []

        def blocked_write(tty, steps, step_delay=None):
            writes.append((tty, steps, step_delay))
            if steps and steps[0][0].startswith("/model"):
                entered.set()
                self.assertTrue(release.wait(2))
            return {"ok": True}

        self.engine._iterm_write = blocked_write
        results = {}
        setting = threading.Thread(target=lambda: results.setdefault("settings",
            self.engine.act({"type": "session_settings", "session_id": "same",
                "model": "opus", "effort": "high",
                "expected_model": "claude-sonnet", "expected_effort": ""})))
        queued = threading.Thread(target=lambda: results.setdefault("queued",
            self.engine._outbox_dispatch({"target_provider": "claude",
                "destination_session_id": "same", "message": "inspect this",
                "_image_paths": ["/private/tmp/fleet-image.png"]})))
        setting.start()
        self.assertTrue(entered.wait(2))
        queued.start()
        time.sleep(.03)
        self.assertEqual(len(writes), 1)
        release.set()
        setting.join(2)
        queued.join(2)
        self.assertFalse(setting.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertTrue(results["settings"]["ok"], results)
        self.assertTrue(results["queued"]["ok"], results)
        self.assertEqual(len(writes), 3)
        self.assertIn("Images attached through Fleet", writes[2][1][0][0])

    def test_claude_turn_start_fence_blocks_registry_lag_until_busy_then_idle(self):
        pid = os.getpid()
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "idle", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        tail = SimpleNamespace(pending={}, poll=lambda: None, permission_mode="default",
                               model="claude-sonnet", last_compact_ep=0, convo_rev=1,
                               turn_state=lambda: "awaiting_input")
        self.engine.tail_for = lambda path: tail
        self.engine.hook_pending = lambda sid, status: None
        self.engine.compacting_secs = lambda sid, cwd, mt: None
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})

        first = self.engine.act({"type": "text", "session_id": "same",
                                 "text": "start the turn"})
        self.assertTrue(first["ok"], first)
        rapid_text = self.engine.act({"type": "text", "session_id": "same",
                                      "text": "too soon"})
        self.assertFalse(rapid_text["ok"])
        self.assertTrue(rapid_text["queueable"])
        rapid_settings = self.engine.act({"type": "session_settings",
            "session_id": "same", "model": "opus", "effort": "high",
            "expected_model": "claude-sonnet", "expected_effort": ""})
        self.assertFalse(rapid_settings["ok"])
        self.assertEqual(len(writes), 1)

        reg["status"] = "busy"
        active = self.engine.act({"type": "text", "session_id": "same",
                                  "text": "still active"})
        self.assertFalse(active["ok"])
        reg["status"] = "idle"
        after_idle = self.engine.act({"type": "session_settings",
            "session_id": "same", "model": "opus", "effort": "high",
            "expected_model": "claude-sonnet", "expected_effort": ""})
        self.assertTrue(after_idle["ok"], after_idle)
        self.assertEqual(len(writes), 3)

    def test_claude_close_interrupts_then_terminates_only_registered_process(self):
        pid = 424242
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "busy", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        process = SimpleNamespace(stdout="/usr/local/bin/claude --model sonnet")
        with mock.patch.object(subprocess, "run", return_value=process), \
             mock.patch.object(os, "kill") as kill, \
             mock.patch.object(time, "sleep"):
            result = self.engine.act({"type": "close", "session_id": "same"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["interrupted"])
        self.assertEqual(writes, [("/dev/ttys-test", [("\x1b", False)], 0.05)])
        kill.assert_called_once_with(pid, signal.SIGTERM)

    def test_claude_close_interrupts_an_active_shell_before_termination(self):
        pid = 424244
        reg = {"sessionId": "same", "pid": pid, "cwd": self.cwd,
               "status": "shell", "name": "Claude"}
        self.engine.live_sessions = lambda: [reg]
        self.engine._tty_cache[pid] = "ttys-test"
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        process = SimpleNamespace(stdout="/usr/local/bin/claude --model sonnet")
        with mock.patch.object(subprocess, "run", return_value=process), \
             mock.patch.object(os, "kill") as kill, \
             mock.patch.object(time, "sleep"):
            result = self.engine.act({"type": "close", "session_id": "same"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["interrupted"])
        self.assertEqual(writes, [("/dev/ttys-test", [("\x1b", False)], 0.05)])
        kill.assert_called_once_with(pid, signal.SIGTERM)

    def test_claude_close_refuses_reused_non_claude_pid(self):
        pid = 424243
        self.engine.live_sessions = lambda: [{"sessionId": "same", "pid": pid,
            "cwd": self.cwd, "status": "idle", "name": "Claude"}]
        process = SimpleNamespace(stdout="/usr/bin/python /work/.claude/fleet-dash/server.py")
        with mock.patch.object(subprocess, "run", return_value=process), \
             mock.patch.object(os, "kill") as kill:
            result = self.engine.act({"type": "close", "session_id": "same"})
        self.assertFalse(result["ok"])
        self.assertIn("non-Claude", result["error"])
        kill.assert_not_called()

    def test_claude_worktree_spawn_is_allowlisted_and_mute_persists(self):
        os.makedirs(os.path.join(self.cwd, ".git"))
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps)) or {"ok": True})
        self.engine.is_trusted = lambda cwd, trusted=None: True
        with mock.patch.object(engine_paths, "HOME", self.tmp.name):
            spawned = self.engine.spawn_session({"cwd": self.cwd, "model": "sonnet",
                "effort": "high", "permission_mode": "acceptEdits",
                "worktree": True, "worktree_name": "live-e2e"})
        self.assertTrue(spawned["ok"])
        command = writes[-1][1][0][0]
        self.assertRegex(command, r"claude --session-id [0-9a-f-]{36} --model sonnet ")
        self.assertIn("--effort high --permission-mode acceptEdits --worktree live-e2e", command)
        self.assertEqual(spawned["session_id"], command.split("--session-id ", 1)[1].split()[0])
        self.assertFalse(spawned["trust_prompt"])
        rejected = self.engine.spawn_session({"cwd": self.cwd,
            "permission_mode": "bypassPermissions"})
        self.assertFalse(rejected["ok"])
        self.assertIn("permission mode", rejected["error"])

        changed = self.engine.update_settings({"mute_session": "same", "muted": True})
        self.assertTrue(changed["ok"])
        self.assertIn("same", self.engine.cfg["muted_sessions"])
        fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"] if item["provider"] == "claude")
        self.assertTrue(claude["muted"])

    def test_reader_width_is_persisted_validated_and_exposed(self):
        self.assertEqual(DEFAULT_CONFIG["reader_width"], "fit")
        changed = self.engine.update_settings({"reader_width": "centered"})
        self.assertEqual(changed, {"ok": True, "reader_width": "centered"})
        self.assertEqual(self.engine.scan()["settings"]["reader_width"], "centered")
        with open(os.path.join(self.base, "config.json")) as handle:
            self.assertEqual(json.load(handle)["reader_width"], "centered")
        invalid = self.engine.update_settings({"reader_width": "left"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(self.engine.cfg["reader_width"], "centered")

    def test_settings_validation_is_atomic_strict_and_concurrency_safe(self):
        before_notify = copy.deepcopy(self.engine.cfg.get("notify"))
        rejected = self.engine.update_settings({
            "notify": {"needs_you": False}, "reader_width": "left"})
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.engine.cfg.get("notify"), before_notify)
        config_path = os.path.join(self.base, "config.json")
        if os.path.exists(config_path):
            with open(config_path) as handle:
                self.assertNotEqual((json.load(handle).get("notify") or {}).get("needs_you"), False)

        for patch in ({"preview_agents": "false"},
                      {"notify": {"needs_you": False}},
                      {"legacy_ntfy_enabled": "true"},
                      {"mute_session": "same", "muted": 1},
                      {"pin_session": "same", "pinned": "yes"},
                      {"unknown_setting": True},
                      {"fleet_quiet_minutes": True}):
            with self.subTest(patch=patch):
                self.assertFalse(self.engine.update_settings(patch)["ok"])

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(
                self.engine.update_settings({"preview_agents": True}))),
            threading.Thread(target=lambda: results.append(
                self.engine.update_settings({"reader_width": "centered"}))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(item["ok"] for item in results))
        with open(config_path) as handle:
            saved = json.load(handle)
        self.assertTrue(saved["preview_agents"])
        self.assertEqual(saved["reader_width"], "centered")

    def test_handoff_preview_redacts_credentials_and_exposes_safe_defaults(self):
        fleet = self.engine.scan()
        claude = next(item for item in fleet["sessions"] if item["provider"] == "claude")
        claude["last_msg"] = {"role": "assistant", "text":
            "Next step: verify Authorization: Bearer abcdefghijklmnop"}
        preview = self.engine.handoff_preview("same", "codex")
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["independent_session"])
        self.assertEqual(preview["defaults"]["cwd"], self.cwd)
        self.assertEqual(preview["defaults"]["mode"], "plan")
        self.assertIn("Source session: claude · same", preview["preview"])
        self.assertIn("Treat this as an independent session", preview["preview"])
        self.assertNotIn("abcdefghijklmnop", redact_handoff_text(
            "Authorization: Bearer abcdefghijklmnop"))
        self.assertIn("[REDACTED", redact_handoff_text(
            "api_key=abcdefghijklmnop"))
        self.assertNotIn("private-material", redact_handoff_text(
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"))

    def test_codex_handoff_starts_exact_thread_with_hi_and_durable_link(self):
        self.engine.scan()
        result = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "cwd": self.cwd, "preview": "Continue exact work",
            "model": "gpt-5.4", "effort": "high", "mode": "default"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["destination_session_id"], "codex:new-thread")
        self.assertEqual(self.codex.started_thread["initial_text"],
                         "hi\n\nContinue exact work")
        link = self.engine._handoff_link("same", "codex:new-thread")
        self.assertEqual(link["status"], "delivered")
        self.assertNotIn("Continue exact work", json.dumps(link))
        fleet = self.engine.scan()
        source = next(item for item in fleet["sessions"] if item["session_id"] == "same")
        self.assertEqual(source["handoff_links"][0]["session_id"], "codex:new-thread")

    def test_claude_handoff_uses_reserved_uuid_and_delivers_only_to_exact_session(self):
        self.engine.scan()
        writes = []
        self.engine._iterm_write = lambda tty, steps, step_delay=None: (
            writes.append((tty, steps, step_delay)) or {"ok": True})
        self.engine.is_trusted = lambda cwd, trusted=None: True
        original_live = self.engine.live_sessions
        spawned_ids = []

        def live():
            if not spawned_ids:
                return original_live()
            return [{"sessionId": spawned_ids[-1], "pid": 9090, "cwd": self.cwd,
                     "status": "idle", "name": "Handoff"},
                    {"sessionId": "similar-but-wrong", "pid": 9191, "cwd": self.cwd,
                     "status": "idle", "name": "Wrong"}]

        def write(tty, steps, step_delay=None):
            writes.append((tty, steps, step_delay))
            if tty == "SPAWN":
                command = steps[0][0]
                spawned_ids.append(command.split("--session-id ", 1)[1].split()[0])
            return {"ok": True}

        self.engine._iterm_write = write
        self.engine.live_sessions = live
        self.engine._tty_cache[9090] = "ttys-exact"
        result = self.engine.execute_handoff({"type": "handoff", "session_id": "codex:same",
            "provider": "claude", "cwd": self.cwd, "preview": "Exact destination"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["destination_session_id"], spawned_ids[0])
        self.assertEqual(writes[-1][0], "/dev/ttys-exact")
        self.assertEqual(writes[-1][1], [("Exact destination", True)])
        self.assertNotEqual(result["destination_session_id"], "similar-but-wrong")

    def test_new_and_existing_claude_handoffs_preserve_unknown_delivery_without_retry(self):
        self.engine.scan()
        self.engine.is_trusted = lambda cwd, trusted=None: True
        original_live = self.engine.live_sessions
        spawned_ids = []

        def live():
            if not spawned_ids:
                return original_live()
            return [{"sessionId": spawned_ids[-1], "pid": 9090, "cwd": self.cwd,
                     "status": "idle", "name": "Handoff"}]

        def uncertain_new(tty, steps, step_delay=None):
            if tty == "SPAWN":
                spawned_ids.append(steps[0][0].split("--session-id ", 1)[1].split()[0])
                return {"ok": True}
            return {"ok": False, "code": "delivery_uncertain",
                    "error": "delivery result was lost"}

        self.engine.live_sessions = live
        self.engine._tty_cache[9090] = "ttys-exact"
        self.engine._iterm_write = uncertain_new
        created = self.engine.execute_handoff({"type": "handoff",
            "session_id": "codex:same", "provider": "claude", "cwd": self.cwd,
            "preview": "Exact but unconfirmed"})
        self.assertEqual(created.get("code"), "delivery_uncertain")
        self.assertFalse(created["retryable"])
        link = self.engine._handoff_link("codex:same", created["destination_session_id"])
        self.assertEqual(link["status"], "confirmation_unknown")

        self.engine.live_sessions = original_live
        self.engine._tty_cache[os.getpid()] = "ttys-source"
        self.engine._record_handoff_link("codex:same", "codex", "same", "claude",
                                         "delivery_failed", "old", "old failure")
        self.engine._iterm_write = lambda tty, steps, step_delay=None: {
            "ok": False, "code": "delivery_uncertain", "error": "lost result"}
        existing = self.engine.execute_handoff({"type": "handoff",
            "session_id": "codex:same", "provider": "claude", "preview": "retry",
            "destination_session_id": "same"})
        self.assertEqual(existing.get("code"), "delivery_uncertain")
        self.assertFalse(existing["retryable"])
        self.assertEqual(self.engine._handoff_link("codex:same", "same")["status"],
                         "confirmation_unknown")

    def test_same_provider_handoff_remains_an_independent_codex_thread(self):
        self.engine.scan()
        result = self.engine.execute_handoff({"type": "handoff",
            "session_id": "codex:same", "provider": "codex", "cwd": self.cwd,
            "preview": "Continue independently", "mode": "plan"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["destination_session_id"], "codex:new-thread")
        self.assertNotEqual(result["destination_session_id"], "codex:same")
        self.assertEqual(self.codex.started_thread["initial_text"],
                         "hi\n\nContinue independently")

    def test_claude_handoff_timeout_is_linked_and_retryable(self):
        self.engine.scan()
        self.engine.is_trusted = lambda cwd, trusted=None: True
        self.engine._iterm_write = lambda tty, steps, step_delay=None: {"ok": True}
        self.engine.live_sessions = lambda: []
        with mock.patch.object(time, "monotonic", side_effect=[0, 31]):
            result = self.engine.execute_handoff({"type": "handoff",
                "session_id": "same", "provider": "claude", "cwd": self.cwd,
                "preview": "Wait for exact identity"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["created"])
        self.assertTrue(result["retryable"])
        link = self.engine._handoff_link("same", result["destination_session_id"])
        self.assertEqual(link["status"], "delivery_failed")

    def test_failed_codex_handoff_cleans_up_only_the_created_worktree(self):
        self.engine.scan()
        created = {"ok": True, "created": True, "cwd": os.path.join(self.tmp.name, "wt"),
                   "root": self.cwd, "branch": "fleet/fail", "worktree_name": "fail"}
        self.engine._create_codex_worktree = lambda cwd, name="": created
        cleaned = []
        self.engine._remove_failed_codex_worktree = lambda value: cleaned.append(value) or None
        self.codex.start_thread = mock.Mock(side_effect=RuntimeError("app-server stopped"))
        result = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "cwd": self.cwd, "preview": "Continue",
            "worktree": True, "worktree_name": "fail", "mode": "plan"})
        self.assertFalse(result["ok"])
        self.assertEqual(cleaned, [created])

    def test_handoff_retry_requires_recorded_exact_destination(self):
        self.engine.scan()
        stale = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "preview": "retry", "destination_session_id": "codex:nope"})
        self.assertFalse(stale["ok"])
        self.assertIn("stale", stale["error"])
        self.engine._record_handoff_link("same", "claude", "codex:retry", "codex",
                                         "delivery_failed", "hash", "old failure")
        retried = self.engine.execute_handoff({"type": "handoff", "session_id": "same",
            "provider": "codex", "preview": "retry exact",
            "destination_session_id": "codex:retry"})
        self.assertTrue(retried["ok"])
        self.assertFalse(retried["created"])
        self.assertEqual(self.codex.actions[-1]["session_id"], "codex:retry")

    def test_codex_worktree_creation_uses_argv_and_validates_name(self):
        os.makedirs(os.path.join(self.cwd, ".git"), exist_ok=True)
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            made = self.engine._create_codex_worktree(self.cwd, "handoff-ui")
        self.assertTrue(made["ok"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ["git", "-C", os.path.realpath(self.cwd),
                                    "worktree", "add"])
        self.assertEqual(argv[-2:], [made["cwd"], "HEAD"])
        invalid = self.engine._create_codex_worktree(self.cwd, "bad name")
        self.assertFalse(invalid["ok"])

    def test_plain_prose_reply_detection_ignores_examples_and_finds_requests(self):
        self.assertTrue(requests_reply("Which option should I implement?"))
        self.assertTrue(requests_reply(
            "### Scope\n\nAnswer both before I continue.\n\nSome background follows."))
        self.assertFalse(requests_reply(
            "The parser handles `value?` and this quoted example: \"Continue?\""))
        self.assertFalse(requests_reply(
            "> Should this quoted requirement count?\n\nImplementation is complete."))
        self.assertFalse(requests_reply(
            "Why did the cache miss? The path changed, so I rebuilt the index."))
        self.assertFalse(requests_reply(
            "I will check whether the provider recovered, then rerun the test."))

    def test_comprehension_tags_and_greetings_are_not_attention_work(self):
        for tag in ("The migration is applied. Does that make sense?",
                    "I rewrote the loader. How does that look?",
                    "Reindexed both providers. What do you think?",
                    "Bumped the cache name. Sound good?",
                    "That is the last one. Right?",
                    "The branch is clean. What's next?",
                    "Both tests pass. Anything else?",
                    "The daemon is restarted. What would you like me to do?"):
            self.assertFalse(requests_reply(tag), tag)

    def test_choices_and_permission_asks_remain_attention_work(self):
        for ask in ("The tests pass. Want me to open the PR?",
                    "I staged the rename. Should I proceed?",
                    "Two paths remain. Do you want option A or option B?",
                    "Both loaders changed. Which file should I edit first?",
                    "The worktree is dirty. How do you want to proceed?"):
            self.assertTrue(requests_reply(ask), ask)

    def test_pure_classifier_explains_priority_without_mutating_input(self):
        session = codex_session()
        session.update(state="running", quiet_s=12, agents_running=1,
                       pending={"kind": "question", "nonce": "q-1"},
                       _latest_prose={"role": "assistant", "text": "Which path?"})
        before = json.dumps(session, sort_keys=True)
        result = classify_placement(session, 100)
        self.assertEqual(json.dumps(session, sort_keys=True), before)
        self.assertEqual((result["ui_group"], result["winning_rule"],
                          result["state_confidence"]),
                         ("needs_you", "placement.pending.question", "confirmed"))
        self.assertIn("placement.state.running", result["suppressed_rules"])
        self.assertEqual([fact["kind"] for fact in result["state_evidence"]][:3],
                         ["provider_signal", "pending_request", "transcript_event"])

    def test_interrupted_turn_never_creates_reply_or_unreviewed_work(self):
        session = codex_session()
        session.update(state="turn_done", interrupted=True,
                       _latest_prose={"role": "assistant",
                                      "text": "Which layout should I use?"})
        result = self.engine.organize_session(session, 100)
        self.assertEqual((result["ui_group"], result["reason_label"]),
                         ("available", "Available"))
        self.assertFalse(result["reply_requested"])
        self.assertFalse(result["new_response"])

    def test_external_completion_is_available_before_it_ages_into_history(self):
        session = codex_session()
        session.update(state="turn_done", headless=True, read_only=True,
                       read_only_reason="Desktop-owned thread", quiet_s=15,
                       _latest_prose={"role": "assistant", "text":
                           "Finished.\n\n- Updated the deployment files\n- Verified the build"})
        current = self.engine.organize_session(session, 100)
        self.assertEqual((current["ui_group"], current["reason_label"],
                          current["access"], current["primary_action"]),
                         ("available", "Available", "view_only", "view"))
        self.assertTrue(current["new_response"])

        older = codex_session()
        older.update(state="idle", headless=True, read_only=True,
                     quiet_s=self.engine.cfg["dormant_seconds"] + 1,
                     _latest_prose={"role": "assistant", "text": "Finished."})
        historical = self.engine.organize_session(older, 200)
        self.assertEqual((historical["ui_group"], historical["reason_label"]),
                         ("history", "External"))

    def test_stale_snapshot_keeps_last_placement_with_explicit_stale_evidence(self):
        session = codex_session()
        session.update(state="stale", stale=True, stale_previous_state="running",
                       stale_reason="App Server stopped", quiet_s=8)
        organized = self.engine.organize_session(session, 100)
        self.assertEqual((organized["state"], organized["normalized_state"],
                          organized["ui_group"], organized["state_confidence"],
                          organized["access"]),
                         ("stale", "running", "working", "stale", "interactive"))
        self.assertIn("stale", [fact["kind"] for fact in organized["state_evidence"]])

    def test_state_journal_deduplicates_recovers_and_pages(self):
        session = codex_session()
        session.update(_latest_prose={"role": "assistant", "text": "Done."})
        available = self.engine.organize_session(session, 100)
        self.engine.record_state_events([available], 100)
        self.engine.record_state_events([available], 102)
        session = codex_session()
        session.update(state="running", reg_status="running", quiet_s=1,
                       _latest_prose={"role": "user", "text": "Continue"})
        running = self.engine.organize_session(session, 110)
        self.engine.record_state_events([running], 110)
        session = codex_session()
        session.update(state="stale", stale=True, stale_previous_state="running",
                       stale_reason="App Server stopped", quiet_s=3)
        stale = self.engine.organize_session(session, 115)
        self.engine.record_state_events([stale], 115)
        session = codex_session()
        session.update(state="running", reg_status="running", quiet_s=1)
        recovered = self.engine.organize_session(session, 120)
        self.engine.record_state_events([recovered], 120)

        count = self.engine.ensure_db().execute(
            "SELECT count(*) FROM state_events WHERE session_id='codex:same'").fetchone()[0]
        self.assertEqual(count, 4)
        first = self.engine.state_history("codex:same", limit=2)
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["events"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(first["events"][0]["winning_rule"], "placement.state.running")
        self.assertEqual(first["events"][1]["confidence"], "stale")
        second = self.engine.state_history(
            "codex:same", cursor=first["next_cursor"], limit=2)
        self.assertEqual(len(second["events"]), 2)
        self.assertIsNone(second["next_cursor"])
        self.assertFalse(self.engine.state_history("bad\nvalue")["ok"])
        self.assertFalse(self.engine.state_history("codex:same", limit=101)["ok"])

    def test_transient_claude_waiting_between_tools_remains_working(self):
        """A progress note followed by another tool is not a request for input."""
        with open(self.transcript, "a") as handle:
            handle.write(json.dumps({
                "type": "user", "timestamp": "2026-07-16T04:54:21Z",
                "message": {"role": "user", "content": "Add the test."},
            }) + "\n")
            handle.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-16T04:54:43Z",
                "message": {"role": "assistant", "model": "claude-sonnet",
                    "stop_reason": "tool_use", "content": [{"type": "text", "text":
                        "Good call — the current fix lives inline in "
                        "`classify_one_worktree`. Let me extract it into a pure "
                        "classifier, then add the test."}]},
            }) + "\n")
        registry = os.path.join(self.sessions, "same.json")
        with open(registry, "w") as handle:
            json.dump({"sessionId": "same", "pid": os.getpid(), "cwd": self.cwd,
                       "status": "waiting", "name": "Claude"}, handle)
        self.engine.codex = FakeCodex()
        now = os.path.getmtime(self.transcript) + 1
        with mock.patch.object(time, "time", return_value=now):
            session = self.engine.scan()["sessions"][0]
        self.assertEqual((session["state"], session["ui_group"],
                          session["reason_label"]),
                         ("running", "working", "Working"))
        self.assertFalse(session["pending"])

    def test_uncorroborated_claude_waiting_must_persist_but_hook_is_immediate(self):
        now = 100.0
        self.assertFalse(self.engine.waiting_confirmed("plain", "waiting", now))
        self.assertFalse(self.engine.waiting_confirmed(
            "plain", "waiting", now + WAITING_CONFIRM_SECONDS - 0.01))
        self.assertTrue(self.engine.waiting_confirmed(
            "plain", "waiting", now + WAITING_CONFIRM_SECONDS))
        self.assertTrue(self.engine.waiting_confirmed(
            "hooked", "waiting", now,
            pending={"kind": "question", "nonce": "q1"}))
        self.assertFalse(self.engine.waiting_confirmed("plain", "busy", now + 10))

    def test_session_organization_maps_every_user_facing_group(self):
        now = 10_000

        def organized(**updates):
            session = codex_session()
            session.update(convo_v="revision:1", quiet_s=20,
                           _latest_prose={"role": "assistant", "text": "Finished."})
            session.update(updates)
            return self.engine.organize_session(session, now)

        question = organized(state="needs_you", pending={"kind": "question"})
        self.assertEqual((question["ui_group"], question["reason_label"],
                          question["primary_action"]),
                         ("needs_you", "Question waiting", "respond"))
        command = organized(state="needs_you", pending={"kind": "permission",
                            "approval_kind": "command"})
        self.assertEqual((command["reason_label"], command["primary_action"]),
                         ("Command approval", "review"))
        file_change = organized(state="needs_you", pending={"kind": "permission",
                                "approval_kind": "file_change"})
        self.assertEqual(file_change["reason_label"], "File approval")
        form = organized(state="needs_you", pending={"kind": "elicitation"})
        self.assertEqual(form["reason_label"], "Form waiting")
        provider_error = organized(state="error", error="protocol failed")
        self.assertEqual((provider_error["ui_group"], provider_error["reason_label"],
                          provider_error["winning_rule"]),
                         ("needs_you", "Fix needed", "placement.provider.error"))
        provider_limit = organized(state="blocked", error="Usage limit reached")
        self.assertEqual((provider_limit["ui_group"], provider_limit["reason_label"],
                          provider_limit["winning_rule"]),
                         ("needs_you", "Limit reached", "placement.provider.limit"))

        reply = organized(state="turn_done", _latest_prose={"role": "assistant",
                           "text": "Which layout should I use?"})
        self.assertEqual((reply["ui_group"], reply["reason_label"]),
                         ("needs_you", "Reply requested"))
        external_reply_fresh = organized(
            state="idle", headless=True, read_only=True, reg_status="notLoaded",
            quiet_s=1799, _latest_prose={"role": "assistant",
                                          "text": "Which layout should I use?"})
        self.assertEqual((external_reply_fresh["ui_group"],
                          external_reply_fresh["reply_requested"]), ("needs_you", True))
        interactive_reply_old = organized(
            state="idle", quiet_s=1800, _latest_prose={"role": "assistant",
                                                         "text": "Which layout should I use?"})
        self.assertEqual((interactive_reply_old["ui_group"],
                          interactive_reply_old["reply_requested"]), ("needs_you", True))
        loaded_external_reply_old = organized(
            state="idle", headless=True, read_only=True, reg_status="loaded",
            quiet_s=1800, _latest_prose={"role": "assistant",
                                          "text": "Which layout should I use?"})
        self.assertEqual((loaded_external_reply_old["ui_group"],
                          loaded_external_reply_old["reply_requested"]), ("needs_you", True))
        external_reply_expired = organized(
            state="idle", headless=True, read_only=True, reg_status="notLoaded",
            quiet_s=1800, _latest_prose={"role": "assistant",
                                          "text": "Which layout should I use?"})
        self.assertEqual((external_reply_expired["ui_group"],
                          external_reply_expired["reason_label"],
                          external_reply_expired["reply_requested"]),
                         ("history", "External", False))
        self.assertIn("reply_request_expired", {item["kind"] for item in
                                                  external_reply_expired["state_evidence"]})
        running = organized(state="running")
        self.assertEqual((running["ui_group"], running["reason_label"]),
                         ("working", "Working"))
        external = organized(state="running", headless=True, read_only=True)
        self.assertEqual((external["ui_group"], external["reason_label"],
                          external["primary_action"], external["access"]),
                         ("working", "Working", "view", "view_only"))
        slow = organized(state="stalled")
        self.assertEqual((slow["ui_group"], slow["reason_label"]),
                         ("working", "Slow"))
        compacting = organized(state="running", compacting=4)
        self.assertEqual((compacting["ui_group"], compacting["reason_label"]),
                         ("working", "Compacting"))
        available = organized(state="idle")
        self.assertEqual((available["ui_group"], available["reason_label"]),
                         ("available", "Available"))
        inactive = organized(state="dormant")
        self.assertEqual((inactive["ui_group"], inactive["reason_label"],
                          inactive["primary_action"]),
                         ("history", "Inactive", "continue"))
        external_idle = organized(state="idle", headless=True, read_only=True,
                                  quiet_s=self.engine.cfg["dormant_seconds"])
        self.assertEqual((external_idle["ui_group"], external_idle["reason_label"],
                          external_idle["access"], external_idle["primary_action"]),
                         ("available", "Available", "view_only", "view"))
        historical = organized(state="idle", headless=True, read_only=True,
                               quiet_s=self.engine.cfg["dormant_seconds"] + 1)
        self.assertEqual((historical["ui_group"], historical["reason_label"]),
                         ("history", "External"))
        reopenable = organized(state="reopenable", capabilities={"reopen": True})
        self.assertEqual((reopenable["ui_group"], reopenable["primary_action"],
                          reopenable["access"]),
                         ("history", "reopen", "reopen"))
        unknown = organized(state="future_protocol_state")
        self.assertEqual((unknown["ui_group"], unknown["state_confidence"]),
                         ("available", "unknown"))

        closed = self.engine.organize_closed({
            "session_id": "codex:closed", "provider": "codex", "closed_at": 9,
            "last_seen": 8, "can_reopen": False})
        self.assertEqual((closed["state"], closed["ui_group"], closed["winning_rule"],
                          closed["state_confidence"]),
                         ("closed", "history", "placement.ledger.closed", "confirmed"))

    def test_pin_reply_dismissal_and_read_markers_persist(self):
        pinned = self.engine.update_settings({"pin_session": "codex:same",
                                              "pinned": True})
        self.assertEqual(pinned["pinned_sessions"], ["codex:same"])
        pinned = self.engine.update_settings({"pin_session": "claude:second",
                                              "pinned": True})
        self.assertEqual(pinned["pinned_sessions"],
                         ["codex:same", "claude:second"])
        pinned = self.engine.update_settings({"pin_session": "codex:same",
                                              "pinned": False})
        self.assertEqual(pinned["pinned_sessions"], ["claude:second"])
        pinned = self.engine.update_settings({"pin_session": "codex:same",
                                              "pinned": True})
        self.assertEqual(pinned["pinned_sessions"],
                         ["claude:second", "codex:same"])
        dismissed = self.engine.update_settings({
            "mark_available_session": "codex:same", "revision": "reply:2"})
        self.assertEqual(dismissed["reply_available"]["codex:same"], "reply:2")
        read = self.engine.update_settings({
            "mark_read_session": "codex:same", "revision": "response:3"})
        self.assertEqual(read["read_sessions"]["codex:same"], "response:3")

        with open(os.path.join(self.base, "config.json")) as handle:
            saved = json.load(handle)
        self.assertEqual(saved["pinned_sessions"], ["claude:second", "codex:same"])
        self.assertEqual(saved["reply_available"]["codex:same"], "reply:2")
        self.assertEqual(saved["read_sessions"]["codex:same"], "response:3")

        session = codex_session()
        session.update(state="turn_done", convo_v="reply:2",
                       _latest_prose={"role": "assistant",
                                      "text": "Should I continue?"})
        organized = self.engine.organize_session(session, time.time())
        self.assertEqual(organized["ui_group"], "available")
        self.assertTrue(organized["pinned"])

    def test_budget_settings_persist_without_copying_budgets_to_config(self):
        saved = self.engine.update_settings({
            "legacy_ntfy_enabled": True,
            "budgets": [{"id": "fleet-token", "scope_type": "fleet",
                         "metric": "tokens", "limit_value": 50000,
                         "block_spawns": False}],
        })
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["budgets"][0]["id"], "fleet-token")
        with open(os.path.join(self.base, "config.json")) as handle:
            config = json.load(handle)
        self.assertNotIn("budgets", config)
        self.engine.scan()
        budget = self.engine.budgets_snapshot()["budgets"][0]
        self.assertEqual((budget["metric"], budget["measurement_scope"]),
                         ("tokens", "partial"))

        for retired in ({"digest_schedule_zone": "America/New_York"},
                        {"dashboard_url": "https://fleet.test/?token=private"},
                        {"spend_threshold_usd": 10}):
            self.assertFalse(self.engine.update_settings(retired)["ok"])
        self.assertFalse(self.engine.update_settings(
            {"mute_session": "", "muted": True})["ok"])

    def test_engine_start_scrubs_known_secrets_from_runtime_log(self):
        log_path = os.path.join(self.base, "fleet-dash.log")
        secret_path = os.path.join(self.base, "push-secrets.json")
        values = {
            "vapid_private_key": "private-vapid-material-1234567890",
            "action_secret": "private-action-material-1234567890",
        }
        with open(secret_path, "w") as handle:
            json.dump(values, handle)
        os.chmod(secret_path, 0o600)
        config = dict(self.engine.cfg)
        config.update({"act_token": "private-act-token-1234",
                       "dashboard_url": "https://fleet.example/private-token"})
        with open(log_path, "wb") as handle:
            handle.write(("before private-act-token-1234 "
                          "https://fleet.example/private-token "
                          "private-vapid-material-1234567890 "
                          "private-action-material-1234567890 after\n").encode())
        replacement = Engine(config)
        try:
            with open(log_path, "rb") as handle:
                scrubbed = handle.read()
            self.assertIn(b"before", scrubbed)
            self.assertIn(b"after", scrubbed)
            for secret in (config["act_token"], config["dashboard_url"], *values.values()):
                self.assertNotIn(secret.encode(), scrubbed)
            self.assertEqual(stat.S_IMODE(os.lstat(log_path).st_mode), 0o600)
        finally:
            if replacement.db:
                replacement.db.close()

    def test_explicit_hard_budget_blocks_new_spawns_but_not_existing_work(self):
        self.engine.update_settings({"budgets": [{
            "id": "hard-fleet", "scope_type": "fleet", "metric": "tokens",
            "limit_value": 1, "block_spawns": True,
        }]})
        snapshot = self.engine.scan()
        self.assertGreater(snapshot["sessions"][0].get("total_tokens") or 0, 1)
        budget_action = next(item for item in snapshot["actions"]
                             if item.get("kind") == "budget")
        self.assertIsNone(budget_action["session_id"])
        self.assertEqual(budget_action["primary_action"], "view_budget")
        self.assertEqual(budget_action["safe_bulk"], [])
        self.assertEqual(budget_action["delivery_state"], "Future spawns blocked")
        result = self.engine.spawn_codex_session({"cwd": self.cwd, "model": "gpt-5.4",
                                                  "effort": "high", "mode": "plan"})
        self.assertFalse(result["ok"])
        self.assertIn("blocked by an exceeded budget", result["error"])
        self.assertEqual(result["budget_blockers"][0]["id"], "hard-fleet")
        self.assertTrue(any(item.get("ui_group") == "available"
                            for item in self.engine.snapshot_cache.get("sessions", []) or
                            snapshot["sessions"]))

    def test_explicit_spawn_limit_fails_closed_when_budget_check_breaks(self):
        self.engine.operations.has_spawn_limits = lambda: True
        self.engine.operations.spawn_blockers = mock.Mock(
            side_effect=RuntimeError("ledger unavailable"))
        blockers = self.engine._spawn_budget_blockers("codex", self.cwd)
        self.assertEqual(blockers[0]["id"], "budget-check-unavailable")
        self.assertEqual(blockers[0]["measurement_scope"], "unavailable")

    def test_action_records_are_stable_deduplicated_and_bulk_triage_is_safe(self):
        session = codex_session()
        session.update(state="needs_you", convo_v="revision:7", quiet_s=5,
                       pending={"kind": "question", "nonce": "question:7",
                                "questions": [{"header": "Scope",
                                               "question": "Which scope?"}]},
                       last_msg={"role": "assistant", "text": "Choose one."})
        organized = self.engine.organize_session(session, 100)
        first = self.engine.action_records([organized, dict(organized)])
        second = self.engine.action_records([organized])
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["action_id"], second[0]["action_id"])
        self.assertEqual((first[0]["kind"], first[0]["request"],
                          first[0]["delivery_state"]),
                         ("question", "Which scope?", "Awaiting response"))
        self.assertNotIn("approve", first[0]["safe_bulk"])
        self.assertNotIn("dismiss", first[0]["safe_bulk"])

        rejected = self.engine.update_settings({"bulk_triage": {
            "operation": "approve", "items": [{"session_id": "codex:same",
                                                   "action_id": first[0]["action_id"]}]}})
        self.assertFalse(rejected["ok"])
        self.engine.snapshot_cache = {"actions": first}
        dismissed = self.engine.update_settings({"bulk_triage": {
            "operation": "dismiss", "items": [{"session_id": "codex:same",
                                                  "action_id": first[0]["action_id"]}]}})
        self.assertFalse(dismissed["ok"])
        self.assertEqual(len(self.engine.action_records([organized])), 1)

    def test_completed_handoffs_are_unreviewed_actions_and_progress_is_not(self):
        reply = codex_session()
        reply.update(ui_group="needs_you", reason_label="Reply requested",
                     primary_action="respond", primary_action_label="Respond",
                     access="interactive", access_label="Interactive",
                     activity_at=90, reply_requested=True, new_response=False,
                     last_msg={"role": "assistant", "text": "Which layout?"})
        outcome = codex_session()
        outcome.update(session_id="codex:other", native_session_id="other",
                       ui_group="available", reason_label="Available",
                       primary_action="continue", primary_action_label="Continue",
                       access="interactive", access_label="Interactive",
                       activity_at=95, reply_requested=False, new_response=True,
                       last_msg={"role": "assistant", "text": "Done.\n\n- Updated the dashboard\n- Tests passed"})
        records = {item["kind"]: item for item in self.engine.action_records([reply, outcome])}
        self.assertIn("mark_available", records["reply"]["safe_bulk"])
        self.assertNotIn("mark_read", records["reply"]["safe_bulk"])
        self.assertEqual((records["outcome"]["request"], records["outcome"]["delivery_state"]),
                         ("Completed work is ready to review", "Unreviewed"))
        self.assertTrue(completed_handoff("Done.\n\n- Updated the dashboard\n- Tests passed"))
        self.assertFalse(completed_handoff("The likely fault is in the fallback calculation. I’m checking it now."))
        self.assertFalse(completed_handoff("Done."))

    def test_workstream_identity_rolls_linked_worktrees_into_main_repository(self):
        main = os.path.join(self.tmp.name, "main-repo")
        linked = os.path.join(self.tmp.name, "linked-worktree")
        gitdir = os.path.join(main, ".git")
        linked_gitdir = os.path.join(gitdir, "worktrees", "linked")
        os.makedirs(linked_gitdir)
        os.makedirs(linked)
        with open(os.path.join(linked, ".git"), "w") as handle:
            handle.write("gitdir: " + linked_gitdir + "\n")
        with open(os.path.join(linked_gitdir, "commondir"), "w") as handle:
            handle.write("../..\n")
        main_identity = self.engine.workstream_identity(main)
        linked_identity = self.engine.workstream_identity(linked)
        self.assertEqual(main_identity["kind"], "git")
        self.assertEqual(main_identity["root"], os.path.realpath(main))
        self.assertEqual(linked_identity["root"], main_identity["root"])
        self.assertEqual(linked_identity["workstream_id"], main_identity["workstream_id"])
        self.assertEqual(linked_identity["worktree"], os.path.realpath(linked))

    def test_secondary_worktree_close_preview_and_cleanup_are_revision_checked(self):
        main = os.path.join(self.tmp.name, "close-main")
        os.makedirs(main)

        def git(*args, cwd=main):
            return subprocess.run(["git", *args], cwd=cwd, check=True,
                                  capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.email", "fleet@example.test")
        git("config", "user.name", "Fleet Test")
        with open(os.path.join(main, ".gitignore"), "w") as handle:
            handle.write("build/\n")
        with open(os.path.join(main, "tracked.txt"), "w") as handle:
            handle.write("base\n")
        git("add", ".gitignore", "tracked.txt")
        git("commit", "-m", "base")

        dirty = os.path.join(self.tmp.name, "close-dirty")
        git("worktree", "add", "-b", "feature/dirty", dirty)
        with open(os.path.join(dirty, "tracked.txt"), "a") as handle:
            handle.write("unstaged\n")
        with open(os.path.join(dirty, "staged.txt"), "w") as handle:
            handle.write("staged\n")
        git("add", "staged.txt", cwd=dirty)
        with open(os.path.join(dirty, "untracked.txt"), "w") as handle:
            handle.write("untracked\n")
        os.makedirs(os.path.join(dirty, "build"))
        with open(os.path.join(dirty, "build", "cache.bin"), "w") as handle:
            handle.write("ignored\n")

        session = {"session_id": "same", "provider": "codex", "cwd": dirty}
        preview = self.engine.close_worktree_preview(session)
        self.assertTrue(preview["secondary_worktree"])
        self.assertTrue(preview["inspect_ok"])
        self.assertFalse(preview["remove_allowed"])
        self.assertTrue(preview["force_remove_allowed"])
        self.assertEqual(preview["dirty_counts"], {
            "staged": 1, "unstaged": 1, "untracked": 1, "conflicts": 0})
        self.assertEqual(preview["ignored_files"], [])
        self.assertEqual(preview["ignored_count"], 0)
        self.assertEqual({item["path"] for item in preview["dirty_files"]},
                         {"tracked.txt", "staged.txt", "untracked.txt"})

        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": [{"session_id": "codex:other",
                "provider": "codex", "title": "Other", "cwd": dirty}]}
        shared = self.engine.close_worktree_preview(session)
        self.assertFalse(shared["force_remove_allowed"])
        self.assertEqual(shared["shared_sessions"][0]["session_id"], "codex:other")
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": []}

        self.engine._mark_cleanup_ticket_closed(preview["cleanup_ticket"], "same")
        removed = self.engine.cleanup_closed_worktree({"session_id": "same",
            "cleanup_ticket": preview["cleanup_ticket"], "force": True})
        self.assertTrue(removed["ok"])
        self.assertTrue(removed["branch_preserved"])
        self.assertFalse(os.path.exists(dirty))
        self.assertEqual(git("show-ref", "--verify", "refs/heads/feature/dirty").returncode, 0)

        clean = os.path.join(self.tmp.name, "close-clean")
        git("worktree", "add", "-b", "feature/clean", clean)
        os.makedirs(os.path.join(clean, "build"))
        with open(os.path.join(clean, "build", "cache.bin"), "w") as handle:
            handle.write("ignored build output\n")
        clean_session = {"session_id": "same", "provider": "codex", "cwd": clean}
        clean_preview = self.engine.close_worktree_preview(clean_session)
        self.assertTrue(clean_preview["remove_allowed"])
        self.assertFalse(clean_preview["dirty"])
        self.assertEqual(clean_preview["dirty_files"], [])
        self.engine._mark_cleanup_ticket_closed(clean_preview["cleanup_ticket"], "same")
        clean_removed = self.engine.cleanup_closed_worktree({"session_id": "same",
            "cleanup_ticket": clean_preview["cleanup_ticket"], "force": False})
        self.assertTrue(clean_removed["ok"])
        self.assertFalse(os.path.exists(clean))
        self.assertEqual(git("show-ref", "--verify", "refs/heads/feature/clean").returncode, 0)

        locked = os.path.join(self.tmp.name, "close-claude-locked")
        git("worktree", "add", "-b", "feature/claude-locked", locked)
        lock_reason = "claude session close-claude-locked (pid 424242 start now)"
        git("worktree", "lock", "--reason", lock_reason, locked)
        locked_session = {"session_id": "same", "provider": "claude", "cwd": locked,
                          "pid": 424242}
        locked_preview = self.engine.close_worktree_preview(locked_session)
        self.assertTrue(locked_preview["inspect_ok"])
        self.assertTrue(locked_preview["owned_lock"])
        self.assertTrue(locked_preview["remove_allowed"])
        self.engine._mark_cleanup_ticket_closed(locked_preview["cleanup_ticket"], "same")
        real_run = subprocess.run
        def closed_process(argv, *args, **kwargs):
            if argv[:2] == ["ps", "-p"]:
                return SimpleNamespace(stdout="")
            return real_run(argv, *args, **kwargs)
        with mock.patch.object(subprocess, "run", side_effect=closed_process):
            locked_removed = self.engine.cleanup_closed_worktree({"session_id": "same",
                "cleanup_ticket": locked_preview["cleanup_ticket"], "force": False})
        self.assertTrue(locked_removed["ok"], locked_removed)
        self.assertFalse(os.path.exists(locked))
        self.assertEqual(git("show-ref", "--verify",
            "refs/heads/feature/claude-locked").returncode, 0)

        foreign_locked = os.path.join(self.tmp.name, "close-foreign-locked")
        git("worktree", "add", "-b", "feature/foreign-locked", foreign_locked)
        git("worktree", "lock", "--reason", "maintenance", foreign_locked)
        foreign_preview = self.engine.close_worktree_preview({
            "session_id": "same", "provider": "claude", "cwd": foreign_locked,
            "pid": 424242})
        self.assertFalse(foreign_preview["inspect_ok"])
        self.assertFalse(foreign_preview["remove_allowed"])

        stale = os.path.join(self.tmp.name, "close-stale")
        git("worktree", "add", "-b", "feature/stale", stale)
        stale_session = {"session_id": "same", "provider": "codex", "cwd": stale}
        stale_preview = self.engine.close_worktree_preview(stale_session)
        with open(os.path.join(stale, "after-preview.txt"), "w") as handle:
            handle.write("changed\n")
        self.engine._mark_cleanup_ticket_closed(stale_preview["cleanup_ticket"], "same")
        rejected = self.engine.cleanup_closed_worktree({"session_id": "same",
            "cleanup_ticket": stale_preview["cleanup_ticket"], "force": False})
        self.assertFalse(rejected["ok"])
        self.assertIn("changed after preview", rejected["error"])
        self.assertTrue(os.path.isdir(stale))

        primary = self.engine.close_worktree_preview(
            {"session_id": "same", "provider": "codex", "cwd": main})
        self.assertFalse(primary["secondary_worktree"])
        self.assertFalse(primary["remove_allowed"])

    def test_workstreams_keep_missing_and_unrelated_folders_separate(self):
        one = codex_session()
        two = codex_session()
        one.update(session_id="codex:one", native_session_id="one",
                   cwd=os.path.join(self.tmp.name, "missing-one"), ui_group="available",
                   reason_label="Available", activity_at=2)
        two.update(session_id="codex:two", native_session_id="two",
                   cwd=os.path.join(self.tmp.name, "missing-two"), ui_group="history",
                   reason_label="External", activity_at=1)
        records = self.engine.workstream_records([one], [two])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item["missing"] for item in records))
        self.assertNotEqual(records[0]["workstream_id"], records[1]["workstream_id"])
        self.assertEqual(sum(item["counts"]["available"] for item in records), 1)
        self.assertEqual(sum(item["counts"]["history"] for item in records), 1)

    def test_workstreams_cache_ignores_poll_timestamps_but_invalidates_on_state(self):
        session = codex_session()
        session.update(cwd=self.cwd, ui_group="available", reason_label="Available",
                       activity_at=2)
        with self.engine.lock:
            self.engine.snapshot_cache = {"t": 1, "sessions": [session], "closed": []}
        with mock.patch.object(self.engine.operations, "budgets_snapshot",
                               return_value={"budgets": []}), mock.patch.object(
                                   self.engine, "workstream_records",
                                   wraps=self.engine.workstream_records) as records:
            first = self.engine.workstreams_snapshot()
            with self.engine.lock:
                self.engine.snapshot_cache = {"t": 2, "sessions": [dict(session)],
                                              "closed": []}
            second = self.engine.workstreams_snapshot()
            self.assertIs(first, second)
            self.assertEqual(records.call_count, 1)

            changed = dict(session, ui_group="working", state="running",
                           reason_label="Working")
            with self.engine.lock:
                self.engine.snapshot_cache = {"t": 3, "sessions": [changed], "closed": []}
            third = self.engine.workstreams_snapshot()
            self.assertIsNot(third, second)
            self.assertEqual(records.call_count, 2)
            self.assertEqual(third["workstreams"][0]["counts"]["working"], 1)

    def test_workstream_identity_prefers_nested_repo_and_resolves_symlinks(self):
        outer = os.path.join(self.tmp.name, "outer")
        inner = os.path.join(outer, "packages", "inner")
        cwd = os.path.join(inner, "src")
        os.makedirs(os.path.join(outer, ".git"))
        os.makedirs(os.path.join(inner, ".git"))
        os.makedirs(cwd)
        link = os.path.join(self.tmp.name, "inner-link")
        os.symlink(inner, link)
        nested = self.engine.workstream_identity(cwd)
        linked = self.engine.workstream_identity(link)
        self.assertEqual(nested["root"], os.path.realpath(inner))
        self.assertEqual(linked["workstream_id"], nested["workstream_id"])

    def test_workstream_identity_invalidates_a_renamed_root_and_flags_bad_metadata(self):
        repo = os.path.join(self.tmp.name, "rename-me")
        os.makedirs(os.path.join(repo, ".git"))
        before = self.engine.workstream_identity(repo)
        renamed = os.path.join(self.tmp.name, "renamed")
        os.rename(repo, renamed)
        after = self.engine.workstream_identity(repo)
        moved = self.engine.workstream_identity(renamed)
        self.assertTrue(after["missing"])
        self.assertNotEqual(before["workstream_id"], moved["workstream_id"])

        broken = os.path.join(self.tmp.name, "broken-worktree")
        os.makedirs(broken)
        with open(os.path.join(broken, ".git"), "w") as handle:
            handle.write("not git metadata\n")
        identity = self.engine.workstream_identity(broken)
        self.assertTrue(identity["stale"])
        self.assertIn("unreadable", identity["error"])

    def test_workstream_records_preserve_detached_branch_and_provider_outage_shape(self):
        session = codex_session()
        session.update(branch="HEAD", ui_group="working", reason_label="Working",
                       activity_at=9, cwd=self.cwd)
        records = self.engine.workstream_records([session], [])
        self.assertEqual(records[0]["branches"], ["HEAD"])
        self.assertEqual(records[0]["providers"], ["codex"])
        self.assertEqual(records[0]["counts"]["working"], 1)
        self.assertEqual(records[0]["repo_summary"]["tests"], "not_observed")

    def test_workstreams_do_not_merge_sessions_whose_locations_are_unknown(self):
        one = codex_session()
        two = codex_session()
        one.update(session_id="codex:one", cwd=None, ui_group="available", activity_at=2)
        two.update(session_id="codex:two", cwd="", ui_group="available", activity_at=1)
        records = self.engine.workstream_records([one, two], [])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item["kind"] == "unknown" for item in records))
        self.assertTrue(all(item["root"] == "Location unavailable" for item in records))
        self.assertNotEqual(records[0]["workstream_id"], records[1]["workstream_id"])

    def test_closed_history_is_filtered_paginated_and_keeps_pins_out_of_listing(self):
        rows = [
            {"session_id": "codex:one", "provider": "codex", "title": "Parser audit",
             "project": "fleet", "primary_action": "view", "pinned": False},
            {"session_id": "claude-two", "provider": "claude", "title": "Parser fix",
             "project": "fleet", "primary_action": "reopen", "pinned": False},
            {"session_id": "claude-pin", "provider": "claude", "title": "Pinned parser",
             "project": "fleet", "primary_action": "view", "pinned": True},
        ]
        with self.engine.lock:
            self.engine.snapshot_cache = {"closed": rows}
        first = self.engine.history_snapshot(limit=1, query="parser")
        self.assertTrue(first["ok"])
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["next_cursor"], 1)
        second = self.engine.history_snapshot(cursor=1, limit=1, query="parser")
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        codex = self.engine.history_snapshot(provider="codex", access="view")
        self.assertEqual([item["session_id"] for item in codex["items"]], ["codex:one"])
        exact = self.engine.history_snapshot(sid="claude-pin")
        self.assertEqual(exact["item"]["title"], "Pinned parser")
        self.assertFalse(self.engine.history_snapshot(provider="future")["ok"])

    def test_repository_routes_are_confined_to_observed_worktrees_and_audit_actions(self):
        os.makedirs(os.path.join(self.cwd, ".git"))
        fleet = self.engine.scan()
        self.assertTrue(fleet["sessions"])

        class FakeCenter:
            def snapshot(inner, root, worktree, test_outcome=None, force=False):
                return {"ok": True, "state": "ok", "root": root, "worktree": worktree,
                    "branch": "feature", "dirty": True, "files": [{"path": "engine.py"}],
                    "revision": "rev-1", "actions": {"commit": {"enabled": True}},
                    "pr": {"state": "none"}, "tests": test_outcome or
                    {"state": "not_observed"}}

            def perform(inner, kind, snapshot, payload):
                return {"ok": True, "kind": kind, "summary": "committed",
                        "snapshot": {**snapshot, "dirty": False, "files": [],
                                     "revision": "rev-2"}}

        self.engine.repo_center = FakeCenter()
        snapshot = self.engine.repository_snapshot("", self.cwd)
        self.assertTrue(snapshot["ok"])
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(os.path.join(outside, ".git"))
        rejected = self.engine.repository_snapshot("", outside)
        self.assertIn("current Fleet", rejected["error"])
        result = self.engine.repository_action({"type": "git_commit", "root": self.cwd,
            "worktree": self.cwd, "revision": "rev-1", "paths": ["engine.py"],
            "message": "Commit"})
        self.assertTrue(result["ok"])
        row = self.engine.ensure_db().execute(
            "SELECT kind,status,summary FROM repo_actions WHERE action_id=?",
            (result["action_id"],)).fetchone()
        self.assertEqual(row, ("git_commit", "succeeded", "committed"))

    def test_workstream_uses_newest_transcript_test_outcome(self):
        one = codex_session()
        one.update(cwd=self.cwd, repo_outcome={"state": "failed", "at": 4,
            "command": "npm test", "provider": "codex"})
        two = codex_session()
        two.update(session_id="codex:two", cwd=self.cwd,
                   repo_outcome={"state": "passed", "at": 8,
                                 "command": "python3 -m unittest", "provider": "claude"})
        records = self.engine.workstream_records([one, two], [])
        self.assertEqual(records[0]["test_outcome"]["state"], "passed")
        self.assertEqual(records[0]["test_outcome"]["command"],
                         "python3 -m unittest")

    def test_status_metrics_are_bounded_and_context_excludes_output(self):
        tail = Tail(os.path.join(self.tmp.name, "status.jsonl"))
        for index in range(52):
            tail._status_cache_track({"cache_creation_input_tokens": index * 1000},
                                     f"2026-07-16T00:00:{index % 60:02d}Z")
        self.assertEqual(len(tail.cache_write_history), 50)
        self.assertEqual((tail.cache_write_history[0], tail.cache_write_history[-1]),
                         (2000, 51000))
        self.assertEqual(tail.cache_write_spikes, 31)
        self.assertEqual(tail.cache_write_peak, 51000)
        tail.last_usage = {"input_tokens": 100, "cache_creation_input_tokens": 200,
                           "cache_read_input_tokens": 700, "output_tokens": 9000}
        self.assertEqual(tail.context_tokens(), 1000)
        metrics = tail.status_metrics(self.engine.cfg)
        self.assertEqual(metrics["cache_read_pct"], 70)
        self.assertEqual(metrics["cache_write"], 200)

    def test_compaction_headroom_requires_explicit_valid_settings(self):
        with open(self.claude_settings, "w") as handle:
            json.dump({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000",
                               "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"}}, handle)
        self.assertEqual(self.engine.claude_compact_headroom(self.cwd, 470000, 1000000),
                         170000)
        self.engine._compact_settings_cache.clear()
        with open(self.claude_settings, "w") as handle:
            json.dump({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "2000000",
                               "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"}}, handle)
        self.assertEqual(self.engine.claude_compact_headroom(self.cwd, 470000, 1000000),
                         330000)
        self.engine._compact_settings_cache.clear()
        with open(self.claude_settings, "w") as handle:
            json.dump({"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "invalid"}}, handle)
        self.assertIsNone(self.engine.claude_compact_headroom(self.cwd, 470000, 1000000))
        self.assertIsNone(self.engine.claude_compact_headroom(self.cwd, None, 1000000))

    def test_operational_git_uses_cached_origin_main_comparison(self):
        calls = []
        self.engine._bounded_process = lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or {"ok": True, "stdout": "10 3\n"})
        worktree = os.path.realpath(self.cwd)
        self.engine._operational_git_probe(worktree, worktree)
        with mock.patch.object(self.engine, "workstream_identity", return_value={
                "kind": "git", "root": worktree, "worktree": worktree,
                "missing": False}):
            status = self.engine.operational_git(worktree)
        self.assertEqual((status["ahead"], status["behind"]), (3, 10))
        self.assertEqual(calls[0][0], ["git", "-C", worktree, "rev-list",
            "--left-right", "--count", "refs/remotes/origin/main...HEAD"])
        self.assertNotIn("fetch", calls[0][0])
        self.assertEqual(calls[0][1]["timeout"], 4)
        with mock.patch.object(self.engine, "workstream_identity", return_value={
                "kind": "folder", "root": worktree, "worktree": worktree,
                "missing": False}):
            nongit = self.engine.operational_git(worktree)
        self.assertIsNone(nongit["worktree_label"])
        self.assertIsNone(nongit["ahead"])

    def test_status_tree_cost_breakdown_and_closed_snapshot_are_preserved(self):
        session = codex_session()
        session.update(provider="claude", session_id="status-session", name="Status",
                       title="Status", cwd=self.cwd, cost=.25, agent_cost=.75,
                       agents_total=2, bridge_url=None, agents=[
                           {"agent_type": "review", "description": "Review one", "cost": .3},
                           {"agent_type": "test", "description": "Test two", "cost": .45}])
        with mock.patch.object(self.engine, "operational_git", return_value={
                "worktree": self.cwd, "worktree_label": "repo", "ahead": 3,
                "behind": 10, "git_observed_at": 1}), \
             mock.patch.object(self.engine, "claude_compact_headroom", return_value=None):
            status = self.engine.session_status_line(session)
        self.assertEqual(status["tree_cost"], 1.0)
        self.assertEqual([item["cost"] for item in status["cost_breakdown"]],
                         [.25, .3, .45])
        session["status_line"] = status
        self.engine.record_sessions([session], 100)
        self.engine.record_sessions([], 101)
        self.engine._closed_sessions_cache = None
        closed = next(item for item in self.engine.closed_sessions()
                      if item["session_id"] == "status-session")
        self.assertEqual(closed["status_line"]["tree_cost"], 1.0)
        self.assertTrue(closed["status_line"]["frozen"])

    def test_unchanged_session_ledger_projection_skips_mutating_sql(self):
        session = codex_session()
        session.update(status_line={"ahead": 2, "git_observed_at": 1},
                       name="Codex", title="Codex")
        self.engine.record_sessions([session], 100)
        statements = []
        self.engine.ensure_db().set_trace_callback(statements.append)
        refreshed = copy.deepcopy(session)
        refreshed["status_line"]["git_observed_at"] = 2
        self.engine.record_sessions([refreshed], 102)
        mutations = [statement for statement in statements
                     if statement.lstrip().upper().startswith(
                         ("INSERT", "UPDATE", "DELETE", "REPLACE", "COMMIT"))]
        self.assertEqual(mutations, [])

    def test_claude_peek_preserves_markdown_blocks(self):
        tail = Tail(self.transcript)
        text = "### Default width\n\nUse **Fit the screen**."
        tail.convo.append({"role": "assistant", "text": text})
        self.assertEqual(tail.last_message(800), {"role": "assistant", "text": text})
        long_text = "x" * 900
        tail.convo.append({"role": "assistant", "text": long_text})
        preview = tail.last_message(800)["text"]
        self.assertEqual(len(preview), 800)
        self.assertTrue(preview.endswith("…"))

    def test_claude_full_chat_keeps_complete_large_messages(self):
        tail = Tail(self.transcript)
        first = "a" * 5001
        second = "b" * 5002
        tail._convo_add("assistant", first, "2026-07-20T10:00:00Z")
        tail._convo_add("assistant", second, "2026-07-20T10:00:01Z")
        self.assertEqual(len(tail.convo), 1)
        self.assertEqual(tail.convo[0]["text"], first + "\n\n" + second)

    def test_task_notification_ends_killed_agent_but_not_a_resumed_agent(self):
        subdir = os.path.join(self.tmp.name, "agent-parent", "subagents")
        os.makedirs(subdir)

        def write_agent(agent_id, rows):
            with open(os.path.join(subdir, agent_id + ".meta.json"), "w") as handle:
                json.dump({"agentType": "quick-build", "description": "Gate batch",
                           "toolUseId": "tool-" + agent_id}, handle)
            with open(os.path.join(subdir, agent_id + ".jsonl"), "w") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")

        killed_id = "agent-killed123"
        resumed_id = "agent-resumed123"
        write_agent(killed_id, [{"type": "user", "timestamp": "2026-07-16T20:39:44.478Z",
            "message": {"role": "user", "content": [{"type": "text",
                "text": "[Request interrupted by user]"}]}}])
        write_agent(resumed_id, [{"type": "assistant",
            "timestamp": "2026-07-16T20:39:45.000Z", "message": {
                "role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "still-running",
                             "name": "Bash", "input": {}}]}}])

        parent_path = os.path.join(self.tmp.name, "agent-parent.jsonl")
        with open(parent_path, "w") as handle:
            for agent_id in (killed_id, resumed_id):
                bare_id = agent_id.removeprefix("agent-")
                prompt = ("<task-notification>\n"
                          f"<task-id>{bare_id}</task-id>\n"
                          "<status>killed</status>\n"
                          "</task-notification>")
                handle.write(json.dumps({"type": "attachment",
                    "timestamp": "2026-07-16T20:39:44.480Z",
                    "attachment": {"type": "queued_command",
                                   "commandMode": "task-notification",
                                   "prompt": prompt}}) + "\n")
        parent = Tail(parent_path)
        self.assertTrue(parent.poll())

        states = {agent["agent_id"]: agent["state"] for agent in
                  self.engine.scan_agents(subdir, time.time(), parent=parent)}
        self.assertEqual(states[killed_id], "ended")
        self.assertEqual(states[resumed_id], "running")

    def test_agent_finalization_dedupe_is_scoped_to_parent(self):
        aid = "agent-collision123"

        def make_parent(name):
            subdir = os.path.join(self.tmp.name, name, "subagents")
            os.makedirs(subdir)
            with open(os.path.join(subdir, aid + ".meta.json"), "w") as handle:
                json.dump({"agentType": "review", "description": name}, handle)
            transcript = os.path.join(subdir, aid + ".jsonl")
            with open(transcript, "w") as handle:
                handle.write(json.dumps({"type": "assistant",
                    "timestamp": "2026-07-16T20:39:45.000Z", "message": {
                        "role": "assistant", "model": "claude-sonnet",
                        "stop_reason": "end_turn", "usage": {"input_tokens": 1,
                            "output_tokens": 1}, "content": [{"type": "text",
                                                               "text": "done"}]}}) + "\n")
            old = time.time() - 60
            os.utime(transcript, (old, old))
            return subdir

        subdirs = [make_parent("parent-one"), make_parent("parent-two")]
        finalized = []
        self.engine.ledger_finalize = lambda subdir, agent_id, meta, tail: (
            finalized.append((subdir, agent_id)))
        for subdir in subdirs:
            self.engine.scan_agents(subdir, time.time())
        self.assertEqual(finalized, [(subdirs[0], aid), (subdirs[1], aid)])
        for subdir in subdirs:
            self.engine.scan_agents(subdir, time.time())
        self.assertEqual(len(finalized), 2)

    def test_tail_tracks_only_known_claude_permission_modes(self):
        path = os.path.join(self.tmp.name, "permission-mode.jsonl")
        rows = [
            {"type": "permission-mode", "permissionMode": "acceptEdits"},
            {"type": "permission-mode", "permissionMode": "invented"},
            {"type": "user", "permissionMode": "plan",
             "message": {"role": "user", "content": "continue"}},
        ]
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        tail = Tail(path)
        self.assertTrue(tail.poll())
        self.assertEqual(tail.permission_mode, "plan")

    def test_iterm_mailbox_serializes_concurrent_actions(self):
        active = 0
        maximum = 0
        seen = []
        guard = threading.Lock()

        def injector(_args, **_kwargs):
            nonlocal active, maximum
            with open(os.path.join(self.base, "inject-request.txt")) as handle:
                tty, request_id, *_ = handle.read().splitlines()
            with guard:
                active += 1
                maximum = max(maximum, active)
                seen.append(tty)
            time.sleep(0.04)
            with open(os.path.join(self.base, "inject-result.txt"), "w") as handle:
                handle.write(f"{request_id} ok")
            with guard:
                active -= 1
            return SimpleNamespace(returncode=0)

        results = []
        barrier = threading.Barrier(3)

        def write(tty):
            barrier.wait()
            results.append(self.engine._iterm_write(tty, [("hello", True)], 0.05))

        threads = [threading.Thread(target=write, args=(tty,))
                   for tty in ("/dev/ttys101", "/dev/ttys202")]
        with mock.patch.object(subprocess, "run", side_effect=injector):
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(maximum, 1)
        self.assertCountEqual(seen, ["/dev/ttys101", "/dev/ttys202"])
        self.assertEqual(results, [{"ok": True}, {"ok": True}])

    def test_worktree_cleanup_ticket_survives_process_closing_retry(self):
        token = "retry-cleanup-ticket"
        worktree = os.path.join(self.tmp.name, "closing-worktree")
        os.makedirs(worktree)
        ticket = {"session_id": "same", "provider": "claude", "root": self.cwd,
                  "worktree": worktree, "revision": "same-revision", "pid": 424242,
                  "expires": time.time() + 300, "closed_at": time.time()}
        with self.engine._cleanup_lock:
            self.engine._cleanup_tickets[token] = ticket
        running = {"value": True}

        def process(argv, **_kwargs):
            if argv[:2] == ["ps", "-p"]:
                return SimpleNamespace(stdout="claude --session" if running["value"] else "")
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        preview = {"inspect_ok": True, "revision": "same-revision",
                   "root": self.cwd, "worktree": worktree, "remove_allowed": True,
                   "force_remove_allowed": False, "owned_lock": False}
        with mock.patch.object(subprocess, "run", side_effect=process), \
             mock.patch.object(self.engine, "close_worktree_preview", return_value=preview), \
             mock.patch.object(self.engine, "_bounded_process",
                               return_value={"ok": True, "stdout": "", "stderr": ""}):
            first = self.engine.cleanup_closed_worktree({
                "session_id": "same", "cleanup_ticket": token, "force": False})
            self.assertFalse(first["ok"])
            self.assertIn("still closing", first["error"])
            self.assertIn(token, self.engine._cleanup_tickets)

            running["value"] = False
            second = self.engine.cleanup_closed_worktree({
                "session_id": "same", "cleanup_ticket": token, "force": False})

        self.assertTrue(second["ok"], second)
        self.assertNotIn(token, self.engine._cleanup_tickets)


if __name__ == "__main__":
    unittest.main()
