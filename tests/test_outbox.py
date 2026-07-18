import os
import tempfile
import threading
import unittest

from outbox import OutboxError, OutboxManager, resolve_local_time


class Clock:
    def __init__(self, value=1_700_000_000):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def session(sid="codex:one", *, group="available", state="idle", provider="codex",
            submit=True, pending=None, agents=None, relay=True,
            queue_submit=False, control_state=None):
    return {"session_id": sid, "provider": provider, "ui_group": group,
            "state": state, "pending": pending, "access": "interactive",
            "control_state": control_state,
            "capabilities": {"submit": submit, "relay_agent": relay,
                             "queue_submit": queue_submit},
            "agents": agents or []}


def snapshot(*sessions, codex=True, claude=True):
    return {"sessions": list(sessions), "providers": {
        "codex": {"ok": codex}, "claude": {"ok": claude}}}


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        counter = iter(range(1000))
        self.upload_root = os.path.join(self.tmp.name, "uploads")
        self.asset_root = os.path.join(self.tmp.name, "outbox-images")
        os.makedirs(self.upload_root)
        self.manager = OutboxManager(os.path.join(self.tmp.name, "ledger.db"),
            clock=self.clock, id_factory=lambda: f"out-{next(counter):04d}",
            lease_seconds=10, recovery_source_root=self.upload_root,
            asset_root=self.asset_root)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **overrides):
        payload = {"kind": "when_available", "message": "hello",
            "target_provider": "codex", "target_session_id": "codex:one",
            "created_zone": "America/New_York"}
        payload.update(overrides)
        return self.manager.create(payload)

    def test_dst_skipped_and_repeated_times_are_explicit(self):
        with self.assertRaises(OutboxError) as skipped:
            resolve_local_time("2026-03-08T02:30", "America/New_York")
        self.assertEqual(skipped.exception.code, "nonexistent_time")
        with self.assertRaises(OutboxError) as repeated:
            resolve_local_time("2026-11-01T01:30", "America/New_York")
        self.assertEqual(repeated.exception.code, "ambiguous_time")
        self.assertEqual(len(repeated.exception.choices), 2)
        first, fold = resolve_local_time("2026-11-01T01:30", "America/New_York", 0)
        second, _ = resolve_local_time("2026-11-01T01:30", "America/New_York", 1)
        self.assertEqual(fold, 0)
        self.assertEqual(second - first, 3600)

    def test_identifiers_times_and_state_filters_are_rejected_not_truncated(self):
        invalid = [
            {"target_session_id": "x" * 321},
            {"target_agent_id": "agent\nchild"},
            {"target_session_id": {"unexpected": "object"}},
            {"message": {"unexpected": "object"}},
            {"created_zone": "UTC" + "x" * 121},
            {"created_zone": ["UTC"]},
            {"kind": "at_time", "trigger_at": None},
            {"kind": "new_session", "trigger_at": None,
             "spawn_spec": {"provider": "codex", "cwd": self.tmp.name}},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(OutboxError):
                self.create(**overrides)
        self.create()
        with self.assertRaises(OutboxError):
            self.manager.list(state="scheduled,future_state")

    def test_timed_message_runs_once_and_equal_times_keep_creation_order(self):
        for text in ("first", "second"):
            self.create(kind="at_time", message=text,
                        trigger_at=self.clock() + 10, local_time=None)
        calls = []
        self.clock.advance(10)
        self.manager.tick(snapshot(session()), {},
                          lambda item: calls.append(item["message"]) or {"ok": True},
                          lambda _item: {"ok": False})
        self.assertEqual(calls, ["first", "second"])
        self.manager.tick(snapshot(session()), {},
                          lambda item: calls.append(item["message"]) or {"ok": True},
                          lambda _item: {"ok": False})
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(self.manager.counts()["states"]["sent"], 2)

    def test_busy_and_pending_targets_wait_then_send(self):
        item = self.create()
        sent = []
        busy = session(group="working", state="running")
        self.manager.tick(snapshot(busy), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertEqual(self.manager.get(item["id"])["state"], "waiting_availability")
        self.clock.advance(1)
        waiting = session(group="needs_you", state="needs_you", pending={"kind": "question"})
        self.manager.tick(snapshot(waiting), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertFalse(sent)
        self.clock.advance(1)
        self.manager.tick(snapshot(session()), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.manager.get(item["id"])["state"], "sent")

    def test_closed_read_only_and_completed_agent_block_visibly(self):
        missing = self.create(message="missing")
        readonly = self.create(message="readonly", target_session_id="codex:read")
        agent = self.create(message="agent", target_session_id="claude-one",
            target_provider="claude", target_agent_id="agent-1")
        read = session("codex:read")
        read["read_only"] = True
        parent = session("claude-one", provider="claude", agents=[
            {"agent_id": "agent-1", "state": "done"}])
        self.manager.tick(snapshot(read, parent), {}, lambda _: {"ok": True}, lambda _: {})
        self.assertEqual(self.manager.get(missing["id"])["state"], "blocked")
        self.assertIn("view only", self.manager.get(readonly["id"])["blocked_reason"])
        self.assertIn("finished", self.manager.get(agent["id"])["blocked_reason"])

    def test_usage_reset_requires_fresh_post_reset_evidence(self):
        reset = self.clock() + 20
        item = self.create(kind="usage_reset", usage_account_id="acct",
            usage_window_id="weekly", observed_reset_at=reset)
        self.assertEqual(item["expires_at"], reset + 86400)
        usage = {"codex": {"account_id": "acct", "buckets": [
            {"id": "weekly", "reset": reset}]}}
        calls = []
        self.manager.tick(snapshot(session()), usage,
                          lambda row: calls.append(row) or {"ok": True}, lambda _: {})
        self.clock.advance(21)
        self.manager.tick(snapshot(session()), usage,
                          lambda row: calls.append(row) or {"ok": True}, lambda _: {})
        self.assertFalse(calls)
        usage["codex"]["buckets"][0]["reset"] = reset + 7 * 86400
        self.manager.tick(snapshot(session()), usage,
                          lambda row: calls.append(row) or {"ok": True}, lambda _: {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.manager.get(item["id"])["state"], "sent")

    def test_provider_outage_backoff_then_blocks_after_24_hours(self):
        item = self.create(kind="at_time", trigger_at=self.clock())
        self.manager.tick(snapshot(codex=False), {}, lambda _: {"ok": True}, lambda _: {})
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "scheduled")
        self.assertGreater(row["next_attempt_at"], self.clock())
        self.clock.advance(86401)
        self.manager.tick(snapshot(codex=False), {}, lambda _: {"ok": True}, lambda _: {})
        self.assertEqual(self.manager.get(item["id"])["state"], "blocked")

    def test_atomic_claim_allows_only_one_concurrent_dispatch(self):
        self.create()
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def dispatch(item):
            calls.append(item["id"])
            entered.set()
            release.wait(2)
            return {"ok": True}

        thread = threading.Thread(target=lambda: self.manager.tick(
            snapshot(session()), {}, dispatch, lambda _: {}))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.manager.tick(snapshot(session()), {}, dispatch, lambda _: {})
        release.set()
        thread.join(2)
        self.assertEqual(calls, ["out-0000"])

    def test_cancel_loses_cleanly_after_atomic_claim(self):
        item = self.create()
        record = self.manager.get(item["id"])
        self.assertTrue(self.manager._claim(record, "sending"))
        with self.assertRaises(OutboxError) as error:
            self.manager.cancel(item["id"])
        self.assertEqual(error.exception.code, "immutable")
        self.assertEqual(self.manager.get(item["id"])["state"], "sending")

    def test_large_due_queue_is_bounded_per_tick_and_keeps_order(self):
        for index in range(45):
            self.create(kind="at_time", message=f"message {index:02d}",
                        trigger_at=self.clock())
        calls = []
        deliver = lambda row: calls.append(row["message"]) or {"ok": True}
        self.manager.tick(snapshot(session()), {}, deliver, lambda _: {})
        self.assertEqual(len(calls), 20)
        self.manager.tick(snapshot(session()), {}, deliver, lambda _: {})
        self.manager.tick(snapshot(session()), {}, deliver, lambda _: {})
        self.assertEqual(calls, [f"message {index:02d}" for index in range(45)])
        self.assertEqual(self.manager.counts()["states"]["sent"], 45)

    def test_expired_dispatch_becomes_confirmation_unknown_and_never_retries(self):
        item = self.create()
        with self.manager._transaction(immediate=True) as db:
            db.execute("UPDATE outbox_messages SET state='sending',claimed_at=?,"
                       "lease_until=?,dispatch_started_at=? WHERE id=?",
                       (self.clock() - 20, self.clock() - 10, self.clock() - 20, item["id"]))
        calls = []
        self.manager.tick(snapshot(session()), {},
                          lambda row: calls.append(row) or {"ok": True}, lambda _: {})
        self.assertFalse(calls)
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "confirmation_unknown")
        self.assertTrue(row["retryable"])

    def test_restart_recovers_only_a_proven_exact_spawn_destination(self):
        item = self.create(kind="new_session", trigger_at=self.clock(),
            target_session_id=None, spawn_spec={"provider": "claude", "cwd": self.tmp.name,
                "model": "sonnet", "effort": "high", "mode": "default",
                "worktree": False, "worktree_name": ""})
        exact = "11111111-1111-4111-8111-111111111111"
        with self.manager._transaction(immediate=True) as db:
            db.execute("UPDATE outbox_messages SET state='spawning',destination_session_id=?,"
                       "claimed_at=?,lease_until=? WHERE id=?",
                       (exact, self.clock() - 20, self.clock() - 10, item["id"]))
        self.manager.recover_expired(snapshot(session(exact, provider="claude")))
        recovered = self.manager.get(item["id"])
        self.assertEqual(recovered["state"], "waiting_availability")
        self.assertEqual(recovered["destination_session_id"], exact)

        unknown = self.create(kind="new_session", trigger_at=self.clock(),
            target_session_id=None, spawn_spec={"provider": "codex", "cwd": self.tmp.name,
                "model": "gpt-5.4", "effort": "high", "mode": "plan",
                "worktree": False, "worktree_name": ""})
        with self.manager._transaction(immediate=True) as db:
            db.execute("UPDATE outbox_messages SET state='spawning',claimed_at=?,lease_until=? "
                       "WHERE id=?", (self.clock() - 20, self.clock() - 10, unknown["id"]))
        self.manager.recover_expired(snapshot())
        self.assertEqual(self.manager.get(unknown["id"])["state"],
                         "confirmation_unknown")

    def test_retry_is_a_new_linked_attempt_and_terminals_stay_immutable(self):
        original = self.create()
        self.manager.tick(snapshot(), {}, lambda _: {"ok": True}, lambda _: {})
        self.assertEqual(self.manager.get(original["id"])["state"], "blocked")
        retry = self.manager.retry(original["id"], {"target_session_id": "codex:one"})
        self.assertEqual(retry["retry_of"], original["id"])
        self.assertNotEqual(retry["id"], original["id"])
        with self.assertRaises(OutboxError):
            self.manager.cancel(original["id"])

    def test_scheduled_codex_spawn_can_deliver_initial_message_atomically(self):
        item = self.create(kind="new_session", trigger_at=self.clock(),
            target_session_id=None, spawn_spec={"provider": "codex", "cwd": self.tmp.name,
                "model": "gpt-5.4", "effort": "high", "mode": "plan",
                "worktree": False, "worktree_name": ""})
        spawned = []
        self.manager.tick(snapshot(), {}, lambda _: {"ok": False},
            lambda row: spawned.append(row) or {"ok": True,
                "session_id": "codex:exact", "message_delivered": True})
        self.assertEqual(len(spawned), 1)
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["destination_session_id"], "codex:exact")

    def test_claude_spawn_waits_for_exact_reserved_session_then_sends(self):
        item = self.create(kind="new_session", trigger_at=self.clock(),
            target_session_id=None, spawn_spec={"provider": "claude", "cwd": self.tmp.name,
                "model": "sonnet", "effort": "high", "mode": "default",
                "worktree": False, "worktree_name": ""})
        destinations = []
        self.manager.tick(snapshot(), {}, lambda _: {"ok": True},
            lambda row: destinations.append(row["destination_session_id"]) or
                {"ok": True, "session_id": row["destination_session_id"]})
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "waiting_availability")
        self.clock.advance(1)
        exact = session(destinations[0], provider="claude")
        sent = []
        self.manager.tick(snapshot(exact), {},
                          lambda record: sent.append(record) or {"ok": True}, lambda _: {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["destination_session_id"], destinations[0])

    def test_recovery_queue_is_idempotent_private_and_copies_images(self):
        source = os.path.join(self.upload_root, "upload.jpg")
        with open(source, "wb") as image:
            image.write(b"jpeg payload")
        first = self.manager.create_recovery(message="inspect this", target_provider="codex",
            target_session_id="codex:one", idempotency_key="send-request-0001",
            image_paths=[source])
        second = self.manager.create_recovery(message="inspect this", target_provider="codex",
            target_session_id="codex:one", idempotency_key="send-request-0001",
            image_paths=[source])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "waiting_provider")
        self.assertEqual(first["image_count"], 1)
        self.assertNotIn("image_paths_json", first)
        owned = self.manager.get_internal(first["id"])["_image_paths"]
        self.assertEqual(len(owned), 1)
        self.assertTrue(owned[0].startswith(os.path.realpath(self.asset_root) + os.sep))
        with open(owned[0], "rb") as image:
            self.assertEqual(image.read(), b"jpeg payload")
        with self.manager._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox_messages").fetchone()[0], 1)

    def test_automatic_claude_delivery_is_idempotent_waits_and_keeps_images(self):
        source = os.path.join(self.upload_root, "claude-upload.jpg")
        with open(source, "wb") as image:
            image.write(b"claude jpeg payload")
        first = self.manager.create_delivery(message="inspect this", target_provider="claude",
            target_session_id="claude-one", idempotency_key="send-request-claude-0001",
            image_paths=[source])
        second = self.manager.create_delivery(message="inspect this", target_provider="claude",
            target_session_id="claude-one", idempotency_key="send-request-claude-0001",
            image_paths=[source])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "waiting_availability")
        self.assertEqual(first["origin"], "automatic_fallback")
        self.assertEqual(first["image_count"], 1)

        sent = []
        busy = session("claude-one", provider="claude", group="working", state="running")
        self.manager.tick(snapshot(busy), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertFalse(sent)
        self.clock.advance(1)
        available = session("claude-one", provider="claude")
        self.manager.tick(snapshot(available), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["_image_paths"],
                         self.manager.get_internal(first["id"])["_image_paths"])
        self.assertEqual(self.manager.get(first["id"])["state"], "sent")
        self.manager.tick(snapshot(available), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.manager.get(first["id"])["image_count"], 0)

    def test_recovery_waits_for_authority_then_dispatches_once_and_cleans_assets(self):
        source = os.path.join(self.upload_root, "upload.jpg")
        with open(source, "wb") as image:
            image.write(b"jpeg payload")
        item = self.manager.create_recovery(message="inspect this", target_provider="codex",
            target_session_id="codex:one", idempotency_key="send-request-0002",
            image_paths=[source])
        sent = []
        reconnecting = session(submit=False, queue_submit=True, control_state="reconnecting")
        self.manager.tick(snapshot(reconnecting), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertFalse(sent)
        waiting = self.manager.get(item["id"])
        self.assertEqual(waiting["state"], "waiting_provider")
        self.assertIn("reconnect", waiting["blocked_reason"].lower())

        self.clock.advance(1)
        connected = session(submit=True, queue_submit=False, control_state="connected")
        self.manager.tick(snapshot(connected), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["_image_paths"],
                         self.manager.get_internal(item["id"])["_image_paths"])
        self.assertEqual(self.manager.get(item["id"])["state"], "sent")
        self.manager.tick(snapshot(connected), {},
                          lambda row: sent.append(row) or {"ok": True}, lambda _: {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.manager.get(item["id"])["image_count"], 0)

    def test_cancelling_recovery_removes_private_images_and_disables_retry(self):
        source = os.path.join(self.upload_root, "upload.jpg")
        with open(source, "wb") as image:
            image.write(b"jpeg payload")
        item = self.manager.create_recovery(message="inspect this", target_provider="codex",
            target_session_id="codex:one", idempotency_key="send-request-0003",
            image_paths=[source])
        owned = self.manager.get_internal(item["id"])["_image_paths"][0]
        cancelled = self.manager.cancel(item["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["image_count"], 0)
        self.assertFalse(os.path.exists(owned))
        self.assertFalse(cancelled["retryable"])


if __name__ == "__main__":
    unittest.main()
