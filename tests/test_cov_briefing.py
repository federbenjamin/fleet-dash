"""Line-coverage tests for briefing.py, briefing_store.py, briefing_scheduler.py.

Complements tests/test_briefing.py (behavioural) by exercising the validation,
error, migration, and defensive branches that behavioural tests skip. No network,
no real ~/.claude: every test builds its own temp sqlite DB and deterministic
clock, mirroring test_briefing's fixtures (which are imported here directly).
"""
import base64
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from fleetdash.briefing import FleetOperations, OperationsError
from fleetdash.briefing_store import PUSHABLE_KINDS
from tests.test_briefing import Clock, session, fleet, action, push_subscription


class CovBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = os.path.join(self.tmp.name, "ledger.db")
        self.ops = FleetOperations(self.path, clock=self.clock,
                                   id_factory=lambda: "bud-fixed")
        self.workstream = lambda cwd: {"workstream_id": "ws-fleet"}

    def tearDown(self):
        self.tmp.cleanup()

    def sql(self, path=None):
        db = sqlite3.connect(path or self.path)
        db.row_factory = sqlite3.Row
        return db

    def qualify(self, device_id="phone", preferences=None):
        self.ops.notification_register_device(
            device_id, device_id.title(), "test", push_subscription(),
            preferences=preferences)
        delivery = self.ops.notification_create_test_delivery(device_id)
        self.ops.notification_claim_delivery()
        self.ops.notification_finish_delivery(delivery["id"], {"ok": True, "status": 201})
        return delivery

    def active_question_event(self, device_id="phone"):
        """Qualify a device and observe an action, returning the active event."""
        self.qualify(device_id)
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        return self.ops.notification_snapshot("desktop")["events"][0]


# --------------------------------------------------------------------------
# briefing.py — __init__, projection, observe, runtime
# --------------------------------------------------------------------------
class InitAndObserveTests(CovBase):
    def test_init_chmod_oserror_is_swallowed(self):
        # briefing.py 66-67: the final 0o600 chmod is best-effort. Patch ONLY
        # briefing.os.chmod (StoreOps._tighten uses briefing_store.os.chmod, so
        # _init_db still succeeds); the 2-arg call with no follow_symlinks raises.
        real = os.chmod

        def fake(path, mode, *a, **k):
            if "follow_symlinks" not in k:
                raise OSError("denied")
            return real(path, mode, *a, **k)

        path = os.path.join(self.tmp.name, "chmod.db")
        with mock.patch("fleetdash.briefing.os.chmod", side_effect=fake):
            ops = FleetOperations(path, clock=self.clock)
        self.assertTrue(os.path.exists(path))
        self.assertIsNotNone(ops)

    def test_projection_input_skips_and_flags_sessions(self):
        # 80: action valid kind but missing session_id -> spec None.
        # 107: session without session_id skipped.
        # 113-118 / 154-168: a stalled session drives the stall spec.
        # 155: a stalled session below the stall threshold is skipped in reconcile.
        stalled = session("stalled-1", group="working", state="stalled",
                          normalized_state="stalled", quiet_s=600)
        stalled_low = session("stalled-low", group="working", state="stalled",
                             normalized_state="stalled", quiet_s=5)
        current = fleet(self.clock, [stalled, stalled_low, session("s2")],
                        actions=[action(sid="", nonce="")])
        current["sessions"].append({"provider": "claude"})  # no session_id
        self.ops.observe(current, self.workstream)
        events = self.ops.notification_snapshot("desktop")["events"]
        kinds = {(item["kind"], item["session_id"]) for item in events}
        self.assertIn(("stall", "stalled-1"), kinds)
        self.assertNotIn(("stall", "stalled-low"), kinds)

    def test_stall_title_change_updates_existing_event(self):
        # briefing_store 571: an existing event upserted with a changed title.
        stalled = session("stall-x", group="working", state="stalled",
                          normalized_state="stalled", quiet_s=900, title="First")
        self.ops.observe(fleet(self.clock, [stalled]), self.workstream)
        self.clock.advance(2)
        # Same revision (convo_v) keeps the event_key; a new title forces update.
        stalled2 = session("stall-x", group="working", state="stalled",
                           normalized_state="stalled", quiet_s=900, title="Renamed")
        self.ops.observe(fleet(self.clock, [stalled2]), self.workstream)
        events = self.ops.notification_snapshot("desktop")["events"]
        stall = next(item for item in events if item["kind"] == "stall")
        self.assertEqual(stall["title"], "Renamed")

    def test_mute_removal_deletes_stored_mute(self):
        # briefing.py 181: a previously stored mute is removed when unmuted live.
        muted = session("m1", muted=True)
        self.ops.observe(fleet(self.clock, [muted]), self.workstream)
        self.clock.advance(2)
        unmuted = session("m1", muted=False, convo_v=2)
        self.ops.observe(fleet(self.clock, [unmuted]), self.workstream)
        with self.sql() as db:
            rows = db.execute(
                "SELECT COUNT(*) FROM notification_session_mutes").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_observe_skips_session_without_id_and_swallows_workstream_error(self):
        # 296: no session_id -> continue. 303-304: workstream_for raises -> None.
        def boom(cwd):
            raise RuntimeError("no workstream")

        current = fleet(self.clock, [session("w1", cwd="/tmp/x")])
        current["sessions"].append({"provider": "claude"})  # 296
        self.ops.observe(current, boom)  # 303-304
        with self.sql() as db:
            row = db.execute(
                "SELECT workstream_id FROM session_measurements WHERE session_id='w1'"
            ).fetchone()
        self.assertIsNone(row["workstream_id"])

    def test_measurement_signature_unchanged_short_circuits(self):
        # briefing.py 335: identical re-observe (runtime frozen) hits `continue`.
        frozen = session("frozen", started_ms=None, first_seen=None)
        f = fleet(self.clock, [frozen])
        self.ops.observe(f, self.workstream)
        self.ops.observe(f, self.workstream)  # signature identical -> continue
        with self.sql() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM session_measurements WHERE session_id='frozen'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_runtime_handles_bad_started_and_bad_closed(self):
        # 275-276: first_seen set, closed_at non-numeric -> None.
        bad_close = session("rc", started_ms=None, first_seen=900000,
                            closed_at="not-a-time", group="history",
                            normalized_state="closed", state="closed")
        snap = fleet(self.clock, [])
        snap["closed"] = [bad_close]
        self.ops.observe(snap, self.workstream)
        # 279-280: started_ms non-numeric -> None.
        bad_start = session("rs", started_ms="abc")
        self.ops.observe(fleet(self.clock, [bad_start]), self.workstream)
        with self.sql() as db:
            for sid in ("rc", "rs"):
                row = db.execute(
                    "SELECT runtime_seconds FROM session_measurements WHERE session_id=?",
                    (sid,)).fetchone()
                self.assertIsNone(row["runtime_seconds"])


# --------------------------------------------------------------------------
# briefing.py — budgets, forecasts, spawn
# --------------------------------------------------------------------------
class BudgetTests(CovBase):
    def test_budget_unknown_metric_branches_for_tokens_runtime_concurrency(self):
        # 539 tokens-None, 544 runtime-None, 546 concurrency.
        self.ops.replace_budgets([
            {"id": "tk", "scope_type": "fleet", "metric": "tokens", "limit_value": 5},
            {"id": "rt", "scope_type": "fleet", "metric": "runtime", "limit_value": 5},
            {"id": "cc", "scope_type": "fleet", "metric": "concurrency",
             "limit_value": 5},
        ])
        # A session with no tokens and no runtime (started/first_seen absent).
        blank = session("blank", started_ms=None, first_seen=None, total_tokens=None)
        self.ops.observe(fleet(self.clock, [blank]), self.workstream)
        budgets = {item["id"]: item for item in
                   self.ops.budgets_snapshot(fleet(self.clock, [blank]))["budgets"]}
        self.assertEqual(budgets["tk"]["unknown_sessions"], 1)
        self.assertEqual(budgets["rt"]["unknown_sessions"], 1)
        self.assertIsInstance(budgets["cc"]["value"], (int, float))

    def test_spawn_budgets_are_collected_for_matching_scope(self):
        # 585-591: budgets that apply to a spawn are returned in spawn_budgets.
        self.ops.replace_budgets([
            {"id": "fleetusd", "scope_type": "fleet", "metric": "usd",
             "limit_value": 100},
        ])
        current = fleet(self.clock, [session()])
        self.ops.observe(current, self.workstream)
        snap = self.ops.budgets_snapshot(current, {
            "provider": "claude", "model": "sonnet", "project": "fleet-dash",
            "workstream_id": "ws-fleet"})
        self.assertEqual([item["id"] for item in snap["spawn_budgets"]], ["fleetusd"])

    def test_spawn_forecast_unknown_provider_and_too_few_samples(self):
        # 632: provider outside claude/codex. 646: fewer than 2 rows.
        current = fleet(self.clock, [session()])
        self.ops.observe(current, self.workstream)
        other = self.ops.budgets_snapshot(current, {"provider": "gemini"})
        self.assertEqual(other["spawn_forecast"]["status"], "not_enough_history")
        few = self.ops.budgets_snapshot(current, {
            "provider": "claude", "model": "sonnet", "project": "fleet-dash"})
        self.assertEqual(few["spawn_forecast"]["status"], "not_enough_history")

    def test_replace_budgets_validation_branches(self):
        with self.assertRaisesRegex(OperationsError, "at most 100"):  # 662
            self.ops.replace_budgets("nope")
        with self.assertRaisesRegex(OperationsError, "must be an object"):  # 668
            self.ops.replace_budgets(["nope"])
        with self.assertRaisesRegex(OperationsError, "needs a target"):  # 678
            self.ops.replace_budgets([
                {"id": "p", "scope_type": "provider", "metric": "usd",
                 "limit_value": 1}])
        with self.assertRaisesRegex(OperationsError, "must be a number"):  # 684-685
            self.ops.replace_budgets([
                {"id": "b", "scope_type": "fleet", "metric": "usd",
                 "limit_value": "abc"}])
        with self.assertRaisesRegex(OperationsError, "greater than zero"):  # 687
            self.ops.replace_budgets([
                {"id": "b", "scope_type": "fleet", "metric": "usd",
                 "limit_value": 0}])
        with self.assertRaisesRegex(OperationsError, "duplicate"):  # 693
            self.ops.replace_budgets([
                {"id": "dup", "scope_type": "fleet", "metric": "usd",
                 "limit_value": 1},
                {"id": "dup", "scope_type": "fleet", "metric": "tokens",
                 "limit_value": 1}])
        with self.assertRaisesRegex(OperationsError, "label is too long"):  # 701
            self.ops.replace_budgets([
                {"id": "b", "scope_type": "fleet", "metric": "usd",
                 "limit_value": 1, "label": "x" * 161}])

    def test_spawn_blockers_resolves_workstream_from_cwd(self):
        # 726-729: cwd supplied without an explicit workstream id.
        self.ops.observe(fleet(self.clock, [session()]), self.workstream)
        blockers = self.ops.spawn_blockers(
            fleet(self.clock, [session()]), "claude", "/Users/example/fleet-dash")
        self.assertEqual(blockers, [])

    def test_has_spawn_limits(self):
        # briefing.py 741-744.
        self.assertFalse(self.ops.has_spawn_limits())
        self.ops.replace_budgets([
            {"id": "hard", "scope_type": "fleet", "metric": "usd",
             "limit_value": 1, "block_spawns": True}])
        self.assertTrue(self.ops.has_spawn_limits())


# --------------------------------------------------------------------------
# briefing.py — review / snapshot validation
# --------------------------------------------------------------------------
class SnapshotValidationTests(CovBase):
    def test_review_cursor_validation(self):
        with self.assertRaises(OperationsError):  # 752-753
            self.ops.review("dev", "abc")
        with self.assertRaises(OperationsError):  # 755
            self.ops.review("dev", -1)

    def test_briefing_snapshot_validation(self):
        with self.assertRaises(OperationsError):  # 770
            self.ops.briefing_snapshot(fleet(self.clock), "bad id!")
        with self.assertRaises(OperationsError):  # 773-774
            self.ops.briefing_snapshot(fleet(self.clock), "dev", None, "x")
        with self.assertRaises(OperationsError):  # 780-783
            self.ops.briefing_snapshot(fleet(self.clock), "dev", "abc")

    def test_notification_snapshot_validation(self):
        with self.assertRaises(OperationsError):  # 844
            self.ops.notification_snapshot("bad id!")
        with self.assertRaises(OperationsError):  # 848-849
            self.ops.notification_snapshot("dev", limit="x")
        with self.assertRaises(OperationsError):  # 851
            self.ops.notification_snapshot("dev", cursor=-1)
        with self.assertRaises(OperationsError):  # 855
            self.ops.notification_snapshot("dev", states=["bogus"])

    def test_notification_snapshot_event_id_and_filters(self):
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        event = self.ops.notification_snapshot("desktop")["events"][0]
        by_id = self.ops.notification_snapshot("desktop", event_id=event["id"])  # 874-876
        self.assertEqual(by_id["events"][0]["id"], event["id"])
        states = self.ops.notification_snapshot("desktop", states=["active"])  # 885-886
        self.assertTrue(states["events"])
        kinds = self.ops.notification_snapshot("desktop", kinds=["question"])  # 888-889
        self.assertTrue(kinds["events"])

    def test_notification_counts_returns_projection(self):
        # 947-948
        counts = self.ops.notification_counts("desktop")
        self.assertEqual(set(counts), {"unread", "active", "event_cursor"})

    def test_delivery_problem_survives_corrupt_event_payload(self):
        # briefing.py 935-936: a delivery problem whose event payload is corrupt.
        ops = FleetOperations(os.path.join(self.tmp.name, "corrupt.db"),
                              clock=self.clock, delivery_retry_delays=(0,),
                              delivery_jitter=lambda delay: delay)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        delivery = ops.notification_create_test_delivery("phone")
        ops.notification_claim_delivery()
        ops.notification_finish_delivery(delivery["id"], {"ok": False, "status": 503})
        ops.notification_claim_delivery()
        ops.notification_finish_delivery(delivery["id"], {"ok": False, "status": 503})
        with sqlite3.connect(os.path.join(self.tmp.name, "corrupt.db")) as db:
            db.execute("""UPDATE notification_events SET payload_json='not-json'
                WHERE source_type='push_test'""")
        problems = ops.notification_snapshot("phone")["delivery_problems"]
        self.assertTrue(problems)
        self.assertIn("can_retry", problems[0])


# --------------------------------------------------------------------------
# briefing.py — device management validation
# --------------------------------------------------------------------------
class DeviceValidationTests(CovBase):
    def test_register_device_validation(self):
        with self.assertRaises(OperationsError):  # 956
            self.ops.notification_register_device(
                "bad id!", "Name", "ios", push_subscription())
        with self.assertRaises(OperationsError):  # 961
            self.ops.notification_register_device(
                "dev", "Name", "ios", push_subscription(),
                permission_state="denied")
        with self.assertRaises(OperationsError):  # 965
            self.ops.notification_register_device(
                "dev", "Name", "ios", push_subscription(),
                endpoint_origin="https://wrong.example")

    def test_devices_snapshot_validation(self):
        with self.assertRaises(OperationsError):  # 1011
            self.ops.notification_devices_snapshot("bad id!")

    def test_update_device_validation(self):
        with self.assertRaises(OperationsError):  # 1027
            self.ops.notification_update_device("bad id!", display_name="x")
        self.ops.notification_register_device(
            "dev", "Name", "ios", push_subscription())
        with self.assertRaises(OperationsError):  # 1031
            self.ops.notification_update_device("dev", display_name="   ")
        with self.assertRaises(OperationsError):  # 1033
            self.ops.notification_update_device("dev", enabled="yes")
        with self.assertRaises(OperationsError):  # 1036
            self.ops.notification_update_device("dev")
        with self.assertRaises(OperationsError):  # 1052
            self.ops.notification_update_device("ghost", display_name="x")

    def test_remove_and_forget_device_validation(self):
        with self.assertRaises(OperationsError):  # 1071
            self.ops.notification_remove_device("bad id!")
        with self.assertRaises(OperationsError):  # 1078
            self.ops.notification_remove_device("ghost")
        with self.assertRaises(OperationsError):  # 1089
            self.ops.notification_forget_device("bad id!")

    def test_mark_read_validation(self):
        with self.assertRaises(OperationsError):  # 1109
            self.ops.notification_mark_read("bad id!", 0)
        with self.assertRaises(OperationsError):  # 1112-1113
            self.ops.notification_mark_read("dev", "abc")
        with self.assertRaises(OperationsError):  # 1115
            self.ops.notification_mark_read("dev", -1)


# --------------------------------------------------------------------------
# briefing.py — snooze / wake / mute / capability
# --------------------------------------------------------------------------
class SnoozeWakeMuteTests(CovBase):
    def test_snooze_validation(self):
        with self.assertRaises(OperationsError):  # 1137-1138
            self.ops.notification_snooze("evt", "rev", "abc")
        with self.assertRaises(OperationsError):  # 1141
            self.ops.notification_snooze("", "rev", self.clock() + 900)
        with self.assertRaises(OperationsError):  # 1147
            self.ops.notification_snooze("ghost", "rev", self.clock() + 900)

    def test_wake_validation(self):
        with self.assertRaises(OperationsError):  # 1161
            self.ops.notification_wake("", "rev")
        with self.assertRaises(OperationsError):  # 1168
            self.ops.notification_wake("ghost", "rev")

    def test_session_mute_validation(self):
        with self.assertRaises(OperationsError):  # 1177
            self.ops.notification_set_session_mute("")

    def test_capability_validation(self):
        with self.assertRaises(OperationsError):  # 1203
            self.ops.notification_consume_capability("not-a-dict")
        with self.assertRaises(OperationsError):  # 1210-1211
            self.ops.notification_consume_capability(
                {"event_id": "e", "device_id": "phone", "action": "mute",
                 "jti_hash": "a" * 64, "expires_at": "abc"})
        with self.assertRaises(OperationsError):  # 1216
            self.ops.notification_consume_capability(
                {"event_id": "e", "device_id": "phone", "action": "bogus",
                 "jti_hash": "a" * 64, "expires_at": self.clock() + 600})

    def test_capability_snooze_and_mute_callback(self):
        # 1237-1258: snooze action and the mute path with a callback.
        event = self.active_question_event("phone")
        snooze_claim = {"event_id": event["id"], "device_id": "phone",
                        "action": "snooze", "jti_hash": "b" * 64,
                        "expires_at": self.clock() + 600}
        result = self.ops.notification_consume_capability(snooze_claim)
        self.assertEqual(result["action"], "snooze")
        # Re-activate the event so the mute capability finds an active event.
        self.ops.notification_wake(event["id"], event["source_revision"])
        seen = []
        mute_claim = {"event_id": event["id"], "device_id": "phone",
                      "action": "mute", "jti_hash": "c" * 64,
                      "expires_at": self.clock() + 600}
        result = self.ops.notification_consume_capability(
            mute_claim, mute_callback=seen.append)  # 1249
        self.assertEqual(result["action"], "mute")
        self.assertEqual(seen, [event["session_id"]])


# --------------------------------------------------------------------------
# briefing.py — legacy / quiet digest / status
# --------------------------------------------------------------------------
class LegacyAndDigestTests(CovBase):
    def test_legacy_notification_diagnostics(self):
        # 1261-1267
        diag = self.ops.legacy_notification_diagnostics()
        self.assertFalse(diag["automatic"])
        self.assertIn("statuses", diag)

    def test_notification_status_rejects_bad_status(self):
        with self.assertRaises(OperationsError):  # 1288
            self.ops.notification_status("k", "bogus")

    def test_quiet_digest_reports_outcomes_and_attention(self):
        # 1314, 1316: outcome + attention categories in the digest.
        with self.sql() as db:
            db.execute("""CREATE TABLE repo_actions(id INTEGER PRIMARY KEY,
                action_id TEXT, kind TEXT, root TEXT, status TEXT, summary TEXT,
                error TEXT, finished_at REAL)""")
            db.execute("""INSERT INTO repo_actions VALUES(
                1,'r1','git_push','/repo','succeeded','pushed',NULL,?)""",
                (self.clock(),))
            db.execute("""CREATE TABLE outbox_messages(id TEXT PRIMARY KEY,
                state TEXT, updated_at REAL, target_provider TEXT,
                destination_session_id TEXT, error TEXT, blocked_reason TEXT)""")
            db.execute("""INSERT INTO outbox_messages VALUES(
                'o1','failed',?,'codex','codex:t','send failed',NULL)""",
                (self.clock(),))
            db.commit()
        start = self.clock() - 1
        self.ops.observe(fleet(self.clock), self.workstream)
        digest = self.ops.quiet_digest(start)
        self.assertIn("outcomes", digest)
        self.assertIn("need review", digest)


# --------------------------------------------------------------------------
# briefing_store.py — helpers, validation, migrations
# --------------------------------------------------------------------------
class StoreHelperTests(CovBase):
    def test_diagnostics_percentiles(self):
        # 110-116: exercise the percentile path (non-empty samples).
        self.ops.observe(fleet(self.clock, [session()]), self.workstream)
        diag = self.ops.diagnostics()
        self.assertIn("connect_p95_ms", diag)
        self.assertGreaterEqual(diag["samples"], 1)

    def test_diagnostics_empty_percentile_returns_zero(self):
        # briefing_store 113: percentile of an empty sample series -> 0.0.
        fresh = FleetOperations(os.path.join(self.tmp.name, "fresh.db"),
                                clock=self.clock)
        diag = fresh.diagnostics()
        self.assertEqual(diag["notification_projection_p95_ms"], 0.0)
        self.assertEqual(diag["notification_enqueue_p95_ms"], 0.0)

    def test_base64url_rejects_bad_charset_and_length(self):
        with self.assertRaises(OperationsError):  # 322
            FleetOperations._base64url("!!!not", "auth key", 16)
        short = base64.urlsafe_b64encode(b"a" * 10).decode().rstrip("=")
        with self.assertRaises(OperationsError):  # 328
            FleetOperations._base64url(short, "auth key", 16)

    def test_push_subscription_branches(self):
        enc = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        with self.assertRaises(OperationsError):  # 334
            FleetOperations._push_subscription("not-a-dict")
        with self.assertRaises(OperationsError):  # 342-343 (bad port)
            FleetOperations._push_subscription(
                {"endpoint": "https://fcm.googleapis.com:999999/x"})
        # 362-363: an unusable extra origin is skipped, then origin still rejected.
        with self.assertRaises(OperationsError):
            FleetOperations._push_subscription(
                push_subscription("https://not-allowed.example/x"),
                extra_origins=["https://bad:999999/x"])
        bad_keys = push_subscription()
        bad_keys["keys"] = "nope"
        with self.assertRaises(OperationsError):  # 373
            FleetOperations._push_subscription(bad_keys)
        bad_point = push_subscription()
        bad_point["keys"]["p256dh"] = enc(b"\x05" + b"p" * 64)
        with self.assertRaises(OperationsError):  # 380
            FleetOperations._push_subscription(bad_point)
        bad_exp = push_subscription()
        bad_exp["expirationTime"] = "later"
        with self.assertRaises(OperationsError):  # 384-386
            FleetOperations._push_subscription(bad_exp)

    def test_device_preferences_validation(self):
        with self.assertRaises(OperationsError):  # 398
            FleetOperations._device_preferences("nope")
        with self.assertRaises(OperationsError):  # 401
            FleetOperations._device_preferences({"unknown": 1})
        with self.assertRaises(OperationsError):  # 406
            FleetOperations._device_preferences({"kinds": "nope"})
        with self.assertRaises(OperationsError):  # 410
            FleetOperations._device_preferences({"kinds": ["bogus"]})
        with self.assertRaises(OperationsError):  # 417
            FleetOperations._device_preferences({"minimum_severity": "loud"})
        with self.assertRaises(OperationsError):  # 423
            FleetOperations._device_preferences({"initial_delay_seconds": 99999})
        # Dedupe path keeps a single copy of a repeated kind.
        prefs = FleetOperations._device_preferences(
            {"kinds": ["question", "question"], "initial_delay_seconds": 5})
        self.assertEqual(prefs["kinds"], ["question"])

    def test_notification_device_health_variants(self):
        base = {"id": "d", "display_name": "D", "platform": "ios",
                "enabled": 1, "permission_state": "granted",
                "test_success_at": None, "preferences_json": "{}",
                "last_success_at": None, "last_failure_at": None}
        # 441: enabled but permission not granted -> permission label as health.
        denied = FleetOperations._notification_device(
            {**base, "permission_state": "denied"})
        self.assertEqual(denied["health"], "denied")
        # 445: tested, last_failure after last_success -> failing.
        failing = FleetOperations._notification_device(
            {**base, "test_success_at": 10, "last_success_at": 1,
             "last_failure_at": 5})
        self.assertEqual(failing["health"], "failing")
        # 449: tested, no failure, no success -> registered (final else).
        registered = FleetOperations._notification_device(
            {**base, "test_success_at": 10, "last_success_at": 0,
             "last_failure_at": 0})
        self.assertEqual(registered["health"], "registered")
        # 434-435: corrupt preferences_json -> {}.
        corrupt = FleetOperations._notification_device(
            {**base, "preferences_json": "not-json"})
        self.assertEqual(corrupt["preferences"], {})

    def test_event_and_notification_event_corrupt_payload(self):
        # 478-479 and 530-531: corrupt payload_json defaults to {}.
        self.assertEqual(
            FleetOperations._event({"muted": 0, "payload_json": "bad"})["payload"], {})
        self.assertEqual(
            FleetOperations._notification_event({"payload_json": "bad"})["payload"], {})

    def test_meta_ignores_corrupt_json(self):
        # briefing_store 508-509.
        with self.ops._transaction() as db:
            db.execute("INSERT INTO operations_meta(key,value_json) VALUES('k','bad')")
            self.assertEqual(self.ops._meta(db, "k", "fallback"), "fallback")

    def test_insert_event_rejects_bad_category(self):
        # briefing_store 487.
        with self.ops._transaction() as db:
            with self.assertRaises(OperationsError):
                self.ops._insert_event(db, key="k", category="nope",
                                       severity="info", title="t", summary="s")

    def test_upsert_notification_event_validation(self):
        with self.ops._transaction() as db:
            with self.assertRaises(OperationsError):  # 540
                self.ops._upsert_notification_event(db, {"event_key": ""}, self.clock())
            with self.assertRaises(OperationsError):  # 544
                self.ops._upsert_notification_event(
                    db, {"event_key": "k", "kind": "bogus", "state": "active"},
                    self.clock())
            with self.assertRaises(OperationsError):  # 548
                self.ops._upsert_notification_event(
                    db, {"event_key": "k", "kind": "question", "state": "active",
                         "severity": "loud"}, self.clock())

    def test_upsert_notification_event_empty_event_id(self):
        # briefing_store 590: an empty event_id factory fails the insert.
        ops = FleetOperations(os.path.join(self.tmp.name, "noid.db"),
                              clock=self.clock, event_id_factory=lambda: "")
        with self.assertRaises(OperationsError):
            ops.observe(fleet(self.clock, actions=[action()]), self.workstream)

    def test_migration_upgrades_kind_policy_and_device_columns(self):
        # briefing_store 246-254, 268: pre-existing old-schema tables get ALTERed.
        path = os.path.join(self.tmp.name, "old.db")
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE notification_kind_policy(
                kind TEXT PRIMARY KEY, mode TEXT NOT NULL, minimum_severity TEXT NOT NULL,
                initial_delay_seconds INTEGER NOT NULL, repeat_interval_seconds INTEGER NOT NULL,
                max_deliveries INTEGER NOT NULL, allow_during_quiet_hours INTEGER NOT NULL,
                effective_after REAL NOT NULL DEFAULT 0, revision INTEGER NOT NULL,
                updated_at REAL NOT NULL)""")
            db.execute("""INSERT INTO notification_kind_policy VALUES(
                'question','remind_once','info',0,900,2,0,0,3,1)""")
            db.execute("""CREATE TABLE notification_devices(
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL, platform TEXT,
                subscription_json TEXT NOT NULL, endpoint_origin TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, permission_state TEXT NOT NULL,
                created_at REAL NOT NULL, last_registered_at REAL NOT NULL,
                last_success_at REAL, last_failure_at REAL, last_failure TEXT,
                read_cursor INTEGER NOT NULL DEFAULT 0,
                preferences_json TEXT NOT NULL DEFAULT '{}')""")
            db.execute("""INSERT INTO notification_devices(
                id,display_name,platform,subscription_json,endpoint_origin,enabled,
                permission_state,created_at,last_registered_at) VALUES(
                'd','D','ios','{}','',1,'granted',1,1)""")
        FleetOperations(path, clock=self.clock)
        with sqlite3.connect(path) as db:
            kind_cols = {row[1] for row in
                         db.execute("PRAGMA table_info(notification_kind_policy)")}
            dev_cols = {row[1] for row in
                        db.execute("PRAGMA table_info(notification_devices)")}
            push_rev = db.execute(
                "SELECT push_revision,revision FROM notification_kind_policy "
                "WHERE kind='question'").fetchone()
        self.assertTrue({"in_app_enabled", "in_app_effective_after",
                         "push_revision"}.issubset(kind_cols))
        self.assertIn("test_success_at", dev_cols)
        self.assertEqual(push_rev[0], push_rev[1])  # 254: push_revision=revision


# --------------------------------------------------------------------------
# briefing_scheduler.py — quiet hours, policy update, delivery internals
# --------------------------------------------------------------------------
class SchedulerTests(CovBase):
    def test_quiet_state_bad_timezone_falls_back_to_utc(self):
        # briefing_scheduler 51-52.
        active, _ = self.ops._quiet_state(
            {"quiet_hours_enabled": True, "quiet_start_minute": 0,
             "quiet_end_minute": 0, "timezone": "Not/AZone"}, self.clock())
        self.assertTrue(active)

    def test_quiet_state_overnight_after_start_advances_end_day(self):
        # briefing_scheduler 67: overnight window, current minute >= start.
        # 23:00 UTC, quiet 22:00->06:00 -> active, end rolls to next day.
        now = 1_000_000  # arbitrary; compute a 23:00 UTC instant below
        from datetime import datetime, timezone
        now = datetime(2026, 3, 10, 23, 0, tzinfo=timezone.utc).timestamp()
        active, end = self.ops._quiet_state(
            {"quiet_hours_enabled": True, "quiet_start_minute": 22 * 60,
             "quiet_end_minute": 6 * 60, "timezone": "UTC"}, now)
        self.assertTrue(active)
        self.assertGreater(end, now)

    def test_policy_update_validation_global(self):
        rev = self.ops.notification_policy_snapshot()["global"]["revision"]
        with self.assertRaises(OperationsError):  # 104
            self.ops.notification_policy_update("nope")
        with self.assertRaises(OperationsError):  # 108
            self.ops.notification_policy_update({"scope": "global", "patch": {}})
        with self.assertRaises(OperationsError):  # 111-112
            self.ops.notification_policy_update(
                {"scope": "global", "patch": {"enabled": True}})
        with self.assertRaises(OperationsError):  # 119
            self.ops.notification_policy_update(
                {"scope": "global", "expected_revision": rev,
                 "patch": {"bogus": 1}})
        with self.assertRaises(OperationsError):  # 123
            self.ops.notification_policy_update(
                {"scope": "global", "expected_revision": rev + 9,
                 "patch": {"enabled": True}})
        with self.assertRaises(OperationsError):  # 128
            self.ops.notification_policy_update(
                {"scope": "global", "expected_revision": rev,
                 "patch": {"enabled": "maybe"}})
        with self.assertRaises(OperationsError):  # 132
            self.ops.notification_policy_update(
                {"scope": "global", "expected_revision": rev,
                 "patch": {"quiet_start_minute": 5000}})
        with self.assertRaises(OperationsError):  # 135-136
            self.ops.notification_policy_update(
                {"scope": "global", "expected_revision": rev,
                 "patch": {"timezone": "Not/AZone"}})

    def test_policy_update_validation_kind(self):
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        rev = rule["revision"]
        with self.assertRaises(OperationsError):  # 153
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "bogus", "expected_revision": rev,
                 "patch": {"mode": "once"}})
        with self.assertRaises(OperationsError):  # 158
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "question", "expected_revision": rev,
                 "patch": {"bogus": 1}})
        with self.assertRaises(OperationsError):  # 167
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "question", "expected_revision": rev,
                 "patch": {"in_app_enabled": "maybe"}})
        with self.assertRaises(OperationsError):  # 169
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "question", "expected_revision": rev,
                 "patch": {"mode": "sometimes"}})
        with self.assertRaises(OperationsError):  # 171
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "question", "expected_revision": rev,
                 "patch": {"minimum_severity": "loud"}})
        with self.assertRaises(OperationsError):  # 179
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "question", "expected_revision": rev,
                 "patch": {"max_deliveries": 0}})
        with self.assertRaises(OperationsError):  # 182
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "question", "expected_revision": rev,
                 "patch": {"allow_during_quiet_hours": "maybe"}})
        with self.assertRaises(OperationsError):  # 220
            self.ops.notification_policy_update(
                {"scope": "bogus", "expected_revision": rev, "patch": {"mode": "off"}})

    def test_kind_policy_revision_mismatch(self):
        # 167 alt path: wrong expected revision for a kind rule.
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        with self.assertRaisesRegex(OperationsError, "changed"):
            self.ops.notification_policy_update(
                {"scope": "kind", "kind": "question",
                 "expected_revision": rule["revision"] + 9,
                 "patch": {"mode": "off"}})

    def test_push_preferences_allow_direct(self):
        # briefing_scheduler 225-234.
        allow = FleetOperations._push_preferences_allow(
            {"kind": "question", "severity": "critical"},
            {"preferences_json": '{"kinds":["question"],"minimum_severity":"warning"}'})
        self.assertTrue(allow)
        deny_kind = FleetOperations._push_preferences_allow(
            {"kind": "reply", "severity": "critical"},
            {"preferences_json": '{"kinds":["question"]}'})
        self.assertFalse(deny_kind)
        corrupt = FleetOperations._push_preferences_allow(
            {"kind": "question", "severity": "info"},
            {"preferences_json": "not-json"})
        self.assertFalse(corrupt)

    def test_policy_delivery_insert_guards(self):
        # 240 bad purpose; 253 queue full; 256 empty delivery id.
        with self.ops._transaction(immediate=True) as db:
            with self.assertRaises(OperationsError):  # 240
                self.ops._policy_delivery_insert(
                    db, {"id": "e", "source_revision": "r"}, {"id": "d"},
                    "bogus", self.clock(), self.clock())

        limited = FleetOperations(os.path.join(self.tmp.name, "lim.db"),
                                  clock=self.clock, delivery_queue_limit=1)
        limited.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        limited.notification_create_test_delivery("phone")  # queue now full (1)
        with limited._transaction(immediate=True) as db:
            self.assertFalse(limited._policy_delivery_insert(  # 253
                db, {"id": "nope", "source_revision": "r"}, {"id": "phone"},
                "reminder", self.clock(), self.clock()))

        emptyid = FleetOperations(os.path.join(self.tmp.name, "eid.db"),
                                  clock=self.clock, delivery_id_factory=lambda: "")
        with emptyid._transaction(immediate=True) as db:
            self.assertFalse(emptyid._policy_delivery_insert(  # 256
                db, {"id": "e", "source_revision": "r"}, {"id": "d"},
                "reminder", self.clock(), self.clock()))

    def test_enqueue_notification_delivery_guards(self):
        # 407, 420, 422, 425-426, 428, 431, 438, 456.
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        test_delivery = self.ops.notification_create_test_delivery("phone")
        event_id = test_delivery["event_id"]
        now = self.clock()
        with self.ops._transaction(immediate=True) as db:
            with self.assertRaises(OperationsError):  # 407
                self.ops._enqueue_notification_delivery(
                    db, event_id, "phone", now, purpose="bogus")
            with self.assertRaises(OperationsError):  # 420
                self.ops._enqueue_notification_delivery(
                    db, "ghost-event", "phone", now)
            with self.assertRaises(OperationsError):  # 422
                self.ops._enqueue_notification_delivery(
                    db, event_id, "ghost-device", now)
        # 425-426 + 428: corrupt payload -> {} -> stale (push_test event resolved).
        with self.sql() as db:
            db.execute("UPDATE notification_events SET payload_json='bad' WHERE id=?",
                       (event_id,))
            db.commit()
        with self.ops._transaction(immediate=True) as db:
            with self.assertRaises(OperationsError):
                self.ops._enqueue_notification_delivery(db, event_id, "phone", now)
        with self.assertRaises(OperationsError):  # 456
            self.ops.notification_enqueue_delivery("", "phone")

    def test_enqueue_stale_and_disabled_device(self):
        # 428 (resolved non-test event) and 431 (disabled device). Use a device
        # with no prior delivery for this event so _enqueue reaches the checks
        # rather than short-circuiting on an existing row.
        event = self.active_question_event("phone")
        self.ops.notification_register_device(
            "other", "Other", "iOS", push_subscription())
        now = self.clock()
        # Resolve the question event so it is neither active nor push_test.
        with self.sql() as db:
            db.execute("UPDATE notification_events SET state='resolved' WHERE id=?",
                       (event["id"],))
            db.commit()
        with self.ops._transaction(immediate=True) as db:
            with self.assertRaises(OperationsError):  # 428
                self.ops._enqueue_notification_delivery(db, event["id"], "other", now)
        # Re-activate the event but disable the device.
        with self.sql() as db:
            db.execute("UPDATE notification_events SET state='active' WHERE id=?",
                       (event["id"],))
            db.execute("UPDATE notification_devices SET enabled=0 WHERE id='other'")
            db.commit()
        with self.ops._transaction(immediate=True) as db:
            with self.assertRaises(OperationsError):  # 431
                self.ops._enqueue_notification_delivery(db, event["id"], "other", now)

    def test_enqueue_empty_delivery_id(self):
        # 438: empty delivery id factory on an active event + enabled device.
        ops = FleetOperations(os.path.join(self.tmp.name, "eq.db"), clock=self.clock)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        ops.notification_create_test_delivery("phone")
        ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        event = ops.notification_snapshot("desktop")["events"][0]
        ops.delivery_id_factory = lambda: ""
        now = self.clock()
        with ops._transaction(immediate=True) as db:
            with self.assertRaises(OperationsError):
                ops._enqueue_notification_delivery(db, event["id"], "phone", now)

    def test_create_test_delivery_guards(self):
        with self.assertRaises(OperationsError):  # 384
            self.ops.notification_create_test_delivery("bad id!")
        ops = FleetOperations(os.path.join(self.tmp.name, "ct.db"), clock=self.clock)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        ops.delivery_id_factory = lambda: ""
        with self.assertRaises(OperationsError):  # 389
            ops.notification_create_test_delivery("phone")

    def test_delivery_status_unknown_returns_none(self):
        # 371: _notification_delivery(None).
        self.assertIsNone(self.ops.notification_delivery_status("ghost"))

    def test_schedule_global_disabled_suppresses(self):
        # briefing_scheduler 289-293.
        self.qualify("phone")
        policy = self.ops.notification_policy_snapshot()["global"]
        self.ops.notification_policy_update(
            {"scope": "global", "expected_revision": policy["revision"],
             "patch": {"enabled": False}})
        # A fresh observe re-runs _schedule with the global policy disabled.
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())

    def test_schedule_skips_events_at_or_below_read_cursor(self):
        # briefing_scheduler 321.
        self.qualify("phone")
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        cursor = self.ops.notification_snapshot("phone")["event_cursor"]
        self.ops.notification_mark_read("phone", cursor + 5)
        self.clock.advance(1)
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())

    def test_finish_delivery_validation(self):
        # 589, 596-597, 604.
        with self.assertRaises(OperationsError):  # 589
            self.ops.notification_finish_delivery("d", "not-a-dict")
        with self.assertRaises(OperationsError):  # 596-597
            self.ops.notification_finish_delivery("d", {"status": "abc"})
        with self.assertRaises(OperationsError):  # 604
            self.ops.notification_finish_delivery("ghost", {"ok": True, "status": 201})

    def test_finish_delivery_corrupt_event_payload(self):
        # briefing_scheduler 610-611: a sending delivery whose event payload is bad.
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        delivery = self.ops.notification_create_test_delivery("phone")
        self.ops.notification_claim_delivery()
        with self.sql() as db:
            db.execute("UPDATE notification_events SET payload_json='bad' "
                       "WHERE source_type='push_test'")
            db.commit()
        result = self.ops.notification_finish_delivery(
            delivery["id"], {"ok": True, "status": 201})
        self.assertEqual(result["status"], "sent")

    def test_retry_delivery_validation(self):
        # 681: not retryable (queued, not failed).
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        queued = self.ops.notification_create_test_delivery("phone")
        with self.assertRaises(OperationsError):
            self.ops.notification_retry_delivery(queued["id"])

    def test_retry_delivery_stale_event(self):
        # 684-685 + 687: failed delivery whose event payload is corrupt / resolved.
        ops = FleetOperations(os.path.join(self.tmp.name, "retry.db"),
                              clock=self.clock)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        delivery = ops.notification_create_test_delivery("phone")
        ops.notification_claim_delivery()
        # A non-retryable HTTP 400 fails immediately regardless of retry budget.
        failed = ops.notification_finish_delivery(
            delivery["id"], {"ok": False, "status": 400})
        self.assertEqual(failed["status"], "failed")
        with sqlite3.connect(os.path.join(self.tmp.name, "retry.db")) as db:
            db.execute("UPDATE notification_events SET payload_json='bad',"
                       "state='resolved' WHERE source_type='push_test'")
        with self.assertRaises(OperationsError):
            ops.notification_retry_delivery(delivery["id"])

    def test_retry_delivery_queue_full_and_empty_id(self):
        # 695 (queue full) and 701 (empty new id).
        ops = FleetOperations(os.path.join(self.tmp.name, "rf.db"),
                              clock=self.clock, delivery_queue_limit=1)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        delivery = ops.notification_create_test_delivery("phone")
        ops.notification_claim_delivery()
        ops.notification_finish_delivery(delivery["id"], {"ok": False, "status": 400})
        # Fill the queue with a dummy row so retry sees it as full.
        now = self.clock()
        with sqlite3.connect(os.path.join(self.tmp.name, "rf.db")) as db:
            db.execute("""INSERT INTO notification_deliveries(
                id,event_id,device_id,generation,purpose,source_revision,status,
                attempt,next_attempt_at,created_at,updated_at)
                VALUES('filler','e2','phone',9,'initial','r','queued',0,?,?,?)""",
                (now, now, now))
        with self.assertRaisesRegex(OperationsError, "queue is full"):  # 695
            ops.notification_retry_delivery(delivery["id"])
        # Now clear the queue and force an empty id for the 701 branch.
        with sqlite3.connect(os.path.join(self.tmp.name, "rf.db")) as db:
            db.execute("DELETE FROM notification_deliveries WHERE id='filler'")
        ops.delivery_id_factory = lambda: ""
        with self.assertRaises(OperationsError):  # 701
            ops.notification_retry_delivery(delivery["id"])


# --------------------------------------------------------------------------
# briefing_scheduler.py — claim terminal branches
# --------------------------------------------------------------------------
class ClaimTerminalTests(CovBase):
    def _queued_test_delivery(self, path_name):
        path = os.path.join(self.tmp.name, path_name)
        ops = FleetOperations(path, clock=self.clock)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        delivery = ops.notification_create_test_delivery("phone")
        return ops, path, delivery

    def test_claim_failed_when_event_missing(self):
        # 491-492: LEFT JOIN yields NULL event_state -> failed.
        ops, path, delivery = self._queued_test_delivery("cm1.db")
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM notification_events")
        self.assertIsNone(ops.notification_claim_delivery())
        self.assertEqual(ops.notification_delivery_status(delivery["id"])["status"],
                         "failed")

    def test_claim_failed_on_bad_purpose(self):
        # 493-494.
        ops, path, delivery = self._queued_test_delivery("cm2.db")
        with sqlite3.connect(path) as db:
            db.execute("UPDATE notification_deliveries SET purpose='bogus' WHERE id=?",
                       (delivery["id"],))
        self.assertIsNone(ops.notification_claim_delivery())
        self.assertEqual(ops.notification_delivery_status(delivery["id"])["status"],
                         "failed")

    def test_claim_suppressed_on_revision_mismatch(self):
        # 495-496.
        ops, path, delivery = self._queued_test_delivery("cm3.db")
        with sqlite3.connect(path) as db:
            db.execute("UPDATE notification_events SET source_revision='changed' "
                       "WHERE source_type='push_test'")
        self.assertIsNone(ops.notification_claim_delivery())
        self.assertEqual(ops.notification_delivery_status(delivery["id"])["status"],
                         "suppressed")

    def test_claim_suppressed_when_device_disabled(self):
        # 497-498.
        ops, path, delivery = self._queued_test_delivery("cm4.db")
        with sqlite3.connect(path) as db:
            db.execute("UPDATE notification_devices SET enabled=0 WHERE id='phone'")
        self.assertIsNone(ops.notification_claim_delivery())
        self.assertEqual(ops.notification_delivery_status(delivery["id"])["status"],
                         "suppressed")

    def test_claim_subscription_expired_scrubs_device(self):
        # 499-500 + 544-546.
        ops, path, delivery = self._queued_test_delivery("cm5.db")
        with sqlite3.connect(path) as db:
            db.execute("UPDATE notification_devices SET subscription_json='{}' "
                       "WHERE id='phone'")
        self.assertIsNone(ops.notification_claim_delivery())
        self.assertEqual(ops.notification_delivery_status(delivery["id"])["status"],
                         "subscription_expired")

    def test_claim_suppressed_when_event_resolved_non_informational(self):
        # 501-505: a non-informational event that is resolved (not push_test).
        self.qualify("phone")
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        with self.sql() as db:
            row = db.execute("""SELECT id FROM notification_deliveries
                WHERE purpose='initial'""").fetchone()
            delivery_id = row["id"]
            db.execute("UPDATE notification_events SET state='resolved' WHERE kind='question'")
            db.commit()
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.assertEqual(
            self.ops.notification_delivery_status(delivery_id)["status"], "suppressed")

    def test_claim_suppressed_when_sequence_below_read_cursor(self):
        # 506-509.
        self.qualify("phone")
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        with self.sql() as db:
            delivery_id = db.execute("""SELECT id FROM notification_deliveries
                WHERE purpose='initial'""").fetchone()["id"]
            db.execute("UPDATE notification_devices SET read_cursor=9999 WHERE id='phone'")
            db.commit()
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.assertEqual(
            self.ops.notification_delivery_status(delivery_id)["status"], "suppressed")

    def test_claim_suppressed_for_delivery_event_of_same_device(self):
        # 510-512: a delivery-type event whose source_id is the target device.
        self.qualify("phone")
        now = self.clock()
        with self.sql() as db:
            db.execute("""INSERT INTO notification_events(
                id,sequence,event_key,kind,state,severity,title,summary,provider,
                session_id,workstream_id,source_type,source_id,source_revision,
                opened_at,changed_at,resolved_at,snoozed_until,reminder_budget,
                last_push_at,payload_json) VALUES(
                'devt',999,'devt-key','failure','active','warning','t','s',NULL,
                NULL,NULL,'delivery','phone','rev',?,?,NULL,NULL,0,NULL,'{}')""",
                (now, now))
            db.execute("""INSERT INTO notification_deliveries(
                id,event_id,device_id,generation,purpose,source_revision,status,
                attempt,next_attempt_at,created_at,updated_at)
                VALUES('devd','devt','phone',1,'initial','rev','queued',0,?,?,?)""",
                (now, now, now))
            db.commit()
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.assertEqual(
            self.ops.notification_delivery_status("devd")["status"], "suppressed")

    def test_claim_suppressed_when_session_muted(self):
        # 513-516.
        self.qualify("phone")
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        with self.sql() as db:
            delivery_id = db.execute("""SELECT id FROM notification_deliveries
                WHERE purpose='initial'""").fetchone()["id"]
            db.execute("""INSERT INTO notification_session_mutes(
                session_id,provider,muted_at) VALUES('s1','claude',0)""")
            db.commit()
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.assertEqual(
            self.ops.notification_delivery_status(delivery_id)["status"], "suppressed")

    def test_claim_holds_for_quiet_hours(self):
        # 517-534 + 536-539: a policy-scheduled job held for quiet hours.
        self.qualify("phone")
        # Enable quiet hours across the whole clock (start==end -> always quiet).
        policy = self.ops.notification_policy_snapshot()["global"]
        self.ops.notification_policy_update(
            {"scope": "global", "expected_revision": policy["revision"],
             "patch": {"quiet_hours_enabled": True, "quiet_start_minute": 0,
                       "quiet_end_minute": 0, "timezone": "UTC"}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        # The scheduler already pushed the job past quiet end; force it due now and
        # re-claim so the claim-time quiet-hours hold path executes.
        with self.sql() as db:
            db.execute("""UPDATE notification_deliveries SET next_attempt_at=?
                WHERE purpose='initial'""", (self.clock(),))
            db.commit()
        self.assertIsNone(self.ops.notification_claim_delivery())
        with self.sql() as db:
            status, error = db.execute("""SELECT status,error FROM notification_deliveries
                WHERE purpose='initial'""").fetchone()
        self.assertEqual(status, "queued")
        self.assertEqual(error, "held for quiet hours")

    def test_claim_tolerates_corrupt_event_payload(self):
        # briefing_scheduler 487-488: a claimable row whose event payload is bad.
        ops, path, delivery = self._queued_test_delivery("cm7.db")
        with sqlite3.connect(path) as db:
            db.execute("UPDATE notification_events SET payload_json='bad' "
                       "WHERE source_type='push_test'")
        # An untested device with a corrupt (non-push_test) payload is suppressed,
        # but the claim loop still parses the payload without crashing.
        self.assertIsNone(ops.notification_claim_delivery())
        self.assertEqual(ops.notification_delivery_status(delivery["id"])["status"],
                         "suppressed")

    def test_schedule_skips_delivery_event_of_eligible_device(self):
        # briefing_scheduler 322-323: a delivery-type event whose source_id is an
        # eligible device is skipped. Driven by a direct _schedule call so the
        # crafted event is not resolved by reconcile first.
        self.qualify("phone")
        now = self.clock()
        with self.sql() as db:
            db.execute("""INSERT INTO notification_events(
                id,sequence,event_key,kind,state,severity,title,summary,provider,
                session_id,workstream_id,source_type,source_id,source_revision,
                opened_at,changed_at,resolved_at,snoozed_until,reminder_budget,
                last_push_at,payload_json) VALUES(
                'dsched',777,'dsched-key','failure','active','warning','t','s',NULL,
                NULL,NULL,'delivery','phone','rev',?,?,NULL,NULL,0,NULL,'{}')""",
                (now, now))
            db.commit()
        with self.ops._transaction(immediate=True) as db:
            self.ops._schedule_notification_deliveries(db, now)
        with self.sql() as db:
            rows = db.execute("""SELECT COUNT(*) FROM notification_deliveries
                WHERE event_id='dsched'""").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_claim_failed_on_invalid_stored_subscription(self):
        # 548-559: a malformed stored subscription fails the delivery + device.
        ops, path, delivery = self._queued_test_delivery("cm6.db")
        with sqlite3.connect(path) as db:
            db.execute("""UPDATE notification_devices
                SET subscription_json='{"endpoint":"https://bad.example/x"}'
                WHERE id='phone'""")
        self.assertIsNone(ops.notification_claim_delivery())
        self.assertEqual(ops.notification_delivery_status(delivery["id"])["status"],
                         "failed")


if __name__ == "__main__":
    unittest.main()
