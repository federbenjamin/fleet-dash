import os
import sqlite3
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


if __name__ == "__main__":
    unittest.main()
