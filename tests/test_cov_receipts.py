"""Coverage for fleetdash/engine_receipts.py — durable action receipts, the
idempotent replay they enable, and their retention window (invariant 76)."""
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))   # sibling test_cov_common import

from test_cov_common import EngineCovBase


class ReceiptStoreTest(EngineCovBase):
    def _action(self, request_id="act-abcdef123456", action_type="text"):
        return {"type": action_type, "session_id": self.sid,
                "client_request_id": request_id, "text": "hello"}

    # -------------------------------------------------------------- claiming
    def test_an_eligible_action_claims_a_receipt(self):
        receipt_id, replay = self.engine.begin_act_receipt(self._action())
        self.assertTrue(receipt_id.startswith("rcpt-"))
        self.assertIsNone(replay)

    def test_an_action_without_a_request_id_is_unchanged(self):
        receipt_id, replay = self.engine.begin_act_receipt(
            {"type": "text", "session_id": self.sid})
        self.assertIsNone(receipt_id)
        self.assertIsNone(replay)

    def test_a_read_only_probe_never_claims_a_receipt(self):
        """Replaying a `noop` or a `focus` costs nothing, so a receipt would only
        add write load to the path this workstream is trying to keep fast."""
        for action_type in ("noop", "focus", "ping"):
            receipt_id, _ = self.engine.begin_act_receipt(
                self._action(action_type=action_type))
            self.assertIsNone(receipt_id, action_type)

    def test_a_malformed_request_id_is_refused(self):
        for value in ("", "short", "x" * 200, "has space", "semi;colon"):
            receipt_id, _ = self.engine.begin_act_receipt(self._action(request_id=value))
            self.assertIsNone(receipt_id, value)

    # --------------------------------------------------------------- replay
    def test_replaying_a_delivered_action_never_writes_keys_again(self):
        receipt_id, _ = self.engine.begin_act_receipt(self._action())
        self.engine.resolve_act_receipt(receipt_id, {"ok": True})
        again_id, replay = self.engine.begin_act_receipt(self._action())
        self.assertEqual(again_id, receipt_id)
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["receipt_state"], "delivered")

    def test_replaying_a_failed_action_returns_the_recorded_failure(self):
        receipt_id, _ = self.engine.begin_act_receipt(self._action())
        self.engine.resolve_act_receipt(
            receipt_id, {"ok": False, "error": "empty text"})
        _, replay = self.engine.begin_act_receipt(self._action())
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["receipt_state"], "failed")
        self.assertIn("empty text", replay["error"])

    def test_replaying_an_uncertain_action_keeps_it_uncertain(self):
        receipt_id, _ = self.engine.begin_act_receipt(self._action())
        self.engine.resolve_act_receipt(receipt_id, {
            "ok": False, "code": "delivery_uncertain", "error": "check the terminal"})
        _, replay = self.engine.begin_act_receipt(self._action())
        self.assertEqual(replay["code"], "delivery_uncertain")
        self.assertEqual(replay["receipt_state"], "uncertain")

    def test_a_still_running_action_is_never_delivered_twice(self):
        self.engine.begin_act_receipt(self._action())
        _, replay = self.engine.begin_act_receipt(self._action())
        self.assertEqual(replay["code"], "in_flight")
        self.assertTrue(replay["duplicate"])

    def test_a_row_that_vanishes_between_insert_and_read_is_not_replayed(self):
        """Losing the race must never silently swallow the user's action."""
        original = self.engine._receipt_db

        class Vanishing:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                if sql.startswith("INSERT INTO act_receipts"):
                    raise sqlite3.IntegrityError("UNIQUE constraint failed")
                if sql.startswith("SELECT receipt_id"):
                    return mock.Mock(fetchone=lambda: None)
                return self.db.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.db, name)

        with mock.patch.object(self.engine, "_receipt_db",
                               lambda: Vanishing(original())):
            receipt_id, replay = self.engine.begin_act_receipt(self._action())
        self.assertIsNone(receipt_id)
        self.assertIsNone(replay)

    # -------------------------------------------------------------- storage
    def test_a_storage_failure_never_blocks_the_action(self):
        with mock.patch.object(self.engine, "_receipt_db",
                               side_effect=sqlite3.Error("disk gone")):
            receipt_id, replay = self.engine.begin_act_receipt(self._action())
        self.assertIsNone(receipt_id)
        self.assertIsNone(replay)

    def test_a_failed_resolve_reports_that_the_receipt_is_not_durable(self):
        receipt_id, _ = self.engine.begin_act_receipt(self._action())
        with mock.patch.object(self.engine, "_receipt_db",
                               side_effect=sqlite3.Error("disk gone")):
            out = self.engine.resolve_act_receipt(receipt_id, {"ok": True})
        self.assertTrue(out["ok"])
        self.assertFalse(out["receipt_durable"])

    def test_resolving_without_a_receipt_returns_the_result_untouched(self):
        result = {"ok": True, "sent": 1}
        self.assertIs(self.engine.resolve_act_receipt(None, result), result)

    # --------------------------------------------------------------- lookup
    def test_lookup_returns_the_recorded_outcome(self):
        receipt_id, _ = self.engine.begin_act_receipt(self._action())
        self.engine.resolve_act_receipt(receipt_id, {"ok": True})
        out = self.engine.act_receipt("act-abcdef123456")
        self.assertTrue(out["found"])
        self.assertEqual(out["receipt"]["state"], "delivered")
        self.assertEqual(out["receipt"]["action_type"], "text")

    def test_lookup_of_an_unknown_id_is_explicitly_ambiguous(self):
        """A missing receipt does NOT prove the action never landed — the reason
        text says so, because the client must not turn it into a verdict."""
        out = self.engine.act_receipt("act-neverseen1234")
        self.assertTrue(out["ok"])
        self.assertFalse(out["found"])
        self.assertIn("never reached Fleet", out["reason"])

    def test_lookup_refuses_a_malformed_id(self):
        out = self.engine.act_receipt("../../etc/passwd")
        self.assertFalse(out["ok"])

    def test_lookup_survives_a_storage_failure(self):
        with mock.patch.object(self.engine, "_receipt_db",
                               side_effect=sqlite3.Error("disk gone")):
            out = self.engine.act_receipt("act-abcdef123456")
        self.assertFalse(out["ok"])
        self.assertIn("receipt lookup failed", out["error"])

    # ------------------------------------------------------------ retention
    def test_receipts_are_pruned_after_their_retention_window(self):
        receipt_id, _ = self.engine.begin_act_receipt(self._action())
        self.engine.resolve_act_receipt(receipt_id, {"ok": True})
        self.engine._act_receipt_prune_due = 0
        removed = self.engine.prune_act_receipts(
            time.time() + self.engine.ACT_RECEIPT_TTL_SECONDS + 60)
        self.assertEqual(removed, 1)
        self.assertFalse(self.engine.act_receipt("act-abcdef123456")["found"])

    def test_pruning_is_rate_limited(self):
        self.engine.prune_act_receipts(1000.0)
        self.assertEqual(self.engine.prune_act_receipts(1001.0), 0)

    def test_pruning_survives_a_storage_failure(self):
        self.engine._act_receipt_prune_due = 0
        with mock.patch.object(self.engine, "_receipt_db",
                               side_effect=sqlite3.Error("disk gone")):
            self.assertEqual(self.engine.prune_act_receipts(), 0)


if __name__ == "__main__":
    unittest.main()
