"""Durable one-time message scheduling for Fleet Dash.

The browser is only an editor.  Delivery is owned by this SQLite-backed manager
so a closed tab, daemon restart, or concurrent HTTP request cannot lose or
duplicate a scheduled message.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PENDING_STATES = {"scheduled", "waiting_availability", "waiting_usage_reset"}
TERMINAL_STATES = {"sent", "confirmation_unknown", "blocked", "failed", "cancelled"}
ALL_STATES = PENDING_STATES | TERMINAL_STATES | {"spawning", "sending"}
KINDS = {"at_time", "when_available", "usage_reset", "new_session"}
STATE_LABELS = {
    "scheduled": "Scheduled",
    "waiting_availability": "Waiting for availability",
    "waiting_usage_reset": "Waiting for usage reset",
    "spawning": "Spawning",
    "sending": "Sending",
    "sent": "Sent",
    "confirmation_unknown": "Confirmation unknown",
    "blocked": "Blocked",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


class OutboxError(ValueError):
    """A user-correctable outbox request error."""

    def __init__(self, message, *, code="invalid"):
        super().__init__(message)
        self.code = code


def _epoch(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise OutboxError("invalid time")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        raise OutboxError("time must be an ISO timestamp with a timezone")


def resolve_local_time(local_time, zone_name, fold=None):
    """Resolve a browser datetime-local value without guessing across DST."""
    try:
        zone = ZoneInfo(str(zone_name or ""))
    except ZoneInfoNotFoundError:
        raise OutboxError("unknown IANA timezone", code="bad_timezone")
    try:
        naive = dt.datetime.fromisoformat(str(local_time or ""))
    except (TypeError, ValueError):
        raise OutboxError("choose a valid local date and time")
    if naive.tzinfo is not None:
        raise OutboxError("local time must not include a timezone offset")
    naive = naive.replace(second=0, microsecond=0)
    candidates = []
    for candidate_fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=candidate_fold)
        utc = aware.astimezone(dt.timezone.utc)
        back = utc.astimezone(zone)
        if back.replace(tzinfo=None) == naive:
            candidates.append((candidate_fold, utc.timestamp(), aware.utcoffset()))
    unique = {}
    for candidate in candidates:
        unique[candidate[1]] = candidate
    candidates = list(unique.values())
    if not candidates:
        raise OutboxError(
            "that local time does not exist because the clock moves forward",
            code="nonexistent_time")
    if len(candidates) > 1:
        if fold not in (0, 1, "0", "1"):
            choices = sorted(candidates, key=lambda item: item[1])
            err = OutboxError(
                "that local time occurs twice; choose the first or second occurrence",
                code="ambiguous_time")
            err.choices = [
                {"fold": item[0], "trigger_at": item[1],
                 "offset": str(item[2])} for item in choices]
            raise err
        selected = next((item for item in candidates if item[0] == int(fold)), None)
        if selected is None:
            raise OutboxError("invalid repeated-time occurrence")
        return selected[1], int(fold)
    return candidates[0][1], candidates[0][0]


class OutboxManager:
    """Persistence, validation, claims, and delivery orchestration."""

    def __init__(self, db_path, *, clock=time.time, id_factory=None,
                 lease_seconds=45, max_batch=20):
        self.db_path = db_path
        self.clock = clock
        self.id_factory = id_factory or (lambda: "out-" + uuid.uuid4().hex)
        self.lease_seconds = max(5, int(lease_seconds))
        self.max_batch = max(1, int(max_batch))
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextlib.contextmanager
    def _transaction(self, immediate=False):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS outbox_messages(
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
                retry_of TEXT, version INTEGER NOT NULL DEFAULT 1)""")
            db.execute("""CREATE INDEX IF NOT EXISTS outbox_pending
                ON outbox_messages(state, next_attempt_at, trigger_at, created_at)""")
            db.execute("""CREATE INDEX IF NOT EXISTS outbox_target
                ON outbox_messages(target_session_id, state, created_at)""")

    @staticmethod
    def _public(row):
        if row is None:
            return None
        item = dict(row)
        try:
            item["spawn_spec"] = json.loads(item.pop("spawn_spec_json") or "null")
        except (TypeError, ValueError):
            item["spawn_spec"] = None
        try:
            item["provider_receipt"] = json.loads(item.get("provider_receipt") or "null")
        except (TypeError, ValueError):
            item["provider_receipt"] = None
        item["state_label"] = STATE_LABELS.get(item.get("state"), "Unknown")
        item["editable"] = item.get("state") in PENDING_STATES and not item.get("claimed_at")
        item["cancellable"] = item["editable"]
        item["retryable"] = item.get("state") in {
            "blocked", "failed", "confirmation_unknown"}
        return item

    @staticmethod
    def _message(value):
        message = str(value or "").replace("\x00", "").strip()
        if not message:
            raise OutboxError("message is required")
        if len(message) > 2000:
            raise OutboxError("message must be 2,000 characters or fewer")
        return message

    @staticmethod
    def _zone(value):
        zone = str(value or "UTC")[:120]
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            raise OutboxError("unknown IANA timezone", code="bad_timezone")
        return zone

    def _trigger(self, payload, kind):
        if kind not in ("at_time", "new_session"):
            return None, None, None
        zone = self._zone(payload.get("created_zone"))
        local_time = str(payload.get("local_time") or "")[:40] or None
        fold = payload.get("trigger_fold")
        if local_time:
            trigger_at, fold = resolve_local_time(local_time, zone, fold)
            return trigger_at, local_time, fold
        return _epoch(payload.get("trigger_at")), None, None

    def _normalize_create(self, payload, *, now=None):
        now = self.clock() if now is None else float(now)
        kind = str(payload.get("kind") or "")
        if kind not in KINDS:
            raise OutboxError("unsupported send mode")
        message = self._message(payload.get("message"))
        zone = self._zone(payload.get("created_zone"))
        trigger_at, local_time, fold = self._trigger(payload, kind)
        target_provider = str(payload.get("target_provider") or "")[:30] or None
        target_session = str(payload.get("target_session_id") or "")[:320] or None
        target_agent = str(payload.get("target_agent_id") or "")[:320] or None
        usage_account = str(payload.get("usage_account_id") or "")[:320] or None
        usage_window = str(payload.get("usage_window_id") or "")[:320] or None
        reset_at = _epoch(payload.get("observed_reset_at"))
        spawn_spec = payload.get("spawn_spec")
        if kind == "new_session":
            if not isinstance(spawn_spec, dict):
                raise OutboxError("new-session settings are required")
            provider = str(spawn_spec.get("provider") or "")
            cwd = str(spawn_spec.get("cwd") or "").strip()
            if provider not in ("claude", "codex") or not cwd:
                raise OutboxError("new session needs a provider and directory")
            target_provider = provider
            target_session = target_agent = None
        else:
            if not target_session or target_provider not in ("claude", "codex"):
                raise OutboxError("choose an exact session target")
            spawn_spec = None
        if kind == "usage_reset" and (not usage_account or not usage_window or reset_at is None):
            raise OutboxError("choose a usage account and reset window")
        state = {"when_available": "waiting_availability",
                 "usage_reset": "waiting_usage_reset"}.get(kind, "scheduled")
        base_trigger = (trigger_at if trigger_at is not None else
                        reset_at if kind == "usage_reset" and reset_at is not None else now)
        return {
            "created_zone": zone, "local_time": local_time, "trigger_fold": fold,
            "kind": kind, "state": state, "message": message,
            "target_provider": target_provider,
            "target_session_id": target_session, "target_agent_id": target_agent,
            "trigger_at": trigger_at, "usage_account_id": usage_account,
            "usage_window_id": usage_window, "observed_reset_at": reset_at,
            "spawn_spec_json": (json.dumps(spawn_spec, separators=(",", ":"),
                                           sort_keys=True) if spawn_spec else None),
            "next_attempt_at": now, "expires_at": base_trigger + 86400,
        }

    def create(self, payload):
        now = self.clock()
        values = self._normalize_create(payload, now=now)
        outbox_id = self.id_factory()
        columns = ["id", "created_at", "updated_at", *values.keys()]
        params = [outbox_id, now, now, *values.values()]
        with self._transaction(immediate=True) as db:
            db.execute(
                f"INSERT INTO outbox_messages({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})", params)
        return self.get(outbox_id)

    def get(self, outbox_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM outbox_messages WHERE id=?",
                             (str(outbox_id or ""),)).fetchone()
        return self._public(row)

    def list(self, *, state=None, cursor=0, limit=100):
        try:
            cursor = max(0, int(cursor))
            limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            raise OutboxError("invalid outbox cursor")
        states = []
        if state:
            states = [part for part in str(state).split(",") if part in ALL_STATES]
            if not states:
                raise OutboxError("unknown outbox state")
        where = " WHERE state IN (%s)" % ",".join("?" for _ in states) if states else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM outbox_messages" + where +
                " ORDER BY CASE WHEN state IN ('blocked','failed','confirmation_unknown') "
                "THEN 0 WHEN state='sent' THEN 2 ELSE 1 END, "
                "COALESCE(trigger_at, created_at), created_at, id LIMIT ? OFFSET ?",
                (*states, limit + 1, cursor)).fetchall()
        more = len(rows) > limit
        return {"ok": True, "items": [self._public(row) for row in rows[:limit]],
                "next_cursor": cursor + limit if more else None}

    def counts(self):
        with self._connect() as db:
            rows = db.execute("SELECT state, COUNT(*) n FROM outbox_messages "
                              "GROUP BY state").fetchall()
        values = {row["state"]: row["n"] for row in rows}
        pending = sum(values.get(state, 0) for state in PENDING_STATES | {"spawning", "sending"})
        attention = sum(values.get(state, 0) for state in
                        ("blocked", "failed", "confirmation_unknown"))
        return {"pending": pending, "attention": attention, "states": values}

    def update(self, outbox_id, patch):
        current = self.get(outbox_id)
        if not current:
            raise OutboxError("outbox message not found", code="stale")
        if not current["editable"]:
            raise OutboxError("only unclaimed pending messages can be edited", code="immutable")
        merged = {**current, **dict(patch or {})}
        if "spawn_spec" not in merged:
            merged["spawn_spec"] = current.get("spawn_spec")
        values = self._normalize_create(merged)
        now = self.clock()
        with self._transaction(immediate=True) as db:
            result = db.execute(
                "UPDATE outbox_messages SET " + ",".join(f"{key}=?" for key in values) +
                ",updated_at=?,version=version+1 WHERE id=? AND state IN "
                "('scheduled','waiting_availability','waiting_usage_reset') "
                "AND claimed_at IS NULL",
                (*values.values(), now, outbox_id))
            if result.rowcount != 1:
                raise OutboxError("outbox message changed; refresh", code="stale")
        return self.get(outbox_id)

    def cancel(self, outbox_id):
        now = self.clock()
        with self._transaction(immediate=True) as db:
            result = db.execute(
                "UPDATE outbox_messages SET state='cancelled',updated_at=?,version=version+1 "
                "WHERE id=? AND state IN ('scheduled','waiting_availability',"
                "'waiting_usage_reset') AND claimed_at IS NULL", (now, outbox_id))
            if result.rowcount != 1:
                raise OutboxError("only an unclaimed pending message can be cancelled",
                                  code="immutable")
        return self.get(outbox_id)

    def retry(self, outbox_id, patch=None):
        current = self.get(outbox_id)
        if not current or not current.get("retryable"):
            raise OutboxError("that outbox message cannot be retried", code="immutable")
        payload = {key: current.get(key) for key in (
            "kind", "message", "target_provider", "target_session_id", "target_agent_id",
            "created_zone", "local_time", "trigger_fold", "trigger_at",
            "usage_account_id", "usage_window_id", "observed_reset_at", "spawn_spec")}
        payload.update(dict(patch or {}))
        if payload.get("kind") in ("at_time", "new_session") and not (
                patch and ("trigger_at" in patch or "local_time" in patch)):
            payload["trigger_at"] = self.clock()
            payload["local_time"] = None
        created = self.create(payload)
        with self._transaction(immediate=True) as db:
            db.execute("UPDATE outbox_messages SET retry_of=? WHERE id=?",
                       (outbox_id, created["id"]))
        return self.get(created["id"])

    def retarget(self, outbox_id, patch):
        current = self.get(outbox_id)
        if not current:
            raise OutboxError("outbox message not found", code="stale")
        if current.get("editable"):
            return self.update(outbox_id, patch)
        if current.get("retryable"):
            return self.retry(outbox_id, patch)
        raise OutboxError("sent and cancelled messages are immutable", code="immutable")

    def send_now(self, outbox_id):
        now = self.clock()
        with self._transaction(immediate=True) as db:
            row = db.execute("SELECT state FROM outbox_messages WHERE id=?",
                             (outbox_id,)).fetchone()
            if not row or row["state"] not in PENDING_STATES:
                raise OutboxError("only a pending message can be sent now", code="immutable")
            db.execute("UPDATE outbox_messages SET state=?,trigger_at=?,next_attempt_at=?,"
                       "updated_at=?,version=version+1 WHERE id=?",
                       ("scheduled" if row["state"] != "waiting_usage_reset" else
                        "waiting_availability", now, now, now, outbox_id))
        return self.get(outbox_id)

    @staticmethod
    def _sessions(snapshot):
        return {str(item.get("session_id")): item for item in
                (snapshot or {}).get("sessions", [])}

    @staticmethod
    def _provider_problem(snapshot, provider):
        status = ((snapshot or {}).get("providers") or {}).get(provider) or {}
        return status.get("ok") is False

    def _target_status(self, record, snapshot):
        sessions = self._sessions(snapshot)
        sid = record.get("destination_session_id") or record.get("target_session_id")
        session = sessions.get(str(sid or ""))
        if not session:
            if record.get("destination_session_id") and self.clock() - record["updated_at"] < 120:
                return "wait", "Waiting for the new session to register", None
            return "block", "The exact target session is no longer live", None
        if session.get("provider_stale") or session.get("stale"):
            return "retry", "Provider state is stale", session
        if session.get("read_only") or session.get("external") or session.get("access") == "view_only":
            return "block", session.get("read_only_reason") or "The target is view only", session
        capabilities = session.get("capabilities") or {}
        agent_id = record.get("target_agent_id")
        if agent_id:
            agent = next((item for item in session.get("agents") or []
                          if str(item.get("agent_id")) == str(agent_id)), None)
            if not agent:
                return "block", "The exact subagent is no longer available", session
            if agent.get("state") in ("done", "ended", "cancelled", "error"):
                return "block", "The target subagent has finished", session
            if not capabilities.get("relay_agent"):
                return "block", "This provider cannot relay to that subagent", session
        elif not capabilities.get("submit"):
            return "block", "The target does not accept messages from Fleet", session
        if session.get("pending") or session.get("ui_group") in ("working", "needs_you") \
                or session.get("state") in ("running", "stalled", "needs_you"):
            return "wait", "Waiting for the target to become available", session
        if session.get("ui_group") != "available":
            return "block", "The target is inactive rather than available", session
        return "ready", None, session

    @staticmethod
    def _usage_window(record, usage):
        provider = record.get("target_provider")
        current = (usage or {}).get(provider)
        if not current or current.get("stale") or current.get("error"):
            return None, "Usage evidence is unavailable"
        account_id = record.get("usage_account_id")
        window_id = record.get("usage_window_id")
        if provider == "claude":
            profiles = current.get("profiles") or [current]
            profile = next((item for item in profiles if str(item.get("id") or
                item.get("email") or "active") == str(account_id)), None)
            if not profile:
                return None, "The selected Claude account is unavailable"
            field = {"five_hour": "five_hour_reset", "weekly": "weekly_reset"}.get(window_id)
            if not field:
                return None, "The selected Claude usage window is unavailable"
            return _epoch(profile.get(field)), None
        account = str(current.get("account_id") or current.get("email") or "active")
        if account != str(account_id):
            return None, "The selected Codex account is unavailable"
        bucket = next((item for item in current.get("buckets") or []
                       if str(item.get("id")) == str(window_id)), None)
        if not bucket:
            return None, "The selected Codex usage window is unavailable"
        return _epoch(bucket.get("reset")), None

    def _set_waiting(self, outbox_id, state, reason=None, *, next_attempt=None,
                     observed_reset=None):
        now = self.clock()
        with self._transaction(immediate=True) as db:
            db.execute("UPDATE outbox_messages SET state=?,updated_at=?,claimed_at=NULL,"
                       "lease_until=NULL,blocked_reason=?,next_attempt_at=?,"
                       "observed_reset_at=COALESCE(?,observed_reset_at),version=version+1 "
                       "WHERE id=?", (state, now, reason, next_attempt or now,
                                      observed_reset, outbox_id))

    def _terminal(self, outbox_id, state, *, error=None, reason=None, receipt=None,
                  destination=None):
        now = self.clock()
        bounded_receipt = None
        if receipt is not None:
            bounded_receipt = json.dumps(receipt, separators=(",", ":"),
                                         sort_keys=True)[:4000]
        with self._transaction(immediate=True) as db:
            db.execute("UPDATE outbox_messages SET state=?,updated_at=?,claimed_at=NULL,"
                       "lease_until=NULL,error=?,blocked_reason=?,provider_receipt=?,"
                       "destination_session_id=COALESCE(?,destination_session_id),"
                       "sent_at=?,version=version+1 WHERE id=?",
                       (state, now, str(error or "")[:1000] or None,
                        str(reason or "")[:1000] or None, bounded_receipt, destination,
                        now if state == "sent" else None, outbox_id))

    def recover_expired(self, snapshot):
        now = self.clock()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM outbox_messages WHERE state IN "
                              "('sending','spawning') AND lease_until<?", (now,)).fetchall()
        for raw in rows:
            record = self._public(raw)
            if record["state"] == "spawning" and record.get("destination_session_id") \
                    and record["destination_session_id"] in self._sessions(snapshot):
                self._set_waiting(record["id"], "waiting_availability",
                                  "Recovered the exact spawned session after restart")
            else:
                self._terminal(record["id"], "confirmation_unknown",
                    reason="Fleet restarted after provider dispatch began; delivery cannot be proven")

    def _claim(self, record, state):
        now = self.clock()
        with self._transaction(immediate=True) as db:
            result = db.execute(
                "UPDATE outbox_messages SET state=?,claimed_at=?,lease_until=?,"
                "dispatch_started_at=?,attempt_count=attempt_count+1,updated_at=?,"
                "version=version+1 WHERE id=? AND version=? AND state=? AND claimed_at IS NULL",
                (state, now, now + self.lease_seconds, now, now, record["id"],
                 record["version"], record["state"]))
            return result.rowcount == 1

    def _transient(self, record, reason):
        now = self.clock()
        if now >= float(record.get("expires_at") or now):
            self._terminal(record["id"], "blocked",
                           reason=reason + " for 24 hours after its trigger")
            return
        attempt = int(record.get("attempt_count") or 0)
        delay = min(300, 2 ** min(8, attempt + 1))
        self._set_waiting(record["id"], record["state"], reason,
                          next_attempt=now + delay)

    def tick(self, snapshot, usage, dispatch, spawn):
        """Evaluate and deliver a bounded batch. Call from one daemon scheduler."""
        self.recover_expired(snapshot)
        now = self.clock()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM outbox_messages WHERE state IN "
                "('scheduled','waiting_availability','waiting_usage_reset') "
                "AND claimed_at IS NULL AND COALESCE(next_attempt_at,0)<=? "
                "ORDER BY COALESCE(trigger_at,created_at),created_at,id LIMIT ?",
                (now, self.max_batch)).fetchall()
        for raw in rows:
            record = self._public(raw)
            if record["state"] == "scheduled" and record.get("trigger_at") is not None \
                    and now < record["trigger_at"]:
                continue
            if self._provider_problem(snapshot, record.get("target_provider")):
                self._transient(record, "Provider is unavailable")
                continue
            if record["state"] == "waiting_usage_reset":
                reset_at, error = self._usage_window(record, usage)
                if error or reset_at is None:
                    self._transient(record, error or "Usage reset evidence is unavailable")
                    continue
                observed = float(record.get("observed_reset_at") or 0)
                if now < observed:
                    if reset_at != observed:
                        self._set_waiting(record["id"], "waiting_usage_reset",
                                          "The provider moved the reported reset time",
                                          observed_reset=reset_at)
                    continue
                if reset_at <= observed:
                    continue
                record["state"] = "waiting_availability"
                self._set_waiting(record["id"], "waiting_availability",
                                  "Fresh post-reset usage evidence received",
                                  observed_reset=reset_at)
                record = self.get(record["id"])
            if record["kind"] == "new_session" and not record.get("destination_session_id"):
                if record.get("target_provider") == "claude":
                    destination = str(uuid.uuid4())
                    with self._transaction(immediate=True) as db:
                        db.execute("UPDATE outbox_messages SET destination_session_id=?,"
                                   "updated_at=?,version=version+1 WHERE id=? AND version=?",
                                   (destination, now, record["id"], record["version"]))
                    record = self.get(record["id"])
                if not self._claim(record, "spawning"):
                    continue
                claimed = self.get(record["id"])
                try:
                    result = spawn(claimed)
                except Exception as exc:
                    self._terminal(record["id"], "failed", error=str(exc))
                    continue
                if not result.get("ok"):
                    self._terminal(record["id"], "failed",
                                   error=result.get("error") or "session spawn failed")
                elif result.get("message_delivered"):
                    self._terminal(record["id"], "sent", receipt=result,
                                   destination=result.get("session_id"))
                else:
                    with self._transaction(immediate=True) as db:
                        db.execute("UPDATE outbox_messages SET state='waiting_availability',"
                                   "destination_session_id=?,provider_receipt=?,claimed_at=NULL,"
                                   "lease_until=NULL,updated_at=?,next_attempt_at=?,version=version+1 "
                                   "WHERE id=?", (result.get("session_id"),
                                    json.dumps(result, separators=(",", ":"))[:4000],
                                    self.clock(), self.clock() + 1, record["id"]))
                continue
            target, reason, _session = self._target_status(record, snapshot)
            if target == "retry":
                self._transient(record, reason)
                continue
            if target == "wait":
                self._set_waiting(record["id"], "waiting_availability", reason,
                                  next_attempt=now + 1)
                continue
            if target == "block":
                self._terminal(record["id"], "blocked", reason=reason)
                continue
            if not self._claim(record, "sending"):
                continue
            claimed = self.get(record["id"])
            try:
                result = dispatch(claimed)
            except Exception as exc:
                self._terminal(record["id"], "failed", error=str(exc))
                continue
            if result.get("ok"):
                self._terminal(record["id"], "sent", receipt=result,
                               destination=record.get("destination_session_id"))
            else:
                self._terminal(record["id"], "failed",
                               error=result.get("error") or "provider rejected the message")
