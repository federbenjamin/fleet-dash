"""Cadence, quiet-hours, and delivery-lease scheduling for FleetOperations.

Split out of briefing.py as pure code motion: quiet-hour math, kind-policy
evaluation and push-revision suppression, delivery leases, and wave/repeat
logic. Consumed as a mixin by FleetOperations.
"""
from __future__ import annotations

import json
from datetime import datetime, time as datetime_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .briefing_store import (
    OperationsError, DEVICE_RE, NOTIFICATION_KINDS, NOTIFICATION_SEVERITIES,
    NOTIFICATION_POLICY_MODES, PUSH_DELIVERY_STATES, PUSH_DELIVERY_PURPOSES,
    PUSHABLE_KINDS, INFORMATIONAL_NOTIFICATION_KINDS, PUSH_SEVERITY_RANK)


class SchedulerOps:
    """Quiet hours, kind-policy cadence, and durable delivery leases."""

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
            return True, SchedulerOps._local_boundary_epoch(
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
        return True, SchedulerOps._local_boundary_epoch(end_day, end_minute, zone)

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

