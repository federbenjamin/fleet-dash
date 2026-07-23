"""SQLite store, schema/migrations, and row helpers for FleetOperations.

Split out of briefing.py as pure code motion: table DDL, the M12 ntfy-legacy
migration and severity normalization, generic (de)serialization/validation,
row projections, and event/meta CRUD. Consumed as a mixin by FleetOperations.
"""
from __future__ import annotations

import contextlib
import base64
import binascii
import hashlib
import ipaddress
import json
import math
import os
import re
import sqlite3
import time
from urllib.parse import urlsplit


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


class StoreOps:
    """DB connection, schema/migrations, and row/serialization helpers."""

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
        except (binascii.Error, ValueError):  # pragma: no cover - defensive: _base64url already decoded the identical value, so this re-decode cannot fail
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
        if len(cls._json(normalized)) > 4096:  # pragma: no cover - defensive: the normalized subscription is bounded by the 2048-char endpoint cap plus fixed 65/16-byte keys, so it never reaches 4096
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
    def _signature(value):
        import hashlib
        return hashlib.sha256(StoreOps._json(value).encode("utf-8")).hexdigest()[:24]

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
        material = StoreOps._json([str(part or "") for part in parts])
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

