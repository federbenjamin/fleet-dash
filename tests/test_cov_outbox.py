"""Line-coverage tests for fleetdash.outbox.

Complements tests/test_outbox.py by exercising the validation, error, race, and
image-recovery branches that the behavioural suite does not reach. Deterministic,
no network, temp sqlite only. Timing is driven by an injected clock; a few
concurrency/version-race branches are provoked by one-shot instance-method
monkeypatches (the double is restored before it can affect anything else).
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from fleetdash.outbox import (OutboxError, OutboxManager, resolve_local_time,
                              _epoch)


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


# The full outbox_messages schema minus the five optional/migrated columns, used
# to prove _init_db's ALTER TABLE migration path (invariant 64 durability).
LEGACY_SCHEMA = """CREATE TABLE outbox_messages(
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    created_zone TEXT NOT NULL, local_time TEXT, trigger_fold INTEGER,
    kind TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
    target_provider TEXT, target_session_id TEXT, target_agent_id TEXT,
    trigger_at REAL, usage_account_id TEXT, usage_window_id TEXT,
    observed_reset_at REAL, spawn_spec_json TEXT,
    claimed_at REAL, lease_until REAL, dispatch_started_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL,
    expires_at REAL, destination_session_id TEXT,
    provider_receipt TEXT, sent_at REAL, error TEXT, blocked_reason TEXT,
    retry_of TEXT, version INTEGER NOT NULL DEFAULT 1)"""


class OutboxCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.counter = iter(range(10000))
        self.upload_root = os.path.join(self.tmp.name, "uploads")
        self.asset_root = os.path.join(self.tmp.name, "outbox-images")
        os.makedirs(self.upload_root)
        self.manager = OutboxManager(
            os.path.join(self.tmp.name, "ledger.db"), clock=self.clock,
            id_factory=lambda: f"out-{next(self.counter):04d}", lease_seconds=10,
            recovery_source_root=self.upload_root, asset_root=self.asset_root)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **overrides):
        payload = {"kind": "when_available", "message": "hello",
                   "target_provider": "codex", "target_session_id": "codex:one",
                   "created_zone": "America/New_York"}
        payload.update(overrides)
        return self.manager.create(payload)

    def _upload(self, name, data=b"jpeg payload"):
        path = os.path.join(self.upload_root, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    # ---- _epoch -----------------------------------------------------------
    def test_epoch_bool_and_string_and_invalid(self):
        with self.assertRaises(OutboxError):
            _epoch(True)                       # line 58 (bool rejected)
        self.assertIsNone(_epoch(None))
        self.assertEqual(_epoch(5), 5.0)
        # Valid ISO with tz (lines 62-65).
        self.assertAlmostEqual(
            _epoch("2026-01-01T00:00:00+00:00"),
            1767225600.0, delta=1)
        with self.assertRaises(OutboxError):        # naive ISO -> ValueError (63-64)
            _epoch("2026-01-01T00:00:00")
        with self.assertRaises(OutboxError):        # garbage (66-67)
            _epoch("not-a-timestamp")

    # ---- resolve_local_time ----------------------------------------------
    def test_resolve_local_time_error_and_normal_paths(self):
        with self.assertRaises(OutboxError) as bad_zone:      # 74-75
            resolve_local_time("2026-01-01T00:00", "Not/AZone")
        self.assertEqual(bad_zone.exception.code, "bad_timezone")
        with self.assertRaises(OutboxError):                  # 78-79 bad local
            resolve_local_time("not-a-datetime", "UTC")
        with self.assertRaises(OutboxError):                  # 81 tz-aware local
            resolve_local_time("2026-01-01T00:00+00:00", "UTC")
        # Unambiguous local time -> single candidate (line 112).
        trigger, fold = resolve_local_time("2026-01-15T09:30", "America/New_York")
        self.assertIn(fold, (0, 1))
        self.assertIsInstance(trigger, float)

    # ---- diagnostics ------------------------------------------------------
    def test_diagnostics_with_and_without_samples(self):
        self.create()
        stats = self.manager.diagnostics()               # 167-171 (non-empty)
        self.assertGreaterEqual(stats["samples"], 1)
        self.manager.db_connect_ms.clear()
        self.manager.db_begin_ms.clear()
        empty = self.manager.diagnostics()               # 165-166 (empty deque)
        self.assertEqual(empty["connect_p95_ms"], 0.0)
        self.assertEqual(empty["begin_wait_p95_ms"], 0.0)

    # ---- _init_db migration ----------------------------------------------
    def test_init_db_adds_missing_columns_to_legacy_table(self):
        legacy_path = os.path.join(self.tmp.name, "legacy.db")
        con = sqlite3.connect(legacy_path)
        con.execute(LEGACY_SCHEMA)
        con.commit()
        con.close()
        # Construction runs _init_db, hitting the ALTER TABLE loop (line 200).
        migrated = OutboxManager(legacy_path, clock=self.clock,
                                 id_factory=lambda: "out-legacy")
        with migrated._connect() as db:
            columns = {row[1] for row in
                       db.execute("PRAGMA table_info(outbox_messages)")}
        for name in ("origin", "idempotency_key", "image_paths_json",
                     "cancelled_at", "cancelled_from_state"):
            self.assertIn(name, columns)

    # ---- _public / _internal on malformed rows ---------------------------
    def test_missing_row_projects_none(self):
        self.assertIsNone(self.manager.get("nope"))            # _public(None) 210-211
        self.assertIsNone(self.manager.get_internal("nope"))   # _internal(None) 246-247

    def test_public_and_internal_tolerate_malformed_json(self):
        with self.manager._transaction(immediate=True) as db:
            db.execute(
                "INSERT INTO outbox_messages(id,created_at,updated_at,created_zone,"
                "kind,state,message,spawn_spec_json,provider_receipt,image_paths_json,"
                "origin,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                ("bad-json", 1.0, 1.0, "UTC", "when_available", "waiting_availability",
                 "m", "{bad", "{bad", "{bad", "automatic_fallback"))
        public = self.manager.get("bad-json")           # 215-216, 219-220, 223-224
        self.assertIsNone(public["spawn_spec"])
        self.assertIsNone(public["provider_receipt"])
        self.assertEqual(public["image_count"], 0)
        internal = self.manager.get_internal("bad-json")  # 250-251
        self.assertEqual(internal["_image_paths"], [])

    # ---- _message / _idempotency_key / _zone -----------------------------
    def test_message_bounds(self):
        with self.assertRaises(OutboxError):        # 261 empty after strip
            self.create(message="   ")
        with self.assertRaises(OutboxError):        # 263 too long
            self.create(message="x" * 2001)

    def test_idempotency_key_validation(self):
        with self.assertRaises(OutboxError):        # 273 too short / bad chars
            self.create(client_request_id="short")
        with self.assertRaises(OutboxError):
            self.create(client_request_id="has space here!!")

    def test_zone_unknown_iana(self):
        with self.assertRaises(OutboxError) as bad:   # 285-286
            self.create(created_zone="Bogus/Zone")
        self.assertEqual(bad.exception.code, "bad_timezone")

    # ---- _trigger ---------------------------------------------------------
    def test_trigger_local_time_validation_and_resolution(self):
        with self.assertRaises(OutboxError):        # 295 non-str local_time
            self.create(kind="at_time", local_time=12345)
        with self.assertRaises(OutboxError):        # 299 over-long local_time
            self.create(kind="at_time", local_time="x" * 41)
        # Valid local_time resolves through resolve_local_time (302-303).
        item = self.create(kind="at_time", local_time="2026-02-01T09:00",
                           created_zone="America/New_York")
        self.assertIsNotNone(item["trigger_at"])
        self.assertEqual(item["local_time"], "2026-02-01T09:00")

    # ---- _normalize_create ------------------------------------------------
    def test_normalize_create_rejects_bad_shapes(self):
        with self.assertRaises(OutboxError):        # 310 unsupported kind
            self.create(kind="nonsense")
        with self.assertRaises(OutboxError):        # 334 new_session spawn_spec not dict
            self.create(kind="new_session", trigger_at=self.clock(),
                        target_session_id=None, spawn_spec="oops")
        with self.assertRaises(OutboxError):        # 338 cwd not str
            self.create(kind="new_session", trigger_at=self.clock(),
                        target_session_id=None,
                        spawn_spec={"provider": "codex", "cwd": 5})
        with self.assertRaises(OutboxError):        # 341 bad provider/empty cwd
            self.create(kind="new_session", trigger_at=self.clock(),
                        target_session_id=None,
                        spawn_spec={"provider": "nope", "cwd": self.tmp.name})
        with self.assertRaises(OutboxError):        # 346 missing target/provider
            self.create(target_provider="mystery")
        with self.assertRaises(OutboxError):        # 349 usage_reset missing fields
            self.create(kind="usage_reset", usage_account_id=None)

    # ---- create_delivery guards ------------------------------------------
    def test_create_delivery_argument_guards(self):
        common = dict(message="hi", target_session_id="codex:one",
                      idempotency_key="delivery-key-0001")
        with self.assertRaises(OutboxError):        # 399 unknown provider
            self.manager.create_delivery(target_provider="grok", **common)
        with self.assertRaises(OutboxError):        # 401 unsupported kind
            self.manager.create_delivery(target_provider="codex", kind="at_time",
                                         **common)
        with self.assertRaises(OutboxError):        # 403 reconnect non-codex
            self.manager.create_delivery(target_provider="claude",
                                         kind="provider_reconnect", **common)
        with self.assertRaises(OutboxError):        # 406 bad origin
            self.manager.create_delivery(target_provider="codex", origin="weird",
                                         **common)
        with self.assertRaises(OutboxError):        # 408 resume w/ wrong origin
            self.manager.create_delivery(target_provider="codex",
                                         kind="resume_session",
                                         origin="automatic_fallback", **common)

    def test_create_delivery_rejects_too_many_images(self):
        sources = [self._upload(f"many-{i}.jpg") for i in range(5)]
        with self.assertRaises(OutboxError):        # 423
            self.manager.create_delivery(
                message="hi", target_provider="codex", target_session_id="codex:one",
                idempotency_key="too-many-images-01", image_paths=sources)

    def test_create_delivery_without_recovery_root_refuses_images(self):
        no_root = OutboxManager(os.path.join(self.tmp.name, "noroot.db"),
                                clock=self.clock, id_factory=lambda: "out-noroot",
                                asset_root=os.path.join(self.tmp.name, "nr-assets"))
        src = self._upload("nr.jpg")
        with self.assertRaises(OutboxError):        # 428-429, cleanup 465-475
            no_root.create_delivery(
                message="hi", target_provider="codex", target_session_id="codex:one",
                idempotency_key="no-recovery-root-1", image_paths=[src])

    def test_create_delivery_rejects_image_outside_recovery_root(self):
        outside = os.path.join(self.tmp.name, "outside.jpg")
        with open(outside, "wb") as handle:
            handle.write(b"payload")
        with self.assertRaises(OutboxError):        # 434-435 + cleanup 471-473
            self.manager.create_delivery(
                message="hi", target_provider="codex", target_session_id="codex:one",
                idempotency_key="outside-root-1", image_paths=[outside])
        # asset_dir was created then removed by the cleanup path.
        self.assertFalse(os.path.exists(os.path.join(self.asset_root, "out-0000")))

    def test_create_delivery_rejects_invalid_image_and_cleans_partial(self):
        good = self._upload("good.jpg", b"realbytes")
        empty = self._upload("empty.jpg", b"")       # size 0 -> invalid (438)
        with self.assertRaises(OutboxError):          # 437-438 + full cleanup 465-475
            self.manager.create_delivery(
                message="hi", target_provider="codex", target_session_id="codex:one",
                idempotency_key="invalid-image-1", image_paths=[good, empty])
        # The first copied image and the asset dir were both removed.
        self.assertFalse(os.path.exists(os.path.join(self.asset_root, "out-0000")))

    def test_create_delivery_cleanup_tolerates_unlink_failure(self):
        # A failed delivery whose cleanup cannot unlink an already-copied image
        # still re-raises; the OSError in the unlink loop is swallowed (472-473)
        # and rmdir of the now non-empty asset dir is swallowed too (476-477).
        good = self._upload("cleanup-good.jpg", b"realbytes")
        empty = self._upload("cleanup-empty.jpg", b"")   # invalid -> triggers cleanup
        with mock.patch("fleetdash.outbox.os.unlink", side_effect=OSError("boom")):
            with self.assertRaises(OutboxError):
                self.manager.create_delivery(
                    message="hi", target_provider="codex",
                    target_session_id="codex:one",
                    idempotency_key="cleanup-unlink-1", image_paths=[good, empty])

    def test_create_delivery_duplicate_race_returns_existing_and_frees_copy(self):
        # A duplicate idempotency row appears AFTER the pre-check but BEFORE the
        # INSERT, forcing the sqlite IntegrityError -> duplicate branch (454-463).
        src = self._upload("dup.jpg")
        key = "dup-race-key-0001"
        real_normalize = self.manager._normalize_create

        def inject(payload, **kwargs):
            self.manager._normalize_create = real_normalize
            with self.manager._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO outbox_messages(id,created_at,updated_at,"
                    "created_zone,kind,state,message,idempotency_key,origin,version)"
                    " VALUES(?,?,?,?,?,?,?,?,?,1)",
                    ("dup-existing", 1.0, 1.0, "UTC", "when_available",
                     "waiting_availability", "dup", key, "automatic_fallback"))
                db.commit()
            return real_normalize(payload, **kwargs)

        self.manager._normalize_create = inject
        result = self.manager.create_delivery(
            message="hi", target_provider="codex", target_session_id="codex:one",
            idempotency_key=key, image_paths=[src])
        self.assertEqual(result["id"], "dup-existing")
        # The copy made for this attempt was removed (462).
        self.assertFalse(os.path.exists(os.path.join(self.asset_root, "out-0000")))

    def test_create_delivery_non_idempotency_integrity_error_reraises(self):
        # A primary-key collision (id_factory returns an existing id) makes the
        # SELECT-by-key find nothing, forcing the `else: raise` re-raise (459-460)
        # and the outer image cleanup (465-475).
        collide = OutboxManager(os.path.join(self.tmp.name, "collide.db"),
                                clock=self.clock, id_factory=lambda: "collide-id",
                                recovery_source_root=self.upload_root,
                                asset_root=os.path.join(self.tmp.name, "collide-assets"))
        with collide._transaction(immediate=True) as db:
            db.execute(
                "INSERT INTO outbox_messages(id,created_at,updated_at,created_zone,"
                "kind,state,message,idempotency_key,origin,version)"
                " VALUES(?,?,?,?,?,?,?,?,?,1)",
                ("collide-id", 1.0, 1.0, "UTC", "when_available",
                 "waiting_availability", "prior", "other-key", "automatic_fallback"))
        src = self._upload("collide.jpg")
        with self.assertRaises(sqlite3.IntegrityError):
            collide.create_delivery(
                message="hi", target_provider="codex", target_session_id="codex:one",
                idempotency_key="brand-new-key-01", image_paths=[src])
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp.name, "collide-assets", "collide-id")))

    # ---- _remove_asset_paths ----------------------------------------------
    def test_remove_asset_paths_branch_coverage(self):
        # Path outside the asset root is ignored (501).
        self.manager._remove_asset_paths(["/etc/hosts"])
        # Missing path under root -> FileNotFoundError swallowed (505-506).
        self.manager._remove_asset_paths(
            [os.path.join(self.asset_root, "ghost", "image-1.jpg")])
        # A directory path under root -> unlink raises OSError -> continue
        # (507-508); its parent (asset_root) is non-empty -> rmdir fails (512-513).
        adir = os.path.join(self.asset_root, "adir")
        os.makedirs(adir)
        keep = os.path.join(self.asset_root, "keep")
        os.makedirs(keep)
        self.manager._remove_asset_paths([adir])
        self.assertTrue(os.path.exists(adir))   # unlink of a dir did nothing
        self.assertTrue(os.path.exists(keep))

    def test_remove_asset_paths_leaves_nonempty_dir(self):
        sub = os.path.join(self.asset_root, "sub")
        os.makedirs(sub)
        target = os.path.join(sub, "image-1.jpg")
        with open(target, "wb") as handle:
            handle.write(b"x")
        sibling = os.path.join(sub, "keep.txt")
        with open(sibling, "wb") as handle:
            handle.write(b"y")
        self.manager._remove_asset_paths([target])   # 502-504 + rmdir fails 512-513
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.exists(sibling))

    # ---- _copy_retry_assets direct branches -------------------------------
    def test_copy_retry_assets_rejects_too_many(self):
        with self.assertRaises(OutboxError):        # 535-536
            self.manager._copy_retry_assets("rid", [f"p{i}" for i in range(5)])

    def test_copy_retry_assets_rejects_source_outside_root(self):
        # First source valid under asset_root, second outside -> raise (546) and
        # cleanup unlinks the partial copy + rmdir (566-571).
        valid_dir = os.path.join(self.asset_root, "src")
        os.makedirs(valid_dir)
        valid = os.path.join(valid_dir, "image-1.jpg")
        with open(valid, "wb") as handle:
            handle.write(b"bytes")
        outside = os.path.join(self.tmp.name, "far.jpg")
        with open(outside, "wb") as handle:
            handle.write(b"bytes")
        with self.assertRaises(OutboxError):
            self.manager._copy_retry_assets("rid-outside", [valid, outside])
        self.assertFalse(os.path.exists(os.path.join(self.asset_root, "rid-outside")))

    def test_copy_retry_assets_rejects_invalid_size(self):
        empty_dir = os.path.join(self.asset_root, "empties")
        os.makedirs(empty_dir)
        empty = os.path.join(empty_dir, "image-1.jpg")
        open(empty, "wb").close()                   # size 0 -> invalid (553-554)
        with self.assertRaises(OutboxError):
            self.manager._copy_retry_assets("rid-empty", [empty])

    def test_copy_retry_assets_maps_unreadable_source_to_expired(self):
        if os.geteuid() == 0:
            self.skipTest("chmod 000 does not block reads for root")
        srcdir = os.path.join(self.asset_root, "unreadable")
        os.makedirs(srcdir)
        src = os.path.join(srcdir, "image-1.jpg")
        with open(src, "wb") as handle:
            handle.write(b"payload")
        os.chmod(src, 0o000)
        try:
            with self.assertRaises(OutboxError) as expired:   # copy OSError 559-560
                self.manager._copy_retry_assets("rid-unreadable", [src])
            self.assertEqual(expired.exception.code, "attachments_unavailable")
        finally:
            os.chmod(src, 0o600)

    # ---- list -------------------------------------------------------------
    def test_list_rejects_invalid_cursor(self):
        with self.assertRaises(OutboxError):        # 602-603
            self.manager.list(cursor="not-a-number")

    # ---- update -----------------------------------------------------------
    def test_update_version_and_state_guards(self):
        item = self.create(message="orig")
        with self.assertRaises(OutboxError) as low:   # 653-654 version < 1
            self.manager.update(item["id"], {"message": "x"}, expected_version=0)
        self.assertEqual(low.exception.code, "stale")
        with self.assertRaises(OutboxError) as missing:   # 656-657 not found
            self.manager.update("ghost", {"message": "x"}, expected_version=1)
        self.assertEqual(missing.exception.code, "stale")

    def test_update_refuses_non_editable(self):
        item = self.create()
        self.assertTrue(self.manager._claim(self.manager.get(item["id"]), "sending"))
        with self.assertRaises(OutboxError) as err:   # 660-661 not editable
            self.manager.update(item["id"], {"message": "x"},
                                expected_version=self.manager.get(item["id"])["version"])
        self.assertEqual(err.exception.code, "immutable")

    def test_update_loses_to_concurrent_version_bump(self):
        # Bump the row's version between get() and the UPDATE predicate so the
        # compare-and-set WHERE misses (rowcount 0 -> raise, line 675).
        item = self.create(message="orig")
        version = item["version"]
        real_normalize = self.manager._normalize_create

        def bump(payload, **kwargs):
            self.manager._normalize_create = real_normalize
            with self.manager._transaction(immediate=True) as db:
                db.execute("UPDATE outbox_messages SET version=version+1 WHERE id=?",
                           (item["id"],))
            return real_normalize(payload, **kwargs)

        self.manager._normalize_create = bump
        with self.assertRaises(OutboxError) as stale:
            self.manager.update(item["id"], {"message": "new"},
                                expected_version=version)
        self.assertEqual(stale.exception.code, "stale")

    # ---- dismiss ----------------------------------------------------------
    def test_dismiss_missing_and_idempotent(self):
        with self.assertRaises(OutboxError) as missing:   # 705-706
            self.manager.dismiss("ghost")
        self.assertEqual(missing.exception.code, "stale")
        item = self.create()
        self.manager.dismiss(item["id"])
        again = self.manager.dismiss(item["id"])          # 711-712 already cancelled
        self.assertTrue(again["cancelled"])

    # ---- retry ------------------------------------------------------------
    def test_retry_refuses_non_retryable(self):
        item = self.create()                              # pending -> not retryable
        with self.assertRaises(OutboxError) as err:       # 727-728
            self.manager.retry(item["id"])
        self.assertEqual(err.exception.code, "immutable")

    def test_retry_of_timed_message_resets_trigger(self):
        item = self.create(kind="at_time", trigger_at=self.clock() + 5,
                           local_time=None)
        self.manager._terminal(item["id"], "failed", error="nope")
        retry = self.manager.retry(item["id"])            # 734-737 reset trigger
        self.assertEqual(retry["retry_of"], item["id"])
        self.assertIsNone(retry["local_time"])

    def test_retry_loses_when_source_state_changes_mid_flight(self):
        # Between the retryable check and the guarded INSERT, flip the source out
        # of a retryable state so the in-transaction re-check raises (751-753) and
        # the just-copied assets are cleaned up (763-769).
        item = self.create()
        self.manager._terminal(item["id"], "failed", error="nope")
        real_copy = self.manager._copy_retry_assets

        def flip(outbox_id, source_paths):
            self.manager._copy_retry_assets = real_copy
            with self.manager._transaction(immediate=True) as db:
                db.execute("UPDATE outbox_messages SET state='sent' WHERE id=?",
                           (item["id"],))
            return real_copy(outbox_id, source_paths)

        self.manager._copy_retry_assets = flip
        with self.assertRaises(OutboxError) as err:
            self.manager.retry(item["id"])
        self.assertEqual(err.exception.code, "immutable")

    # ---- retarget ---------------------------------------------------------
    def test_retarget_missing_editable_retryable_and_immutable(self):
        with self.assertRaises(OutboxError) as missing:   # 774-775
            self.manager.retarget("ghost", {"message": "x"}, expected_version=1)
        self.assertEqual(missing.exception.code, "stale")

        # Retryable source -> routes to retry (778-779).
        failed = self.create(message="to-retry")
        self.manager._terminal(failed["id"], "failed", error="nope")
        routed = self.manager.retarget(failed["id"], {"message": "again"})
        self.assertEqual(routed["retry_of"], failed["id"])

        # Sent/terminal non-retryable -> immutable (780).
        sent = self.create(message="done")
        self.manager._terminal(sent["id"], "sent")
        with self.assertRaises(OutboxError) as immutable:
            self.manager.retarget(sent["id"], {"message": "x"})
        self.assertEqual(immutable.exception.code, "immutable")

    # ---- send_now ---------------------------------------------------------
    def test_send_now_moves_pending_and_rejects_terminal(self):
        item = self.create()                              # waiting_availability
        moved = self.manager.send_now(item["id"])         # 789-792
        self.assertEqual(moved["state"], "scheduled")

        usage_item = self.create(kind="usage_reset", usage_account_id="acct",
                                 usage_window_id="weekly",
                                 observed_reset_at=self.clock() + 5)
        moved_usage = self.manager.send_now(usage_item["id"])  # waiting_usage_reset branch
        self.assertEqual(moved_usage["state"], "waiting_availability")

        sent = self.create(message="done")
        self.manager._terminal(sent["id"], "sent")
        with self.assertRaises(OutboxError) as err:       # 787-788 not pending
            self.manager.send_now(sent["id"])
        self.assertEqual(err.exception.code, "immutable")

    # ---- _target_status ---------------------------------------------------
    def test_target_status_stale_session_retries(self):
        item = self.create()
        stale = session()
        stale["stale"] = True
        self.manager.tick(snapshot(stale), {},
                          lambda _: {"ok": True}, lambda _: {})
        row = self.manager.get(item["id"])                # 817-818 -> transient 1053-1054
        self.assertGreater(row["next_attempt_at"], self.clock())

    def test_target_status_reconnect_without_submit_blocks(self):
        item = self.manager.create_recovery(
            message="hi", target_provider="codex", target_session_id="codex:one",
            idempotency_key="reconnect-noblock-1")
        dead = session(submit=False, queue_submit=False, control_state="connected")
        self.manager.tick(snapshot(dead), {}, lambda _: {"ok": True}, lambda _: {})
        blocked = self.manager.get(item["id"])            # 828-829
        self.assertEqual(blocked["state"], "blocked")

    def test_target_status_agent_missing_and_terminal_and_relay(self):
        # Missing agent -> block (837-838).
        missing = self.create(message="m1", target_provider="claude",
                              target_session_id="claude-a", target_agent_id="agent-x")
        parent_no_agent = session("claude-a", provider="claude", agents=[])
        self.manager.tick(snapshot(parent_no_agent), {},
                          lambda _: {"ok": True}, lambda _: {})
        self.assertIn("no longer available",
                      self.manager.get(missing["id"])["blocked_reason"])

        # Agent present but relay capability missing -> block (841-842).
        norelay = self.create(message="m2", target_provider="claude",
                              target_session_id="claude-b", target_agent_id="agent-y")
        parent_norelay = session("claude-b", provider="claude", relay=False,
                                 agents=[{"agent_id": "agent-y", "state": "running"}])
        self.manager.tick(snapshot(parent_norelay), {},
                          lambda _: {"ok": True}, lambda _: {})
        self.assertIn("cannot relay",
                      self.manager.get(norelay["id"])["blocked_reason"])

    def test_target_status_no_submit_blocks(self):
        item = self.create()
        nosubmit = session(submit=False)
        self.manager.tick(snapshot(nosubmit), {},
                          lambda _: {"ok": True}, lambda _: {})
        self.assertIn("does not accept",                  # 843-844
                      self.manager.get(item["id"])["blocked_reason"])

    # ---- _usage_window ----------------------------------------------------
    def test_usage_window_branches(self):
        mgr = self.manager
        # Unavailable evidence (863-864).
        self.assertEqual(
            mgr._usage_window({"target_provider": "codex"}, {}),
            (None, "Usage evidence is unavailable"))
        self.assertEqual(
            mgr._usage_window({"target_provider": "codex"},
                              {"codex": {"stale": True}}),
            (None, "Usage evidence is unavailable"))

        # Claude profile happy path (868-870, 873, 876).
        claude_usage = {"claude": {"profiles": [
            {"id": "active", "weekly_reset": "2026-01-01T00:00:00+00:00"}]}}
        reset, err = mgr._usage_window(
            {"target_provider": "claude", "usage_account_id": "active",
             "usage_window_id": "weekly"}, claude_usage)
        self.assertIsNone(err)
        self.assertIsInstance(reset, float)
        # Claude profile not found (871-872).
        self.assertEqual(
            mgr._usage_window({"target_provider": "claude",
                               "usage_account_id": "ghost",
                               "usage_window_id": "weekly"}, claude_usage),
            (None, "The selected Claude account is unavailable"))
        # Claude window not found (874-875).
        self.assertEqual(
            mgr._usage_window({"target_provider": "claude",
                               "usage_account_id": "active",
                               "usage_window_id": "hourly"}, claude_usage),
            (None, "The selected Claude usage window is unavailable"))

        # Codex account mismatch (877-879).
        codex_usage = {"codex": {"account_id": "acct", "buckets": [
            {"id": "weekly", "reset": "2026-01-01T00:00:00+00:00"}]}}
        self.assertEqual(
            mgr._usage_window({"target_provider": "codex",
                               "usage_account_id": "other",
                               "usage_window_id": "weekly"}, codex_usage),
            (None, "The selected Codex account is unavailable"))
        # Codex bucket not found (882-883).
        self.assertEqual(
            mgr._usage_window({"target_provider": "codex",
                               "usage_account_id": "acct",
                               "usage_window_id": "daily"}, codex_usage),
            (None, "The selected Codex usage window is unavailable"))

    def test_usage_reset_without_evidence_is_deferred(self):
        # _usage_window error inside tick -> transient (990-992).
        item = self.create(kind="usage_reset", usage_account_id="acct",
                           usage_window_id="weekly",
                           observed_reset_at=self.clock() - 1)
        self.manager.tick(snapshot(session()), {},          # no usage evidence
                          lambda _: {"ok": True}, lambda _: {})
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "waiting_usage_reset")
        self.assertGreater(row["next_attempt_at"], self.clock())

    # ---- tick spawn / dispatch failure branches ---------------------------
    def test_spawn_claim_failure_leaves_row_pending(self):
        # _claim returns False -> continue (1026-1027); spawn is never called.
        item = self.create(kind="new_session", trigger_at=self.clock(),
                           target_session_id=None,
                           spawn_spec={"provider": "codex", "cwd": self.tmp.name})
        self.manager._claim = lambda record, state: False
        spawned = []
        self.manager.tick(snapshot(), {}, lambda _: {"ok": True},
                          lambda row: spawned.append(row) or {"ok": True})
        self.assertFalse(spawned)
        self.assertEqual(self.manager.get(item["id"])["state"], "scheduled")

    def test_spawn_exception_fails_row(self):
        item = self.create(kind="new_session", trigger_at=self.clock(),
                           target_session_id=None,
                           spawn_spec={"provider": "codex", "cwd": self.tmp.name})

        def boom(_row):
            raise RuntimeError("spawn blew up")

        self.manager.tick(snapshot(), {}, lambda _: {"ok": True}, boom)  # 1030-1032
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "failed")
        self.assertIn("spawn blew up", row["error"])

    def test_spawn_result_not_ok_fails_with_kind_message(self):
        item = self.create(kind="new_session", trigger_at=self.clock(),
                           target_session_id=None,
                           spawn_spec={"provider": "codex", "cwd": self.tmp.name})
        self.manager.tick(snapshot(), {}, lambda _: {"ok": True},
                          lambda _row: {"ok": False})                     # 1034-1038
        self.assertEqual(self.manager.get(item["id"])["error"],
                         "session spawn failed")

    def test_resume_result_not_ok_reports_resume_failure(self):
        item = self.manager.create_closed_resume(
            message="continue", target_provider="codex",
            target_session_id="codex:closed", idempotency_key="resume-fail-1")
        self.manager.tick(snapshot(), {}, lambda _: {"ok": True},
                          lambda _row: {"ok": False})                     # resume branch 1037
        self.assertEqual(self.manager.get(item["id"])["error"],
                         "session resume failed")

    def test_dispatch_claim_failure_skips_dispatch(self):
        item = self.create()
        self.manager._claim = lambda record, state: False   # 1064-1065 continue
        dispatched = []
        self.manager.tick(snapshot(session()), {},
                          lambda row: dispatched.append(row) or {"ok": True},
                          lambda _: {})
        self.assertFalse(dispatched)
        self.assertEqual(self.manager.get(item["id"])["state"], "waiting_availability")

    def test_dispatch_exception_fails_row(self):
        item = self.create()

        def boom(_row):
            raise RuntimeError("dispatch blew up")

        self.manager.tick(snapshot(session()), {}, boom, lambda _: {})   # 1068-1070
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "failed")
        self.assertIn("dispatch blew up", row["error"])

    def test_dispatch_queueable_result_waits_for_provider(self):
        item = self.create()
        self.manager.tick(snapshot(session()), {},
                          lambda _row: {"ok": False, "queueable": True},  # 1080-1084
                          lambda _: {})
        self.assertEqual(self.manager.get(item["id"])["state"], "waiting_provider")

    def test_dispatch_control_unavailable_waits_for_provider(self):
        item = self.create()
        self.manager.tick(snapshot(session()), {},
                          lambda _row: {"ok": False,
                                        "code": "provider_control_unavailable"},
                          lambda _: {})
        self.assertEqual(self.manager.get(item["id"])["state"], "waiting_provider")

    def test_dispatch_plain_failure_marks_failed(self):
        item = self.create()
        self.manager.tick(snapshot(session()), {},
                          lambda _row: {"ok": False, "error": "rejected"},  # 1085-1087
                          lambda _: {})
        row = self.manager.get(item["id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error"], "rejected")

    def test_dispatch_bare_failure_uses_default_message(self):
        item = self.create()
        self.manager.tick(snapshot(session()), {},
                          lambda _row: {}, lambda _: {})     # 1086 default message
        self.assertEqual(self.manager.get(item["id"])["error"],
                         "provider rejected the message")


if __name__ == "__main__":
    unittest.main()
