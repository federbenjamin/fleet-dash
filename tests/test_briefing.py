import base64
import os
import sqlite3
import stat
import tempfile
import unittest

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
        self.assertTrue(any("network down" in item["summary"]
                            for item in snap["sections"]["attention"]))
        self.assertIsNone(self.ops.fleet_activity_transition(2))
        self.clock.advance(10)
        quiet_since = self.ops.fleet_activity_transition(0)
        self.assertEqual(quiet_since, self.clock())
        restarted = FleetOperations(self.path, clock=self.clock)
        self.assertEqual(restarted.fleet_activity_transition(0), quiet_since)

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

    def test_push_delivery_enqueue_coalesces_same_event_and_device(self):
        self.ops.notification_register_device(
            "phone", "Phone", "iOS", push_subscription())
        delivery = self.ops.notification_create_test_delivery("phone")
        duplicate = self.ops.notification_enqueue_delivery(
            delivery["event_id"], "phone")
        self.assertEqual(duplicate["id"], delivery["id"])
        self.assertEqual(duplicate["generation"], 1)
        self.assertEqual(self.ops.notification_delivery_diagnostics()["queued"], 1)

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
        retried = self.ops.notification_retry_delivery(queued["id"])
        self.assertEqual(retried["status"], "queued")

    def test_push_queue_bound_rejects_new_work_without_evicting_existing(self):
        bounded_path = os.path.join(self.tmp.name, "bounded.db")
        bounded = FleetOperations(bounded_path, clock=self.clock, delivery_queue_limit=1)
        bounded.notification_register_device("phone", "Phone", "iOS", push_subscription())
        first = bounded.notification_create_test_delivery("phone")
        with self.assertRaisesRegex(OperationsError, "queue is full"):
            bounded.notification_create_test_delivery("phone")
        self.assertEqual(bounded.notification_delivery_status(first["id"])["status"],
                         "queued")

    def test_legacy_notification_table_migrates_transactionally_and_still_dispatches(self):
        other = os.path.join(self.tmp.name, "legacy.db")
        with sqlite3.connect(other) as db:
            db.execute("""CREATE TABLE notification_deliveries(
                event_key TEXT PRIMARY KEY, category TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""INSERT INTO notification_deliveries VALUES(
                'old','needs_you','Waiting','Old work','sent',NULL,1,1)""")
        migrated = FleetOperations(other, clock=self.clock)
        with sqlite3.connect(other) as db:
            legacy = db.execute("""SELECT event_key,status
                FROM notification_deliveries_legacy""").fetchall()
            columns = {row[1] for row in
                       db.execute("PRAGMA table_info(notification_deliveries)").fetchall()}
        self.assertEqual(legacy, [("old", "sent")])
        self.assertIn("event_id", columns)
        self.assertNotIn("event_key", columns)
        self.assertTrue(migrated.notification_claim(
            "new", "needs_you", "Waiting", "New work", dispatch=True))
        migrated.notification_status("new", "sent")
        restarted = FleetOperations(other, clock=self.clock)
        self.assertFalse(restarted.notification_claim(
            "new", "needs_you", "Waiting", "New work", dispatch=True))


if __name__ == "__main__":
    unittest.main()
