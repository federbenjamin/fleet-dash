"""Durable briefings, notification audit, budgets, and honest forecasts."""
from __future__ import annotations

import contextlib
import base64
import binascii
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import sqlite3
import statistics
import threading
import time
import uuid
from collections import deque
from urllib.parse import urlsplit
from datetime import datetime, time as datetime_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EVENT_CATEGORIES = {
    "attention", "completed", "slow", "outcome", "budget", "measurement",
    "notification",
}
BUDGET_SCOPES = {"fleet", "provider", "workstream", "session"}
BUDGET_METRICS = {"usd", "tokens", "runtime", "concurrency"}
DEVICE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
NOTIFICATION_STATES = {"active", "snoozed", "resolved", "expired"}
NOTIFICATION_KINDS = {
    "question", "approval", "form", "reply", "failure", "stall", "completion",
    "artifact", "outcome", "budget", "measurement", "notification",
}
NOTIFICATION_SEVERITIES = {"info", "warning", "critical"}
PUSH_PERMISSION_STATES = {"granted", "denied", "prompt", "expired", "unsupported"}
DEFAULT_PUSH_ORIGINS = {
    "https://fcm.googleapis.com",
    "https://updates.push.services.mozilla.com",
    "https://web.push.apple.com",
}
PUSH_DELIVERY_STATES = {
    "queued", "sending", "retrying", "sent", "failed", "suppressed",
    "subscription_expired",
}
PUSH_RETRY_DELAYS = (2, 10, 30, 120, 600)
PUSHABLE_KINDS = set(NOTIFICATION_KINDS)
INFORMATIONAL_NOTIFICATION_KINDS = {
    "completion", "artifact", "outcome", "budget", "measurement", "notification"}
PUSH_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
PUSH_SEVERITY_ALIASES = {"success": "info", "high": "critical"}
PUSH_DELIVERY_PURPOSES = {"initial", "reminder", "snooze_wake", "manual_retry", "test"}
NOTIFICATION_POLICY_MODES = {"off", "once", "remind_once", "repeat"}
DEFAULT_KIND_POLICY = {
    kind: ({"in_app_enabled": True, "mode": "once", "minimum_severity": "info",
            "initial_delay_seconds": 0, "repeat_interval_seconds": 900,
            "max_deliveries": 1, "allow_during_quiet_hours": False}
           if kind == "stall" else
           {"in_app_enabled": True, "mode": "remind_once", "minimum_severity": "info",
            "initial_delay_seconds": 0, "repeat_interval_seconds": 900,
            "max_deliveries": 2, "allow_during_quiet_hours": False}
           if kind in {"question", "approval", "form", "reply", "failure"} else
           {"in_app_enabled": True, "mode": "off", "minimum_severity": "info",
            "initial_delay_seconds": 0, "repeat_interval_seconds": 900,
            "max_deliveries": 1, "allow_during_quiet_hours": False})
    for kind in NOTIFICATION_KINDS}


class OperationsError(ValueError):
    """A user-correctable briefing or budget request error."""


class FleetOperations:
    """SQLite-backed operational event stream and budget evaluator.

    The class deliberately consumes Fleet's normalized snapshot. Provider adapters
    remain responsible for deciding which measurements are exact or unavailable.
    """

    def __init__(self, db_path, *, clock=time.time, id_factory=None,
                 event_id_factory=None, delivery_id_factory=None,
                 delivery_queue_limit=1000, delivery_lease_seconds=30,
                 delivery_retry_delays=None, delivery_jitter=None):
        self.db_path = db_path
        self.clock = clock
        self.id_factory = id_factory or (lambda: "bud-" + uuid.uuid4().hex)
        self.event_id_factory = event_id_factory or (lambda: "evt-" + uuid.uuid4().hex)
        self.delivery_id_factory = delivery_id_factory or (lambda: "push-" + uuid.uuid4().hex)
        self.delivery_queue_limit = max(1, int(delivery_queue_limit))
        self.delivery_lease_seconds = max(5, min(300, int(delivery_lease_seconds)))
        self.delivery_retry_delays = tuple(delivery_retry_delays or PUSH_RETRY_DELAYS)
        self.delivery_jitter = delivery_jitter or (
            lambda delay: random.uniform(float(delay) * .8, float(delay) * 1.2))
        self.lock = threading.RLock()
        self.measurement_signatures = {}
        self.briefing_generation = 0
        self.notification_projection_signature = None
        self.db_connect_ms = deque(maxlen=240)
        self.db_begin_ms = deque(maxlen=240)
        self.notification_projection_ms = deque(maxlen=240)
        self.notification_projection_parts_ms = {
            name: deque(maxlen=240) for name in ("sources", "providers", "lifecycle", "briefing")}
        self.notification_enqueue_ms = deque(maxlen=240)
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _connect(self):
        started = time.perf_counter()
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        self._tighten_db_permissions()
        self.db_connect_ms.append((time.perf_counter() - started) * 1000)
        return db

    def _tighten_db_permissions(self):
        for path in (self.db_path, self.db_path + "-wal", self.db_path + "-shm"):
            try:
                if not os.path.islink(path):
                    os.chmod(path, 0o600, follow_symlinks=False)
            except FileNotFoundError:
                pass

    @contextlib.contextmanager
    def _transaction(self, immediate=False):
        db = self._connect()
        try:
            started = time.perf_counter()
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            self.db_begin_ms.append((time.perf_counter() - started) * 1000)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            self._tighten_db_permissions()
            db.close()

    def diagnostics(self):
        def percentile(values, quantile):
            ordered = sorted(values)
            if not ordered:
                return 0.0
            index = min(len(ordered)-1, max(0, round((len(ordered)-1)*quantile)))
            return round(ordered[index], 3)
        return {"connect_p95_ms": percentile(self.db_connect_ms, .95),
                "begin_wait_p95_ms": percentile(self.db_begin_ms, .95),
                "notification_projection_p95_ms": percentile(
                    self.notification_projection_ms, .95),
                "notification_projection_samples": len(self.notification_projection_ms),
                "notification_projection_parts_p95_ms": {
                    name: percentile(values, .95)
                    for name, values in self.notification_projection_parts_ms.items()},
                "notification_enqueue_p95_ms": percentile(
                    self.notification_enqueue_ms, .95),
                "notification_enqueue_samples": len(self.notification_enqueue_ms),
                "samples": max(len(self.db_connect_ms), len(self.db_begin_ms))}

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations_meta(
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS briefing_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
                category TEXT NOT NULL, severity TEXT NOT NULL,
                title TEXT NOT NULL, summary TEXT NOT NULL,
                provider TEXT, session_id TEXT, workstream_id TEXT,
                source_type TEXT NOT NULL, source_id TEXT,
                link_kind TEXT, link_id TEXT, muted INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}')""")
            db.execute("""CREATE INDEX IF NOT EXISTS briefing_events_created
                ON briefing_events(id DESC, category)""")
            db.execute("""CREATE TABLE IF NOT EXISTS briefing_reviews(
                device_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS budgets(
                id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT,
                metric TEXT NOT NULL, limit_value REAL NOT NULL,
                block_spawns INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1, label TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS budgets_scope
                ON budgets(enabled, scope_type, scope_id, metric)""")
            columns = {row[1] for row in
                       db.execute("PRAGMA table_info(notification_deliveries)").fetchall()}
            if "event_key" in columns:
                db.execute("ALTER TABLE notification_deliveries RENAME TO notification_deliveries_legacy")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_deliveries_legacy(
                event_key TEXT PRIMARY KEY, category TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""UPDATE notification_deliveries_legacy
                SET error='Legacy ntfy delivery failed'
                WHERE status='failed' AND error IS NOT NULL""")
            db.execute("""UPDATE briefing_events
                SET title='Legacy ntfy test failed', summary='Legacy ntfy delivery failed'
                WHERE event_key LIKE 'notification-failed:%'""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_events(
                id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
                event_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, state TEXT NOT NULL,
                severity TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                provider TEXT, session_id TEXT, workstream_id TEXT,
                source_type TEXT NOT NULL, source_id TEXT, source_revision TEXT NOT NULL,
                opened_at REAL NOT NULL, changed_at REAL NOT NULL, resolved_at REAL,
                snoozed_until REAL, reminder_budget INTEGER NOT NULL DEFAULT 0,
                last_push_at REAL, payload_json TEXT NOT NULL DEFAULT '{}')""")
            db.execute("""CREATE INDEX IF NOT EXISTS notification_events_state
                ON notification_events(state, sequence DESC)""")
            db.execute("""CREATE INDEX IF NOT EXISTS notification_events_session
                ON notification_events(session_id, state)""")
            db.execute("""UPDATE notification_events
                SET title='Legacy ntfy test failed', summary='Legacy ntfy delivery failed'
                WHERE source_type='briefing' AND source_id LIKE 'notification-failed:%'""")
            db.execute("""UPDATE notification_events SET severity=CASE severity
                WHEN 'success' THEN 'info' WHEN 'high' THEN 'critical' END
                WHERE severity IN ('success','high')""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_devices(
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL, platform TEXT,
                subscription_json TEXT NOT NULL, endpoint_origin TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, permission_state TEXT NOT NULL,
                created_at REAL NOT NULL, last_registered_at REAL NOT NULL,
                last_success_at REAL, test_success_at REAL,
                last_failure_at REAL, last_failure TEXT,
                read_cursor INTEGER NOT NULL DEFAULT 0,
                preferences_json TEXT NOT NULL DEFAULT '{}')""")
            db.execute("""CREATE INDEX IF NOT EXISTS notification_devices_enabled
                ON notification_devices(enabled, permission_state)""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_read_cursors(
                device_id TEXT PRIMARY KEY, read_cursor INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_deliveries(
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL, device_id TEXT NOT NULL,
                generation INTEGER NOT NULL, purpose TEXT NOT NULL DEFAULT 'initial',
                source_revision TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL,
                claimed_at REAL, lease_until REAL, next_attempt_at REAL,
                remote_status INTEGER, remote_id TEXT, error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(event_id,device_id,generation))""")
            db.execute("""CREATE INDEX IF NOT EXISTS notification_delivery_due
                ON notification_deliveries(status, next_attempt_at)""")
            db.execute("""CREATE INDEX IF NOT EXISTS notification_delivery_event
                ON notification_deliveries(event_id, device_id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_session_mutes(
                session_id TEXT PRIMARY KEY, provider TEXT, muted_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_capability_uses(
                jti_hash TEXT PRIMARY KEY, event_id TEXT NOT NULL, device_id TEXT NOT NULL,
                action TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at REAL NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS notification_capability_expiry
                ON notification_capability_uses(expires_at)""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_global_policy(
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                enabled INTEGER NOT NULL, quiet_hours_enabled INTEGER NOT NULL,
                quiet_start_minute INTEGER NOT NULL, quiet_end_minute INTEGER NOT NULL,
                timezone TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_kind_policy(
                kind TEXT PRIMARY KEY, in_app_enabled INTEGER NOT NULL,
                in_app_effective_after REAL NOT NULL DEFAULT 0,
                mode TEXT NOT NULL, minimum_severity TEXT NOT NULL,
                initial_delay_seconds INTEGER NOT NULL, repeat_interval_seconds INTEGER NOT NULL,
                max_deliveries INTEGER NOT NULL, allow_during_quiet_hours INTEGER NOT NULL,
                effective_after REAL NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL, push_revision INTEGER NOT NULL,
                updated_at REAL NOT NULL)""")
            now = self.clock()
            db.execute("""INSERT OR IGNORE INTO notification_global_policy(
                singleton_id,enabled,quiet_hours_enabled,quiet_start_minute,
                quiet_end_minute,timezone,revision,updated_at)
                VALUES(1,1,0,1320,420,'UTC',1,?)""", (now,))
            kind_columns = {row[1] for row in
                            db.execute("PRAGMA table_info(notification_kind_policy)").fetchall()}
            if "in_app_enabled" not in kind_columns:
                db.execute("""ALTER TABLE notification_kind_policy ADD COLUMN
                    in_app_enabled INTEGER NOT NULL DEFAULT 1""")
            if "in_app_effective_after" not in kind_columns:
                db.execute("""ALTER TABLE notification_kind_policy ADD COLUMN
                    in_app_effective_after REAL NOT NULL DEFAULT 0""")
            if "push_revision" not in kind_columns:
                db.execute("""ALTER TABLE notification_kind_policy ADD COLUMN
                    push_revision INTEGER NOT NULL DEFAULT 1""")
                db.execute("UPDATE notification_kind_policy SET push_revision=revision")
            for kind, policy in sorted(DEFAULT_KIND_POLICY.items()):
                db.execute("""INSERT OR IGNORE INTO notification_kind_policy(
                    kind,in_app_enabled,in_app_effective_after,mode,minimum_severity,initial_delay_seconds,
                    repeat_interval_seconds,max_deliveries,allow_during_quiet_hours,
                    effective_after,revision,push_revision,updated_at)
                    VALUES(?,1,0,?,?,?,?,?,?,0,1,1,?)""", (
                    kind, policy["mode"], policy["minimum_severity"],
                    policy["initial_delay_seconds"], policy["repeat_interval_seconds"],
                    policy["max_deliveries"],
                    1 if policy["allow_during_quiet_hours"] else 0, now))
            device_columns = {row[1] for row in
                              db.execute("PRAGMA table_info(notification_devices)").fetchall()}
            if "test_success_at" not in device_columns:
                db.execute("ALTER TABLE notification_devices ADD COLUMN test_success_at REAL")
            delivery_columns = {row[1] for row in
                                db.execute("PRAGMA table_info(notification_deliveries)").fetchall()}
            if "purpose" not in delivery_columns:
                db.execute("""ALTER TABLE notification_deliveries ADD COLUMN
                    purpose TEXT NOT NULL DEFAULT 'initial'""")
            if "source_revision" not in delivery_columns:
                db.execute("ALTER TABLE notification_deliveries ADD COLUMN source_revision TEXT")
            if "cadence_index" not in delivery_columns:
                db.execute("""ALTER TABLE notification_deliveries ADD COLUMN
                    cadence_index INTEGER NOT NULL DEFAULT 0""")
            if "global_policy_revision" not in delivery_columns:
                db.execute("""ALTER TABLE notification_deliveries ADD COLUMN
                    global_policy_revision INTEGER""")
            if "kind_policy_revision" not in delivery_columns:
                db.execute("""ALTER TABLE notification_deliveries ADD COLUMN
                    kind_policy_revision INTEGER""")
            db.execute("""UPDATE notification_deliveries SET purpose='test'
                WHERE event_id IN (SELECT id FROM notification_events
                    WHERE source_type='push_test')""")
            db.execute("""UPDATE notification_devices SET test_success_at=COALESCE(
                test_success_at,(SELECT MAX(d.updated_at) FROM notification_deliveries d
                    JOIN notification_events e ON e.id=d.event_id
                    WHERE d.device_id=notification_devices.id AND d.status='sent'
                    AND e.source_type='push_test'))""")
            db.execute("""CREATE TABLE IF NOT EXISTS session_measurements(
                session_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                workstream_id TEXT, project TEXT, model TEXT,
                cost REAL, cost_known INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER, runtime_seconds REAL, concurrency INTEGER,
                state TEXT, ui_group TEXT, revision TEXT, files_n INTEGER,
                repo_signature TEXT, muted INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS measurements_history
                ON session_measurements(provider, model, project, updated_at DESC)""")
            db.execute("""CREATE TABLE IF NOT EXISTS metric_samples(
                id INTEGER PRIMARY KEY AUTOINCREMENT, sample_key TEXT NOT NULL,
                metric TEXT NOT NULL, at REAL NOT NULL, value REAL NOT NULL,
                measurement_scope TEXT NOT NULL)""")
            db.execute("""CREATE INDEX IF NOT EXISTS metric_samples_key
                ON metric_samples(sample_key, metric, at DESC)""")
            self._set_meta(db, "notification_schema_version", 4)

    @staticmethod
    def _text(value, limit):
        return str(value or "").replace("\x00", " ").strip()[:limit]

    @staticmethod
    def _json(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _base64url(value, label, length):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise OperationsError(f"invalid notification {label}")
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (binascii.Error, ValueError, TypeError):
            raise OperationsError(f"invalid notification {label}")
        if len(decoded) != length:
            raise OperationsError(f"invalid notification {label}")
        return value

    @classmethod
    def _push_subscription(cls, subscription, extra_origins=None):
        if not isinstance(subscription, dict):
            raise OperationsError("invalid notification subscription")
        endpoint = subscription.get("endpoint")
        if (not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048 or
                any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)):
            raise OperationsError("invalid notification endpoint")
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except (binascii.Error, ValueError):
            raise OperationsError("invalid notification endpoint")
        host = (parsed.hostname or "").rstrip(".").lower()
        if (parsed.scheme != "https" or not host or port not in (None, 443) or
                parsed.username is not None or parsed.password is not None or
                parsed.fragment or len(host) > 253 or len(parsed.path) > 1536 or
                len(parsed.query) > 1024):
            raise OperationsError("invalid notification endpoint")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise OperationsError("invalid notification endpoint")
        origin = "https://" + host
        allowed = set(DEFAULT_PUSH_ORIGINS)
        for value in extra_origins or ():
            try:
                candidate = urlsplit(str(value))
                candidate_port = candidate.port
            except (TypeError, ValueError):
                continue
            if (candidate.scheme == "https" and candidate.hostname and
                    candidate_port in (None, 443) and not candidate.path.strip("/") and
                    not candidate.query and not candidate.fragment and
                    candidate.username is None and candidate.password is None):
                allowed.add("https://" + candidate.hostname.rstrip(".").lower())
        if origin not in allowed and not host.endswith(".notify.windows.com"):
            raise OperationsError("notification push service is not allowed")
        keys = subscription.get("keys")
        if not isinstance(keys, dict):
            raise OperationsError("invalid notification subscription keys")
        p256dh = cls._base64url(keys.get("p256dh"), "p256dh key", 65)
        try:
            public_key = base64.urlsafe_b64decode(p256dh + "=" * (-len(p256dh) % 4))
        except (binascii.Error, ValueError):
            raise OperationsError("invalid notification p256dh key")
        if public_key[0] != 4:
            raise OperationsError("invalid notification p256dh key")
        auth = cls._base64url(keys.get("auth"), "auth key", 16)
        expiration = subscription.get("expirationTime")
        if expiration is not None:
            if isinstance(expiration, bool) or not isinstance(expiration, (int, float)) \
               or not math.isfinite(expiration) or expiration < 0 or expiration > 9e15:
                raise OperationsError("invalid notification expiration")
        normalized = {"endpoint": endpoint, "expirationTime": expiration,
                      "keys": {"p256dh": p256dh, "auth": auth}}
        if len(cls._json(normalized)) > 4096:
            raise OperationsError("notification subscription is too large")
        return normalized, origin

    @staticmethod
    def _device_preferences(preferences):
        if preferences is None:
            return None
        if not isinstance(preferences, dict):
            raise OperationsError("invalid notification preferences")
        unknown = set(preferences) - {"kinds", "minimum_severity", "initial_delay_seconds"}
        if unknown:
            raise OperationsError("invalid notification preferences")
        out = {}
        if "kinds" in preferences:
            kinds = preferences["kinds"]
            if not isinstance(kinds, list) or len(kinds) > len(NOTIFICATION_KINDS):
                raise OperationsError("invalid notification kinds")
            normalized = []
            for item in kinds:
                if item not in NOTIFICATION_KINDS:
                    raise OperationsError("invalid notification kinds")
                if item not in normalized:
                    normalized.append(item)
            out["kinds"] = normalized
        if "minimum_severity" in preferences:
            severity = preferences["minimum_severity"]
            if severity not in NOTIFICATION_SEVERITIES:
                raise OperationsError("invalid notification severity")
            out["minimum_severity"] = severity
        if "initial_delay_seconds" in preferences:
            delay = preferences["initial_delay_seconds"]
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) \
               or not math.isfinite(delay) or not 0 <= delay <= 3600:
                raise OperationsError("invalid notification delay")
            out["initial_delay_seconds"] = int(delay)
        return out

    @staticmethod
    def _notification_device(row):
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["tested"] = bool(item.pop("test_success_at", None))
        try:
            item["preferences"] = json.loads(item.pop("preferences_json") or "{}")
        except (TypeError, ValueError):
            item["preferences"] = {}
        last_success = float(item.get("last_success_at") or 0)
        last_failure = float(item.get("last_failure_at") or 0)
        if not item["enabled"]:
            health = "disabled"
        elif item.get("permission_state") != "granted":
            health = item.get("permission_state") or "unavailable"
        elif not item["tested"]:
            health = "registered"
        elif last_failure > last_success:
            health = "failing"
        elif last_success:
            health = "healthy"
        else:
            health = "registered"
        item["health"] = health
        return item

    @staticmethod
    def _global_policy(row):
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["quiet_hours_enabled"] = bool(item["quiet_hours_enabled"])
        return item

    @staticmethod
    def _kind_policy(row):
        item = dict(row)
        item["in_app_enabled"] = bool(item["in_app_enabled"])
        item["allow_during_quiet_hours"] = bool(item["allow_during_quiet_hours"])
        return item

    @staticmethod
    def _local_boundary_epoch(day, minute, zone):
        """Resolve a wall-clock quiet boundary safely across DST transitions.

        A repeated time uses the later occurrence so quiet hours do not end in
        the middle of the repeated hour. A nonexistent spring-forward time
        advances to the first real local minute.
        """
        naive = datetime.combine(
            day, datetime_time(int(minute) // 60, int(minute) % 60))
        for advance in range(181):
            candidate = naive + timedelta(minutes=advance)
            epochs = []
            for fold in (0, 1):
                aware = candidate.replace(tzinfo=zone, fold=fold)
                epoch = aware.timestamp()
                back = datetime.fromtimestamp(epoch, zone)
                if back.replace(tzinfo=None) == candidate:
                    epochs.append(epoch)
            if epochs:
                return max(set(epochs))
        return naive.replace(tzinfo=zone).timestamp()

    @staticmethod
    def _quiet_state(policy, now):
        if not policy.get("quiet_hours_enabled"):
            return False, None
        try:
            zone = ZoneInfo(policy["timezone"])
        except (KeyError, ZoneInfoNotFoundError):
            zone = ZoneInfo("UTC")
        current = datetime.fromtimestamp(float(now), zone)
        start_minute = int(policy["quiet_start_minute"])
        end_minute = int(policy["quiet_end_minute"])
        if start_minute == end_minute:
            return True, FleetOperations._local_boundary_epoch(
                current.date() + timedelta(days=1), end_minute, zone)
        minute = current.hour * 60 + current.minute
        overnight = start_minute > end_minute
        active = ((minute >= start_minute or minute < end_minute) if overnight else
                  start_minute <= minute < end_minute)
        if not active:
            return False, None
        end_day = current.date()
        if overnight and minute >= start_minute:
            end_day += timedelta(days=1)
        return True, FleetOperations._local_boundary_epoch(end_day, end_minute, zone)

    def notification_policy_snapshot(self):
        with self.lock, self._connect() as db:
            global_row = db.execute(
                "SELECT * FROM notification_global_policy WHERE singleton_id=1").fetchone()
            kind_rows = db.execute(
                "SELECT * FROM notification_kind_policy ORDER BY kind").fetchall()
            active_counts = {row[0]: int(row[1]) for row in db.execute("""
                SELECT kind,COUNT(*) FROM notification_events
                WHERE state='active' GROUP BY kind""").fetchall()}
            next_rows = db.execute("""SELECT d.id,d.event_id,d.device_id,d.purpose,
                    d.next_attempt_at,d.status,e.kind,e.title
                FROM notification_deliveries d JOIN notification_events e ON e.id=d.event_id
                WHERE d.status IN ('queued','retrying') ORDER BY d.next_attempt_at LIMIT 20""").fetchall()
            recent_rows = db.execute("""SELECT d.id,d.event_id,d.device_id,d.purpose,
                    d.status,d.attempt,d.remote_status,d.updated_at,e.kind,e.title
                FROM notification_deliveries d JOIN notification_events e ON e.id=d.event_id
                WHERE d.status IN ('sent','failed','suppressed','subscription_expired')
                ORDER BY d.updated_at DESC LIMIT 30""").fetchall()
            muted = [dict(row) for row in db.execute("""SELECT session_id,provider,muted_at
                FROM notification_session_mutes ORDER BY muted_at DESC LIMIT 1000""").fetchall()]
        kinds = []
        for row in kind_rows:
            item = self._kind_policy(row)
            item["active_matches"] = active_counts.get(item["kind"], 0)
            kinds.append(item)
        quiet, quiet_end = self._quiet_state(self._global_policy(global_row), self.clock())
        return {"ok": True, "global": {**self._global_policy(global_row),
                    "quiet_now": quiet, "quiet_ends_at": quiet_end},
                "kinds": kinds, "muted_sessions": muted,
                "next_deliveries": [dict(row) for row in next_rows],
                "recent_deliveries": [dict(row) for row in recent_rows]}

    def notification_policy_update(self, payload):
        if not isinstance(payload, dict):
            raise OperationsError("invalid notification policy update")
        scope = payload.get("scope")
        patch = payload.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise OperationsError("notification policy update is empty")
        try:
            expected = int(payload.get("expected_revision"))
        except (TypeError, ValueError):
            raise OperationsError("notification policy revision is required")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            if scope == "global":
                allowed = {"enabled", "quiet_hours_enabled", "quiet_start_minute",
                           "quiet_end_minute", "timezone"}
                if set(patch) - allowed:
                    raise OperationsError("unknown global notification setting")
                row = db.execute(
                    "SELECT * FROM notification_global_policy WHERE singleton_id=1").fetchone()
                if int(row["revision"]) != expected:
                    raise OperationsError("notification policy changed; refresh and try again")
                values = dict(row)
                values.update(patch)
                for key in ("enabled", "quiet_hours_enabled"):
                    if not isinstance(values[key], bool) and values[key] not in (0, 1):
                        raise OperationsError(f"{key.replace('_', ' ')} must be on or off")
                for key in ("quiet_start_minute", "quiet_end_minute"):
                    if isinstance(values[key], bool) or not isinstance(values[key], (int, float)) \
                       or int(values[key]) != values[key] or not 0 <= int(values[key]) < 1440:
                        raise OperationsError("quiet-hour times are invalid")
                try:
                    ZoneInfo(str(values["timezone"]))
                except (ZoneInfoNotFoundError, ValueError):
                    raise OperationsError("quiet hours need a valid IANA timezone")
                revision = expected + 1
                changed = db.execute("""UPDATE notification_global_policy SET enabled=?,
                    quiet_hours_enabled=?,quiet_start_minute=?,quiet_end_minute=?,timezone=?,
                    revision=?,updated_at=? WHERE singleton_id=1 AND revision=?""", (
                    1 if values["enabled"] else 0,
                    1 if values["quiet_hours_enabled"] else 0,
                    int(values["quiet_start_minute"]), int(values["quiet_end_minute"]),
                    str(values["timezone"]), revision, now, expected)).rowcount
                if not changed:
                    raise OperationsError("notification policy changed; refresh and try again")
                db.execute("""UPDATE notification_deliveries SET status='suppressed',
                    error='global policy changed',updated_at=?
                    WHERE status IN ('queued','retrying') AND purpose!='test'""", (now,))
            elif scope == "kind":
                kind = self._text(payload.get("kind"), 40)
                if kind not in NOTIFICATION_KINDS:
                    raise OperationsError("unknown notification kind")
                allowed = {"in_app_enabled", "mode", "minimum_severity", "initial_delay_seconds",
                           "repeat_interval_seconds", "max_deliveries",
                           "allow_during_quiet_hours"}
                if set(patch) - allowed:
                    raise OperationsError("unknown notification rule setting")
                row = db.execute(
                    "SELECT * FROM notification_kind_policy WHERE kind=?", (kind,)).fetchone()
                if int(row["revision"]) != expected:
                    raise OperationsError("notification rule changed; refresh and try again")
                values = dict(row)
                values.update(patch)
                if not isinstance(values["in_app_enabled"], bool) and \
                        values["in_app_enabled"] not in (0, 1):
                    raise OperationsError("in-app notification setting must be on or off")
                if values["mode"] not in NOTIFICATION_POLICY_MODES:
                    raise OperationsError("unknown notification cadence")
                if values["minimum_severity"] not in NOTIFICATION_SEVERITIES:
                    raise OperationsError("unknown notification severity")
                numeric = (("initial_delay_seconds", 0, 86400),
                           ("repeat_interval_seconds", 60, 604800),
                           ("max_deliveries", 1, 100))
                for key, low, high in numeric:
                    value = values[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) \
                       or int(value) != value or not low <= int(value) <= high:
                        raise OperationsError(f"invalid {key.replace('_', ' ')}")
                if not isinstance(values["allow_during_quiet_hours"], bool) and \
                        values["allow_during_quiet_hours"] not in (0, 1):
                    raise OperationsError("quiet-hours override must be on or off")
                possible_daily = (1 if values["mode"] in ("off", "once") else
                                  min(int(values["max_deliveries"]),
                                      1 + 86400 // int(values["repeat_interval_seconds"])))
                if possible_daily > 12 and payload.get("confirm_aggressive") is not True:
                    raise OperationsError(
                        "this rule can send more than 12 pushes per day; confirm the high cadence")
                revision = expected + 1
                push_fields = allowed - {"in_app_enabled"}
                push_changed = bool(set(patch) & push_fields)
                effective_after = ((0 if payload.get("apply_current") is True else now)
                                   if push_changed else float(row["effective_after"] or 0))
                push_revision = int(row["push_revision"]) + (1 if push_changed else 0)
                enabling_in_app = (bool(values["in_app_enabled"]) and
                                   not bool(row["in_app_enabled"]))
                in_app_after = ((0 if payload.get("apply_current") is True else now)
                                if enabling_in_app else
                                float(row["in_app_effective_after"] or 0))
                changed = db.execute("""UPDATE notification_kind_policy SET in_app_enabled=?,
                    in_app_effective_after=?,mode=?,
                    minimum_severity=?,initial_delay_seconds=?,repeat_interval_seconds=?,
                    max_deliveries=?,allow_during_quiet_hours=?,effective_after=?,revision=?,
                    push_revision=?,updated_at=? WHERE kind=? AND revision=?""", (
                    1 if values["in_app_enabled"] else 0, in_app_after,
                    values["mode"], values["minimum_severity"],
                    int(values["initial_delay_seconds"]),
                    int(values["repeat_interval_seconds"]), int(values["max_deliveries"]),
                    1 if values["allow_during_quiet_hours"] else 0,
                    effective_after, revision, push_revision, now, kind, expected)).rowcount
                if not changed:
                    raise OperationsError("notification rule changed; refresh and try again")
                if push_changed:
                    db.execute("""UPDATE notification_deliveries SET status='suppressed',
                        error='event rule changed',updated_at=? WHERE status IN ('queued','retrying')
                        AND purpose!='test'
                        AND event_id IN (SELECT id FROM notification_events WHERE kind=?)""",
                               (now, kind))
            else:
                raise OperationsError("unknown notification policy scope")
        return self.notification_policy_snapshot()

    @staticmethod
    def _signature(value):
        import hashlib
        return hashlib.sha256(FleetOperations._json(value).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _event(row):
        item = dict(row)
        item["muted"] = bool(item.get("muted"))
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            item["payload"] = {}
        return item

    def _insert_event(self, db, *, key, category, severity, title, summary,
                      provider=None, session_id=None, workstream_id=None,
                      source_type="fleet", source_id=None, link_kind=None,
                      link_id=None, muted=False, payload=None, created_at=None):
        if category not in EVENT_CATEGORIES:
            raise OperationsError("invalid briefing category")
        inserted = db.execute("""INSERT OR IGNORE INTO briefing_events(
            event_key,created_at,category,severity,title,summary,provider,session_id,
            workstream_id,source_type,source_id,link_kind,link_id,muted,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            self._text(key, 300), float(created_at or self.clock()), category,
            self._text(severity, 20) or "info", self._text(title, 160),
            self._text(summary, 800), self._text(provider, 30) or None,
            self._text(session_id, 320) or None, self._text(workstream_id, 120) or None,
            self._text(source_type, 40), self._text(source_id, 320) or None,
            self._text(link_kind, 40) or None, self._text(link_id, 320) or None,
            1 if muted else 0, self._json(payload or {})))
        if inserted.rowcount:
            self.briefing_generation += 1

    def _meta(self, db, key, default=None):
        row = db.execute("SELECT value_json FROM operations_meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return default

    def _set_meta(self, db, key, value):
        db.execute("""INSERT INTO operations_meta(key,value_json) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json""",
                   (key, self._json(value)))

    @staticmethod
    def _canonical_notification_key(*parts):
        material = FleetOperations._json([str(part or "") for part in parts])
        return "ntf-" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _notification_event(row, read_cursor=0):
        raw = dict(row)
        item = {key: raw.get(key) for key in (
            "id", "sequence", "kind", "state", "severity", "title", "summary",
            "provider", "session_id", "workstream_id", "source_revision", "opened_at",
            "changed_at", "resolved_at", "snoozed_until")}
        try:
            payload = json.loads(raw.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        item["payload"] = {key: payload.get(key) for key in ("link_kind", "link_id")
                           if payload.get(key) is not None}
        item["unread"] = int(item.get("sequence") or 0) > int(read_cursor or 0)
        return item

    def _upsert_notification_event(self, db, spec, now):
        event_key = self._text(spec.get("event_key"), 300)
        if not event_key:
            raise OperationsError("notification event key is required")
        kind = self._text(spec.get("kind"), 40)
        state = self._text(spec.get("state"), 20)
        if kind not in NOTIFICATION_KINDS or state not in NOTIFICATION_STATES:
            raise OperationsError("invalid notification event")
        raw_severity = self._text(spec.get("severity"), 20) or "info"
        severity = PUSH_SEVERITY_ALIASES.get(raw_severity, raw_severity)
        if severity not in NOTIFICATION_SEVERITIES:
            raise OperationsError("invalid notification severity")
        row = db.execute("SELECT * FROM notification_events WHERE event_key=?",
                         (event_key,)).fetchone()
        if row:
            next_state = state
            snoozed_until = row["snoozed_until"]
            if state == "active" and row["state"] == "snoozed":
                # The policy sweep owns snooze expiry and its single wake delivery.
                # Canonical reconciliation must not erase that evidence first.
                next_state = "snoozed"
            elif next_state != "snoozed":
                snoozed_until = None
            resolved_at = now if next_state in ("resolved", "expired") else None
            changed = any((
                row["kind"] != kind,
                row["state"] != next_state,
                row["severity"] != severity,
                row["title"] != self._text(spec.get("title"), 160),
                row["summary"] != self._text(spec.get("summary"), 800),
                row["source_revision"] != self._text(spec.get("source_revision"), 320),
                (row["payload_json"] or "{}") != self._json(spec.get("payload") or {}),
            ))
            if changed:
                db.execute("""UPDATE notification_events SET kind=?,state=?,severity=?,
                    title=?,summary=?,provider=?,session_id=?,workstream_id=?,source_type=?,
                    source_id=?,source_revision=?,changed_at=?,resolved_at=?,snoozed_until=?,
                    reminder_budget=?,payload_json=? WHERE event_key=?""", (
                    kind, next_state, severity,
                    self._text(spec.get("title"), 160), self._text(spec.get("summary"), 800),
                    self._text(spec.get("provider"), 30) or None,
                    self._text(spec.get("session_id"), 320) or None,
                    self._text(spec.get("workstream_id"), 120) or None,
                    self._text(spec.get("source_type"), 40),
                    self._text(spec.get("source_id"), 320) or None,
                    self._text(spec.get("source_revision"), 320), now, resolved_at,
                    snoozed_until, int(row["reminder_budget"] or 0),
                    self._json(spec.get("payload") or {}), event_key))
            return row["id"]
        sequence = int(db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM notification_events").fetchone()[0])
        event_id = self._text(self.event_id_factory(), 100)
        if not event_id:
            raise OperationsError("notification event ID is required")
        resolved_at = now if state in ("resolved", "expired") else None
        opened_at = float(spec.get("opened_at") or now)
        db.execute("""INSERT INTO notification_events(
            id,sequence,event_key,kind,state,severity,title,summary,provider,session_id,
            workstream_id,source_type,source_id,source_revision,opened_at,changed_at,
            resolved_at,snoozed_until,reminder_budget,last_push_at,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            event_id, sequence, event_key, kind, state,
            severity,
            self._text(spec.get("title"), 160), self._text(spec.get("summary"), 800),
            self._text(spec.get("provider"), 30) or None,
            self._text(spec.get("session_id"), 320) or None,
            self._text(spec.get("workstream_id"), 120) or None,
            self._text(spec.get("source_type"), 40),
            self._text(spec.get("source_id"), 320) or None,
            self._text(spec.get("source_revision"), 320), opened_at, now, resolved_at, None,
            int(spec.get("reminder_budget") or 0), None,
            self._json(spec.get("payload") or {})))
        return event_id

    def _action_notification_spec(self, action):
        action_kind = self._text(action.get("kind"), 40)
        kind = action_kind if action_kind in ("question", "approval", "form", "reply") \
            else "failure"
        if action_kind not in ("question", "approval", "form", "reply", "problem", "attention"):
            return None
        provider = self._text(action.get("provider") or "claude", 30)
        sid = self._text(action.get("session_id"), 320)
        revision = self._text(action.get("pending_nonce") or action.get("revision") or
                              action.get("action_id"), 320)
        if not sid or not revision:
            return None
        return {
            "event_key": self._canonical_notification_key(
                "action", provider, sid, kind, revision),
            "kind": kind, "state": "active", "severity": "warning",
            "title": action.get("title") or action.get("request") or "Fleet needs you",
            "summary": action.get("request") or action.get("delivery_state") or
                       action.get("reason") or "Response needed",
            "provider": provider, "session_id": sid, "source_type": "action",
            "source_id": action.get("pending_nonce") or sid,
            "source_revision": revision,
            "reminder_budget": 1,
            "payload": {"action_id": action.get("action_id"), "kind": action_kind,
                        "access": action.get("access")},
        }

    def _notification_projection_input(self, fleet):
        actions = [spec for spec in
                   (self._action_notification_spec(action)
                    for action in fleet.get("actions") or []) if spec]
        actions.sort(key=lambda item: item["event_key"])
        stall_threshold = max(30, int(float(
            (fleet.get("settings") or {}).get("stall_seconds") or 240)))
        sessions = []
        for session in fleet.get("sessions") or []:
            sid = self._text(session.get("session_id"), 320)
            if not sid:
                continue
            item = {"session_id": sid,
                    "provider": self._text(session.get("provider") or "claude", 30),
                    "muted": bool(session.get("muted"))}
            stalled = (session.get("state") == "stalled" or
                       session.get("normalized_state") == "stalled")
            if stalled and float(session.get("quiet_s") or 0) >= stall_threshold:
                item.update({"stall": True,
                             "revision": self._text(session.get("turn_id") or
                                 session.get("convo_v") or session.get("activity_at"), 320),
                             "title": self._text(session.get("title") or
                                 session.get("name"), 160)})
            sessions.append(item)
        sessions.sort(key=lambda item: item["session_id"])
        failures = {provider: {key: state.get(key) for key in
                    ("ok", "error", "state", "revision")}
                    for provider, state in (fleet.get("providers") or {}).items()
                    if state and state.get("ok") is False}
        return self._signature({"actions": actions, "sessions": sessions,
                                "provider_failures": failures,
                                "briefing_generation": self.briefing_generation,
                                "stall_threshold": stall_threshold}), bool(failures)

    def _reconcile_notification_events(self, db, fleet, now):
        part_started = time.perf_counter()
        current = {}
        for action in fleet.get("actions") or []:
            spec = self._action_notification_spec(action)
            if spec:
                current[spec["event_key"]] = spec

        stall_threshold = max(30, int(float(
            (fleet.get("settings") or {}).get("stall_seconds") or 240)))
        sessions = list(fleet.get("sessions") or [])
        live_mutes = {}
        live_unmuted = set()
        for session in sessions:
            sid = self._text(session.get("session_id"), 320)
            provider = self._text(session.get("provider") or "claude", 30)
            if sid:
                if session.get("muted"):
                    live_mutes[sid] = provider
                else:
                    live_unmuted.add(sid)
            if (session.get("state") != "stalled" and
                    session.get("normalized_state") != "stalled"):
                continue
            if float(session.get("quiet_s") or 0) < stall_threshold or not sid:
                continue
            revision = self._text(session.get("turn_id") or session.get("convo_v") or
                                  session.get("activity_at"), 320)
            spec = {
                "event_key": self._canonical_notification_key(
                    "stall", provider, sid, revision),
                "kind": "stall", "state": "active", "severity": "warning",
                "title": session.get("title") or session.get("name") or "Slow work",
                "summary": "Session stopped showing progress",
                "provider": provider, "session_id": sid, "source_type": "stall",
                "source_id": sid, "source_revision": revision, "reminder_budget": 0,
                "payload": {},
            }
            current[spec["event_key"]] = spec

        stored_mutes = {row[0]: row[1] for row in db.execute(
            "SELECT session_id,provider FROM notification_session_mutes").fetchall()}
        changed_mutes = [(sid, provider, now) for sid, provider in live_mutes.items()
                         if stored_mutes.get(sid) != provider]
        if changed_mutes:
            db.executemany("""INSERT INTO notification_session_mutes(
                session_id,provider,muted_at) VALUES(?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET provider=excluded.provider""",
                           changed_mutes)
        removed_mutes = stored_mutes.keys() & live_unmuted
        if removed_mutes:
            db.executemany("DELETE FROM notification_session_mutes WHERE session_id=?",
                           ((sid,) for sid in removed_mutes))
        self.notification_projection_parts_ms["sources"].append(
            (time.perf_counter() - part_started) * 1000)

        part_started = time.perf_counter()
        provider_seen = self._meta(db, "notification_provider_failure_seen", {}) or {}
        next_provider_seen = {}
        for provider, state in (fleet.get("providers") or {}).items():
            if not state or state.get("ok") is not False:
                continue
            revision = self._signature({
                "error": state.get("error"), "state": state.get("state"),
                "revision": state.get("revision")})
            prior = provider_seen.get(provider) or {}
            count = int(prior.get("count") or 0) + 1 if prior.get("revision") == revision else 1
            next_provider_seen[provider] = {"revision": revision, "count": count,
                                            "first_seen": prior.get("first_seen") or now}
            if count < 2:
                continue
            spec = {
                "event_key": self._canonical_notification_key(
                    "provider", provider, revision),
                "kind": "failure", "state": "active", "severity": "warning",
                "title": f"{str(provider).title()} unavailable",
                "summary": state.get("error") or "Provider evidence is stale",
                "provider": provider, "source_type": "provider",
                "source_id": provider, "source_revision": revision,
                "reminder_budget": 1, "payload": {},
            }
            current[spec["event_key"]] = spec
        if next_provider_seen != provider_seen:
            self._set_meta(db, "notification_provider_failure_seen", next_provider_seen)
        self.notification_projection_parts_ms["providers"].append(
            (time.perf_counter() - part_started) * 1000)

        part_started = time.perf_counter()
        for spec in current.values():
            self._upsert_notification_event(db, spec, now)
        delivery_current = {row[0] for row in db.execute("""SELECT e.event_key
            FROM notification_events e JOIN notification_devices v ON v.id=e.source_id
            WHERE e.source_type='delivery' AND e.state IN ('active','snoozed')
            AND v.last_failure_at IS NOT NULL
            AND v.last_failure_at>COALESCE(v.last_success_at,0)""").fetchall()}
        rows = db.execute("""SELECT event_key FROM notification_events
            WHERE state IN ('active','snoozed')
            AND source_type IN ('action','stall','provider','delivery')""").fetchall()
        missing = [row[0] for row in rows
                   if row[0] not in current and row[0] not in delivery_current]
        db.executemany("""UPDATE notification_events SET state='resolved',changed_at=?,
            resolved_at=?,snoozed_until=NULL WHERE event_key=?""",
                       ((now, now, event_key) for event_key in missing))
        self.notification_projection_parts_ms["lifecycle"].append(
            (time.perf_counter() - part_started) * 1000)

        part_started = time.perf_counter()
        cursor = int(self._meta(db, "notification_briefing_cursor", 0) or 0)
        rows = db.execute("""SELECT * FROM briefing_events WHERE id>? ORDER BY id""",
                          (cursor,)).fetchall()
        category_kind = {
            "completed": "completion", "outcome": "outcome", "budget": "budget",
            "measurement": "measurement", "notification": "notification",
            "attention": "failure", "slow": "stall",
        }
        for row in rows:
            kind = "artifact" if row["source_type"] == "artifact" else \
                category_kind.get(row["category"], "outcome")
            self._upsert_notification_event(db, {
                "event_key": self._canonical_notification_key(
                    "briefing", row["event_key"]),
                "kind": kind, "state": "resolved", "severity": row["severity"],
                "title": row["title"], "summary": row["summary"],
                "provider": row["provider"], "session_id": row["session_id"],
                "workstream_id": row["workstream_id"], "source_type": "briefing",
                "source_id": row["event_key"], "source_revision": row["event_key"],
                "opened_at": row["created_at"],
                "reminder_budget": 0,
                "payload": {"briefing_id": row["id"], "category": row["category"],
                            "link_kind": row["link_kind"], "link_id": row["link_id"]},
            }, now)
        if rows:
            self._set_meta(db, "notification_briefing_cursor", rows[-1]["id"])
        self.notification_projection_parts_ms["briefing"].append(
            (time.perf_counter() - part_started) * 1000)

    @staticmethod
    def _push_preferences_allow(event, device):
        try:
            preferences = json.loads(device["preferences_json"] or "{}")
        except (TypeError, ValueError):
            return False
        kinds = preferences.get("kinds")
        if kinds is not None and event["kind"] not in kinds:
            return False
        minimum = preferences.get("minimum_severity", "info")
        return (PUSH_SEVERITY_RANK.get(event["severity"], -1) >=
                PUSH_SEVERITY_RANK.get(minimum, 0))

    def _policy_delivery_insert(self, db, event, device, purpose, due, now, *,
                                cadence_index=0, global_revision=None,
                                kind_revision=None):
        if purpose not in PUSH_DELIVERY_PURPOSES:
            raise OperationsError("invalid notification delivery purpose")
        existing = db.execute("""SELECT * FROM notification_deliveries
            WHERE event_id=? AND device_id=? AND purpose=? AND cadence_index=?
            AND source_revision=? AND COALESCE(global_policy_revision,-1)=COALESCE(?,-1)
            AND COALESCE(kind_policy_revision,-1)=COALESCE(?,-1)
            ORDER BY generation DESC LIMIT 1""",
            (event["id"], device["id"], purpose, cadence_index,
             event["source_revision"], global_revision, kind_revision)).fetchone()
        if existing:
            return False
        queued = int(db.execute("""SELECT COUNT(*) FROM notification_deliveries
            WHERE status IN ('queued','sending','retrying')""").fetchone()[0])
        if queued >= self.delivery_queue_limit:
            return False
        delivery_id = self._text(self.delivery_id_factory(), 100)
        if not delivery_id:
            return False
        generation = int(db.execute("""SELECT COALESCE(MAX(generation),0)+1
            FROM notification_deliveries WHERE event_id=? AND device_id=?""",
            (event["id"], device["id"])).fetchone()[0])
        db.execute("""INSERT INTO notification_deliveries(
            id,event_id,device_id,generation,purpose,source_revision,status,attempt,
            next_attempt_at,created_at,updated_at,cadence_index,
            global_policy_revision,kind_policy_revision)
            VALUES(?,?,?,?,?,?,'queued',0,?,?,?,?,?,?)""", (
            delivery_id, event["id"], device["id"], generation, purpose,
            event["source_revision"], max(now, float(due)), now, now,
            int(cadence_index), global_revision, kind_revision))
        return True

    def _schedule_notification_deliveries(self, db, now):
        """Apply global, per-kind, quiet-hour, mute, and cadence policy."""
        expired = db.execute("""SELECT id FROM notification_events
            WHERE state='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until<=?""",
            (now,)).fetchall()
        expired_ids = {row[0] for row in expired}
        if expired_ids:
            db.executemany("""UPDATE notification_events SET state='active',
                snoozed_until=NULL,reminder_budget=0,changed_at=? WHERE id=?""",
                ((now, event_id) for event_id in expired_ids))
        db.execute("""UPDATE notification_deliveries SET status='suppressed',
            error='event snoozed',updated_at=? WHERE status IN ('queued','retrying')
            AND purpose IN ('initial','reminder') AND event_id IN
                (SELECT id FROM notification_events WHERE state='snoozed')""", (now,))

        global_row = db.execute(
            "SELECT * FROM notification_global_policy WHERE singleton_id=1").fetchone()
        global_policy = self._global_policy(global_row)
        if not global_policy["enabled"]:
            db.execute("""UPDATE notification_deliveries SET status='suppressed',
                error='push policy disabled',updated_at=? WHERE status IN ('queued','retrying')
                AND purpose!='test'""",
                       (now,))
            return 0
        kind_policy = {row["kind"]: self._kind_policy(row) for row in db.execute(
            "SELECT * FROM notification_kind_policy").fetchall()}
        events = db.execute("""SELECT * FROM notification_events
            WHERE state='active' OR (state='resolved' AND kind IN
                ('completion','artifact','outcome','budget','measurement','notification'))
            ORDER BY sequence""").fetchall()
        devices = db.execute("""SELECT * FROM notification_devices
            WHERE enabled=1 AND permission_state='granted' AND subscription_json!='{}'
            AND test_success_at IS NOT NULL
            AND (last_failure_at IS NULL OR last_success_at>=last_failure_at)
            ORDER BY id""").fetchall()
        inserted = 0
        for event in events:
            rule = kind_policy.get(event["kind"])
            if (not rule or rule["mode"] == "off" or
                    PUSH_SEVERITY_RANK.get(event["severity"], -1) <
                    PUSH_SEVERITY_RANK.get(rule["minimum_severity"], 0) or
                    max(float(event["opened_at"]), float(event["changed_at"])) <=
                    float(rule["effective_after"] or 0)):
                continue
            if event["session_id"] and db.execute(
                    "SELECT 1 FROM notification_session_mutes WHERE session_id=?",
                    (event["session_id"],)).fetchone():
                continue
            eligible = []
            for device in devices:
                if int(event["sequence"]) <= int(device["read_cursor"] or 0):
                    continue
                if event["source_type"] == "delivery" and event["source_id"] == device["id"]:
                    continue
                eligible.append(device)

            if event["id"] in expired_ids:
                prior_wake = db.execute("""SELECT 1 FROM notification_deliveries
                    WHERE event_id=? AND purpose='snooze_wake' LIMIT 1""",
                    (event["id"],)).fetchone()
                if not prior_wake:
                    for device in eligible:
                        inserted += self._policy_delivery_insert(
                            db, event, device, "snooze_wake", now, now,
                            cadence_index=0, global_revision=global_policy["revision"],
                            kind_revision=rule["push_revision"])
                continue
            for device in eligible:
                sent_rows = db.execute("""SELECT updated_at FROM notification_deliveries
                    WHERE event_id=? AND device_id=? AND source_revision=? AND status='sent'
                    AND purpose IN ('initial','reminder','snooze_wake')
                    ORDER BY updated_at""", (
                    event["id"], device["id"], event["source_revision"])).fetchall()
                sent_count = len(sent_rows)
                maximum = (1 if rule["mode"] == "once" else
                           2 if rule["mode"] == "remind_once" else
                           int(rule["max_deliveries"]))
                if event["state"] == "resolved":
                    maximum = 1
                if sent_count >= maximum:
                    continue
                cadence_index = sent_count
                purpose = "initial" if sent_count == 0 else "reminder"
                due = (float(event["opened_at"]) + int(rule["initial_delay_seconds"])
                       if sent_count == 0 else
                       float(sent_rows[-1]["updated_at"]) +
                       int(rule["repeat_interval_seconds"]))
                quiet, quiet_end = self._quiet_state(global_policy, max(now, due))
                if quiet and not rule["allow_during_quiet_hours"]:
                    due = max(due, quiet_end or due)
                inserted += self._policy_delivery_insert(
                    db, event, device, purpose, due, now,
                    cadence_index=cadence_index,
                    global_revision=global_policy["revision"],
                    kind_revision=rule["push_revision"])
        return inserted

    @staticmethod
    def _runtime(session, now):
        started = session.get("started_ms")
        if started is None:
            started = session.get("first_seen")
            if started is not None:
                try:
                    end = float(session.get("closed_at") or now)
                    return max(0.0, end - float(started))
                except (TypeError, ValueError):
                    return None
        try:
            return max(0.0, now - float(started) / 1000.0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _repo_signature(session):
        outcome = session.get("repo_outcome")
        return FleetOperations._signature(outcome) if outcome else ""

    def observe(self, fleet, workstream_for=None):
        """Persist normalized transitions and current measurements idempotently."""
        now = float(fleet.get("t") or self.clock())
        sessions = list(fleet.get("sessions") or []) + list(fleet.get("closed") or [])
        seen_ids = set()
        with self.lock, self._transaction(immediate=True) as db:
            for session in sessions:
                sid = self._text(session.get("session_id"), 320)
                if not sid:
                    continue
                seen_ids.add(sid)
                provider = self._text(session.get("provider") or "claude", 30)
                workstream_id = None
                if workstream_for and session.get("cwd"):
                    try:
                        workstream_id = workstream_for(session.get("cwd")).get("workstream_id")
                    except Exception:
                        workstream_id = None
                muted = bool(session.get("muted"))
                session_cost = session.get("cost")
                agent_cost = session.get("agent_cost")
                agent_known = (isinstance(agent_cost, (int, float)) or
                               int(session.get("agents_total") or 0) == 0)
                exact_capability = (session.get("capabilities") or {}).get("exact_cost")
                if exact_capability is None and provider == "claude" and session.get("closed_at"):
                    exact_capability = True
                cost_known = bool(exact_capability and
                                  isinstance(session_cost, (int, float)) and agent_known)
                measured_cost = (float(session_cost) + float(agent_cost or 0)
                                 if cost_known else None)
                total_tokens = session.get("total_tokens")
                revision = self._text(session.get("convo_v") or session.get("closed_at"), 320)
                files_n = int(session.get("files_n") or 0)
                repo_signature = self._repo_signature(session)
                state = self._text(session.get("normalized_state") or session.get("state"), 40)
                ui_group = self._text(session.get("ui_group"), 40)
                runtime = self._runtime(session, now)
                concurrency = ((1 if ui_group == "working" else 0) +
                               int(session.get("agents_running") or 0))
                measurement_signature = (
                    provider, workstream_id, muted, measured_cost, cost_known, total_tokens,
                    revision, files_n, repo_signature, state, ui_group,
                    round(runtime, 3) if isinstance(runtime, (int, float)) else None,
                    concurrency, self._text(session.get("project"), 320),
                    self._text(session.get("model"), 160),
                    self._text(session.get("title") or session.get("name"), 320),
                    self._text(session.get("reason_label"), 160))
                if self.measurement_signatures.get(sid) == measurement_signature:
                    continue
                previous = db.execute(
                    "SELECT * FROM session_measurements WHERE session_id=?", (sid,)).fetchone()
                if isinstance(total_tokens, bool) or not isinstance(total_tokens, (int, float)):
                    total_tokens = previous["tokens"] if previous else None

                if previous:
                    prior_state = previous["ui_group"]
                    if prior_state in ("working", "needs_you") and ui_group in ("available", "history"):
                        self._insert_event(
                            db, key=f"completed:{sid}:{revision or state}:{ui_group}",
                            category="completed", severity="success",
                            title="Work completed",
                            summary=(session.get("reason_label") or session.get("title") or
                                     session.get("name") or sid), provider=provider,
                            session_id=sid, workstream_id=workstream_id,
                            source_type="session", source_id=sid, link_kind="session",
                            link_id=sid, muted=muted,
                            payload={"state": state, "ui_group": ui_group})
                    if files_n > int(previous["files_n"] or 0):
                        self._insert_event(
                            db, key=f"artifact:{sid}:{revision}:{files_n}", category="outcome",
                            severity="success", title="Artifacts delivered",
                            summary=f"{files_n} file artifact{'s' if files_n != 1 else ''} available",
                            provider=provider, session_id=sid, workstream_id=workstream_id,
                            source_type="artifact", source_id=sid, link_kind="session",
                            link_id=sid, muted=muted, payload={"files_n": files_n})
                    if repo_signature and repo_signature != (previous["repo_signature"] or ""):
                        outcome = session.get("repo_outcome") or {}
                        self._insert_event(
                            db, key=f"repo:{sid}:{repo_signature}", category="outcome",
                            severity="success" if outcome.get("status") == "passed" else "info",
                            title="Repository outcome observed",
                            summary=(outcome.get("summary") or outcome.get("status") or
                                     "Repository evidence changed"), provider=provider,
                            session_id=sid, workstream_id=workstream_id,
                            source_type="repository", source_id=sid, link_kind="session",
                            link_id=sid, muted=muted, payload=outcome)

                db.execute("""INSERT INTO session_measurements(
                    session_id,provider,workstream_id,project,model,cost,cost_known,tokens,
                    runtime_seconds,concurrency,state,ui_group,revision,files_n,repo_signature,
                    muted,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                    provider=excluded.provider,workstream_id=excluded.workstream_id,
                    project=excluded.project,model=excluded.model,cost=excluded.cost,
                    cost_known=excluded.cost_known,tokens=excluded.tokens,
                    runtime_seconds=excluded.runtime_seconds,concurrency=excluded.concurrency,
                    state=excluded.state,ui_group=excluded.ui_group,revision=excluded.revision,
                    files_n=excluded.files_n,repo_signature=excluded.repo_signature,
                    muted=excluded.muted,updated_at=excluded.updated_at""", (
                    sid, provider, workstream_id, self._text(session.get("project"), 320),
                    self._text(session.get("model"), 160),
                    measured_cost,
                    1 if cost_known else 0, int(total_tokens) if total_tokens is not None else None,
                    runtime, concurrency, state, ui_group, revision, files_n, repo_signature,
                    1 if muted else 0, now))
                self.measurement_signatures[sid] = measurement_signature

            self.measurement_signatures = {
                sid: signature for sid, signature in self.measurement_signatures.items()
                if sid in seen_ids}

            self._observe_external_outcomes(db)
            evaluations = self._evaluate_budgets(db, fleet)
            previous_alerts = self._meta(db, "budget_alert_episodes", {}) or {}
            next_alerts = {}
            for item in evaluations:
                prior = previous_alerts.get(item["id"]) or {}
                status = item["status"]
                episode = int(prior.get("episode") or 0)
                if status != prior.get("status"):
                    episode += 1
                next_alerts[item["id"]] = {"status": status, "episode": episode}
                if status not in ("warning", "exceeded", "unavailable"):
                    continue
                event_key = f"budget:{item['id']}:{status}:{episode}"
                self._insert_event(
                    db, key=event_key,
                    category="budget" if status != "unavailable" else "measurement",
                    severity="high" if status == "exceeded" else "warning",
                    title=item["label"], summary=item["summary"], source_type="budget",
                    source_id=item["id"], link_kind="budget", link_id=item["id"],
                    payload=item)
                event = db.execute(
                    "SELECT created_at FROM briefing_events WHERE event_key=?", (event_key,)
                ).fetchone()
                item["alert_key"] = event_key
                item["alert_created_at"] = float(event[0]) if event else now
            self._set_meta(db, "budget_alert_episodes", next_alerts)
            self._sample_budget_values(db, evaluations, now)
            projection_started = time.perf_counter()
            projection_signature, provider_failure = self._notification_projection_input(fleet)
            if (provider_failure or
                    projection_signature != self.notification_projection_signature):
                self._reconcile_notification_events(db, fleet, now)
                self.notification_projection_signature = projection_signature
            self.notification_projection_ms.append(
                (time.perf_counter() - projection_started) * 1000)
            enqueue_started = time.perf_counter()
            self._schedule_notification_deliveries(db, now)
            self.notification_enqueue_ms.append(
                (time.perf_counter() - enqueue_started) * 1000)
        return evaluations

    def _observe_external_outcomes(self, db):
        last_repo = int(self._meta(db, "last_repo_action_id", 0) or 0)
        try:
            rows = db.execute("""SELECT id,action_id,kind,root,status,summary,error,finished_at
                FROM repo_actions WHERE id>? AND status IN ('succeeded','failed') ORDER BY id""",
                              (last_repo,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            status = row[4]
            self._insert_event(
                db, key=f"repo-action:{row[1]}:{status}",
                category="outcome" if status == "succeeded" else "attention",
                severity="success" if status == "succeeded" else "high",
                title=("Repository action completed" if status == "succeeded" else
                       "Repository action failed"),
                summary=row[5] or row[6] or row[2], source_type="repo_action",
                source_id=row[1], link_kind="repository", link_id=row[3],
                payload={"kind": row[2], "root": row[3], "status": status,
                         "finished_at": row[7]})
        if rows:
            self._set_meta(db, "last_repo_action_id", rows[-1][0])

        last_outbox = float(self._meta(db, "last_outbox_terminal_at", 0) or 0)
        try:
            rows = db.execute("""SELECT id,state,updated_at,target_provider,
                destination_session_id,error,blocked_reason FROM outbox_messages
                WHERE updated_at>? AND state IN ('sent','blocked','failed','confirmation_unknown')
                ORDER BY updated_at,id""", (last_outbox,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            state = row[1]
            ok = state == "sent"
            self._insert_event(
                db, key=f"outbox:{row[0]}:{state}",
                category="outcome" if ok else "attention",
                severity="success" if ok else "high",
                title="Scheduled message sent" if ok else "Outbox delivery needs review",
                summary=(row[5] or row[6] or state.replace("_", " ")),
                provider=row[3], session_id=row[4], source_type="outbox",
                source_id=row[0], link_kind="outbox", link_id=row[0],
                payload={"state": state})
        if rows:
            self._set_meta(db, "last_outbox_terminal_at", max(float(row[2]) for row in rows))

    def _sample_budget_values(self, db, evaluations, now):
        for item in evaluations:
            value = item.get("value")
            if not isinstance(value, (int, float)) or item.get("measurement_scope") == "unavailable":
                continue
            key = item["id"]
            previous = db.execute("""SELECT at,value FROM metric_samples
                WHERE sample_key=? AND metric=? ORDER BY id DESC LIMIT 1""",
                                  (key, item["metric"])).fetchone()
            if previous and now - float(previous[0]) < 300 and abs(float(previous[1])-value) < 1e-9:
                continue
            db.execute("""INSERT INTO metric_samples(sample_key,metric,at,value,measurement_scope)
                VALUES(?,?,?,?,?)""", (key, item["metric"], now, float(value),
                                        item["measurement_scope"]))
        cutoff = now - 90 * 86400
        db.execute("DELETE FROM metric_samples WHERE at<?", (cutoff,))

    def _budget_rows(self, db):
        return [dict(row) for row in db.execute(
            "SELECT * FROM budgets WHERE enabled=1 ORDER BY created_at,id").fetchall()]

    @staticmethod
    def _matches(budget, session):
        scope, target = budget["scope_type"], str(budget.get("scope_id") or "")
        if scope == "fleet":
            return True
        if scope == "provider":
            return session.get("provider") == target
        if scope == "workstream":
            return session.get("workstream_id") == target
        return session.get("session_id") == target

    def _evaluate_budgets(self, db, fleet):
        measurements = [dict(row) for row in db.execute(
            "SELECT * FROM session_measurements").fetchall()]
        current_ids = {str(item.get("session_id") or "") for item in
                       list(fleet.get("sessions") or []) + list(fleet.get("closed") or [])}
        measurements = [row for row in measurements if row["session_id"] in current_ids]
        out = []
        for budget in self._budget_rows(db):
            matched = [row for row in measurements if self._matches(budget, row)]
            metric = budget["metric"]
            known, unknown = [], 0
            for row in matched:
                if metric == "usd":
                    if row.get("cost_known") and row.get("cost") is not None:
                        known.append(float(row["cost"]))
                    else:
                        unknown += 1
                elif metric == "tokens":
                    if row.get("tokens") is not None:
                        known.append(float(row["tokens"]))
                    else:
                        unknown += 1
                elif metric == "runtime":
                    if row.get("runtime_seconds") is not None:
                        known.append(float(row["runtime_seconds"]))
                    else:
                        unknown += 1
                else:
                    known.append(float(row.get("concurrency") or 0))
            value = sum(known) if known else None
            measurement_scope = ("unavailable" if value is None else
                                 "partial" if unknown else
                                 "token_only" if metric == "tokens" and
                                 any(not row.get("cost_known") for row in matched) else "exact")
            limit_value = float(budget["limit_value"])
            ratio = value / limit_value if value is not None and limit_value else None
            status = ("unavailable" if value is None else "exceeded" if ratio >= 1 else
                      "warning" if ratio >= .8 else "ok")
            unit = {"usd": "$", "tokens": " tokens", "runtime": " seconds",
                    "concurrency": " concurrent"}[metric]
            if value is None:
                summary = f"{metric} measurement unavailable for this scope"
            else:
                rendered = f"${value:.2f}" if metric == "usd" else f"{value:,.0f}{unit}"
                cap = f"${limit_value:.2f}" if metric == "usd" else f"{limit_value:,.0f}{unit}"
                summary = f"{rendered} of {cap} · {measurement_scope.replace('_', ' ')}"
            out.append({
                **budget, "block_spawns": bool(budget.get("block_spawns")),
                "enabled": bool(budget.get("enabled")), "value": value,
                "headroom": max(0.0, limit_value-value) if value is not None else None,
                "ratio": ratio, "status": status,
                "measurement_scope": measurement_scope,
                "matched_sessions": len(matched), "unknown_sessions": unknown,
                "label": budget.get("label") or
                         f"{budget['scope_type'].title()} {metric} budget",
                "summary": summary,
            })
        return out

    def budgets_snapshot(self, fleet, spawn=None):
        with self.lock, self._connect() as db:
            evaluations = self._evaluate_budgets(db, fleet)
            forecasts = self._budget_forecasts(db, evaluations)
            spawn_forecast = self._spawn_forecast(db, spawn or {}) if spawn else None
            spawn_budgets = []
            if spawn:
                for item in evaluations:
                    applies = (item["scope_type"] == "fleet" or
                               item["scope_type"] == "provider" and
                               item.get("scope_id") == spawn.get("provider") or
                               item["scope_type"] == "workstream" and
                               item.get("scope_id") == spawn.get("workstream_id"))
                    if applies:
                        spawn_budgets.append(item)
        return {"ok": True, "budgets": evaluations, "forecasts": forecasts,
                "spawn_forecast": spawn_forecast, "spawn_budgets": spawn_budgets,
                "measurement_labels": {
                    "exact": "Exact provider measurement",
                    "partial": "Partial: some sessions do not expose this measurement",
                    "token_only": "Token-only: no currency conversion is claimed",
                    "unavailable": "Unavailable from current provider evidence",
                }}

    def _budget_forecasts(self, db, evaluations):
        now = self.clock()
        out = {}
        for item in evaluations:
            rows = db.execute("""SELECT at,value,measurement_scope FROM metric_samples
                WHERE sample_key=? AND metric=? AND at>=? ORDER BY at""",
                              (item["id"], item["metric"], now - 7*86400)).fetchall()
            positive = []
            for prior, current in zip(rows, rows[1:]):
                dt = float(current[0]) - float(prior[0])
                dv = float(current[1]) - float(prior[1])
                if dt > 0 and dv > 0:
                    positive.append(dv / dt)
            if len(positive) < 2 or item.get("headroom") is None:
                out[item["id"]] = {"status": "not_enough_history",
                                   "sample_size": len(positive), "confidence": "none"}
                continue
            rate = statistics.median(positive)
            eta = item["headroom"] / rate if rate > 0 else None
            confidence = "high" if len(positive) >= 10 else "medium" if len(positive) >= 4 else "low"
            out[item["id"]] = {"status": "forecast", "sample_size": len(positive),
                               "confidence": confidence, "burn_per_hour": rate*3600,
                               "seconds_to_limit": eta}
        return out

    def _spawn_forecast(self, db, spawn):
        provider = self._text(spawn.get("provider"), 30)
        model = self._text(spawn.get("model"), 160)
        project = self._text(spawn.get("project") or
                             os.path.basename(str(spawn.get("cwd") or "")), 320)
        if provider not in ("claude", "codex"):
            return {"status": "not_enough_history", "sample_size": 0,
                    "confidence": "none"}
        clauses, params = ["provider=?"], [provider]
        if model:
            clauses.append("model=?")
            params.append(model)
        if project:
            clauses.append("project=?")
            params.append(project)
        rows = [dict(row) for row in db.execute(
            "SELECT cost,cost_known,tokens,runtime_seconds FROM session_measurements WHERE " +
            " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT 50", params).fetchall()]
        samples = len(rows)
        if samples < 2:
            return {"status": "not_enough_history", "sample_size": samples,
                    "confidence": "none", "provider": provider, "model": model,
                    "project": project}
        confidence = "high" if samples >= 10 else "medium" if samples >= 4 else "low"
        def median(key, predicate=lambda row: True):
            values = [float(row[key]) for row in rows if predicate(row) and row.get(key) is not None]
            return statistics.median(values) if len(values) >= 2 else None
        return {"status": "forecast", "sample_size": samples, "confidence": confidence,
                "provider": provider, "model": model, "project": project,
                "median_usd": median("cost", lambda row: bool(row.get("cost_known"))),
                "median_tokens": median("tokens"),
                "median_runtime_seconds": median("runtime_seconds"),
                "currency_scope": "exact" if provider == "claude" else "unavailable"}

    def replace_budgets(self, payload):
        if not isinstance(payload, list) or len(payload) > 100:
            raise OperationsError("budgets must be a list of at most 100 items")
        now = self.clock()
        normalized = []
        seen = set()
        for raw in payload:
            if not isinstance(raw, dict):
                raise OperationsError("each budget must be an object")
            scope = str(raw.get("scope_type") or "")
            metric = str(raw.get("metric") or "")
            if scope not in BUDGET_SCOPES or metric not in BUDGET_METRICS:
                raise OperationsError("unsupported budget scope or metric")
            raw_scope_id = str(raw.get("scope_id") or "")
            if len(raw_scope_id) > 320 or any(ord(char) < 32 for char in raw_scope_id):
                raise OperationsError("budget target is too long or invalid")
            scope_id = self._text(raw_scope_id, 320) or None
            if scope != "fleet" and not scope_id:
                raise OperationsError(f"{scope} budget needs a target")
            raw_limit = raw.get("limit_value")
            if isinstance(raw_limit, bool):
                raise OperationsError("budget limit must be a number")
            try:
                limit_value = float(raw_limit)
            except (TypeError, ValueError):
                raise OperationsError("budget limit must be a number")
            if not math.isfinite(limit_value) or limit_value <= 0 or limit_value > 1e15:
                raise OperationsError("budget limit must be greater than zero")
            raw_id = str(raw.get("id") or "")
            if len(raw_id) > 80 or any(ord(char) < 32 for char in raw_id):
                raise OperationsError("budget ID is too long or invalid")
            budget_id = self._text(raw_id, 80) or self.id_factory()
            if budget_id in seen:
                raise OperationsError("duplicate budget ID")
            seen.add(budget_id)
            if "block_spawns" in raw and not isinstance(raw["block_spawns"], bool):
                raise OperationsError("block_spawns must be boolean")
            if "enabled" in raw and not isinstance(raw["enabled"], bool):
                raise OperationsError("enabled must be boolean")
            raw_label = str(raw.get("label") or "")
            if len(raw_label) > 160 or any(ord(char) < 32 for char in raw_label):
                raise OperationsError("budget label is too long or invalid")
            normalized.append({
                "id": budget_id, "scope_type": scope, "scope_id": scope_id,
                "metric": metric, "limit_value": limit_value,
                "block_spawns": raw.get("block_spawns") is True,
                "enabled": raw.get("enabled") is not False,
                "label": self._text(raw_label, 160) or None,
            })
        with self.lock, self._transaction(immediate=True) as db:
            prior_created = {row[0]: row[1] for row in db.execute(
                "SELECT id,created_at FROM budgets").fetchall()}
            db.execute("DELETE FROM budgets")
            db.executemany("""INSERT INTO budgets(id,scope_type,scope_id,metric,limit_value,
                block_spawns,enabled,label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           [(item["id"], item["scope_type"], item["scope_id"], item["metric"],
                             item["limit_value"], 1 if item["block_spawns"] else 0,
                             1 if item["enabled"] else 0, item["label"],
                             prior_created.get(item["id"], now), now)
                            for item in normalized])
        return normalized

    def spawn_blockers(self, fleet, provider, cwd, workstream_id=None):
        with self.lock, self._connect() as db:
            evaluations = self._evaluate_budgets(db, fleet)
            if cwd and not workstream_id:
                row = db.execute("""SELECT workstream_id FROM session_measurements
                    WHERE project=? AND workstream_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1""",
                                 (os.path.basename(str(cwd).rstrip(os.sep)),)).fetchone()
                workstream_id = row[0] if row else None
        blockers = []
        for item in evaluations:
            if not item.get("block_spawns") or item.get("status") != "exceeded":
                continue
            if item["scope_type"] == "fleet" or (
                    item["scope_type"] == "provider" and item.get("scope_id") == provider) or (
                    item["scope_type"] == "workstream" and item.get("scope_id") == workstream_id):
                blockers.append(item)
        return blockers

    def has_spawn_limits(self):
        with self.lock, self._connect() as db:
            row = db.execute("""SELECT 1 FROM budgets
                WHERE enabled=1 AND block_spawns=1 LIMIT 1""").fetchone()
        return bool(row)

    def review(self, device_id, cursor):
        device_id = str(device_id or "")
        if not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid briefing device ID")
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            raise OperationsError("invalid briefing cursor")
        if cursor < 0:
            raise OperationsError("invalid briefing cursor")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            maximum = int(db.execute("SELECT COALESCE(MAX(id),0) FROM briefing_events").fetchone()[0])
            cursor = min(cursor, maximum)
            db.execute("""INSERT INTO briefing_reviews(device_id,cursor,updated_at)
                VALUES(?,?,?) ON CONFLICT(device_id) DO UPDATE SET
                cursor=MAX(briefing_reviews.cursor,excluded.cursor),updated_at=excluded.updated_at""",
                       (device_id, cursor, now))
            saved = db.execute("SELECT cursor FROM briefing_reviews WHERE device_id=?",
                               (device_id,)).fetchone()[0]
        return int(saved)

    def briefing_snapshot(self, fleet, device_id="default", cursor=None, limit=100):
        if not DEVICE_RE.fullmatch(str(device_id or "")):
            raise OperationsError("invalid briefing device ID")
        try:
            limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            raise OperationsError("invalid briefing limit")
        with self.lock, self._connect() as db:
            saved_row = db.execute("SELECT cursor FROM briefing_reviews WHERE device_id=?",
                                   (device_id,)).fetchone()
            saved = int(saved_row[0]) if saved_row else 0
            if cursor not in (None, ""):
                try:
                    saved = max(saved, int(cursor))
                except (TypeError, ValueError):
                    raise OperationsError("invalid briefing cursor")
            max_id = int(db.execute("SELECT COALESCE(MAX(id),0) FROM briefing_events").fetchone()[0])
            rows = db.execute("""SELECT * FROM briefing_events WHERE id>? ORDER BY id DESC
                LIMIT ?""", (saved, limit)).fetchall()
            unread = [self._event(row) for row in rows]
            reviewed_rows = db.execute("""SELECT * FROM briefing_events WHERE id<=?
                ORDER BY id DESC LIMIT 20""", (saved,)).fetchall()
            reviewed = [self._event(row) for row in reviewed_rows]
            evaluations = self._evaluate_budgets(db, fleet)

        attention = []
        for action in fleet.get("actions") or []:
            attention.append({
                "id": "current:" + str(action.get("action_id") or action.get("session_id")),
                "category": "attention", "severity": action.get("urgency") or "warning",
                "title": action.get("request_label") or "Needs attention",
                "summary": action.get("delivery_label") or action.get("reason_label") or "Response needed",
                "provider": action.get("provider"), "session_id": action.get("session_id"),
                "link_kind": "session", "link_id": action.get("session_id"),
                "muted": bool(action.get("muted")), "current": True,
            })
        for provider, state in (fleet.get("providers") or {}).items():
            if state and state.get("ok") is False:
                attention.append({"id": "provider:"+provider, "category": "measurement",
                                  "severity": "warning", "title": f"{provider.title()} unavailable",
                                  "summary": state.get("error") or "Provider evidence is stale",
                                  "provider": provider, "current": True})
        slow = []
        threshold = max(30, int(float(fleet.get("settings", {}).get("stall_seconds") or 240)))
        for session in fleet.get("sessions") or []:
            quiet = float(session.get("quiet_s") or 0)
            if session.get("ui_group") != "working" or quiet < threshold:
                continue
            slow.append({"id": "slow:"+str(session.get("session_id")),
                         "category": "slow", "severity": "warning",
                         "title": session.get("title") or session.get("name") or "Still working",
                         "summary": f"No observed progress for {int(quiet // 60)} minutes",
                         "provider": session.get("provider"),
                         "session_id": session.get("session_id"), "link_kind": "session",
                         "link_id": session.get("session_id"),
                         "muted": bool(session.get("muted")), "current": True})
        sections = {
            "attention": attention + [item for item in unread if item["category"] in
                                      ("attention", "notification")],
            "completed": [item for item in unread if item["category"] == "completed"],
            "slow": slow,
            "outcomes": [item for item in unread if item["category"] == "outcome"],
            "budgets": evaluations,
            "measurements": [item for item in unread if item["category"] == "measurement"],
            "reviewed": reviewed,
        }
        muted_omitted = sum(1 for name in ("attention", "completed", "slow", "outcomes")
                            for item in sections[name] if item.get("muted"))
        return {"ok": True, "device_id": device_id, "review_cursor": saved,
                "next_cursor": max_id, "unread": max(0, max_id-saved),
                "muted_omitted": muted_omitted, "sections": sections}

    def notification_snapshot(self, device_id="default", cursor=None, limit=100,
                              states=None, kinds=None, event_id=None):
        device_id = str(device_id or "default")
        if not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid notification device ID")
        try:
            limit = max(1, min(200, int(limit)))
            cursor = None if cursor in (None, "") else int(cursor)
        except (TypeError, ValueError):
            raise OperationsError("invalid notification pagination")
        if cursor is not None and cursor < 0:
            raise OperationsError("invalid notification pagination")
        states = {str(item) for item in (states or []) if str(item)}
        kinds = {str(item) for item in (kinds or []) if str(item)}
        if not states.issubset(NOTIFICATION_STATES) or not kinds.issubset(NOTIFICATION_KINDS):
            raise OperationsError("invalid notification filter")
        event_id = self._text(event_id, 100) or None
        with self.lock, self._connect() as db:
            device = db.execute("""SELECT read_cursor FROM notification_devices
                WHERE id=?""", (device_id,)).fetchone()
            browser = db.execute("""SELECT read_cursor FROM notification_read_cursors
                WHERE device_id=?""", (device_id,)).fetchone()
            maximum = int(db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM notification_events").fetchone()[0])
            saved_cursors = [int(row[0]) for row in (device, browser) if row]
            # A browser identity begins at the current edge, like a newly registered
            # push device. The successful page load immediately persists that edge;
            # old history remains queryable without appearing as a notification blast.
            read_cursor = max(saved_cursors) if saved_cursors else maximum
            if not saved_cursors:
                db.execute("""INSERT OR IGNORE INTO notification_read_cursors(
                    device_id,read_cursor,updated_at) VALUES(?,?,?)""",
                           (device_id, read_cursor, self.clock()))
            if event_id:
                rows = db.execute("SELECT * FROM notification_events WHERE id=?",
                                  (event_id,)).fetchall()
                has_more = False
            else:
                before = maximum + 1 if cursor is None else cursor
                query = """SELECT e.* FROM notification_events e
                    JOIN notification_kind_policy k ON k.kind=e.kind
                    WHERE e.sequence<? AND k.in_app_enabled=1
                    AND MAX(e.opened_at,e.changed_at)>k.in_app_effective_after"""
                params = [before]
                if states:
                    query += " AND e.state IN (" + ",".join("?" for _ in states) + ")"
                    params.extend(sorted(states))
                if kinds:
                    query += " AND e.kind IN (" + ",".join("?" for _ in kinds) + ")"
                    params.extend(sorted(kinds))
                query += " ORDER BY e.sequence DESC LIMIT ?"
                params.append(limit + 1)
                rows = db.execute(query, params).fetchall()
                has_more = len(rows) > limit
                rows = rows[:limit]
            events = [self._notification_event(row, read_cursor) for row in rows]
            muted_sessions = {row[0] for row in db.execute(
                "SELECT session_id FROM notification_session_mutes").fetchall()}
            for item in events:
                item["muted"] = bool(item.get("session_id") in muted_sessions)
            unread = int(db.execute("""SELECT COUNT(*) FROM notification_events e
                JOIN notification_kind_policy k ON k.kind=e.kind
                WHERE e.sequence>? AND k.in_app_enabled=1
                AND MAX(e.opened_at,e.changed_at)>k.in_app_effective_after""",
                (read_cursor,)).fetchone()[0])
            active = int(db.execute("""SELECT COUNT(*) FROM notification_events e
                JOIN notification_kind_policy k ON k.kind=e.kind
                WHERE e.state IN ('active','snoozed') AND k.in_app_enabled=1
                AND MAX(e.opened_at,e.changed_at)>k.in_app_effective_after""").fetchone()[0])
            delivery_rows = db.execute("""SELECT d.id,d.event_id,d.device_id,d.generation,
                    d.status,d.attempt,d.remote_status,d.created_at,d.updated_at,
                    e.payload_json,
                    v.display_name,v.platform,v.enabled,v.permission_state,
                    v.last_success_at,v.test_success_at,v.last_failure_at
                FROM notification_deliveries d
                JOIN notification_events e ON e.id=d.event_id
                JOIN notification_devices v ON v.id=d.device_id
                JOIN notification_kind_policy k ON k.kind='failure'
                WHERE v.last_failure_at IS NOT NULL
                AND v.last_failure_at>COALESCE(v.last_success_at,0)
                AND k.in_app_enabled=1 AND d.updated_at>k.in_app_effective_after
                AND d.status IN ('failed','subscription_expired','queued','sending','retrying')
                AND NOT EXISTS(SELECT 1 FROM notification_deliveries newer
                    WHERE newer.event_id=d.event_id AND newer.device_id=d.device_id
                    AND newer.generation>d.generation)
                ORDER BY d.updated_at DESC LIMIT 50""").fetchall()
            delivery_problems = []
            for row in delivery_rows:
                problem = {key: row[key] for key in (
                    "id", "event_id", "device_id", "generation", "status", "attempt",
                    "remote_status", "created_at", "updated_at", "display_name", "platform",
                    "permission_state", "last_success_at", "last_failure_at")}
                problem["enabled"] = bool(row["enabled"])
                try:
                    push_test = bool(json.loads(row["payload_json"] or "{}").get("push_test"))
                except (TypeError, ValueError):
                    push_test = False
                problem["can_retry"] = (row["status"] == "failed" and bool(row["enabled"])
                                        and row["permission_state"] == "granted"
                                        and (push_test or bool(row["test_success_at"])))
                delivery_problems.append(problem)
        return {"ok": True, "device_id": device_id, "read_cursor": read_cursor,
                "event_cursor": maximum, "unread": unread, "active": active,
                "events": events, "delivery_problems": delivery_problems,
                "next_cursor": (events[-1]["sequence"] if has_more and events else None)}

    def notification_counts(self, device_id="default"):
        snapshot = self.notification_snapshot(device_id, limit=1)
        return {key: snapshot[key] for key in ("unread", "active", "event_cursor")}

    def notification_register_device(self, device_id, display_name, platform,
                                     subscription, endpoint_origin=None,
                                     permission_state="granted", preferences=None,
                                     allowed_origins=None):
        device_id = str(device_id or "")
        if not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid notification device ID")
        display_name = self._text(display_name, 80)
        platform = self._text(platform, 80)
        permission_state = self._text(permission_state, 20)
        if not display_name or not platform or permission_state != "granted":
            raise OperationsError("invalid notification device")
        subscription, derived_origin = self._push_subscription(
            subscription, extra_origins=allowed_origins)
        if endpoint_origin and self._text(endpoint_origin, 320) != derived_origin:
            raise OperationsError("notification endpoint origin mismatch")
        endpoint_origin = derived_origin
        validated_preferences = self._device_preferences(preferences)
        subscription_json = self._json(subscription)
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT preferences_json,subscription_json,test_success_at FROM notification_devices WHERE id=?",
                (device_id,)).fetchone()
            browser = db.execute("""SELECT read_cursor FROM notification_read_cursors
                WHERE device_id=?""", (device_id,)).fetchone()
            preferences_json = self._json(validated_preferences) if preferences is not None else \
                (existing["preferences_json"] if existing else "{}")
            test_success_at = (existing["test_success_at"] if existing and
                               existing["subscription_json"] == subscription_json else None)
            maximum = int(db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM notification_events").fetchone()[0])
            initial_cursor = int(browser[0]) if browser else maximum
            db.execute("""INSERT INTO notification_devices(
                id,display_name,platform,subscription_json,endpoint_origin,enabled,
                permission_state,created_at,last_registered_at,test_success_at,read_cursor,preferences_json)
                VALUES(?,?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,platform=excluded.platform,
                subscription_json=excluded.subscription_json,
                endpoint_origin=excluded.endpoint_origin,enabled=1,
                permission_state=excluded.permission_state,
                last_registered_at=excluded.last_registered_at,
                test_success_at=excluded.test_success_at,
                read_cursor=MAX(notification_devices.read_cursor,excluded.read_cursor),
                preferences_json=excluded.preferences_json""", (
                device_id, display_name, platform, subscription_json, endpoint_origin,
                permission_state, now, now, test_success_at, initial_cursor, preferences_json))
            db.execute("""INSERT INTO notification_read_cursors(
                device_id,read_cursor,updated_at) VALUES(?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET
                read_cursor=MAX(read_cursor,excluded.read_cursor),updated_at=excluded.updated_at""",
                       (device_id, initial_cursor, now))
            row = db.execute("""SELECT id,display_name,platform,enabled,permission_state,
                created_at,last_registered_at,last_success_at,test_success_at,last_failure_at,last_failure,
                read_cursor,preferences_json FROM notification_devices WHERE id=?""",
                             (device_id,)).fetchone()
        return self._notification_device(row)

    def notification_devices_snapshot(self, current_device_id=None):
        current_device_id = str(current_device_id or "")
        if current_device_id and not DEVICE_RE.fullmatch(current_device_id):
            raise OperationsError("invalid notification device ID")
        with self.lock, self._connect() as db:
            rows = db.execute("""SELECT id,display_name,platform,enabled,permission_state,
                created_at,last_registered_at,last_success_at,test_success_at,last_failure_at,read_cursor,
                preferences_json FROM notification_devices
                ORDER BY enabled DESC,last_registered_at DESC,id""").fetchall()
        devices = [self._notification_device(row) for row in rows]
        current = next((item for item in devices if item["id"] == current_device_id), None)
        return {"ok": True, "devices": devices, "current_device": current,
                "registered": len(devices),
                "enabled": sum(item["enabled"] for item in devices)}

    def notification_update_device(self, device_id, *, display_name=None,
                                   enabled=None, preferences=None):
        device_id = str(device_id or "")
        if not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid notification device ID")
        if display_name is not None:
            display_name = self._text(display_name, 80)
            if not display_name:
                raise OperationsError("invalid notification device name")
        if enabled is not None and not isinstance(enabled, bool):
            raise OperationsError("invalid notification device state")
        validated_preferences = self._device_preferences(preferences)
        if display_name is None and enabled is None and preferences is None:
            raise OperationsError("no notification device setting supplied")
        fields, values = [], []
        if display_name is not None:
            fields.append("display_name=?")
            values.append(display_name)
        if enabled is not None:
            fields.append("enabled=?")
            values.append(1 if enabled else 0)
        if preferences is not None:
            fields.append("preferences_json=?")
            values.append(self._json(validated_preferences))
        values.append(device_id)
        with self.lock, self._transaction(immediate=True) as db:
            current = db.execute("""SELECT subscription_json,permission_state
                FROM notification_devices WHERE id=?""", (device_id,)).fetchone()
            if not current:
                raise OperationsError("notification device is not registered")
            if enabled is True and (current["subscription_json"] == "{}" or
                                    current["permission_state"] != "granted"):
                raise OperationsError("notification device must reconnect before enabling")
            changed = db.execute(
                "UPDATE notification_devices SET " + ",".join(fields) + " WHERE id=?",
                values).rowcount
            if not changed:
                raise OperationsError("notification device settings were not changed")
            row = db.execute("""SELECT id,display_name,platform,enabled,permission_state,
                created_at,last_registered_at,last_success_at,test_success_at,last_failure_at,read_cursor,
                preferences_json FROM notification_devices WHERE id=?""",
                             (device_id,)).fetchone()
        return self._notification_device(row)

    def notification_remove_device(self, device_id, permission_state="expired"):
        device_id = str(device_id or "")
        permission_state = self._text(permission_state, 20)
        if not DEVICE_RE.fullmatch(device_id) or permission_state not in PUSH_PERMISSION_STATES:
            raise OperationsError("invalid notification device")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            changed = db.execute("""UPDATE notification_devices SET subscription_json='{}',
                endpoint_origin='',enabled=0,permission_state=?,last_registered_at=?
                WHERE id=?""", (permission_state, now, device_id)).rowcount
            if not changed:
                raise OperationsError("notification device is not registered")
            row = db.execute("""SELECT id,display_name,platform,enabled,permission_state,
                created_at,last_registered_at,last_success_at,test_success_at,last_failure_at,read_cursor,
                preferences_json FROM notification_devices WHERE id=?""",
                             (device_id,)).fetchone()
        return self._notification_device(row)

    def notification_forget_device(self, device_id):
        """Forget a disconnected device without erasing delivery history."""
        device_id = str(device_id or "")
        if not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid notification device")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            current = db.execute(
                "SELECT id FROM notification_devices WHERE id=?", (device_id,)).fetchone()
            if not current:
                raise OperationsError("notification device is not registered")
            # A claimed worker revalidates the device before delivery. Everything
            # still queued can be made terminal immediately and remains useful as
            # bounded delivery-history evidence.
            db.execute("""UPDATE notification_deliveries
                SET status='suppressed',error='device removed',lease_until=NULL,updated_at=?
                WHERE device_id=? AND status IN ('queued','retrying')""", (now, device_id))
            db.execute("DELETE FROM notification_read_cursors WHERE device_id=?", (device_id,))
            db.execute("DELETE FROM notification_devices WHERE id=?", (device_id,))
        return {"id": device_id, "removed": True}

    def notification_mark_read(self, device_id, cursor):
        device_id = str(device_id or "")
        if not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid notification device ID")
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            raise OperationsError("invalid notification cursor")
        if cursor < 0:
            raise OperationsError("invalid notification cursor")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            maximum = int(db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM notification_events").fetchone()[0])
            cursor = min(cursor, maximum)
            db.execute("""INSERT INTO notification_read_cursors(
                device_id,read_cursor,updated_at) VALUES(?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET
                read_cursor=MAX(read_cursor,excluded.read_cursor),updated_at=excluded.updated_at""",
                       (device_id, cursor, now))
            db.execute("""UPDATE notification_devices SET
                read_cursor=MAX(read_cursor,?) WHERE id=?""", (cursor, device_id))
            saved = int(db.execute("""SELECT read_cursor FROM notification_read_cursors
                WHERE device_id=?""", (device_id,)).fetchone()[0])
        return saved

    def notification_snooze(self, event_id, source_revision, until):
        event_id = self._text(event_id, 100)
        source_revision = self._text(source_revision, 320)
        try:
            until = float(until)
        except (TypeError, ValueError):
            raise OperationsError("invalid snooze time")
        now = self.clock()
        if not event_id or not source_revision or not now < until <= now + 30 * 86400:
            raise OperationsError("invalid snooze request")
        with self.lock, self._transaction(immediate=True) as db:
            row = db.execute("""SELECT source_revision,state FROM notification_events
                WHERE id=?""", (event_id,)).fetchone()
            if (not row or row["source_revision"] != source_revision or
                    row["state"] not in ("active", "snoozed")):
                raise OperationsError("notification event is stale")
            db.execute("""UPDATE notification_events SET state='snoozed',
                snoozed_until=?,reminder_budget=0,changed_at=? WHERE id=?""",
                       (until, now, event_id))
            db.execute("""UPDATE notification_deliveries SET status='suppressed',
                error='event snoozed',updated_at=? WHERE event_id=?
                AND purpose IN ('initial','reminder')
                AND status IN ('queued','retrying')""", (now, event_id))
        return until

    def notification_wake(self, event_id, source_revision):
        event_id = self._text(event_id, 100)
        source_revision = self._text(source_revision, 320)
        if not event_id or not source_revision:
            raise OperationsError("invalid wake request")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            row = db.execute("""SELECT source_revision,state FROM notification_events
                WHERE id=?""", (event_id,)).fetchone()
            if (not row or row["source_revision"] != source_revision or
                    row["state"] != "snoozed"):
                raise OperationsError("notification event is stale")
            db.execute("""UPDATE notification_events SET state='active',
                snoozed_until=NULL,changed_at=? WHERE id=?""", (now, event_id))
        return True

    def notification_set_session_mute(self, session_id, provider=None, muted=True):
        session_id = self._text(session_id, 320)
        provider = self._text(provider, 30) or None
        if not session_id:
            raise OperationsError("invalid notification session ID")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            if muted:
                db.execute("""INSERT INTO notification_session_mutes(
                    session_id,provider,muted_at) VALUES(?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET provider=excluded.provider""",
                           (session_id, provider, now))
                db.execute("""UPDATE notification_deliveries SET status='suppressed',
                    error='session muted',updated_at=? WHERE status IN ('queued','retrying')
                    AND event_id IN (SELECT id FROM notification_events WHERE session_id=?)""",
                           (now, session_id))
            else:
                db.execute("DELETE FROM notification_session_mutes WHERE session_id=?",
                           (session_id,))
        return bool(muted)

    def notification_session_muted(self, session_id):
        with self.lock, self._connect() as db:
            row = db.execute("SELECT 1 FROM notification_session_mutes WHERE session_id=?",
                             (self._text(session_id, 320),)).fetchone()
        return bool(row)

    def notification_consume_capability(self, claims, *, mute_callback=None):
        """Consume one verified action capability and apply only its fixed safe action."""
        if not isinstance(claims, dict):
            raise OperationsError("notification capability is unavailable")
        event_id = self._text(claims.get("event_id"), 100)
        device_id = self._text(claims.get("device_id"), 80)
        action = claims.get("action")
        jti_hash = self._text(claims.get("jti_hash"), 64)
        try:
            expires_at = float(claims.get("expires_at"))
        except (TypeError, ValueError):
            raise OperationsError("notification capability is unavailable")
        now = self.clock()
        if (not event_id or not DEVICE_RE.fullmatch(device_id) or
                action not in ("snooze", "mute") or
                not re.fullmatch(r"[0-9a-f]{64}", jti_hash or "") or expires_at < now):
            raise OperationsError("notification capability is unavailable")
        with self.lock, self._transaction(immediate=True) as db:
            event = db.execute("""SELECT id,state,kind,provider,session_id
                FROM notification_events WHERE id=?""", (event_id,)).fetchone()
            device = db.execute("""SELECT enabled,permission_state,subscription_json,
                test_success_at FROM notification_devices WHERE id=?""",
                (device_id,)).fetchone()
            used = db.execute("SELECT 1 FROM notification_capability_uses WHERE jti_hash=?",
                              (jti_hash,)).fetchone()
            if (used or not event or event["state"] != "active" or
                    event["kind"] not in PUSHABLE_KINDS or not device or
                    not device["enabled"] or device["permission_state"] != "granted" or
                    device["subscription_json"] == "{}" or not device["test_success_at"] or
                    (action == "mute" and not event["session_id"])):
                raise OperationsError("notification capability is unavailable")
            db.execute("""DELETE FROM notification_capability_uses
                WHERE expires_at<?""", (now - 30 * 86400,))
            db.execute("""INSERT INTO notification_capability_uses(
                jti_hash,event_id,device_id,action,expires_at,consumed_at)
                VALUES(?,?,?,?,?,?)""",
                       (jti_hash, event_id, device_id, action, expires_at, now))
            if action == "snooze":
                until = now + 900
                db.execute("""UPDATE notification_events SET state='snoozed',
                    snoozed_until=?,reminder_budget=0,changed_at=? WHERE id=?""",
                           (until, now, event_id))
                db.execute("""UPDATE notification_deliveries SET status='suppressed',
                    error='event snoozed',updated_at=? WHERE event_id=?
                    AND purpose IN ('initial','reminder')
                    AND status IN ('queued','retrying')""", (now, event_id))
                return {"action": action, "event_id": event_id, "until": until}
            session_id = event["session_id"]
            if mute_callback:
                mute_callback(session_id)
            db.execute("""INSERT INTO notification_session_mutes(
                session_id,provider,muted_at) VALUES(?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET provider=excluded.provider""",
                       (session_id, event["provider"], now))
            db.execute("""UPDATE notification_deliveries SET status='suppressed',
                error='session muted',updated_at=? WHERE status IN ('queued','retrying')
                AND event_id IN (SELECT id FROM notification_events WHERE session_id=?)""",
                       (now, session_id))
            return {"action": action, "event_id": event_id, "session_id": session_id}

    @staticmethod
    def _notification_delivery(row):
        """Return only operator-safe delivery state; subscriptions never cross this boundary."""
        if not row:
            return None
        item = {key: row[key] for key in (
            "id", "event_id", "device_id", "generation", "purpose", "status", "attempt",
            "next_attempt_at", "remote_status", "created_at", "updated_at")
                if key in row.keys()}
        item["generation"] = int(item.get("generation") or 0)
        item["attempt"] = int(item.get("attempt") or 0)
        return item

    def notification_create_test_delivery(self, device_id):
        """Persist one explicit minimal test push; the caller never waits for delivery."""
        device_id = str(device_id or "")
        if not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid notification device ID")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            delivery_identity = self._text(self.delivery_id_factory(), 100)
            if not delivery_identity:
                raise OperationsError("notification delivery ID is required")
            event_id = self._upsert_notification_event(db, {
                "event_key": self._canonical_notification_key(
                    "push-test", device_id, delivery_identity),
                "kind": "notification", "state": "resolved", "severity": "info",
                "title": "Fleet notification test",
                "summary": "Web Push delivery is working.",
                "source_type": "push_test", "source_id": device_id,
                "source_revision": delivery_identity, "opened_at": now,
                "reminder_budget": 0, "payload": {"push_test": True},
            }, now)
            row = self._enqueue_notification_delivery(
                db, event_id, device_id, now, delivery_id=delivery_identity, purpose="test")
        return self._notification_delivery(row)

    def _enqueue_notification_delivery(self, db, event_id, device_id, now, delivery_id=None,
                                       purpose="initial"):
        if purpose not in PUSH_DELIVERY_PURPOSES:
            raise OperationsError("invalid notification delivery purpose")
        existing = db.execute("""SELECT * FROM notification_deliveries
            WHERE event_id=? AND device_id=? AND purpose=?
            ORDER BY generation DESC LIMIT 1""",
            (event_id, device_id, purpose)).fetchone()
        if existing:
            return existing
        event = db.execute("""SELECT state,payload_json,source_revision
            FROM notification_events WHERE id=?""",
                           (event_id,)).fetchone()
        device = db.execute("""SELECT enabled,permission_state,subscription_json
            FROM notification_devices WHERE id=?""", (device_id,)).fetchone()
        if not event:
            raise OperationsError("notification event is unavailable")
        if not device:
            raise OperationsError("notification device is not registered")
        try:
            event_payload = json.loads(event["payload_json"] or "{}")
        except (TypeError, ValueError):
            event_payload = {}
        if event["state"] != "active" and not event_payload.get("push_test"):
            raise OperationsError("notification event is stale")
        if (not device["enabled"] or device["permission_state"] != "granted" or
                device["subscription_json"] == "{}"):
            raise OperationsError("notification device must reconnect before testing")
        queued = int(db.execute("""SELECT COUNT(*) FROM notification_deliveries
            WHERE status IN ('queued','sending','retrying')""").fetchone()[0])
        if queued >= self.delivery_queue_limit:
            raise OperationsError("notification delivery queue is full")
        delivery_id = self._text(delivery_id or self.delivery_id_factory(), 100)
        if not delivery_id:
            raise OperationsError("notification delivery ID is required")
        generation = int(db.execute("""SELECT COALESCE(MAX(generation),0)+1
            FROM notification_deliveries WHERE event_id=? AND device_id=?""",
            (event_id, device_id)).fetchone()[0])
        db.execute("""INSERT INTO notification_deliveries(
            id,event_id,device_id,generation,purpose,source_revision,status,attempt,claimed_at,lease_until,
            next_attempt_at,remote_status,remote_id,error,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'queued',0,NULL,NULL,?,NULL,NULL,NULL,?,?)""",
                   (delivery_id, event_id, device_id, generation, purpose,
                    event["source_revision"], now, now, now))
        return db.execute("SELECT * FROM notification_deliveries WHERE id=?",
                          (delivery_id,)).fetchone()

    def notification_enqueue_delivery(self, event_id, device_id):
        """Coalesce repeated policy scans to one durable job per event and device."""
        event_id = self._text(event_id, 100)
        device_id = str(device_id or "")
        if not event_id or not DEVICE_RE.fullmatch(device_id):
            raise OperationsError("invalid notification delivery target")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            event = db.execute("SELECT source_type FROM notification_events WHERE id=?",
                               (event_id,)).fetchone()
            purpose = "test" if event and event["source_type"] == "push_test" else "initial"
            row = self._enqueue_notification_delivery(
                db, event_id, device_id, now, purpose=purpose)
        return self._notification_delivery(row)

    def notification_claim_delivery(self, allowed_origins=None):
        """Claim one due job with a durable lease and return its internal send material."""
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            db.execute("""UPDATE notification_deliveries SET status='retrying',
                next_attempt_at=?,lease_until=NULL,error='worker lease expired',updated_at=?
                WHERE status='sending' AND lease_until IS NOT NULL AND lease_until<=?""",
                       (now, now, now))
            rows = db.execute("""SELECT d.*,e.kind,e.state AS event_state,e.title,e.summary,
                    e.session_id,e.source_type,e.source_id,e.source_revision AS event_revision,
                    e.severity,e.payload_json,e.sequence,
                    v.enabled,v.permission_state,v.subscription_json,v.endpoint_origin,
                    v.preferences_json,v.read_cursor,v.test_success_at
                FROM notification_deliveries d
                LEFT JOIN notification_events e ON e.id=d.event_id
                LEFT JOIN notification_devices v ON v.id=d.device_id
                WHERE d.status IN ('queued','retrying') AND d.next_attempt_at<=?
                ORDER BY d.next_attempt_at,d.created_at LIMIT 20""", (now,)).fetchall()
            for row in rows:
                try:
                    event_payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError):
                    event_payload = {}
                terminal = None
                hold_until = None
                if row["event_state"] is None or row["enabled"] is None:
                    terminal = "failed"
                elif row["purpose"] not in PUSH_DELIVERY_PURPOSES:
                    terminal = "failed"
                elif row["source_revision"] != row["event_revision"]:
                    terminal = "suppressed"
                elif not row["enabled"]:
                    terminal = "suppressed"
                elif row["permission_state"] != "granted" or row["subscription_json"] == "{}":
                    terminal = "subscription_expired"
                elif (row["event_state"] != "active" and
                      not (row["event_state"] == "resolved" and
                           row["kind"] in INFORMATIONAL_NOTIFICATION_KINDS) and
                      not event_payload.get("push_test")):
                    terminal = "suppressed"
                elif (not event_payload.get("push_test") and
                      (row["kind"] not in PUSHABLE_KINDS or not row["test_success_at"] or
                       int(row["sequence"] or 0) <= int(row["read_cursor"] or 0))):
                    terminal = "suppressed"
                elif (row["source_type"] == "delivery" and
                      row["source_id"] == row["device_id"]):
                    terminal = "suppressed"
                elif row["session_id"] and db.execute(
                        "SELECT 1 FROM notification_session_mutes WHERE session_id=?",
                        (row["session_id"],)).fetchone():
                    terminal = "suppressed"
                elif (not event_payload.get("push_test") and
                      row["global_policy_revision"] is not None):
                    global_row = db.execute("""SELECT * FROM notification_global_policy
                        WHERE singleton_id=1""").fetchone()
                    rule_row = db.execute("""SELECT * FROM notification_kind_policy
                        WHERE kind=?""", (row["kind"],)).fetchone()
                    if (not global_row or not rule_row or not global_row["enabled"] or
                            rule_row["mode"] == "off" or
                            int(row["global_policy_revision"]) != int(global_row["revision"]) or
                            int(row["kind_policy_revision"] or -1) != int(rule_row["push_revision"]) or
                            PUSH_SEVERITY_RANK.get(row["severity"], -1) <
                            PUSH_SEVERITY_RANK.get(rule_row["minimum_severity"], 0)):
                        terminal = "suppressed"
                    elif not rule_row["allow_during_quiet_hours"]:
                        quiet, quiet_end = self._quiet_state(
                            self._global_policy(global_row), now)
                        if quiet:
                            hold_until = quiet_end
                if hold_until and not terminal:
                    db.execute("""UPDATE notification_deliveries SET status='queued',
                        next_attempt_at=?,error='held for quiet hours',updated_at=? WHERE id=?""",
                               (hold_until, now, row["id"]))
                    continue
                if terminal:
                    db.execute("""UPDATE notification_deliveries SET status=?,error=?,
                        updated_at=? WHERE id=?""",
                               (terminal, terminal.replace("_", " "), now, row["id"]))
                    if terminal == "subscription_expired":
                        db.execute("""UPDATE notification_devices SET subscription_json='{}',
                            endpoint_origin='',enabled=0 WHERE id=?""", (row["device_id"],))
                    continue
                try:
                    subscription = json.loads(row["subscription_json"] or "{}")
                    subscription, _ = self._push_subscription(
                        subscription, extra_origins=allowed_origins)
                except (TypeError, ValueError, OperationsError):
                    db.execute("""UPDATE notification_deliveries SET status='failed',
                        error='invalid stored subscription',updated_at=? WHERE id=?""",
                               (now, row["id"]))
                    db.execute("""UPDATE notification_devices SET last_failure_at=?,
                        last_failure='invalid stored subscription' WHERE id=?""",
                               (now, row["device_id"]))
                    continue
                claimed = db.execute("""UPDATE notification_deliveries SET status='sending',
                    attempt=attempt+1,claimed_at=?,lease_until=?,updated_at=?
                    WHERE id=? AND status IN ('queued','retrying')""", (
                    now, now + self.delivery_lease_seconds, now, row["id"])).rowcount
                if not claimed:
                    continue
                unread = int(db.execute("""SELECT COUNT(*) FROM notification_events
                    WHERE sequence>(SELECT read_cursor FROM notification_devices WHERE id=?)""",
                    (row["device_id"],)).fetchone()[0])
                event_cursor = int(db.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM notification_events").fetchone()[0])
                return {"id": row["id"], "event_id": row["event_id"],
                        "device_id": row["device_id"], "generation": row["generation"],
                        "purpose": row["purpose"],
                        "attempt": int(row["attempt"] or 0) + 1,
                        "subscription": subscription, "endpoint_origin": row["endpoint_origin"],
                        "event": {"kind": row["kind"], "title": row["title"],
                                  "summary": row["summary"],
                                  "session_id": row["session_id"],
                                  "source_revision": row["event_revision"],
                                  "push_test": bool(event_payload.get("push_test")),
                                  "unread": min(999, max(0, unread)),
                                  "cursor": event_cursor}}
        return None

    def notification_finish_delivery(self, delivery_id, result):
        """Persist one helper result and schedule only bounded, durable retries."""
        delivery_id = self._text(delivery_id, 100)
        if not delivery_id or not isinstance(result, dict):
            raise OperationsError("invalid notification delivery result")
        now = self.clock()
        try:
            remote_status = result.get("status")
            remote_status = None if remote_status is None else int(remote_status)
            retry_after = result.get("retry_after")
            retry_after = None if retry_after is None else max(0, min(600, float(retry_after)))
        except (TypeError, ValueError):
            raise OperationsError("invalid notification delivery result")
        code = self._text(result.get("code"), 80) or \
            (f"HTTP {remote_status}" if remote_status is not None else "delivery failed")
        with self.lock, self._transaction(immediate=True) as db:
            row = db.execute("SELECT * FROM notification_deliveries WHERE id=?",
                             (delivery_id,)).fetchone()
            if not row or row["status"] != "sending":
                raise OperationsError("notification delivery lease is stale")
            event_meta = db.execute("SELECT payload_json FROM notification_events WHERE id=?",
                                    (row["event_id"],)).fetchone()
            try:
                is_push_test = bool(json.loads(event_meta["payload_json"] or "{}").get(
                    "push_test")) if event_meta else False
            except (TypeError, ValueError):
                is_push_test = False
            attempt = int(row["attempt"] or 0)
            if bool(result.get("ok")) and remote_status is not None and 200 <= remote_status < 300:
                status, next_attempt, error = "sent", None, None
                db.execute("""UPDATE notification_devices SET last_success_at=?,
                    test_success_at=CASE WHEN ? THEN ? ELSE test_success_at END,
                    last_failure=NULL WHERE id=?""",
                           (now, 1 if is_push_test else 0, now, row["device_id"]))
                db.execute("""UPDATE notification_events SET
                    last_push_at=COALESCE(last_push_at,?) WHERE id=?""",
                           (now, row["event_id"]))
                db.execute("""UPDATE notification_events SET state='resolved',changed_at=?,
                    resolved_at=?,snoozed_until=NULL WHERE source_type='delivery'
                    AND source_id=? AND state IN ('active','snoozed')""",
                           (now, now, row["device_id"]))
            elif remote_status in (404, 410):
                status, next_attempt, error = "subscription_expired", None, "subscription expired"
                db.execute("""UPDATE notification_devices SET subscription_json='{}',
                    endpoint_origin='',enabled=0,permission_state='expired',last_failure_at=?,
                    last_failure='subscription expired' WHERE id=?""", (now, row["device_id"]))
            else:
                retryable = (remote_status in (408, 429) or
                             (remote_status is not None and remote_status >= 500) or
                             (remote_status is None and bool(result.get("retryable"))))
                if retryable and attempt <= len(self.delivery_retry_delays):
                    base = max(0.0, float(self.delivery_retry_delays[attempt-1]))
                    delay = max(0.0, float(self.delivery_jitter(base)))
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                    status, next_attempt, error = "retrying", now + delay, code
                else:
                    status, next_attempt, error = "failed", None, code
                db.execute("""UPDATE notification_devices SET last_failure_at=?,last_failure=?
                    WHERE id=?""", (now, code, row["device_id"]))
            db.execute("""UPDATE notification_deliveries SET status=?,lease_until=NULL,
                next_attempt_at=?,remote_status=?,remote_id=?,error=?,updated_at=? WHERE id=?""", (
                status, next_attempt, remote_status,
                self._text(result.get("remote_id"), 160) or None,
                error, now, delivery_id))
            saved = db.execute("SELECT * FROM notification_deliveries WHERE id=?",
                               (delivery_id,)).fetchone()
            if status in ("failed", "subscription_expired"):
                db.execute("""UPDATE notification_events SET state='resolved',changed_at=?,
                    resolved_at=?,snoozed_until=NULL WHERE source_type='delivery'
                    AND source_id=? AND state IN ('active','snoozed')""",
                           (now, now, row["device_id"]))
                self._upsert_notification_event(db, {
                    "event_key": self._canonical_notification_key(
                        "delivery", row["device_id"], delivery_id),
                    "kind": "failure", "state": "active", "severity": "warning",
                    "title": "Notification delivery failed",
                    "summary": "A registered device needs delivery review.",
                    "source_type": "delivery", "source_id": row["device_id"],
                    "source_revision": delivery_id, "reminder_budget": 1,
                    "payload": {"delivery_id": delivery_id},
                }, now)
        return self._notification_delivery(saved)

    def notification_retry_delivery(self, delivery_id):
        """Create a new generation for a failed job after device/event revalidation."""
        delivery_id = self._text(delivery_id, 100)
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            prior = db.execute("""SELECT d.*,e.state AS event_state,e.payload_json,
                    v.enabled,v.permission_state,v.subscription_json,v.test_success_at
                FROM notification_deliveries d
                JOIN notification_events e ON e.id=d.event_id
                JOIN notification_devices v ON v.id=d.device_id WHERE d.id=?""",
                (delivery_id,)).fetchone()
            if not prior or prior["status"] not in ("failed", "subscription_expired"):
                raise OperationsError("notification delivery is not retryable")
            try:
                payload = json.loads(prior["payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if (prior["event_state"] != "active" and not payload.get("push_test")):
                raise OperationsError("notification event is stale")
            if (not prior["enabled"] or prior["permission_state"] != "granted" or
                    prior["subscription_json"] == "{}" or
                    (not payload.get("push_test") and not prior["test_success_at"])):
                raise OperationsError("notification device must pass a test before retrying")
            queued = int(db.execute("""SELECT COUNT(*) FROM notification_deliveries
                WHERE status IN ('queued','sending','retrying')""").fetchone()[0])
            if queued >= self.delivery_queue_limit:
                raise OperationsError("notification delivery queue is full")
            generation = int(db.execute("""SELECT COALESCE(MAX(generation),0)+1
                FROM notification_deliveries WHERE event_id=? AND device_id=?""",
                (prior["event_id"], prior["device_id"])).fetchone()[0])
            new_id = self._text(self.delivery_id_factory(), 100)
            if not new_id:
                raise OperationsError("notification delivery id is unavailable")
            db.execute("""INSERT INTO notification_deliveries(
                id,event_id,device_id,generation,purpose,source_revision,status,attempt,
                next_attempt_at,created_at,updated_at)
                VALUES(?,?,?,?,'manual_retry',?,'queued',0,?,?,?)""",
                (new_id, prior["event_id"], prior["device_id"], generation,
                 prior["source_revision"], now, now, now))
            saved = db.execute("SELECT * FROM notification_deliveries WHERE id=?",
                               (new_id,)).fetchone()
        return self._notification_delivery(saved)

    def notification_delivery_status(self, delivery_id):
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM notification_deliveries WHERE id=?",
                             (self._text(delivery_id, 100),)).fetchone()
        return self._notification_delivery(row)

    def notification_delivery_diagnostics(self):
        now = self.clock()
        with self.lock, self._connect() as db:
            counts = {row["status"]: int(row["n"]) for row in db.execute(
                "SELECT status,COUNT(*) AS n FROM notification_deliveries GROUP BY status")}
            current_counts = {row["status"]: int(row["n"]) for row in db.execute("""
                SELECT d.status,COUNT(*) AS n FROM notification_deliveries d
                JOIN (SELECT event_id,device_id,MAX(generation) AS generation
                    FROM notification_deliveries GROUP BY event_id,device_id) latest
                ON latest.event_id=d.event_id AND latest.device_id=d.device_id
                AND latest.generation=d.generation GROUP BY d.status""")}
            purposes = {row["purpose"]: int(row["n"]) for row in db.execute("""
                SELECT purpose,COUNT(*) AS n FROM notification_deliveries
                WHERE status IN ('queued','sending','retrying') GROUP BY purpose""")}
            oldest = db.execute("""SELECT MIN(created_at) FROM notification_deliveries
                WHERE status IN ('queued','sending','retrying')""").fetchone()[0]
            next_retry = db.execute("""SELECT MIN(next_attempt_at) FROM notification_deliveries
                WHERE status IN ('queued','retrying')""").fetchone()[0]
            last_success = db.execute("""SELECT MAX(updated_at) FROM notification_deliveries
                WHERE status='sent'""").fetchone()[0]
            last_failure = db.execute("""SELECT MAX(updated_at) FROM notification_deliveries
                WHERE status IN ('failed','subscription_expired')""").fetchone()[0]
            latencies = [float(row[0]) for row in db.execute("""SELECT updated_at-created_at
                FROM notification_deliveries WHERE status='sent'
                ORDER BY updated_at DESC LIMIT 240""").fetchall()]
        ordered_latency = sorted(latencies)
        latency_p95 = (ordered_latency[min(len(ordered_latency)-1,
            max(0, round((len(ordered_latency)-1)*.95)))] if ordered_latency else 0)
        return {"queued": sum(counts.get(state, 0) for state in
                              ("queued", "sending", "retrying")),
                "failed": current_counts.get("failed", 0),
                "subscription_expired": current_counts.get("subscription_expired", 0),
                "oldest_pending_seconds": round(max(0, now-float(oldest)), 3) if oldest else 0,
                "next_retry_seconds": round(max(0, float(next_retry)-now), 3)
                                      if next_retry else 0,
                "last_success_at": last_success, "last_failure_at": last_failure,
                "sent_latency_p95_seconds": round(latency_p95, 3),
                "pending_by_purpose": {purpose: purposes.get(purpose, 0)
                                       for purpose in sorted(PUSH_DELIVERY_PURPOSES)},
                "statuses": {state: counts.get(state, 0) for state in
                             sorted(PUSH_DELIVERY_STATES)}}

    def legacy_notification_diagnostics(self):
        with self.lock, self._connect() as db:
            counts = {row["status"]: int(row["n"]) for row in db.execute(
                """SELECT status,COUNT(*) AS n FROM notification_deliveries_legacy
                GROUP BY status""")}
            last_attempt = db.execute(
                "SELECT MAX(updated_at) FROM notification_deliveries_legacy").fetchone()[0]
        return {"automatic": False, "last_attempt_at": last_attempt,
                "statuses": {state: counts.get(state, 0) for state in
                             ("queued", "sent", "failed", "disabled", "suppressed_seed")}}

    def notification_claim(self, key, category, title, body, *, dispatch):
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            row = db.execute("SELECT status FROM notification_deliveries_legacy WHERE event_key=?",
                             (key,)).fetchone()
            if row:
                return False
            status = "queued" if dispatch else "suppressed_seed"
            db.execute("""INSERT INTO notification_deliveries_legacy(
                event_key,category,title,body,status,error,created_at,updated_at)
                VALUES(?,?,?,?,?,NULL,?,?)""", (self._text(key, 300),
                self._text(category, 40), self._text(title, 160), self._text(body, 800),
                status, now, now))
            return bool(dispatch)

    def notification_status(self, key, status, error=None):
        if status not in ("sent", "failed", "disabled"):
            raise OperationsError("invalid notification status")
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            db.execute("""UPDATE notification_deliveries_legacy SET status=?,error=?,updated_at=?
                WHERE event_key=?""", (status, self._text(error, 800) or None, now, key))
            if status == "failed":
                row = db.execute("""SELECT category,title,body FROM notification_deliveries_legacy
                    WHERE event_key=?""", (key,)).fetchone()
                if row:
                    self._insert_event(
                        db, key=f"notification-failed:{key}", category="notification",
                        severity="warning", title="Legacy ntfy test failed",
                        summary="Legacy ntfy delivery failed", source_type="notification",
                        source_id=key, link_kind="settings",
                        payload={"category": row[0], "title": row[1]})

    def quiet_digest(self, since):
        with self.lock, self._connect() as db:
            counts = {row[0]: int(row[1]) for row in db.execute("""SELECT category,COUNT(*)
                FROM briefing_events WHERE created_at>=? GROUP BY category""", (since,)).fetchall()}
            muted = int(db.execute("""SELECT COUNT(*) FROM briefing_events
                WHERE created_at>=? AND muted=1""", (since,)).fetchone()[0])
        parts = []
        if counts.get("completed"):
            parts.append(f"{counts['completed']} completed")
        if counts.get("outcome"):
            parts.append(f"{counts['outcome']} outcomes")
        if counts.get("attention"):
            parts.append(f"{counts['attention']} need review")
        if muted:
            parts.append(f"{muted} muted")
        return " · ".join(parts) or "All active work is quiet"

    def fleet_activity_transition(self, busy, now=None):
        """Persist active→quiet episode identity across daemon restarts."""
        now = float(self.clock() if now is None else now)
        busy = int(busy or 0)
        with self.lock, self._transaction(immediate=True) as db:
            previous = int(self._meta(db, "fleet_busy", 0) or 0)
            quiet_since = self._meta(db, "fleet_quiet_since", None)
            if busy > 0:
                quiet_since = None
            elif previous > 0:
                quiet_since = now
            self._set_meta(db, "fleet_busy", busy)
            self._set_meta(db, "fleet_quiet_since", quiet_since)
        return quiet_since
