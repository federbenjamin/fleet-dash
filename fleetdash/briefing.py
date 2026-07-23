"""Durable briefings, notification audit, budgets, and honest forecasts.

FleetOperations is the public facade: projections, observation, budgets, and
device/notification management. The SQLite store lives in briefing_store and the
cadence/delivery scheduler in briefing_scheduler; both are mixed in here.
"""
from __future__ import annotations

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

from .briefing_store import (
    StoreOps, OperationsError, BUDGET_SCOPES, BUDGET_METRICS, DEVICE_RE,
    NOTIFICATION_STATES, NOTIFICATION_KINDS, PUSH_PERMISSION_STATES,
    PUSH_RETRY_DELAYS, PUSHABLE_KINDS)
from .briefing_scheduler import SchedulerOps

__all__ = ["FleetOperations", "OperationsError"]


class FleetOperations(StoreOps, SchedulerOps):
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

