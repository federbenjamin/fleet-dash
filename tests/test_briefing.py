import base64
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from briefing import FleetOperations, OperationsError


class Clock:
    def __init__(self, value=1_000_000):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def session(sid="s1", provider="claude", group="working", **overrides):
    exact = provider == "claude"
    item = {
        "session_id": sid, "provider": provider, "title": sid, "name": sid,
        "project": "fleet-dash", "cwd": "/Users/example/fleet-dash",
        "model": "sonnet" if provider == "claude" else "gpt-5.3-codex",
        "normalized_state": "running" if group == "working" else "idle",
        "state": "running" if group == "working" else "idle",
        "ui_group": group, "reason_label": "Turn in progress",
        "convo_v": 1, "files_n": 0, "agents_running": 0,
        "started_ms": 900_000_000, "quiet_s": 10, "muted": False,
        "cost": 3.0 if exact else None, "total_tokens": 10_000,
        "capabilities": {"exact_cost": exact},
    }
    item.update(overrides)
    return item


def fleet(clock, sessions=None, actions=None, providers=None):
    sessions = list(sessions or [])
    return {
        "t": clock(), "sessions": sessions, "closed": [],
        "actions": list(actions or []),
        "providers": providers or {"claude": {"ok": True}, "codex": {"ok": True}},
        "settings": {"stall_seconds": 240},
        "totals": {"sessions": len(sessions), "busy": sum(
            item.get("ui_group") == "working" for item in sessions),
            "agents_running": sum(item.get("agents_running") or 0 for item in sessions)},
    }


def action(sid="s1", provider="claude", kind="question", nonce="ask-1", **overrides):
    item = {
        "action_id": "action-" + sid + "-" + nonce,
        "session_id": sid, "provider": provider, "kind": kind,
        "request": "Choose a deployment target", "delivery_state": "Awaiting response",
        "reason": "Question waiting", "revision": "rev-1", "pending_nonce": nonce,
        "title": sid, "access": "interactive", "muted": False,
    }
    item.update(overrides)
    return item


def push_subscription(endpoint="https://web.push.apple.com/Qfixture"):
    encoded = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"endpoint": endpoint, "expirationTime": None,
            "keys": {"p256dh": encoded(b"\x04" + b"p" * 64),
                     "auth": encoded(b"a" * 16)}}


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = os.path.join(self.tmp.name, "ledger.db")
        self.ops = FleetOperations(self.path, clock=self.clock,
                                   id_factory=lambda: "bud-fixed")
        self.workstream = lambda cwd: {"workstream_id": "ws-fleet"}

    def tearDown(self):
        self.tmp.cleanup()

    def qualify_push_device(self, device_id="phone", preferences=None):
        self.ops.notification_register_device(
            device_id, device_id.title(), "test", push_subscription(),
            preferences=preferences)
        delivery = self.ops.notification_create_test_delivery(device_id)
        self.assertEqual(self.ops.notification_claim_delivery()["id"], delivery["id"])
        self.ops.notification_finish_delivery(delivery["id"], {"ok": True, "status": 201})
        return delivery

    def test_completion_artifact_and_repository_events_dedupe_across_restart(self):
        first = fleet(self.clock, [session()])
        self.ops.observe(first, self.workstream)
        self.clock.advance(20)
        done = session(group="available", normalized_state="turn_done", state="turn_done",
                       reason_label="Response ready", convo_v=2, files_n=2,
                       repo_outcome={"status": "passed", "summary": "Tests passed"})
        second = fleet(self.clock, [done])
        self.ops.observe(second, self.workstream)

        snap = self.ops.briefing_snapshot(second, "phone")
        self.assertEqual(len(snap["sections"]["completed"]), 1)
        self.assertEqual({item["title"] for item in snap["sections"]["outcomes"]},
                         {"Artifacts delivered", "Repository outcome observed"})
        cursor = self.ops.review("phone", snap["next_cursor"])
        self.assertGreater(cursor, 0)

        restarted = FleetOperations(self.path, clock=self.clock)
        restarted.observe(second, self.workstream)
        after = restarted.briefing_snapshot(second, "phone")
        self.assertEqual(after["unread"], 0)
        self.assertEqual(len(after["sections"]["reviewed"]), 3)

    def test_closed_session_preserves_cumulative_tokens_and_freezes_runtime(self):
        live = fleet(self.clock, [session(total_tokens=4321, started_ms=900_000_000)])
        self.ops.observe(live, self.workstream)
        self.clock.advance(200)
        closed = session(group="history", normalized_state="closed", state="closed",
                         total_tokens=None, started_ms=None, first_seen=900000,
                         closed_at=self.clock()-50, capabilities={}, cost=4, agent_cost=1,
                         agents_total=1)
        snapshot = fleet(self.clock, [])
        snapshot["closed"] = [closed]
        self.ops.observe(snapshot, self.workstream)
        self.ops.replace_budgets([
            {"id": "tokens", "scope_type": "session", "scope_id": "s1",
             "metric": "tokens", "limit_value": 5000},
            {"id": "runtime", "scope_type": "session", "scope_id": "s1",
             "metric": "runtime", "limit_value": 200000},
            {"id": "usd", "scope_type": "session", "scope_id": "s1",
             "metric": "usd", "limit_value": 10},
        ])
        values = {item["id"]: item for item in self.ops.budgets_snapshot(snapshot)["budgets"]}
        self.assertEqual(values["tokens"]["value"], 4321)
        self.assertEqual(values["runtime"]["value"], self.clock()-50-900000)
        self.assertEqual(values["usd"]["value"], 5)

    def test_current_attention_slow_provider_failure_and_muted_omission(self):
        blocked = session(group="needs_you", normalized_state="needs_you", state="needs_you",
                          quiet_s=600, muted=True)
        active = session("s2", quiet_s=601)
        action = {"action_id": "a1", "session_id": "s1", "provider": "claude",
                  "request_label": "Question", "delivery_label": "Choose an option",
                  "muted": True}
        current = fleet(self.clock, [blocked, active], [action],
                        {"claude": {"ok": True}, "codex": {"ok": False,
                         "error": "socket unavailable"}})
        self.ops.observe(current, self.workstream)
        snap = self.ops.briefing_snapshot(current, "desktop")
        self.assertTrue(any(item["title"] == "Question"
                            for item in snap["sections"]["attention"]))
        self.assertTrue(any(item["title"] == "Codex unavailable"
                            for item in snap["sections"]["attention"]))
        self.assertEqual(snap["sections"]["slow"][0]["session_id"], "s2")
        self.assertGreaterEqual(snap["muted_omitted"], 1)

    def test_repository_and_outbox_outcomes_are_imported_once(self):
        db = sqlite3.connect(self.path)
        db.execute("""CREATE TABLE repo_actions(id INTEGER PRIMARY KEY, action_id TEXT,
            kind TEXT, root TEXT, status TEXT, summary TEXT, error TEXT, finished_at REAL)""")
        db.execute("INSERT INTO repo_actions VALUES(1,'repo-1','git_push','/repo','succeeded','pushed',NULL,?)",
                   (self.clock(),))
        db.execute("""CREATE TABLE outbox_messages(id TEXT PRIMARY KEY,state TEXT,
            updated_at REAL,target_provider TEXT,destination_session_id TEXT,error TEXT,
            blocked_reason TEXT)""")
        db.execute("INSERT INTO outbox_messages VALUES('out-1','blocked',?,'codex','codex:t',NULL,'target closed')",
                   (self.clock(),))
        db.commit();db.close()
        current = fleet(self.clock)
        self.ops.observe(current, self.workstream)
        self.ops.observe(current, self.workstream)
        snap = self.ops.briefing_snapshot(current, "desktop")
        self.assertEqual([item["summary"] for item in snap["sections"]["outcomes"]],
                         ["pushed"])
        self.assertTrue(any(item["summary"] == "target closed"
                            for item in snap["sections"]["attention"]))

    def test_budget_measurement_scopes_and_warning_states(self):
        self.ops.replace_budgets([
            {"id": "usd", "scope_type": "fleet", "metric": "usd", "limit_value": 3.5},
            {"id": "tokens", "scope_type": "provider", "scope_id": "codex",
             "metric": "tokens", "limit_value": 9000},
            {"id": "runtime", "scope_type": "workstream", "scope_id": "ws-fleet",
             "metric": "runtime", "limit_value": 10},
            {"id": "missing", "scope_type": "session", "scope_id": "not-here",
             "metric": "usd", "limit_value": 1},
        ])
        mixed = fleet(self.clock, [session(), session("c1", "codex")])
        self.ops.observe(mixed, self.workstream)
        snap = self.ops.budgets_snapshot(mixed)
        budgets = {item["id"]: item for item in snap["budgets"]}
        self.assertEqual(budgets["usd"]["measurement_scope"], "partial")
        self.assertEqual(budgets["usd"]["status"], "warning")
        self.assertEqual(budgets["tokens"]["measurement_scope"], "token_only")
        self.assertEqual(budgets["tokens"]["status"], "exceeded")
        self.assertEqual(budgets["runtime"]["status"], "exceeded")
        self.assertEqual(budgets["missing"]["measurement_scope"], "unavailable")

    def test_budget_identifiers_and_booleans_are_not_silently_coerced(self):
        invalid = [
            {"id": "x" * 81, "scope_type": "fleet", "metric": "tokens",
             "limit_value": 1},
            {"id": "strict", "scope_type": "fleet", "metric": "tokens",
             "limit_value": 1, "block_spawns": "false"},
            {"id": "strict", "scope_type": "fleet", "metric": "tokens",
             "limit_value": 1, "enabled": 1},
            {"id": "strict", "scope_type": "fleet", "metric": "tokens",
             "limit_value": True},
            {"id": "strict", "scope_type": "provider", "scope_id": "bad\nprovider",
             "metric": "tokens", "limit_value": 1},
        ]
        for budget in invalid:
            with self.subTest(budget=budget), self.assertRaises(OperationsError):
                self.ops.replace_budgets([budget])

    def test_hard_budget_blocks_only_matching_future_spawns(self):
        self.ops.replace_budgets([
            {"id": "hard", "scope_type": "provider", "scope_id": "claude",
             "metric": "usd", "limit_value": 2, "block_spawns": True},
            {"id": "alert", "scope_type": "provider", "scope_id": "codex",
             "metric": "tokens", "limit_value": 1, "block_spawns": False},
        ])
        mixed = fleet(self.clock, [session(), session("c1", "codex")])
        self.ops.observe(mixed, self.workstream)
        self.assertEqual([item["id"] for item in
                          self.ops.spawn_blockers(mixed, "claude", "", "ws-fleet")],
                         ["hard"])
        self.assertEqual(self.ops.spawn_blockers(mixed, "codex", "", "ws-fleet"), [])

    def test_budget_alerts_dedupe_within_an_episode_and_return_after_recovery(self):
        self.ops.replace_budgets([
            {"id": "episode", "scope_type": "fleet", "metric": "tokens",
             "limit_value": 100},
        ])
        first = fleet(self.clock, [session(total_tokens=85)])
        first_eval = self.ops.observe(first, self.workstream)[0]
        self.clock.advance(10)
        second = fleet(self.clock, [session(total_tokens=90, convo_v=2)])
        second_eval = self.ops.observe(second, self.workstream)[0]
        self.assertEqual(first_eval["alert_key"], second_eval["alert_key"])
        self.assertEqual(first_eval["alert_created_at"], second_eval["alert_created_at"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM briefing_events WHERE category='budget'"
            ).fetchone()[0], 1)

        self.clock.advance(10)
        self.ops.observe(fleet(self.clock, [session(total_tokens=50, convo_v=3)]),
                         self.workstream)
        self.clock.advance(10)
        third_eval = self.ops.observe(
            fleet(self.clock, [session(total_tokens=85, convo_v=4)]), self.workstream)[0]
        self.assertNotEqual(third_eval["alert_key"], first_eval["alert_key"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM briefing_events WHERE category='budget'"
            ).fetchone()[0], 2)

    def test_budget_validation_is_atomic(self):
        self.ops.replace_budgets([
            {"id": "valid", "scope_type": "fleet", "metric": "tokens",
             "limit_value": 100},
        ])
        with self.assertRaises(OperationsError):
            self.ops.replace_budgets([
                {"id": "bad", "scope_type": "fleet", "metric": "money",
                 "limit_value": 1},
            ])
        snap = self.ops.budgets_snapshot(fleet(self.clock))
        self.assertEqual([item["id"] for item in snap["budgets"]], ["valid"])

    def test_spawn_forecast_has_sample_size_confidence_and_no_codex_currency(self):
        rows = []
        for index in range(4):
            rows.append(session(f"s{index}", cost=2+index, total_tokens=1000*(index+1),
                                project="fleet-dash", model="sonnet"))
        current = fleet(self.clock, rows)
        self.ops.observe(current, self.workstream)
        forecast = self.ops.budgets_snapshot(current, {
            "provider": "claude", "model": "sonnet", "project": "fleet-dash",
        })["spawn_forecast"]
        self.assertEqual(forecast["confidence"], "medium")
        self.assertEqual(forecast["sample_size"], 4)
        self.assertEqual(forecast["median_usd"], 3.5)

        codex = fleet(self.clock, [session("c1", "codex"), session("c2", "codex")])
        self.ops.observe(codex, self.workstream)
        cforecast = self.ops.budgets_snapshot(codex, {
            "provider": "codex", "model": "gpt-5.3-codex", "project": "fleet-dash",
        })["spawn_forecast"]
        self.assertEqual(cforecast["currency_scope"], "unavailable")
        self.assertIsNone(cforecast["median_usd"])

    def test_recent_burn_forecast_requires_multiple_positive_samples(self):
        self.ops.replace_budgets([
            {"id": "burn", "scope_type": "fleet", "metric": "tokens",
             "limit_value": 100_000},
        ])
        current = fleet(self.clock, [session(total_tokens=1000)])
        self.ops.observe(current, self.workstream)
        for tokens in (2000, 5000):
            self.clock.advance(600)
            current = fleet(self.clock, [session(total_tokens=tokens, convo_v=tokens)])
            self.ops.observe(current, self.workstream)
        forecast = self.ops.budgets_snapshot(current)["forecasts"]["burn"]
        self.assertEqual(forecast["status"], "forecast")
        self.assertEqual(forecast["sample_size"], 2)
        self.assertEqual(forecast["confidence"], "low")
        self.assertGreater(forecast["seconds_to_limit"], 0)

    def test_notification_dedupe_failure_and_quiet_episode_survive_restart(self):
        self.assertFalse(self.ops.notification_claim(
            "seed", "needs_you", "Waiting", "Old work", dispatch=False))
        self.assertFalse(self.ops.notification_claim(
            "seed", "needs_you", "Waiting", "Old work", dispatch=True))
        self.assertTrue(self.ops.notification_claim(
            "new", "needs_you", "Waiting", "New work", dispatch=True))
        self.ops.notification_status("new", "failed", "network down")

        current = fleet(self.clock)
        snap = self.ops.briefing_snapshot(current, "phone")
        self.assertNotIn("network down", repr(snap))
        self.assertFalse(any(item.get("id") == "new"
                             for item in snap["sections"]["attention"]))
        self.ops.observe(current, self.workstream)
        history = self.ops.notification_snapshot("phone")["events"]
        self.assertTrue(any(item["title"] == "Legacy ntfy test failed"
                            and item["state"] == "resolved" for item in history))
        self.assertIsNone(self.ops.fleet_activity_transition(2))
        self.clock.advance(10)
        quiet_since = self.ops.fleet_activity_transition(0)
        self.assertEqual(quiet_since, self.clock())
        restarted = FleetOperations(self.path, clock=self.clock)
        self.assertEqual(restarted.fleet_activity_transition(0), quiet_since)

    def test_unchanged_notification_projection_uses_zero_io_path(self):
        current = fleet(self.clock, [session()])
        self.ops.observe(current, self.workstream)
        with mock.patch.object(self.ops, "_reconcile_notification_events",
                               wraps=self.ops._reconcile_notification_events) as reconcile:
            self.clock.advance(2)
            current["t"] = self.clock()
            self.ops.observe(current, self.workstream)
            reconcile.assert_not_called()
            changed = fleet(self.clock, [session(muted=True)])
            self.ops.observe(changed, self.workstream)
            reconcile.assert_called_once()

    def test_quiet_digest_reports_muted_omissions_without_dropping_in_app_event(self):
        self.ops.observe(fleet(self.clock, [session(muted=True)]), self.workstream)
        start = self.clock()
        self.clock.advance(10)
        done = session(group="available", normalized_state="turn_done", state="turn_done",
                       convo_v=2, muted=True)
        current = fleet(self.clock, [done])
        self.ops.observe(current, self.workstream)
        snap = self.ops.briefing_snapshot(current, "phone")
        self.assertEqual(snap["sections"]["completed"][0]["muted"], True)
        self.assertIn("1 muted", self.ops.quiet_digest(start))

    def test_review_cursor_validation_and_monotonicity(self):
        with self.assertRaises(OperationsError):
            self.ops.review("contains space", 0)
        self.ops.notification_claim("n1", "test", "Title", "Body", dispatch=True)
        self.ops.notification_status("n1", "failed", "boom")
        snap = self.ops.briefing_snapshot(fleet(self.clock), "device-1")
        high = self.ops.review("device-1", snap["next_cursor"])
        self.assertEqual(self.ops.review("device-1", 0), high)

    def test_canonical_notification_identity_uses_full_session_and_native_revision(self):
        first = "codex:thread-shared-prefix-alpha"
        second = "codex:thread-shared-prefix-beta"
        current = fleet(self.clock, actions=[action(first, "codex"), action(second, "codex")])
        self.ops.observe(current, self.workstream)
        snap = self.ops.notification_snapshot("desktop")
        self.assertEqual(len(snap["events"]), 2)
        self.assertEqual({item["session_id"] for item in snap["events"]}, {first, second})
        self.assertEqual({item["state"] for item in snap["events"]}, {"active"})
        original = {item["session_id"]: (item["id"], item["opened_at"])
                    for item in snap["events"]}

        restarted = FleetOperations(self.path, clock=self.clock)
        restarted.observe(current, self.workstream)
        after = restarted.notification_snapshot("desktop")
        self.assertEqual({item["session_id"]: (item["id"], item["opened_at"])
                          for item in after["events"]}, original)

        self.clock.advance(10)
        changed = fleet(self.clock, actions=[action(first, "codex", nonce="ask-2"),
                                             action(second, "codex")])
        restarted.observe(changed, self.workstream)
        rows = restarted.notification_snapshot("desktop", limit=10)["events"]
        self.assertEqual(sum(item["session_id"] == first and item["state"] == "active"
                             for item in rows), 1)
        self.assertEqual(sum(item["session_id"] == first and item["state"] == "resolved"
                             for item in rows), 1)

    def test_snooze_persists_for_same_evidence_and_resolves_when_evidence_disappears(self):
        current = fleet(self.clock, actions=[action()])
        self.ops.observe(current, self.workstream)
        event = self.ops.notification_snapshot("desktop")["events"][0]
        until = self.clock() + 900
        self.assertEqual(self.ops.notification_snooze(
            event["id"], event["source_revision"], until), until)
        self.ops.observe(current, self.workstream)
        self.assertEqual(self.ops.notification_snapshot("desktop")["events"][0]["state"],
                         "snoozed")
        self.assertTrue(self.ops.notification_wake(event["id"], event["source_revision"]))
        self.assertEqual(self.ops.notification_snapshot("desktop")["events"][0]["state"],
                         "active")
        self.ops.notification_snooze(event["id"], event["source_revision"], until)
        self.clock.advance(20)
        self.ops.observe(fleet(self.clock), self.workstream)
        resolved = self.ops.notification_snapshot("desktop")["events"][0]
        self.assertEqual(resolved["state"], "resolved")
        self.assertIsNotNone(resolved["resolved_at"])

    def test_device_seed_cursor_read_cursor_and_indefinite_session_mute(self):
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        device = self.ops.notification_register_device(
            "phone-1", "Phone", "ios", push_subscription(),
            "https://web.push.apple.com", preferences={"minimum_severity": "warning"})
        self.assertEqual(device["read_cursor"], 1)
        self.assertNotIn("subscription_json", device)
        refreshed = self.ops.notification_register_device(
            "phone-1", "Phone", "ios",
            push_subscription("https://web.push.apple.com/Qreplacement"),
            "https://web.push.apple.com")
        self.assertEqual(refreshed["preferences"], {"minimum_severity": "warning"})

        self.clock.advance(10)
        current = fleet(self.clock, actions=[action(), action("s2", nonce="ask-2")])
        self.ops.observe(current, self.workstream)
        snap = self.ops.notification_snapshot("phone-1")
        self.assertEqual(snap["unread"], 1)
        high = self.ops.notification_mark_read("phone-1", snap["event_cursor"])
        self.assertEqual(self.ops.notification_mark_read("phone-1", 0), high)
        self.assertEqual(self.ops.notification_snapshot("phone-1")["unread"], 0)

        self.assertTrue(self.ops.notification_set_session_mute("s2", "claude", True))
        self.assertTrue(self.ops.notification_session_muted("s2"))
        self.clock.advance(31 * 86400)
        restarted = FleetOperations(self.path, clock=self.clock)
        self.assertTrue(restarted.notification_session_muted("s2"))
        self.assertFalse(restarted.notification_set_session_mute("s2", "claude", False))
        self.assertFalse(restarted.notification_session_muted("s2"))

    def test_browser_read_cursor_works_without_push_registration_and_survives_restart(self):
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        seeded = self.ops.notification_snapshot("browser-only")
        self.assertEqual(seeded["event_cursor"], 1)
        self.assertEqual(seeded["read_cursor"], 1)
        self.assertEqual(seeded["unread"], 0)

        self.clock.advance(10)
        self.ops.observe(fleet(self.clock, actions=[action(), action("s2", nonce="ask-2")]),
                         self.workstream)
        changed = self.ops.notification_snapshot("browser-only")
        self.assertEqual(changed["unread"], 1)
        self.assertEqual(self.ops.notification_mark_read("browser-only", changed["event_cursor"]), 2)

        restarted = FleetOperations(self.path, clock=self.clock)
        saved = restarted.notification_snapshot("browser-only")
        self.assertEqual(saved["read_cursor"], 2)
        self.assertEqual(saved["unread"], 0)
        registered = restarted.notification_register_device(
            "browser-only", "Browser", "macOS", push_subscription())
        self.assertEqual(registered["read_cursor"], 2)

    def test_notification_snapshot_projects_mute_and_redacted_delivery_problem(self):
        ops = FleetOperations(os.path.join(self.tmp.name, "problems.db"), clock=self.clock,
                              delivery_retry_delays=(1,), delivery_jitter=lambda delay: delay)
        self.assertEqual(os.stat(os.path.join(self.tmp.name, "problems.db")).st_mode & 0o777,
                         0o600)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        delivery = ops.notification_create_test_delivery("phone")
        ops.notification_claim_delivery()
        retrying = ops.notification_finish_delivery(delivery["id"], {
            "ok": False, "status": 503, "error": "secret endpoint detail"})
        self.assertEqual(retrying["status"], "retrying")
        self.clock.advance(1)
        ops.notification_claim_delivery()
        failed = ops.notification_finish_delivery(delivery["id"], {
            "ok": False, "status": 503, "error": "secret endpoint detail"})
        self.assertEqual(failed["status"], "failed")
        snapshot = ops.notification_snapshot("phone")
        problem = snapshot["delivery_problems"][0]
        self.assertEqual(problem["id"], delivery["id"])
        self.assertTrue(problem["can_retry"])
        self.assertNotIn("error", problem)
        self.assertNotIn("subscription", repr(problem))
        self.assertNotIn("endpoint", repr(problem))

        current = fleet(self.clock, actions=[action("muted-session")])
        ops.observe(current, self.workstream)
        ops.notification_set_session_mute("muted-session", "claude", True)
        projected = next(item for item in ops.notification_snapshot("phone")["events"]
                         if item.get("session_id") == "muted-session")
        self.assertTrue(projected["muted"])
        for private_key in ("event_key", "source_id", "source_type", "reminder_budget",
                            "last_push_at", "payload_json"):
            self.assertNotIn(private_key, projected)

    def test_push_subscription_boundary_and_redacted_device_lifecycle(self):
        rejected = [
            push_subscription("http://fcm.googleapis.com/fcm/send/x"),
            push_subscription("https://127.0.0.1/push"),
            push_subscription("https://user:pass@fcm.googleapis.com/push"),
            push_subscription("https://internal.example.test/push"),
            push_subscription("https://fcm.googleapis.com:444/push"),
            push_subscription("https://fcm.googleapis.com/push#fragment"),
            push_subscription("https://fcm.googleapis.com/push\nignored"),
        ]
        for subscription in rejected:
            with self.subTest(endpoint=subscription["endpoint"]), \
                    self.assertRaises(OperationsError):
                self.ops.notification_register_device(
                    "desktop", "Desktop", "macOS", subscription)

        invalid_key = push_subscription()
        invalid_key["keys"]["auth"] = "short"
        with self.assertRaises(OperationsError):
            self.ops.notification_register_device(
                "desktop", "Desktop", "macOS", invalid_key)

        device = self.ops.notification_register_device(
            "desktop", "Desktop", "macOS",
            push_subscription("https://fcm.googleapis.com/fcm/send/secret"),
            preferences={"kinds": ["question", "approval"],
                         "minimum_severity": "info", "initial_delay_seconds": 30})
        self.assertEqual(device["health"], "registered")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertNotIn("endpoint_origin", device)
        self.assertNotIn("subscription_json", device)

        renamed = self.ops.notification_update_device(
            "desktop", display_name="Studio Mac", enabled=False,
            preferences={"minimum_severity": "critical"})
        self.assertEqual(renamed["display_name"], "Studio Mac")
        self.assertEqual(renamed["health"], "disabled")
        listing = self.ops.notification_devices_snapshot("desktop")
        self.assertEqual(listing["current_device"]["id"], "desktop")
        self.assertNotIn("subscription_json", repr(listing))
        self.assertNotIn("fcm.googleapis.com", repr(listing))

        removed = self.ops.notification_remove_device("desktop")
        self.assertEqual(removed["permission_state"], "expired")
        with self.assertRaises(OperationsError):
            self.ops.notification_update_device("desktop", enabled=True)
        with sqlite3.connect(self.path) as db:
            stored = db.execute("""SELECT subscription_json,endpoint_origin
                FROM notification_devices WHERE id='desktop'""").fetchone()
        self.assertEqual(stored, ("{}", ""))

        forgotten = self.ops.notification_forget_device("desktop")
        self.assertEqual(forgotten, {"id": "desktop", "removed": True})
        listing = self.ops.notification_devices_snapshot("desktop")
        self.assertIsNone(listing["current_device"])
        self.assertEqual(listing["devices"], [])
        with self.assertRaises(OperationsError):
            self.ops.notification_forget_device("desktop")

    def test_explicit_exact_push_origin_can_be_allowed_without_wildcards(self):
        device = self.ops.notification_register_device(
            "custom", "Custom", "test",
            push_subscription("https://push.example.test/send/secret"),
            allowed_origins=["https://push.example.test"])
        self.assertEqual(device["id"], "custom")
        with self.assertRaises(OperationsError):
            self.ops.notification_register_device(
                "other", "Other", "test",
                push_subscription("https://sub.push.example.test/send/secret"),
                allowed_origins=["https://push.example.test"])

    def test_push_delivery_lease_success_and_redacted_diagnostics(self):
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        queued = self.ops.notification_create_test_delivery("phone")
        self.assertEqual(queued["status"], "queued")
        claim = self.ops.notification_claim_delivery()
        self.assertEqual(claim["id"], queued["id"])
        self.assertEqual(claim["attempt"], 1)
        self.assertEqual(claim["event"]["push_test"], True)
        self.assertIn("subscription", claim)
        sent = self.ops.notification_finish_delivery(
            queued["id"], {"ok": True, "status": 201, "remote_id": "safe-id"})
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(sent["remote_status"], 201)
        device = self.ops.notification_devices_snapshot("phone")["current_device"]
        self.assertEqual(device["health"], "healthy")
        diagnostics = self.ops.notification_delivery_diagnostics()
        self.assertEqual(diagnostics["statuses"]["sent"], 1)
        self.assertNotIn("web.push.apple.com", repr(diagnostics))
        self.assertNotIn("subscription", repr(sent))

    def test_forgetting_device_suppresses_queued_delivery_and_keeps_history(self):
        self.ops.notification_register_device(
            "old-phone", "Old phone", "iOS", push_subscription())
        queued = self.ops.notification_create_test_delivery("old-phone")
        self.ops.notification_forget_device("old-phone")
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM notification_deliveries WHERE id=?",
                             (queued["id"],)).fetchone()
        self.assertEqual(row["status"], "suppressed")
        self.assertEqual(row["error"], "device removed")
        self.assertEqual(self.ops.notification_devices_snapshot()["devices"], [])

    def test_push_delivery_enqueue_coalesces_same_event_and_device(self):
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        delivery = self.ops.notification_create_test_delivery("phone")
        duplicate = self.ops.notification_enqueue_delivery(
            delivery["event_id"], "phone")
        self.assertEqual(duplicate["id"], delivery["id"])
        self.assertEqual(duplicate["generation"], 1)
        self.assertEqual(self.ops.notification_delivery_diagnostics()["queued"], 1)

    def test_production_policy_requires_test_and_hard_excludes_informational_events(self):
        self.ops.notification_register_device(
            "untested", "Untested", "test", push_subscription())
        self.qualify_push_device("phone")
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        claim = self.ops.notification_claim_delivery()
        self.assertEqual(claim["device_id"], "phone")
        self.assertEqual(claim["purpose"], "initial")
        self.assertEqual(claim["event"]["kind"], "question")
        self.assertIsNone(self.ops.notification_claim_delivery())

        self.ops.notification_finish_delivery(claim["id"], {"ok": True, "status": 201})
        self.clock.advance(10)
        self.ops.observe(fleet(self.clock, [session(group="available",
            normalized_state="turn_done", state="turn_done", convo_v=2)]), self.workstream)
        with sqlite3.connect(self.path) as db:
            purposes = db.execute("""SELECT e.kind,d.purpose FROM notification_deliveries d
                JOIN notification_events e ON e.id=d.event_id
                WHERE d.purpose!='test'""").fetchall()
        self.assertEqual(purposes, [("question", "initial")])

    def test_briefing_severity_maps_success_to_info_and_high_to_critical(self):
        self.qualify_push_device("phone")
        policy = self.ops.notification_policy_snapshot()
        completion = next(item for item in policy["kinds"]
                          if item["kind"] == "completion")
        self.ops.notification_policy_update({"scope": "kind", "kind": "completion",
            "expected_revision": completion["revision"],
            "patch": {"mode": "once", "minimum_severity": "info"}})
        budget = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                      if item["kind"] == "budget")
        self.ops.notification_policy_update({"scope": "kind", "kind": "budget",
            "expected_revision": budget["revision"],
            "patch": {"mode": "once", "minimum_severity": "critical"}})
        self.ops.replace_budgets([{"id": "critical-budget", "scope_type": "fleet",
            "metric": "tokens", "limit_value": 1}])

        self.clock.advance(1)
        self.ops.observe(fleet(self.clock, [session()]), self.workstream)
        critical_delivery = self.ops.notification_claim_delivery()
        self.assertEqual(critical_delivery["event"]["kind"], "budget")
        with sqlite3.connect(self.path) as db:
            critical_severity = db.execute(
                "SELECT severity FROM notification_events WHERE id=?",
                (critical_delivery["event_id"],)).fetchone()[0]
        self.assertEqual(critical_severity, "critical")
        self.ops.notification_finish_delivery(
            critical_delivery["id"], {"ok": True, "status": 201})

        self.clock.advance(1)
        self.ops.observe(fleet(self.clock, [session(group="available",
            normalized_state="turn_done", state="turn_done", convo_v=2)]), self.workstream)
        completion_delivery = self.ops.notification_claim_delivery()
        self.assertEqual(completion_delivery["event"]["kind"], "completion")
        with sqlite3.connect(self.path) as db:
            completion_severity = db.execute(
                "SELECT severity FROM notification_events WHERE id=?",
                (completion_delivery["event_id"],)).fetchone()[0]
        self.assertEqual(completion_severity, "info")
        self.ops.notification_finish_delivery(
            completion_delivery["id"], {"ok": True, "status": 201})
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE notification_events SET severity='success' WHERE id=?",
                       (completion_delivery["event_id"],))
            db.execute("UPDATE notification_events SET severity='high' WHERE id=?",
                       (critical_delivery["event_id"],))
        FleetOperations(self.path, clock=self.clock)
        with sqlite3.connect(self.path) as db:
            migrated = dict(db.execute(
                "SELECT id,severity FROM notification_events WHERE id IN (?,?)",
                (completion_delivery["event_id"], critical_delivery["event_id"])).fetchall())
        self.assertEqual(migrated[completion_delivery["event_id"]], "info")
        self.assertEqual(migrated[critical_delivery["event_id"]], "critical")

    def test_global_policy_applies_delay_and_one_reminder_wave_to_all_devices(self):
        self.qualify_push_device("phone", {"kinds": ["question"],
            "minimum_severity": "warning", "initial_delay_seconds": 30})
        policy = self.ops.notification_policy_snapshot()
        question = next(item for item in policy["kinds"] if item["kind"] == "question")
        self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": question["revision"], "apply_current": True,
            "patch": {"initial_delay_seconds": 30}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.clock.advance(30)
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        initial = self.ops.notification_claim_delivery()
        self.assertEqual(initial["purpose"], "initial")
        self.ops.notification_finish_delivery(initial["id"], {"ok": True, "status": 201})

        self.clock.advance(899)
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.clock.advance(1)
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        reminder = self.ops.notification_claim_delivery()
        self.assertEqual(reminder["purpose"], "reminder")
        self.ops.notification_finish_delivery(reminder["id"], {"ok": True, "status": 201})
        self.clock.advance(3600)
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())
        with sqlite3.connect(self.path) as db:
            purposes = db.execute("""SELECT purpose,COUNT(*) FROM notification_deliveries
                WHERE purpose!='test' GROUP BY purpose ORDER BY purpose""").fetchall()
        self.assertEqual(purposes, [("initial", 1), ("reminder", 1)])

    def test_notification_policy_has_every_kind_and_never_exposes_delivery_secrets(self):
        snapshot = self.ops.notification_policy_snapshot()
        self.assertEqual({item["kind"] for item in snapshot["kinds"]}, {
            "question", "approval", "form", "reply", "failure", "stall",
            "completion", "artifact", "outcome", "budget", "measurement",
            "notification"})
        self.assertEqual(next(item for item in snapshot["kinds"]
                              if item["kind"] == "question")["mode"], "remind_once")
        self.assertEqual(next(item for item in snapshot["kinds"]
                              if item["kind"] == "completion")["mode"], "off")
        self.assertNotIn("subscription", repr(snapshot))
        self.assertNotIn("endpoint", repr(snapshot))

    def test_explicit_push_test_survives_global_and_kind_policy_edits(self):
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        delivery = self.ops.notification_create_test_delivery("phone")
        policy = self.ops.notification_policy_snapshot()
        self.ops.notification_policy_update({"scope": "global",
            "expected_revision": policy["global"]["revision"],
            "patch": {"quiet_hours_enabled": True}})
        policy = self.ops.notification_policy_snapshot()
        notice = next(item for item in policy["kinds"]
                      if item["kind"] == "notification")
        self.ops.notification_policy_update({"scope": "kind", "kind": "notification",
            "expected_revision": notice["revision"], "patch": {"mode": "once"}})
        claim = self.ops.notification_claim_delivery()
        self.assertEqual(claim["id"], delivery["id"])
        self.assertEqual(claim["purpose"], "test")

    def test_policy_revision_conflict_and_aggressive_cadence_confirmation(self):
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        changed = self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": rule["revision"], "patch": {"mode": "repeat",
                "repeat_interval_seconds": 3600, "max_deliveries": 8}})
        current = next(item for item in changed["kinds"] if item["kind"] == "question")
        with self.assertRaisesRegex(OperationsError, "changed"):
            self.ops.notification_policy_update({"scope": "kind", "kind": "question",
                "expected_revision": rule["revision"], "patch": {"mode": "off"}})
        with self.assertRaisesRegex(OperationsError, "more than 12"):
            self.ops.notification_policy_update({"scope": "kind", "kind": "question",
                "expected_revision": current["revision"], "patch": {
                    "repeat_interval_seconds": 60, "max_deliveries": 100}})
        accepted = self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": current["revision"], "confirm_aggressive": True,
            "patch": {"repeat_interval_seconds": 60, "max_deliveries": 100}})
        self.assertEqual(next(item for item in accepted["kinds"]
            if item["kind"] == "question")["repeat_interval_seconds"], 60)

    def test_enabling_rule_does_not_backfill_active_event_without_explicit_choice(self):
        self.qualify_push_device("phone")
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        disabled = self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": rule["revision"], "patch": {"mode": "off"}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        rule = next(item for item in disabled["kinds"] if item["kind"] == "question")
        self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": rule["revision"], "patch": {"mode": "once"}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.clock.advance(1)
        self.ops.observe(fleet(self.clock, actions=[action(nonce="ask-2")]), self.workstream)
        self.assertEqual(self.ops.notification_claim_delivery()["event"]["source_revision"],
                         "ask-2")

    def test_repeat_policy_stops_at_exact_successful_delivery_maximum(self):
        self.qualify_push_device("phone")
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": rule["revision"], "apply_current": True,
            "patch": {"mode": "repeat", "repeat_interval_seconds": 60,
                      "max_deliveries": 3}})
        current = fleet(self.clock, actions=[action()])
        for index in range(3):
            self.ops.observe(current, self.workstream)
            delivery = self.ops.notification_claim_delivery()
            self.assertIsNotNone(delivery, index)
            self.ops.notification_finish_delivery(delivery["id"], {"ok": True, "status": 201})
            self.clock.advance(60)
        self.ops.observe(current, self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())

    def test_quiet_hours_hold_then_kind_override_delivers_without_replay(self):
        self.qualify_push_device("phone")
        policy = self.ops.notification_policy_snapshot()
        global_policy = policy["global"]
        self.ops.notification_policy_update({"scope": "global",
            "expected_revision": global_policy["revision"], "patch": {
                "quiet_hours_enabled": True, "quiet_start_minute": 0,
                "quiet_end_minute": 0, "timezone": "UTC"}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": rule["revision"], "apply_current": True,
            "patch": {"allow_during_quiet_hours": True}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        delivery = self.ops.notification_claim_delivery()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery["purpose"], "initial")

    def test_snooze_replaces_reminder_with_one_wake_and_mute_suppresses_all_devices(self):
        self.qualify_push_device("phone")
        self.qualify_push_device("desktop")
        current = fleet(self.clock, actions=[action()])
        self.ops.observe(current, self.workstream)
        first = self.ops.notification_claim_delivery()
        second = self.ops.notification_claim_delivery()
        self.ops.notification_finish_delivery(first["id"], {"ok": True, "status": 201})
        self.ops.notification_finish_delivery(second["id"], {"ok": True, "status": 201})
        event = self.ops.notification_snapshot("phone")["events"][0]
        self.ops.notification_snooze(
            event["id"], event["source_revision"], self.clock() + 900)
        self.clock.advance(900)
        current = fleet(self.clock, actions=[action()])
        self.ops.observe(current, self.workstream)
        wake_one = self.ops.notification_claim_delivery()
        wake_two = self.ops.notification_claim_delivery()
        self.assertEqual({wake_one["purpose"], wake_two["purpose"]}, {"snooze_wake"})
        self.ops.notification_finish_delivery(wake_one["id"], {"ok": True, "status": 201})
        self.ops.notification_finish_delivery(wake_two["id"], {"ok": True, "status": 201})
        self.clock.advance(1800)
        current = fleet(self.clock, actions=[action()])
        self.ops.observe(current, self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())

        next_current = fleet(self.clock, actions=[action(nonce="ask-2")])
        self.ops.notification_set_session_mute("s1", "claude", True)
        self.ops.observe(next_current, self.workstream)
        self.assertIsNone(self.ops.notification_claim_delivery())
        self.assertTrue(self.ops.notification_session_muted("s1"))

    def test_global_policy_ignores_obsolete_per_device_kind_preferences(self):
        self.qualify_push_device("phone", {"kinds": ["completion"],
            "minimum_severity": "critical", "initial_delay_seconds": 3600})
        self.qualify_push_device("desktop", {"kinds": []})
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": rule["revision"], "apply_current": True,
            "patch": {"mode": "once", "initial_delay_seconds": 0}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        deliveries = [self.ops.notification_claim_delivery(),
                      self.ops.notification_claim_delivery()]
        self.assertEqual({item["device_id"] for item in deliveries},
                         {"phone", "desktop"})

    def test_claim_suppresses_job_if_policy_revision_changed_after_scheduling(self):
        self.qualify_push_device("phone")
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE notification_kind_policy SET revision=revision+1 "
                       "WHERE kind='question'")
        self.assertIsNone(self.ops.notification_claim_delivery())
        with sqlite3.connect(self.path) as db:
            state, error = db.execute("""SELECT status,error FROM notification_deliveries
                WHERE purpose='initial' ORDER BY created_at DESC LIMIT 1""").fetchone()
        self.assertEqual(state, "suppressed")
        self.assertEqual(error, "suppressed")

    def test_global_disable_suppresses_already_queued_policy_jobs(self):
        self.qualify_push_device("phone")
        rule = next(item for item in self.ops.notification_policy_snapshot()["kinds"]
                    if item["kind"] == "question")
        self.ops.notification_policy_update({"scope": "kind", "kind": "question",
            "expected_revision": rule["revision"], "apply_current": True,
            "patch": {"initial_delay_seconds": 60}})
        self.ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        policy = self.ops.notification_policy_snapshot()["global"]
        self.ops.notification_policy_update({"scope": "global",
            "expected_revision": policy["revision"], "patch": {"enabled": False}})
        self.assertIsNone(self.ops.notification_claim_delivery())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("""SELECT status FROM notification_deliveries
                WHERE purpose='initial' ORDER BY created_at DESC LIMIT 1""").fetchone()[0],
                "suppressed")

    def test_quiet_boundaries_are_deterministic_across_spring_and_fall_dst(self):
        zone = ZoneInfo("America/New_York")
        spring_now = datetime(2026, 3, 8, 1, 45, tzinfo=zone).timestamp()
        active, spring_end = self.ops._quiet_state({"quiet_hours_enabled": True,
            "quiet_start_minute": 60, "quiet_end_minute": 150,
            "timezone": "America/New_York"}, spring_now)
        self.assertTrue(active)
        self.assertEqual(datetime.fromtimestamp(spring_end, zone).strftime("%H:%M"), "03:00")

        fall_now = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=0).timestamp()
        active, fall_end = self.ops._quiet_state({"quiet_hours_enabled": True,
            "quiet_start_minute": 0, "quiet_end_minute": 90,
            "timezone": "America/New_York"}, fall_now)
        self.assertTrue(active)
        resolved = datetime.fromtimestamp(fall_end, zone)
        self.assertEqual(resolved.strftime("%H:%M"), "01:30")
        self.assertEqual(resolved.fold, 1)

    def test_existing_delivery_schema_is_upgraded_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "ledger.db")
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE notification_deliveries(
                    id TEXT PRIMARY KEY,event_id TEXT NOT NULL,device_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL,
                    claimed_at REAL,lease_until REAL,next_attempt_at REAL,remote_status INTEGER,
                    remote_id TEXT,error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    UNIQUE(event_id,device_id,generation))""")
                db.execute("""INSERT INTO notification_deliveries(
                    id,event_id,device_id,generation,status,attempt,created_at,updated_at)
                    VALUES('old','event','phone',1,'sent',1,1,1)""")
            FleetOperations(path, clock=lambda: 10)
            with sqlite3.connect(path) as db:
                columns = {row[1] for row in db.execute(
                    "PRAGMA table_info(notification_deliveries)")}
                self.assertTrue({"purpose", "source_revision", "cadence_index",
                    "global_policy_revision", "kind_policy_revision"}.issubset(columns))
                self.assertEqual(db.execute(
                    "SELECT id,status FROM notification_deliveries").fetchone(),
                    ("old", "sent"))

    def test_provider_failure_requires_two_matching_scans(self):
        failed = {"claude": {"ok": False, "error": "adapter unavailable"}}
        self.ops.observe(fleet(self.clock, providers=failed), self.workstream)
        self.assertFalse(any(item["kind"] == "failure" and item["state"] == "active"
                             for item in self.ops.notification_snapshot("browser")["events"]))
        self.clock.advance(2)
        self.ops.observe(fleet(self.clock, providers=failed), self.workstream)
        self.assertTrue(any(item["kind"] == "failure" and item["state"] == "active"
                            for item in self.ops.notification_snapshot("browser")["events"]))

    def test_terminal_delivery_failure_notifies_only_another_healthy_tested_device(self):
        path = os.path.join(self.tmp.name, "delivery-failure.db")
        ops = FleetOperations(path, clock=self.clock, delivery_retry_delays=(0,),
                              delivery_jitter=lambda delay: delay)
        for device in ("phone", "desktop"):
            ops.notification_register_device(
                device, device.title(), "test", push_subscription())
            test_delivery = ops.notification_create_test_delivery(device)
            ops.notification_claim_delivery()
            ops.notification_finish_delivery(test_delivery["id"], {"ok": True, "status": 201})
        current = fleet(self.clock, actions=[action()])
        ops.observe(current, self.workstream)
        first, second = ops.notification_claim_delivery(), ops.notification_claim_delivery()
        phone = first if first["device_id"] == "phone" else second
        desktop = second if phone is first else first
        ops.notification_finish_delivery(desktop["id"], {"ok": True, "status": 201})
        self.clock.advance(1)
        ops.notification_finish_delivery(phone["id"], {"ok": False, "status": 503})
        retry = ops.notification_claim_delivery()
        self.assertEqual(retry["id"], phone["id"])
        ops.notification_finish_delivery(phone["id"], {"ok": False, "status": 503})
        self.clock.advance(1)
        ops.observe(fleet(self.clock, actions=[action()]), self.workstream)
        problem_push = ops.notification_claim_delivery()
        self.assertEqual(problem_push["event"]["kind"], "failure")
        self.assertEqual(problem_push["device_id"], "desktop")
        self.assertIsNone(ops.notification_claim_delivery())

    def test_push_delivery_retry_after_lease_reclaim_and_exhaustion(self):
        retry_path = os.path.join(self.tmp.name, "retry.db")
        ops = FleetOperations(
            retry_path, clock=self.clock, delivery_retry_delays=(2, 10),
            delivery_jitter=lambda delay: delay)
        ops.notification_register_device("phone", "Phone", "iOS", push_subscription())
        queued = ops.notification_create_test_delivery("phone")
        first = ops.notification_claim_delivery()
        retrying = ops.notification_finish_delivery(
            first["id"], {"ok": False, "status": 429, "retry_after": 5})
        self.assertEqual(retrying["status"], "retrying")
        self.assertEqual(retrying["next_attempt_at"], self.clock() + 5)
        self.assertIsNone(ops.notification_claim_delivery())
        self.clock.advance(5)
        second = ops.notification_claim_delivery()
        self.assertEqual(second["attempt"], 2)

        # A crashed worker leaves a sending lease. A fresh process reclaims it,
        # increments the durable attempt, and never duplicates the generation.
        self.clock.advance(31)
        restarted = FleetOperations(
            retry_path, clock=self.clock, delivery_retry_delays=(2, 10),
            delivery_jitter=lambda delay: delay)
        third = restarted.notification_claim_delivery()
        self.assertEqual(third["id"], queued["id"])
        self.assertEqual(third["attempt"], 3)
        exhausted = restarted.notification_finish_delivery(
            third["id"], {"ok": False, "retryable": True, "code": "timeout"})
        self.assertEqual(exhausted["status"], "failed")

        retried = restarted.notification_retry_delivery(queued["id"])
        self.assertEqual(retried["generation"], 2)
        self.assertNotEqual(retried["id"], queued["id"])

    def test_push_410_expires_and_scrubs_subscription_until_reconnect(self):
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        queued = self.ops.notification_create_test_delivery("phone")
        self.ops.notification_claim_delivery()
        expired = self.ops.notification_finish_delivery(
            queued["id"], {"ok": False, "status": 410})
        self.assertEqual(expired["status"], "subscription_expired")
        device = self.ops.notification_devices_snapshot("phone")["current_device"]
        self.assertEqual(device["permission_state"], "expired")
        self.assertEqual(device["health"], "disabled")
        with sqlite3.connect(self.path) as db:
            stored = db.execute("""SELECT subscription_json,endpoint_origin
                FROM notification_devices WHERE id='phone'""").fetchone()
        self.assertEqual(stored, ("{}", ""))
        with self.assertRaises(OperationsError):
            self.ops.notification_retry_delivery(queued["id"])

        self.ops.notification_register_device(
            "phone", "Phone", "iOS",
            push_subscription("https://web.push.apple.com/Qreplacement"))
        replacement = self.ops.notification_create_test_delivery("phone")
        self.ops.notification_claim_delivery()
        self.ops.notification_finish_delivery(
            replacement["id"], {"ok": True, "status": 201})
        retried = self.ops.notification_retry_delivery(queued["id"])
        self.assertEqual(retried["status"], "queued")

    def test_push_restart_matrix_preserves_snooze_retry_expiry_and_capability_use(self):
        self.qualify_push_device("phone")
        current = fleet(self.clock, actions=[action()])
        self.ops.observe(current, self.workstream)
        event = self.ops.notification_snapshot("phone")["events"][0]
        self.ops.notification_snooze(
            event["id"], event["source_revision"], self.clock() + 900)

        restarted = FleetOperations(
            self.path, clock=self.clock, delivery_retry_delays=(5,),
            delivery_jitter=lambda delay: delay)
        self.assertEqual(restarted.notification_snapshot("phone")["events"][0]["state"],
                         "snoozed")
        self.assertIsNone(restarted.notification_claim_delivery())
        self.clock.advance(900)
        current = fleet(self.clock, actions=[action()])
        restarted.observe(current, self.workstream)
        after_wake = FleetOperations(
            self.path, clock=self.clock, delivery_retry_delays=(5,),
            delivery_jitter=lambda delay: delay)
        wake = after_wake.notification_claim_delivery()
        self.assertEqual(wake["purpose"], "snooze_wake")
        after_wake.notification_finish_delivery(wake["id"], {"ok": True, "status": 201})
        self.assertIsNone(after_wake.notification_claim_delivery())

        retry = after_wake.notification_create_test_delivery("phone")
        self.assertEqual(after_wake.notification_claim_delivery()["id"], retry["id"])
        after_wake.notification_finish_delivery(
            retry["id"], {"ok": False, "status": 503, "code": "http_503"})
        before_due = FleetOperations(
            self.path, clock=self.clock, delivery_retry_delays=(5,),
            delivery_jitter=lambda delay: delay)
        self.assertIsNone(before_due.notification_claim_delivery())
        self.clock.advance(5)
        self.assertEqual(before_due.notification_claim_delivery()["id"], retry["id"])
        before_due.notification_finish_delivery(retry["id"], {"ok": True, "status": 201})

        before_due.notification_register_device(
            "expired", "Expired", "iOS", push_subscription())
        expired = before_due.notification_create_test_delivery("expired")
        self.assertEqual(before_due.notification_claim_delivery()["id"], expired["id"])
        before_due.notification_finish_delivery(expired["id"], {"ok": False, "status": 410})
        after_expiry = FleetOperations(self.path, clock=self.clock)
        device = after_expiry.notification_devices_snapshot("expired")["current_device"]
        self.assertEqual((device["health"], device["permission_state"]),
                         ("disabled", "expired"))
        with sqlite3.connect(self.path) as db:
            stored = db.execute("""SELECT subscription_json,endpoint_origin
                FROM notification_devices WHERE id='expired'""").fetchone()
        self.assertEqual(stored, ("{}", ""))

        claims = {"event_id": event["id"], "device_id": "phone", "action": "mute",
                  "jti_hash": "a" * 64, "expires_at": self.clock() + 600}
        self.assertEqual(after_expiry.notification_consume_capability(claims)["action"],
                         "mute")
        replay_process = FleetOperations(self.path, clock=self.clock)
        with self.assertRaisesRegex(OperationsError, "unavailable"):
            replay_process.notification_consume_capability(claims)

    def test_push_queue_bound_rejects_new_work_without_evicting_existing(self):
        bounded_path = os.path.join(self.tmp.name, "bounded.db")
        bounded = FleetOperations(bounded_path, clock=self.clock, delivery_queue_limit=1)
        bounded.notification_register_device("phone", "Phone", "iOS", push_subscription())
        first = bounded.notification_create_test_delivery("phone")
        with self.assertRaisesRegex(OperationsError, "queue is full"):
            bounded.notification_create_test_delivery("phone")
        self.assertEqual(bounded.notification_delivery_status(first["id"])["status"],
                         "queued")

    def test_legacy_notification_table_migrates_as_scrubbed_history_only(self):
        other = os.path.join(self.tmp.name, "legacy.db")
        with sqlite3.connect(other) as db:
            db.execute("""CREATE TABLE notification_deliveries(
                event_key TEXT PRIMARY KEY, category TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""INSERT INTO notification_deliveries VALUES(
                'old','needs_you','Waiting','Old work','failed',
                'https://ntfy.test/private-topic?token=secret',1,1)""")
        migrated = FleetOperations(other, clock=self.clock)
        with sqlite3.connect(other) as db:
            legacy = db.execute("""SELECT event_key,status,error
                FROM notification_deliveries_legacy""").fetchall()
            columns = {row[1] for row in
                       db.execute("PRAGMA table_info(notification_deliveries)").fetchall()}
        self.assertEqual(legacy, [("old", "failed", "Legacy ntfy delivery failed")])
        self.assertIn("event_id", columns)
        self.assertNotIn("event_key", columns)
        briefing = migrated.briefing_snapshot(fleet(self.clock), "desktop")
        self.assertNotIn("private-topic", repr(briefing))
        self.assertFalse(any(item.get("id") == "old"
                             for item in briefing["sections"]["attention"]))
        self.assertTrue(migrated.notification_claim(
            "new", "legacy_test", "Fleet legacy notification test",
            "Manual ntfy delivery is working.", dispatch=True))
        migrated.notification_status("new", "sent")
        restarted = FleetOperations(other, clock=self.clock)
        self.assertFalse(restarted.notification_claim(
            "new", "needs_you", "Waiting", "New work", dispatch=True))


if __name__ == "__main__":
    unittest.main()
