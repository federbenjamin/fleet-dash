"""Coverage for fleetdash/engine_notify.py (NotifyOps mixin).

Builds a real Engine with Codex disabled and instance paths patched into a
temp dir (the same fixture technique as tests/test_engine_providers.py), then
swaps controllable fakes into ``operations`` / ``outbox`` / ``web_push`` to
exercise every snapshot, mutation, push, and Outbox dispatch branch.
"""
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash import paths as engine_paths
from fleetdash.config import DEFAULT_CONFIG
from fleetdash.engine import Engine
from fleetdash import engine_notify as notify_module
from fleetdash.outbox import OutboxError
from fleetdash.briefing import OperationsError


class FakeWebPush:
    def __init__(self, status=None):
        self._status = status or {"configured": True, "public_key": "pk",
                                  "delivery": "ready", "runtime": "up",
                                  "helper": {"ready": True, "restarts": 0,
                                             "state": "ready"},
                                  "queue": {"pending": 0}}
        self.wake_event = threading.Event()
        self.started = 0
        self.tests = []
        self.capability_calls = []

    def status(self):
        return dict(self._status)

    def start(self):
        self.started += 1

    def enqueue_test(self, device_id):
        self.tests.append(device_id)
        return {"id": "delivery-test", "device_id": device_id}

    def capability_action(self, token, mute_callback=None):
        self.capability_calls.append(token)
        if mute_callback:
            mute_callback("sid-mute")
        return {"action": "open", "event_id": "evt"}


class NotifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "fleet")
        os.makedirs(self.base)
        self.patchers = [
            mock.patch.object(engine_paths, "HOME", self.tmp.name),
            mock.patch.object(engine_paths, "BASE", self.base),
            mock.patch.object(engine_paths, "CAPTURE_BASE", self.base),
        ]
        for patcher in self.patchers:
            patcher.start()
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({"codex_enabled": False, "act_token": "secret", "ntfy_topic": ""})
        self.engine = Engine(cfg)
        # Fresh controllable doubles for every provider surface.
        self.engine.operations = mock.MagicMock()
        self.engine.outbox = mock.MagicMock()
        self.engine.web_push = None

    def tearDown(self):
        if self.engine.db:
            self.engine.db.close()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()


class OutboxUsageOptionsTest(NotifyBase):
    def test_claude_profiles_and_codex_buckets_are_projected(self):
        with self.engine.lock:
            self.engine.snapshot_cache["provider_usage"] = {
                "claude": {"profiles": [
                    {"id": "p1", "email": "a@x", "name": "A",
                     "five_hour_reset": 111, "weekly_reset": 222},
                    {"id": "p2", "name": "no-windows"}]},
                "codex": {"account_id": "cx", "email": "cx@x", "buckets": [
                    {"id": "b1", "label": "Bucket", "reset": 333},
                    {"id": None, "reset": 444}]}}
        options = self.engine.outbox_usage_options()
        claude = next(o for o in options if o["provider"] == "claude")
        self.assertEqual(claude["account_id"], "p1")
        self.assertEqual({w["id"] for w in claude["windows"]},
                         {"five_hour", "weekly"})
        codex = next(o for o in options if o["provider"] == "codex")
        self.assertEqual([w["id"] for w in codex["windows"]], ["b1"])

    def test_single_claude_dict_without_profiles_list_is_wrapped(self):
        with self.engine.lock:
            self.engine.snapshot_cache["provider_usage"] = {
                "claude": {"email": "solo@x", "five_hour_reset": 9}}
        options = self.engine.outbox_usage_options()
        self.assertEqual(options[0]["account_id"], "solo@x")

    def test_empty_usage_yields_no_options(self):
        self.assertEqual(self.engine.outbox_usage_options(), [])


class SnapshotWrapperTest(NotifyBase):
    def test_outbox_snapshot_success_and_errors(self):
        self.engine.outbox.list.return_value = {"ok": True, "items": []}
        self.engine.outbox.counts.return_value = {"pending": 0}
        with mock.patch.object(self.engine, "outbox_usage_options",
                               return_value=[]):
            out = self.engine.outbox_snapshot(state="pending", cursor=0, limit=5)
        self.assertEqual(out["summary"], {"pending": 0})
        self.assertEqual(out["usage_options"], [])

        self.engine.outbox.list.side_effect = OutboxError("bad", code="nope")
        out = self.engine.outbox_snapshot()
        self.assertEqual((out["ok"], out["code"]), (False, "nope"))

        self.engine.outbox.list.side_effect = RuntimeError("kaboom")
        out = self.engine.outbox_snapshot()
        self.assertIn("temporarily unavailable", out["error"])

    def test_briefing_snapshot_success_and_errors(self):
        self.engine.operations.briefing_snapshot.return_value = {"ok": True}
        self.assertTrue(self.engine.briefing_snapshot("phone")["ok"])

        self.engine.operations.briefing_snapshot.side_effect = OperationsError("x")
        self.assertFalse(self.engine.briefing_snapshot()["ok"])

        self.engine.operations.briefing_snapshot.side_effect = RuntimeError("y")
        self.assertIn("temporarily unavailable",
                      self.engine.briefing_snapshot()["error"])

    def test_notifications_snapshot_success_and_errors(self):
        self.engine.operations.notification_snapshot.return_value = {"ok": True}
        self.assertTrue(self.engine.notifications_snapshot("d")["ok"])

        self.engine.operations.notification_snapshot.side_effect = OperationsError("x")
        self.assertFalse(self.engine.notifications_snapshot()["ok"])

        self.engine.operations.notification_snapshot.side_effect = RuntimeError("y")
        self.assertIn("temporarily unavailable",
                      self.engine.notifications_snapshot()["error"])


class PushConfigTest(NotifyBase):
    def test_push_config_uses_starting_runtime_when_web_push_absent(self):
        self.engine.operations.notification_devices_snapshot.return_value = {
            "current_device": {"id": "d"}, "devices": [], "registered": 0,
            "enabled": 0}
        self.engine.operations.notification_delivery_diagnostics.return_value = {"q": 1}
        out = self.engine.push_config("d")
        self.assertEqual(out["delivery"], "starting")
        self.assertFalse(out["configured"])

    def test_push_config_uses_live_runtime_when_present(self):
        self.engine.web_push = FakeWebPush()
        self.engine.operations.notification_devices_snapshot.return_value = {
            "current_device": {"id": "d"}, "devices": [{"id": "d"}],
            "registered": 1, "enabled": 1}
        out = self.engine.push_config("d")
        self.assertTrue(out["configured"])
        self.assertEqual(out["public_key"], "pk")

    def test_push_config_operations_error_and_generic(self):
        self.engine.operations.notification_devices_snapshot.side_effect = \
            OperationsError("x")
        self.assertFalse(self.engine.push_config()["ok"])
        self.engine.operations.notification_devices_snapshot.side_effect = \
            RuntimeError("y")
        self.assertIn("temporarily unavailable", self.engine.push_config()["error"])

    def test_push_devices_success_and_errors(self):
        self.engine.operations.notification_devices_snapshot.return_value = {"ok": 1}
        self.assertEqual(self.engine.push_devices("d"), {"ok": 1})
        self.engine.operations.notification_devices_snapshot.side_effect = \
            OperationsError("x")
        self.assertFalse(self.engine.push_devices()["ok"])
        self.engine.operations.notification_devices_snapshot.side_effect = \
            RuntimeError("y")
        self.assertIn("temporarily unavailable", self.engine.push_devices()["error"])

    def test_push_diagnostics_absent_and_present(self):
        self.engine.operations.notification_delivery_diagnostics.return_value = {"q": 0}
        absent = self.engine.push_diagnostics()
        self.assertEqual(absent["delivery"], "starting")
        self.engine.web_push = FakeWebPush()
        present = self.engine.push_diagnostics()
        self.assertEqual(present["delivery"], "ready")
        self.assertEqual(present["runtime"], "up")

    def test_legacy_ntfy_diagnostics(self):
        self.engine.operations.legacy_notification_diagnostics.return_value = {"extra": 1}
        self.engine.cfg["legacy_ntfy_enabled"] = True
        self.engine.cfg["ntfy_topic"] = "t"
        self.engine.cfg["ntfy_server"] = "s"
        out = self.engine.legacy_ntfy_diagnostics()
        self.assertTrue(out["enabled"])
        self.assertTrue(out["configured"])
        self.assertEqual(out["extra"], 1)


class PushSubscriptionTest(NotifyBase):
    def test_register_remove_forget_branches(self):
        self.engine.operations.notification_forget_device.return_value = {"id": "f"}
        self.engine.operations.notification_remove_device.return_value = {"id": "r"}
        self.engine.operations.notification_register_device.return_value = {"id": "n"}

        self.assertEqual(self.engine.push_subscription({"forget": True,
            "device_id": "f"})["device"], {"id": "f"})
        self.assertEqual(self.engine.push_subscription({"remove": True,
            "device_id": "r"})["device"], {"id": "r"})
        self.assertEqual(self.engine.push_subscription({"device_id": "n",
            "subscription": {}})["device"], {"id": "n"})

    def test_push_subscription_errors(self):
        self.engine.operations.notification_register_device.side_effect = \
            OperationsError("bad sub")
        out = self.engine.push_subscription({"device_id": "x"})
        self.assertEqual(out["error"], "bad sub")
        self.engine.operations.notification_register_device.side_effect = \
            RuntimeError("y")
        self.assertIn("could not be saved",
                      self.engine.push_subscription({"device_id": "x"})["error"])

    def test_push_device_settings_success_and_errors(self):
        self.engine.operations.notification_update_device.return_value = {"id": "d"}
        out = self.engine.push_device_settings({"device_id": "d",
            "display_name": "n", "enabled": True, "preferences": {"a": 1}})
        self.assertEqual(out["device"], {"id": "d"})
        self.engine.operations.notification_update_device.side_effect = \
            OperationsError("bad")
        self.assertEqual(self.engine.push_device_settings({})["error"], "bad")
        self.engine.operations.notification_update_device.side_effect = \
            RuntimeError("y")
        self.assertIn("could not be saved",
                      self.engine.push_device_settings({})["error"])


class NotificationMutationTest(NotifyBase):
    def test_mark_read_success_and_errors(self):
        self.engine.operations.notification_mark_read.return_value = 42
        self.assertEqual(self.engine.notifications_mark_read({})["cursor"], 42)
        self.engine.operations.notification_mark_read.side_effect = OperationsError("x")
        self.assertFalse(self.engine.notifications_mark_read({})["ok"])
        self.engine.operations.notification_mark_read.side_effect = RuntimeError("y")
        self.assertIn("could not be saved",
                      self.engine.notifications_mark_read({})["error"])

    def test_snooze_success_and_errors(self):
        self.engine.operations.notification_snooze.return_value = 999
        self.assertEqual(self.engine.notifications_snooze({})["until"], 999)
        self.engine.operations.notification_snooze.side_effect = OperationsError("x")
        self.assertFalse(self.engine.notifications_snooze({})["ok"])
        self.engine.operations.notification_snooze.side_effect = RuntimeError("y")
        self.assertIn("could not be snoozed",
                      self.engine.notifications_snooze({})["error"])

    def test_wake_success_and_errors(self):
        self.engine.operations.notification_wake.return_value = None
        self.assertTrue(self.engine.notifications_wake({})["ok"])
        self.engine.operations.notification_wake.side_effect = OperationsError("x")
        self.assertFalse(self.engine.notifications_wake({})["ok"])
        self.engine.operations.notification_wake.side_effect = RuntimeError("y")
        self.assertIn("could not be woken",
                      self.engine.notifications_wake({})["error"])

    def test_mute_success(self):
        self.engine.operations.notification_snapshot.return_value = {"events": [
            {"source_revision": "rev-1", "session_id": "sid-1", "provider": "claude"}]}
        with mock.patch.object(self.engine, "update_settings",
                               return_value={"ok": True}) as settings:
            out = self.engine.notifications_mute({"event_id": "e",
                "source_revision": "rev-1", "muted": True})
        self.assertTrue(out["ok"])
        self.assertEqual(out["session_id"], "sid-1")
        settings.assert_called_once()
        self.engine.operations.notification_set_session_mute.assert_called_once_with(
            "sid-1", "claude", True)

    def test_mute_stale_event_is_rejected(self):
        self.engine.operations.notification_snapshot.return_value = {"events": [
            {"source_revision": "other", "session_id": "sid-1"}]}
        out = self.engine.notifications_mute({"event_id": "e",
            "source_revision": "rev-1", "muted": True})
        self.assertFalse(out["ok"])

    def test_mute_invalid_flag_is_rejected(self):
        self.engine.operations.notification_snapshot.return_value = {"events": [
            {"source_revision": "rev-1", "session_id": "sid-1"}]}
        out = self.engine.notifications_mute({"event_id": "e",
            "source_revision": "rev-1", "muted": "yes"})
        self.assertFalse(out["ok"])

    def test_mute_settings_failure_is_reported(self):
        self.engine.operations.notification_snapshot.return_value = {"events": [
            {"source_revision": "rev-1", "session_id": "sid-1"}]}
        with mock.patch.object(self.engine, "update_settings",
                               return_value={"ok": False, "error": "denied"}):
            out = self.engine.notifications_mute({"event_id": "e",
                "source_revision": "rev-1", "muted": True})
        self.assertEqual(out["error"], "denied")

    def test_mute_generic_exception(self):
        self.engine.operations.notification_snapshot.side_effect = RuntimeError("boom")
        out = self.engine.notifications_mute({"event_id": "e",
            "source_revision": "rev-1", "muted": True})
        self.assertIn("could not be saved", out["error"])

    def test_retry_success_with_and_without_web_push(self):
        self.engine.operations.notification_retry_delivery.return_value = {"id": "x"}
        out = self.engine.notifications_retry({"delivery_id": "x"})
        self.assertTrue(out["ok"])
        web = FakeWebPush()
        self.engine.web_push = web
        self.engine.notifications_retry({"delivery_id": "x"})
        self.assertTrue(web.wake_event.is_set())

    def test_retry_errors(self):
        self.engine.operations.notification_retry_delivery.side_effect = \
            OperationsError("x")
        self.assertFalse(self.engine.notifications_retry({})["ok"])
        self.engine.operations.notification_retry_delivery.side_effect = \
            RuntimeError("y")
        self.assertIn("could not be retried",
                      self.engine.notifications_retry({})["error"])


class NotificationPolicyTest(NotifyBase):
    def test_snapshot_success_and_errors(self):
        self.engine.operations.notification_policy_snapshot.return_value = {"ok": True}
        self.assertTrue(self.engine.notification_policy_snapshot()["ok"])
        self.engine.operations.notification_policy_snapshot.side_effect = \
            OperationsError("x")
        self.assertFalse(self.engine.notification_policy_snapshot()["ok"])
        self.engine.operations.notification_policy_snapshot.side_effect = \
            RuntimeError("y")
        self.assertIn("temporarily unavailable",
                      self.engine.notification_policy_snapshot()["error"])

    def test_update_success_and_errors(self):
        self.engine.operations.notification_policy_update.return_value = {"ok": True}
        web = FakeWebPush()
        self.engine.web_push = web
        self.assertTrue(self.engine.notification_policy_update({})["ok"])
        self.assertTrue(web.wake_event.is_set())
        self.engine.operations.notification_policy_update.side_effect = \
            OperationsError("x")
        self.assertFalse(self.engine.notification_policy_update({})["ok"])
        self.engine.operations.notification_policy_update.side_effect = \
            RuntimeError("y")
        self.assertIn("could not be saved",
                      self.engine.notification_policy_update({})["error"])


class WebPushLifecycleTest(NotifyBase):
    def test_start_web_push_creates_then_reuses(self):
        created = []

        class FakeService:
            def __init__(self, operations, base, cfg):
                created.append((operations, base, cfg))
                self.starts = 0

            def start(self):
                self.starts += 1

        with mock.patch.object(notify_module, "WebPushService", FakeService):
            self.engine.start_web_push()
            first = self.engine.web_push
            self.engine.start_web_push()
        self.assertIs(self.engine.web_push, first)
        self.assertEqual(len(created), 1)
        self.assertEqual(first.starts, 2)

    def test_capability_action_rejects_non_string(self):
        self.assertFalse(self.engine.push_capability_action({"capability": 5})["ok"])
        self.assertFalse(self.engine.push_capability_action("nope")["ok"])

    def test_capability_action_without_service(self):
        self.engine.web_push = None
        self.assertFalse(
            self.engine.push_capability_action({"capability": "t"})["ok"])

    def test_capability_action_success_runs_mute_callback(self):
        web = FakeWebPush()
        self.engine.web_push = web
        with mock.patch.object(self.engine, "_persist_config_fields") as persist:
            out = self.engine.push_capability_action({"capability": "signed"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "open")
        self.assertEqual(web.capability_calls, ["signed"])
        # mute callback persisted muted_sessions and updated cfg in place
        persist.assert_called_once()
        self.assertIn("sid-mute", self.engine.cfg["muted_sessions"])

    def test_push_test_service_absent_or_unconfigured(self):
        self.engine.web_push = None
        out = self.engine.push_test({"device_id": "d"})
        self.assertEqual(out["code"], "delivery_unavailable")
        self.engine.web_push = FakeWebPush(status={"configured": False})
        self.assertEqual(self.engine.push_test({})["code"], "delivery_unavailable")

    def test_push_test_success_and_errors(self):
        web = FakeWebPush()
        self.engine.web_push = web
        out = self.engine.push_test({"device_id": "d"})
        self.assertTrue(out["ok"])
        self.assertEqual(web.tests, ["d"])

        class Boom(FakeWebPush):
            def enqueue_test(self, device_id):
                raise OperationsError("nope")

        self.engine.web_push = Boom()
        self.assertFalse(self.engine.push_test({})["ok"])

        class Boom2(FakeWebPush):
            def enqueue_test(self, device_id):
                raise RuntimeError("x")

        self.engine.web_push = Boom2()
        self.assertIn("could not be queued", self.engine.push_test({})["error"])


class BudgetsAndBriefingActionTest(NotifyBase):
    def test_budgets_snapshot_with_spawn_resolves_workstream(self):
        self.engine.operations.budgets_snapshot.return_value = {"ok": True}
        with mock.patch.object(self.engine, "workstream_identity",
                               return_value={"workstream_id": "ws-1"}):
            out = self.engine.budgets_snapshot({"cwd": "/work/repo"})
        self.assertTrue(out["ok"])
        _, kwargs = self.engine.operations.budgets_snapshot.call_args
        self.assertEqual(kwargs["spawn"]["workstream_id"], "ws-1")

    def test_budgets_snapshot_errors(self):
        self.engine.operations.budgets_snapshot.side_effect = OperationsError("x")
        self.assertFalse(self.engine.budgets_snapshot()["ok"])
        self.engine.operations.budgets_snapshot.side_effect = RuntimeError("y")
        self.assertIn("temporarily unavailable", self.engine.budgets_snapshot()["error"])

    def test_briefing_action_success_and_errors(self):
        self.engine.operations.review.return_value = 7
        self.assertEqual(self.engine.briefing_action({})["cursor"], 7)
        self.engine.operations.review.side_effect = OperationsError("x")
        self.assertFalse(self.engine.briefing_action({})["ok"])
        self.engine.operations.review.side_effect = RuntimeError("y")
        self.assertIn("review failed", self.engine.briefing_action({})["error"])


class ValidateOutboxSpawnTest(NotifyBase):
    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.cwd)

    def base_spec(self, **over):
        spec = {"provider": "claude", "cwd": self.cwd}
        spec.update(over)
        return spec

    def test_not_dict_and_unknown_provider(self):
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn("x")
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn({"provider": "gemini",
                                                "cwd": self.cwd})

    def test_directory_checks(self):
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn({"provider": "claude",
                "cwd": os.path.join(self.tmp.name, "missing")})
        outside = tempfile.mkdtemp()
        try:
            with mock.patch.object(engine_paths, "HOME",
                                   os.path.join(self.tmp.name, "home")):
                os.makedirs(os.path.join(self.tmp.name, "home"))
                with self.assertRaises(OutboxError):
                    self.engine._validate_outbox_spawn({"provider": "claude",
                        "cwd": outside})
        finally:
            os.rmdir(outside)

    def test_claude_model_effort_and_permission_validation(self):
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn(self.base_spec(model="not-a-model"))
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn(self.base_spec(effort="turbo"))
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn(
                self.base_spec(permission_mode="ludicrous"))
        good = self.engine._validate_outbox_spawn(self.base_spec(
            model=self.engine.MODELS[0], effort=self.engine.EFFORTS[0],
            permission_mode="default"))
        self.assertEqual(good["provider"], "claude")

    def test_codex_model_effort_and_mode_validation(self):
        self.engine.codex = SimpleNamespace(models=[
            {"id": "gpt-5.4", "efforts": ["high"]}])
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn({"provider": "codex",
                "cwd": self.cwd, "model": "unknown"})
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn({"provider": "codex",
                "cwd": self.cwd, "model": "gpt-5.4", "effort": "low"})
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn({"provider": "codex",
                "cwd": self.cwd, "mode": "wild"})
        good = self.engine._validate_outbox_spawn({"provider": "codex",
            "cwd": self.cwd, "model": "gpt-5.4", "effort": "high", "mode": "plan"})
        self.assertEqual(good["provider"], "codex")
        self.assertEqual(good["permission_mode"], "")

    def test_worktree_name_and_git_requirement(self):
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn(
                self.base_spec(worktree_name="bad name!"))
        with self.assertRaises(OutboxError):
            self.engine._validate_outbox_spawn(self.base_spec(worktree=True))
        os.makedirs(os.path.join(self.cwd, ".git"))
        good = self.engine._validate_outbox_spawn(
            self.base_spec(worktree=True, worktree_name="wt1"))
        self.assertTrue(good["worktree"])
        self.assertEqual(good["worktree_name"], "wt1")


class PrepareOutboxPayloadTest(NotifyBase):
    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.cwd)

    def test_new_session_validates_spawn_spec(self):
        prepared = self.engine._prepare_outbox_payload({"kind": "new_session",
            "spawn_spec": {"provider": "claude", "cwd": self.cwd}})
        self.assertEqual(prepared["spawn_spec"]["provider"], "claude")

    def test_new_session_in_staging_forces_source_root(self):
        self.engine.cfg["instance_mode"] = "staging"
        with mock.patch.object(self.engine, "_staging_source_root",
                               return_value=self.cwd):
            prepared = self.engine._prepare_outbox_payload({
                "spawn_spec": {"provider": "claude", "cwd": "/ignored"}})
        self.assertEqual(prepared["spawn_spec"]["cwd"], os.path.realpath(self.cwd))
        self.assertFalse(prepared["spawn_spec"]["worktree"])

    def test_new_session_in_staging_without_source_root_raises(self):
        self.engine.cfg["instance_mode"] = "staging"
        with mock.patch.object(self.engine, "_staging_source_root",
                               return_value=None):
            with self.assertRaises(OutboxError):
                self.engine._prepare_outbox_payload({
                    "spawn_spec": {"provider": "claude", "cwd": self.cwd}})

    def test_target_session_blocked_status_raises(self):
        with mock.patch.object(self.engine, "_outbox_current_target",
                               return_value=("block", "no good", None)):
            with self.assertRaises(OutboxError):
                self.engine._prepare_outbox_payload({
                    "target_session_id": "same", "message": "hi"})

    def test_target_session_staging_owner_gate(self):
        self.engine.cfg["instance_mode"] = "staging"
        with mock.patch.object(self.engine, "_staging_owns", return_value=False):
            with self.assertRaises(OutboxError):
                self.engine._prepare_outbox_payload({"target_session_id": "prod"})

    def test_target_session_allowed_passes_through(self):
        with mock.patch.object(self.engine, "_outbox_current_target",
                               return_value=("ok", None, None)):
            prepared = self.engine._prepare_outbox_payload({
                "target_session_id": "same", "message": "hi"})
        self.assertEqual(prepared["target_session_id"], "same")

    def test_usage_reset_requires_fresh_window(self):
        with mock.patch.object(self.engine, "outbox_usage_options", return_value=[]):
            with self.assertRaises(OutboxError):
                self.engine._prepare_outbox_payload({"kind": "usage_reset",
                    "target_provider": "claude", "usage_account_id": "a",
                    "usage_window_id": "five_hour"})

    def test_usage_reset_records_observed_reset(self):
        options = [{"provider": "claude", "account_id": "a", "windows": [
            {"id": "five_hour", "reset": 555}]}]
        with mock.patch.object(self.engine, "outbox_usage_options",
                               return_value=options):
            prepared = self.engine._prepare_outbox_payload({"kind": "usage_reset",
                "target_provider": "claude", "usage_account_id": "a",
                "usage_window_id": "five_hour"})
        self.assertEqual(prepared["observed_reset_at"], 555)


class OutboxActionTest(NotifyBase):
    def test_each_action_type_dispatches(self):
        self.engine.outbox.counts.return_value = {"pending": 0}
        for typ, method in (("outbox_cancel", "cancel"),
                            ("outbox_delete", "dismiss"),
                            ("outbox_send_now", "send_now")):
            getattr(self.engine.outbox, method).return_value = {"id": typ}
            out = self.engine.outbox_action({"type": typ, "outbox_id": "1"})
            self.assertEqual(out["item"], {"id": typ})

    def test_create_update_retry_retarget_prepare_payload(self):
        self.engine.outbox.counts.return_value = {"pending": 0}
        self.engine.outbox.create.return_value = {"id": "c"}
        self.engine.outbox.update.return_value = {"id": "u"}
        self.engine.outbox.retry.return_value = {"id": "rt"}
        self.engine.outbox.retarget.return_value = {"id": "rr"}
        with mock.patch.object(self.engine, "_prepare_outbox_payload",
                               side_effect=lambda p: dict(p or {})):
            self.assertEqual(self.engine.outbox_action(
                {"type": "outbox_create", "message": "x"})["item"], {"id": "c"})
            self.assertEqual(self.engine.outbox_action(
                {"type": "outbox_update", "outbox_id": "1",
                 "patch": {"a": 1}})["item"], {"id": "u"})
            self.assertEqual(self.engine.outbox_action(
                {"type": "outbox_retry", "outbox_id": "1",
                 "patch": {"a": 1}})["item"], {"id": "rt"})
            # retry with no patch passes None
            self.assertEqual(self.engine.outbox_action(
                {"type": "outbox_retry", "outbox_id": "1"})["item"], {"id": "rt"})
            self.engine.outbox.retry.assert_called_with("1", None)
            self.assertEqual(self.engine.outbox_action(
                {"type": "outbox_retarget", "outbox_id": "1",
                 "patch": {"a": 1}})["item"], {"id": "rr"})

    def test_unknown_action(self):
        self.assertFalse(self.engine.outbox_action({"type": "nope"})["ok"])

    def test_outbox_error_with_choices_and_generic(self):
        err = OutboxError("bad", code="conflict")
        err.choices = ["a", "b"]
        self.engine.outbox.cancel.side_effect = err
        out = self.engine.outbox_action({"type": "outbox_cancel", "outbox_id": "1"})
        self.assertEqual(out["choices"], ["a", "b"])
        self.assertEqual(out["code"], "conflict")

        self.engine.outbox.cancel.side_effect = RuntimeError("boom")
        out = self.engine.outbox_action({"type": "outbox_cancel", "outbox_id": "1"})
        self.assertIn("outbox action failed", out["error"])


class OutboxDispatchTest(NotifyBase):
    def test_relay_dispatch_uses_agent_channel(self):
        with mock.patch.object(self.engine, "act",
                               return_value={"ok": True}) as act:
            out = self.engine._outbox_dispatch({"target_agent_id": "agent-x",
                "destination_session_id": "same", "message": "hi",
                "target_provider": "claude"})
        self.assertTrue(out["ok"])
        self.assertEqual(act.call_args[0][0]["type"], "relay")

    def test_claude_text_dispatch(self):
        with mock.patch.object(self.engine, "act",
                               return_value={"ok": True}) as act:
            out = self.engine._outbox_dispatch({"target_session_id": "same",
                "message": "hi", "target_provider": "claude"})
        self.assertTrue(out["accepted"])
        self.assertEqual(act.call_args[0][0]["type"], "text")

    def test_claude_image_dispatch_uses_queued_writer(self):
        with mock.patch.object(self.engine, "_claude_mutation_lock",
                               return_value=threading.Lock()), \
             mock.patch.object(self.engine, "_write_claude_queued_message",
                               return_value={"ok": True}) as writer:
            out = self.engine._outbox_dispatch({"target_session_id": "same",
                "message": "hi", "target_provider": "claude",
                "_image_paths": ["/q/img.png"]})
        self.assertTrue(out["ok"])
        self.assertEqual(writer.call_args[0][0]["type"], "image_text")

    def test_codex_dispatch_terminal_route_and_fallback(self):
        self.engine.codex = SimpleNamespace(
            native=lambda sid: sid.split(":", 1)[1],
            act=lambda action: {"ok": True, "via": "appserver"})
        with self.engine.lock:
            self.engine.snapshot_cache["sessions"] = [
                {"session_id": "codex:t", "provider": "codex", "read_only": False}]
        with mock.patch.object(self.engine, "_codex_terminal_route",
                               return_value={"tty": "/dev/ttys1"}), \
             mock.patch.object(self.engine, "_write_codex_terminal",
                               return_value={"ok": True, "via": "terminal"}) as term:
            out = self.engine._outbox_dispatch({"target_session_id": "codex:t",
                "message": "hi", "target_provider": "codex"})
        self.assertTrue(out["ok"])
        term.assert_called_once()

        with mock.patch.object(self.engine, "_codex_terminal_route",
                               return_value=None):
            out = self.engine._outbox_dispatch({"target_session_id": "codex:t",
                "message": "hi", "target_provider": "codex"})
        self.assertTrue(out["ok"])


class WriteClaudeQueuedMessageTest(NotifyBase):
    def test_not_live(self):
        with mock.patch.object(self.engine, "live_sessions", return_value=[]):
            out = self.engine._write_claude_queued_message({"session_id": "x"})
        self.assertTrue(out["queueable"])

    def test_not_idle(self):
        with mock.patch.object(self.engine, "live_sessions",
                               return_value=[{"sessionId": "x", "status": "busy"}]):
            out = self.engine._write_claude_queued_message({"session_id": "x"})
        self.assertEqual(out["code"], "provider_control_unavailable")

    def test_waiting_or_compacting_keeps_queued(self):
        reg = {"sessionId": "x", "status": "idle", "cwd": self.tmp.name}
        with mock.patch.object(self.engine, "live_sessions", return_value=[reg]), \
             mock.patch.object(self.engine, "tail_for",
                               return_value=SimpleNamespace(poll=lambda: None,
                                                            pending=True,
                                                            convo_rev=1)), \
             mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None), \
             mock.patch.object(self.engine, "_claude_turn_fenced", return_value=False):
            out = self.engine._write_claude_queued_message({"session_id": "x"})
        self.assertEqual(out["code"], "provider_control_unavailable")

    def test_no_images(self):
        reg = {"sessionId": "x", "status": "idle", "cwd": self.tmp.name}
        with mock.patch.object(self.engine, "live_sessions", return_value=[reg]), \
             mock.patch.object(self.engine, "tail_for",
                               return_value=SimpleNamespace(poll=lambda: None,
                                                            pending=False,
                                                            convo_rev=1)), \
             mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None), \
             mock.patch.object(self.engine, "_claude_turn_fenced", return_value=False):
            out = self.engine._write_claude_queued_message(
                {"session_id": "x", "image_paths": []})
        self.assertEqual(out["error"], "no images")

    def test_success_foreground_records_fence(self):
        reg = {"sessionId": "x", "status": "idle", "cwd": self.tmp.name,
               "pid": os.getpid()}
        tail = SimpleNamespace(poll=lambda: None, pending=False, convo_rev=3)
        fences = []
        with mock.patch.object(self.engine, "live_sessions", return_value=[reg]), \
             mock.patch.object(self.engine, "tail_for", return_value=tail), \
             mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None), \
             mock.patch.object(self.engine, "_claude_turn_fenced", return_value=False), \
             mock.patch.object(self.engine, "_is_background_claude", return_value=False), \
             mock.patch.object(self.engine, "_tty_for_pid", return_value="ttys5"), \
             mock.patch.object(self.engine, "_iterm_write",
                               return_value={"ok": True}) as write, \
             mock.patch.object(self.engine, "_record_claude_turn_fence",
                               side_effect=lambda sid, base: fences.append(sid)):
            out = self.engine._write_claude_queued_message({"session_id": "x",
                "text": "/status", "image_paths": ["/q/a.png", "/q/b.png"]})
        self.assertTrue(out["ok"])
        self.assertEqual(fences, ["x"])
        # slash + no space and two images added a trailing space and captions
        sent_text = write.call_args[0][1][0][0]
        self.assertIn("Images attached through Fleet", sent_text)

    def test_success_foreground_no_tty(self):
        reg = {"sessionId": "x", "status": "idle", "cwd": self.tmp.name,
               "pid": os.getpid()}
        tail = SimpleNamespace(poll=lambda: None, pending=False, convo_rev=3)
        with mock.patch.object(self.engine, "live_sessions", return_value=[reg]), \
             mock.patch.object(self.engine, "tail_for", return_value=tail), \
             mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None), \
             mock.patch.object(self.engine, "_claude_turn_fenced", return_value=False), \
             mock.patch.object(self.engine, "_is_background_claude", return_value=False), \
             mock.patch.object(self.engine, "_tty_for_pid", return_value=None):
            out = self.engine._write_claude_queued_message({"session_id": "x",
                "text": "hi", "image_paths": ["/q/a.png"]})
        self.assertIn("no terminal", out["error"])

    def test_success_background(self):
        reg = {"sessionId": "x", "status": "idle", "cwd": self.tmp.name}
        tail = SimpleNamespace(poll=lambda: None, pending=False, convo_rev=3)
        with mock.patch.object(self.engine, "live_sessions", return_value=[reg]), \
             mock.patch.object(self.engine, "tail_for", return_value=tail), \
             mock.patch.object(self.engine, "hook_pending", return_value=None), \
             mock.patch.object(self.engine, "compacting_secs", return_value=None), \
             mock.patch.object(self.engine, "_claude_turn_fenced", return_value=False), \
             mock.patch.object(self.engine, "_is_background_claude", return_value=True), \
             mock.patch.object(self.engine, "_write_background_claude",
                               return_value={"ok": True}), \
             mock.patch.object(self.engine, "_record_claude_turn_fence"):
            out = self.engine._write_claude_queued_message({"session_id": "x",
                "text": "hi", "image_paths": ["/q/a.png"]})
        self.assertTrue(out["ok"])


class MessageCanSendNowTest(NotifyBase):
    def test_none_and_flagged_sessions_block(self):
        self.assertFalse(self.engine._message_can_send_now(None))
        self.assertFalse(self.engine._message_can_send_now({"pending": {}}))
        self.assertFalse(self.engine._message_can_send_now({"stale": True}))
        self.assertFalse(self.engine._message_can_send_now(
            {"capabilities": {"submit": False}}))

    def test_idle_available_can_send(self):
        self.assertTrue(self.engine._message_can_send_now(
            {"capabilities": {"submit": True}, "state": "idle"}))

    def test_active_claude_blocks_but_codex_active_turn_sends(self):
        self.assertFalse(self.engine._message_can_send_now(
            {"capabilities": {"submit": True}, "state": "running",
             "provider": "claude"}))
        self.assertTrue(self.engine._message_can_send_now(
            {"capabilities": {"submit": True}, "state": "running",
             "provider": "codex", "control_state": "connected_active"}))


class QueueWhenAvailableTest(NotifyBase):
    def test_success_uses_default_message_for_images(self):
        self.engine.outbox.create_delivery.return_value = {"id": "o1",
            "state": "waiting_availability"}
        out = self.engine._queue_when_available(
            {"session_id": "x", "image_paths": ["/a.png"]}, "claude")
        self.assertTrue(out["queued"])
        _, kwargs = self.engine.outbox.create_delivery.call_args
        self.assertIn("attached image", kwargs["message"])

    def test_outbox_error_and_generic(self):
        self.engine.outbox.create_delivery.side_effect = OutboxError("bad",
            code="c")
        out = self.engine._queue_when_available({"session_id": "x",
            "text": "hi"}, "claude")
        self.assertEqual(out["code"], "c")
        self.engine.outbox.create_delivery.side_effect = RuntimeError("boom")
        out = self.engine._queue_when_available({"session_id": "x",
            "text": "hi"}, "claude")
        self.assertIn("could not be saved", out["error"])


class SendNowOrQueueTest(NotifyBase):
    def test_unknown_provider_session(self):
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value=None):
            out = self.engine._send_now_or_queue({"session_id": "x"})
        self.assertIn("not live", out["error"])

    def test_target_blocked(self):
        with self.engine.lock:
            self.engine.snapshot_cache["sessions"] = [{"session_id": "x"}]
        self.engine.outbox._target_status.return_value = ("block", "no", None)
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"):
            out = self.engine._send_now_or_queue({"session_id": "x"})
        self.assertFalse(out["ok"])

    def test_queue_when_not_sendable(self):
        with self.engine.lock:
            self.engine.snapshot_cache["sessions"] = [
                {"session_id": "x", "state": "running", "provider": "claude",
                 "capabilities": {"submit": True}}]
        self.engine.outbox._target_status.return_value = ("ok", "waiting", None)
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "_queue_when_available",
                               return_value={"ok": True, "queued": True}) as q:
            out = self.engine._send_now_or_queue({"session_id": "x", "text": "hi"})
        self.assertTrue(out["queued"])
        q.assert_called_once()

    def test_send_now_success_and_queueable_fallback(self):
        with self.engine.lock:
            self.engine.snapshot_cache["sessions"] = [
                {"session_id": "x", "state": "idle", "provider": "claude",
                 "capabilities": {"submit": True}}]
        self.engine.outbox._target_status.return_value = ("ok", None, None)
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "act",
                               return_value={"ok": True}):
            out = self.engine._send_now_or_queue({"type": "text",
                "session_id": "x", "text": "hi"})
        self.assertEqual(out["delivery"], "sent_now")

        # act says queued -> return as-is
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "act",
                               return_value={"ok": True, "queued": True}):
            out = self.engine._send_now_or_queue({"type": "image_text",
                "session_id": "x", "text": "hi", "upload_ids": ["u"]})
        self.assertTrue(out["queued"])

        # act fails queueable -> queue fallback
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "act",
                               return_value={"ok": False, "queueable": True,
                                             "error": "later"}), \
             mock.patch.object(self.engine, "_queue_when_available",
                               return_value={"ok": True, "queued": True}) as q:
            out = self.engine._send_now_or_queue({"type": "text",
                "session_id": "x", "text": "hi"})
        self.assertTrue(out["queued"])
        q.assert_called_once()

        # act fails non-queueable -> return failure
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "act",
                               return_value={"ok": False, "error": "no"}):
            out = self.engine._send_now_or_queue({"type": "text",
                "session_id": "x", "text": "hi"})
        self.assertFalse(out["ok"])


class DismissThenSendTest(NotifyBase):
    def _snap(self, session):
        with self.engine.lock:
            self.engine.snapshot_cache["sessions"] = [session] if session else []

    def test_missing_nonce(self):
        out = self.engine._dismiss_question_then_send({"session_id": "x"})
        self.assertIn("nonce is required", out["error"])

    def test_not_live(self):
        self._snap(None)
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value=None):
            out = self.engine._dismiss_question_then_send({"session_id": "x",
                "nonce": "n"})
        self.assertIn("not live", out["error"])

    def test_pending_changed(self):
        self._snap({"session_id": "x",
                    "pending": {"kind": "question", "nonce": "other"}})
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"):
            out = self.engine._dismiss_question_then_send({"session_id": "x",
                "nonce": "n"})
        self.assertIn("changed", out["error"])

    def test_dismiss_failure_returned(self):
        self._snap({"session_id": "x",
                    "pending": {"kind": "question", "nonce": "n"}})
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "act",
                               return_value={"ok": False, "error": "no"}):
            out = self.engine._dismiss_question_then_send({"session_id": "x",
                "nonce": "n"})
        self.assertFalse(out["ok"])

    def test_queue_failure_returned(self):
        self._snap({"session_id": "x",
                    "pending": {"kind": "question", "nonce": "n"}})
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "act", return_value={"ok": True}), \
             mock.patch.object(self.engine, "_queue_when_available",
                               return_value={"ok": False, "error": "q"}):
            out = self.engine._dismiss_question_then_send({"session_id": "x",
                "nonce": "n", "text": "hi"})
        self.assertFalse(out["ok"])

    def test_success(self):
        self._snap({"session_id": "x",
                    "pending": {"kind": "question", "nonce": "n"}})
        with mock.patch.object(self.engine, "_known_message_provider",
                               return_value="claude"), \
             mock.patch.object(self.engine, "act", return_value={"ok": True}), \
             mock.patch.object(self.engine, "_queue_when_available",
                               return_value={"ok": True, "queued": True,
                                             "outbox_id": "o"}):
            out = self.engine._dismiss_question_then_send({"session_id": "x",
                "nonce": "n", "text": "hi"})
        self.assertTrue(out["dismissed"])
        self.assertEqual(out["dismissed_nonce"], "n")


class QueueCodexRecoveryTest(NotifyBase):
    def test_success_and_errors(self):
        self.engine.outbox.create_recovery.return_value = {"id": "r",
            "state": "waiting_provider"}
        out = self.engine._queue_codex_recovery({"session_id": "codex:x",
            "image_paths": ["/a.png", "/b.png"]})
        self.assertTrue(out["queued"])
        _, kwargs = self.engine.outbox.create_recovery.call_args
        self.assertIn("attached images", kwargs["message"])

        self.engine.outbox.create_recovery.side_effect = OutboxError("bad", code="c")
        out = self.engine._queue_codex_recovery({"session_id": "codex:x",
            "text": "hi"})
        self.assertEqual(out["code"], "c")

        self.engine.outbox.create_recovery.side_effect = RuntimeError("boom")
        out = self.engine._queue_codex_recovery({"session_id": "codex:x",
            "text": "hi"})
        self.assertIn("recovery queue", out["error"])


class WriteCodexTerminalTest(NotifyBase):
    def test_no_route(self):
        out = self.engine._write_codex_terminal({"type": "text"}, None)
        self.assertEqual(out["code"], "provider_control_unavailable")

    def test_unsupported_type(self):
        out = self.engine._write_codex_terminal({"type": "relay"},
                                                {"tty": "/dev/ttys1"})
        self.assertIn("unsupported", out["error"])

    def test_image_without_paths(self):
        out = self.engine._write_codex_terminal({"type": "image_text",
            "image_paths": []}, {"tty": "/dev/ttys1"})
        self.assertEqual(out["error"], "no images")

    def test_empty_text(self):
        out = self.engine._write_codex_terminal({"type": "text", "text": "  "},
                                                {"tty": "/dev/ttys1"})
        self.assertEqual(out["error"], "empty text")

    def test_text_success_with_slash_guard(self):
        with mock.patch.object(self.engine, "_iterm_write",
                               return_value={"ok": True}) as write:
            out = self.engine._write_codex_terminal({"type": "text",
                "text": "/status", "session_id": "codex:x"},
                {"tty": "/dev/ttys1"})
        self.assertEqual(out["transport"], "codex_terminal")
        self.assertTrue(out["accepted"])
        self.assertEqual(write.call_args[0][1][0][0], "/status ")

    def test_image_text_success(self):
        with mock.patch.object(self.engine, "_iterm_write",
                               return_value={"ok": True}):
            out = self.engine._write_codex_terminal({"type": "image_text",
                "text": "", "image_paths": ["/a.png"], "session_id": "codex:x"},
                {"tty": "/dev/ttys1"})
        self.assertTrue(out["ok"])


class OutboxSpawnTest(NotifyBase):
    def test_resume_session_codex_claude_unknown(self):
        self.engine.codex = SimpleNamespace(
            resume_owned_thread=lambda sid: {"ok": True, "session_id": sid})
        out = self.engine._outbox_spawn({"kind": "resume_session",
            "target_provider": "codex", "target_session_id": "codex:x"})
        self.assertTrue(out["ok"])

        with mock.patch.object(self.engine, "reopen_claude_session",
                               return_value={"ok": True, "session_id": "y"}):
            out = self.engine._outbox_spawn({"kind": "resume_session",
                "target_provider": "claude", "target_session_id": "y"})
        self.assertTrue(out["ok"])

        out = self.engine._outbox_spawn({"kind": "resume_session",
            "target_provider": "gemini", "target_session_id": "z"})
        self.assertFalse(out["ok"])

    def test_spawn_codex_and_claude(self):
        with mock.patch.object(self.engine, "spawn_codex_session",
                               return_value={"ok": True, "session_id": "codex:new"}):
            out = self.engine._outbox_spawn({"spawn_spec": {"provider": "codex"},
                "message": "hi"})
        self.assertEqual(out["provider"], "codex")
        self.assertTrue(out["message_delivered"])

        with mock.patch.object(self.engine, "spawn_session",
                               return_value={"ok": True, "session_id": "new",
                                             "trust_prompt": True}):
            out = self.engine._outbox_spawn({"spawn_spec": {"provider": "claude"},
                "destination_session_id": "resv"})
        self.assertEqual(out["provider"], "claude")
        self.assertTrue(out["trust_prompt"])


class RunOutboxAndResumeTest(NotifyBase):
    def test_run_outbox_ticks(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"provider_usage": {"claude": {}}}
        self.engine.run_outbox()
        self.engine.outbox.tick.assert_called_once()

    def test_resume_and_send_validation(self):
        self.assertIn("missing session",
                      self.engine.resume_and_send({"text": "hi",
                          "client_request_id": "req-12345"})["error"])
        self.assertIn("1", self.engine.resume_and_send({"session_id": "s",
            "text": "", "client_request_id": "req-12345"})["error"])
        self.assertIn("request ID", self.engine.resume_and_send({"session_id": "s",
            "text": "hi", "client_request_id": "short"})["error"])

    def test_resume_and_send_not_allowed(self):
        with mock.patch.object(self.engine, "closed_resume_capability",
                               return_value=(False, "no reopen")):
            out = self.engine.resume_and_send({"session_id": "s", "text": "hi",
                "client_request_id": "req-123456"})
        self.assertEqual(out["error"], "no reopen")

    def test_resume_and_send_success(self):
        self.engine.outbox.create_closed_resume.return_value = {"id": "o1"}
        self.engine.outbox.get.return_value = {"id": "o1", "state": "waiting"}
        with mock.patch.object(self.engine, "closed_resume_capability",
                               return_value=(True, None)), \
             mock.patch.object(self.engine, "run_outbox"):
            out = self.engine.resume_and_send({"session_id": "codex:s",
                "text": "hi", "client_request_id": "req-123456"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["queued"])

    def test_resume_and_send_failed_state(self):
        self.engine.outbox.create_closed_resume.return_value = {"id": "o1"}
        self.engine.outbox.get.return_value = {"id": "o1", "state": "failed",
            "error": "boom"}
        with mock.patch.object(self.engine, "closed_resume_capability",
                               return_value=(True, None)), \
             mock.patch.object(self.engine, "run_outbox"):
            out = self.engine.resume_and_send({"session_id": "s", "text": "hi",
                "client_request_id": "req-123456"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["queue_state"], "failed")

    def test_resume_and_send_outbox_error_and_generic(self):
        with mock.patch.object(self.engine, "closed_resume_capability",
                               return_value=(True, None)):
            self.engine.outbox.create_closed_resume.side_effect = \
                OutboxError("bad", code="c")
            out = self.engine.resume_and_send({"session_id": "s", "text": "hi",
                "client_request_id": "req-123456"})
            self.assertEqual(out["code"], "c")

            self.engine.outbox.create_closed_resume.side_effect = RuntimeError("boom")
            out = self.engine.resume_and_send({"session_id": "s", "text": "hi",
                "client_request_id": "req-123456"})
            self.assertIn("could not be queued", out["error"])


class OutboxCurrentTargetTest(NotifyBase):
    def test_builds_record_and_delegates(self):
        with self.engine.lock:
            self.engine.snapshot_cache = {"sessions": []}
        self.engine.outbox._target_status.return_value = ("ok", None, {})
        status, reason, extra = self.engine._outbox_current_target({
            "target_provider": "claude", "target_session_id": "x"})
        self.assertEqual(status, "ok")
        record = self.engine.outbox._target_status.call_args[0][0]
        self.assertEqual(record["target_session_id"], "x")
        self.assertLess(record["updated_at"], time.time())


if __name__ == "__main__":
    unittest.main()
