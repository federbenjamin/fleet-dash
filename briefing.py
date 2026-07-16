"""Durable briefings, notification audit, budgets, and honest forecasts."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import uuid


EVENT_CATEGORIES = {
    "attention", "completed", "slow", "outcome", "budget", "measurement",
    "notification",
}
BUDGET_SCOPES = {"fleet", "provider", "workstream", "session"}
BUDGET_METRICS = {"usd", "tokens", "runtime", "concurrency"}
DEVICE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")


class OperationsError(ValueError):
    """A user-correctable briefing or budget request error."""


class FleetOperations:
    """SQLite-backed operational event stream and budget evaluator.

    The class deliberately consumes Fleet's normalized snapshot. Provider adapters
    remain responsible for deciding which measurements are exact or unavailable.
    """

    def __init__(self, db_path, *, clock=time.time, id_factory=None):
        self.db_path = db_path
        self.clock = clock
        self.id_factory = id_factory or (lambda: "bud-" + uuid.uuid4().hex)
        self.lock = threading.RLock()
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
            db.execute("""CREATE TABLE IF NOT EXISTS notification_deliveries(
                event_key TEXT PRIMARY KEY, category TEXT NOT NULL,
                title TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
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
            db.execute("""CREATE TABLE IF NOT EXISTS operations_meta(
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL)""")

    @staticmethod
    def _text(value, limit):
        return str(value or "").replace("\x00", " ").strip()[:limit]

    @staticmethod
    def _json(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

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
        db.execute("""INSERT OR IGNORE INTO briefing_events(
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
        with self.lock, self._transaction(immediate=True) as db:
            for session in sessions:
                sid = self._text(session.get("session_id"), 320)
                if not sid:
                    continue
                previous = db.execute(
                    "SELECT * FROM session_measurements WHERE session_id=?", (sid,)).fetchone()
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
                if isinstance(total_tokens, bool) or not isinstance(total_tokens, (int, float)):
                    total_tokens = previous["tokens"] if previous else None
                revision = self._text(session.get("convo_v") or session.get("closed_at"), 320)
                files_n = int(session.get("files_n") or 0)
                repo_signature = self._repo_signature(session)
                state = self._text(session.get("normalized_state") or session.get("state"), 40)
                ui_group = self._text(session.get("ui_group"), 40)
                runtime = self._runtime(session, now)
                concurrency = ((1 if ui_group == "working" else 0) +
                               int(session.get("agents_running") or 0))

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
            scope_id = self._text(raw.get("scope_id"), 320) or None
            if scope != "fleet" and not scope_id:
                raise OperationsError(f"{scope} budget needs a target")
            try:
                limit_value = float(raw.get("limit_value"))
            except (TypeError, ValueError):
                raise OperationsError("budget limit must be a number")
            if not math.isfinite(limit_value) or limit_value <= 0 or limit_value > 1e15:
                raise OperationsError("budget limit must be greater than zero")
            budget_id = self._text(raw.get("id"), 80) or self.id_factory()
            if budget_id in seen:
                raise OperationsError("duplicate budget ID")
            seen.add(budget_id)
            normalized.append({
                "id": budget_id, "scope_type": scope, "scope_id": scope_id,
                "metric": metric, "limit_value": limit_value,
                "block_spawns": bool(raw.get("block_spawns")),
                "enabled": raw.get("enabled") is not False,
                "label": self._text(raw.get("label"), 160) or None,
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
            failures = [dict(row) for row in db.execute("""SELECT event_key,title,error,updated_at
                FROM notification_deliveries WHERE status='failed' ORDER BY updated_at DESC LIMIT 20""").fetchall()]

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
        for failure in failures:
            attention.append({"id": failure["event_key"], "category": "notification",
                              "severity": "warning", "title": failure["title"],
                              "summary": failure["error"] or "Push delivery failed",
                              "current": True, "link_kind": "settings"})

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

    def notification_claim(self, key, category, title, body, *, dispatch):
        now = self.clock()
        with self.lock, self._transaction(immediate=True) as db:
            row = db.execute("SELECT status FROM notification_deliveries WHERE event_key=?",
                             (key,)).fetchone()
            if row:
                return False
            status = "queued" if dispatch else "suppressed_seed"
            db.execute("""INSERT INTO notification_deliveries(
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
            db.execute("""UPDATE notification_deliveries SET status=?,error=?,updated_at=?
                WHERE event_key=?""", (status, self._text(error, 800) or None, now, key))
            if status == "failed":
                row = db.execute("""SELECT category,title,body FROM notification_deliveries
                    WHERE event_key=?""", (key,)).fetchone()
                if row:
                    self._insert_event(
                        db, key=f"notification-failed:{key}", category="notification",
                        severity="warning", title="Push notification failed",
                        summary=error or "ntfy request failed", source_type="notification",
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
