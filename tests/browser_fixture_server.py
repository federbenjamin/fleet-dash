#!/usr/bin/env python3
"""Deterministic HTTP fixture for the repository-native browser suite."""
import copy
import hashlib
import json
import os
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "abcdef123456"
LOCK = threading.Lock()


def capabilities(**overrides):
    out = {"submit": True, "interrupt": False, "takeover": False,
           "close": True,
           "focus_terminal": False, "answer_structured": False,
           "decide_approval": False, "spawn_agent": True, "relay_agent": True,
           "relay_agent_direct": False, "account_usage": True, "exact_cost": False,
           "measured_throughput": False, "archive": True, "compact": True,
           "review": True, "files": True}
    out.update(overrides)
    return out


def base_session(provider, sid, title):
    codex = provider == "codex"
    return {"session_id": sid, "native_session_id": sid.split(":")[-1],
            "provider": provider, "pid": None if codex else 4242,
            "name": title, "title": title, "project": "fleet-dash",
            "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
            "model": "gpt-5.4" if codex else "claude-sonnet-4-5",
            "family": "codex" if codex else "sonnet", "effort": "high",
            "collaboration_mode": "plan" if codex else None,
            "running": None, "last_msg": {"role": "assistant",
                "text": "Ready for the next task."}, "state": "idle",
            "reg_status": "idle", "quiet_s": 3, "ctx_tokens": 1200,
            "total_tokens": 18000 if codex else 24000,
            "ctx_pct": 12.5, "cost": None if codex else 0.12,
            "agent_cost": None if codex else 0.03,
            "cost_source": "unavailable" if codex else "calculated",
            "bridge_url": None, "started_ms": int(time.time() * 1000) - 60_000,
            "pending": None, "compacting": None, "muted": False,
            "convo_v": "1:1" if codex else 1, "files_n": 1 if codex else 0,
            "agents": [], "agents_running": 0, "agents_total": 0,
            "headless": False, "read_only": False, "read_only_reason": None,
            "codex_source": "appServer" if codex else None,
            "ui_group": "available", "reason_label": "Available",
            "primary_action": "continue", "primary_action_label": "Continue",
            "access": "interactive", "access_label": "Interactive",
            "external": False, "provider_stale": False,
            "reply_requested": False, "new_response": False,
            "handoff_links": [],
            "activity_at": time.time() - 3, "pinned": False,
            "normalized_state": "idle", "winning_rule": "placement.default.available",
            "suppressed_rules": [], "state_confidence": "confirmed",
            "state_evidence": [
                {"kind": "provider_signal", "label": "Provider signal",
                 "value": f"{provider} state idle; CLI status idle",
                 "confidence": "confirmed"},
                {"kind": "transcript_event", "label": "Latest transcript event",
                 "value": "assistant message at revision 1", "confidence": "confirmed"},
                {"kind": "age", "label": "Last activity", "value": "3s quiet",
                 "confidence": "confirmed"}],
            "capabilities": capabilities(exact_cost=not codex,
                focus_terminal=True, focus_terminal_mode="attach" if codex else None,
                measured_throughput=not codex)}


def fresh_state():
    claude = base_session("claude", "claude-one", "Claude parser fix")
    codex = base_session("codex", "codex:thread-one", "Codex parity work")
    codex["agents"] = [{"agent_id": "child-one", "session_id": codex["session_id"],
        "agent_type": "reviewer", "description": "Review protocol mapping", "depth": 0,
        "model": "gpt-5.4", "family": "codex", "effort": "high",
        "state": "done", "total_tokens": None, "cost": None,
        "tokens": {}, "spark": [], "tok_per_s": None, "started": None,
        "last": None, "last_msg": None, "convo_v": 1}]
    codex.update(agents_running=0, agents_total=1)
    context = [{"role": "user", "text": "Audit parity"},
               {"role": "assistant", "text": "Working through the matrix."},
               {"role": "event", "kind": "reasoning", "title": "Reasoning",
                "detail": "Compared protocol states", "level": "info"}]
    repo = {"ok": True, "state": "ok", "root": "/Users/test/fleet-dash",
        "worktree": "/Users/test/fleet-dash", "worktrees": ["/Users/test/fleet-dash",
            "/Users/test/fleet-dash-worktrees/ui"], "title": "fleet-dash",
        "branch": "codex-integration", "detached": False, "head_oid": "abc123",
        "upstream": "origin/codex-integration", "ahead": 2, "behind": 0,
        "dirty": True, "conflicts": 0, "remotes": ["origin"], "remote": "origin",
        "remote_branch": "codex-integration", "default_base": "main",
        "files": [{"path": "engine.py", "status": ".M", "staged": False,
            "unstaged": True, "untracked": False, "conflict": False},
            {"path": "repo_center.py", "status": "??", "staged": False,
             "unstaged": True, "untracked": True, "conflict": False}],
        "latest_commit": {"oid": "abc123", "short_oid": "abc123",
            "subject": "Add workstreams", "at": int(time.time()) - 60},
        "pr": {"state": "none", "summary": "No pull request"},
        "tests": {"state": "passed", "command": "python3 -m unittest discover",
            "at": time.time() - 30, "source": "transcript", "provider": "claude",
            "session_id": "claude-one", "result": "115 passed"},
        "observed_at": time.time(), "elapsed_ms": 1.4, "revision": "repo-rev-1",
        "cached": False, "actions": {}, "recent_actions": []}
    repo["actions"] = {"commit": {"enabled": True, "reason": None,
            "files": copy.deepcopy(repo["files"]), "default_message": "Update 2 files"},
        "push": {"enabled": True, "reason": None, "remote": "origin",
            "branch": "codex-integration", "ahead": 2, "set_upstream": False},
        "pr_create_draft": {"enabled": True, "reason": None,
            "title": "Add repository outcome center", "body": "", "base": "main"},
        "pr_mark_ready": {"enabled": False, "reason": "No pull request",
            "number": None, "url": None}}
    return {"sessions": [claude, codex], "closed": [], "actions": [],
            "contexts": {"claude-one": copy.deepcopy(context),
                         "codex:thread-one": copy.deepcopy(context)},
            "scenario": "base", "codex_error": None,
            "evidence": {
                "claude-one": [
                    {"id": 12, "session_id": "claude-one", "provider": "claude",
                     "at": time.time() - 3, "raw_state": "idle", "reg_status": "idle",
                     "normalized_state": "idle", "ui_group": "available",
                     "reason": "Available", "access": "interactive",
                     "primary_action": "continue", "evidence_kind": "provider_signal",
                     "evidence_summary": "Provider signal: claude state idle; Last activity: 3s quiet",
                     "revision": "1", "winning_rule": "placement.default.available",
                     "suppressed_rules": [], "confidence": "confirmed", "evidence": []},
                    {"id": 8, "session_id": "claude-one", "provider": "claude",
                     "at": time.time() - 60, "raw_state": "running", "reg_status": "busy",
                     "normalized_state": "running", "ui_group": "working",
                     "reason": "Working", "access": "interactive",
                     "primary_action": "open", "evidence_kind": "provider_signal",
                     "evidence_summary": "Provider signal: claude state running",
                     "revision": "0", "winning_rule": "placement.state.running",
                     "suppressed_rules": ["placement.default.available"],
                     "confidence": "confirmed", "evidence": []}],
                "codex:thread-one": [
                    {"id": 13, "session_id": "codex:thread-one", "provider": "codex",
                     "at": time.time() - 3, "raw_state": "idle", "reg_status": "idle",
                     "normalized_state": "idle", "ui_group": "available",
                     "reason": "Available", "access": "interactive",
                     "primary_action": "continue", "evidence_kind": "provider_signal",
                     "evidence_summary": "Provider signal: codex state idle; Last activity: 3s quiet",
                     "revision": "1:1", "winning_rule": "placement.default.available",
                     "suppressed_rules": [], "confidence": "confirmed", "evidence": []}]},
            "notify": {"needs_you": True, "stall": True, "spend": True,
                       "fleet_quiet": True, "scheduled_digest": False},
            "settings": {"awaiting_input_notify_seconds": 180, "stall_seconds": 240,
                "spend_threshold_usd": 5, "fleet_quiet_minutes": 0, "dashboard_url": "",
                "digest_schedule_time": "09:00", "digest_schedule_zone": "UTC",
                "preview_sessions": True, "preview_session_lines": 2,
                "preview_agents": False, "preview_agent_lines": 1,
                "reader_width": "fit", "pinned_sessions": []},
            "reply_available": {}, "read_sessions": {}, "dismissed_actions": {},
            "repo": repo, "repo_actions": [], "outbox": [], "budgets": [],
            "briefing_reviewed": {}}


STATE = fresh_state()


def fixture_actions(sessions):
    records = []
    for session in sessions:
        pending = session.get("pending") or {}
        kind = request = delivery = None
        safe = ["mute"]
        revision = str(session.get("convo_v") or "")
        if pending.get("kind") == "question":
            kind, delivery = "question", "Awaiting response"
            request = " · ".join(item.get("question") or item.get("header") or "Question"
                                 for item in pending.get("questions") or [])
            revision = str(pending.get("nonce") or revision)
        elif pending.get("kind") == "permission":
            kind, request, delivery = "approval", "Review command approval", "Awaiting decision"
            revision = str(pending.get("nonce") or revision)
        elif pending.get("kind") == "elicitation":
            kind, request, delivery = "form", pending.get("message") or "Form waiting", "Awaiting response"
            revision = str(pending.get("nonce") or revision)
        elif session.get("reply_requested"):
            kind, request, delivery = "reply", "Reply requested", "Awaiting response"
            safe.append("mark_available")
        elif session.get("ui_group") == "needs_you":
            kind = "problem" if session.get("state") in ("error", "stalled_or_prompt") else "attention"
            request, delivery = session.get("error") or session.get("reason_label"), "Intervention needed"
        elif session.get("new_response"):
            kind, request, delivery = "outcome", "Completed work is ready to review", "Unreviewed"
            safe.extend(("mark_read", "dismiss"))
        if not kind:
            continue
        raw = "\0".join((session.get("provider") or "claude", session["session_id"], kind, revision))
        action_id = "act-" + hashlib.sha256(raw.encode()).hexdigest()[:24]
        if action_id in STATE.get("dismissed_actions", {}):
            continue
        records.append({"action_id": action_id, "session_id": session["session_id"],
            "provider": session.get("provider") or "claude", "kind": kind,
            "request": request, "context": (pending.get("input_summary") or
                (session.get("last_msg") or {}).get("text") or session.get("project")),
            "created_at": session.get("activity_at") or time.time(),
            "reason": session.get("reason_label"), "access": session.get("access"),
            "access_label": session.get("access_label"),
            "primary_action": session.get("primary_action"),
            "primary_action_label": session.get("primary_action_label"),
            "delivery_state": delivery, "safe_bulk": safe,
            "revision": str(session.get("convo_v") or ""),
            "pending_nonce": pending.get("nonce"), "title": session.get("title"),
            "project": session.get("project"), "muted": session.get("muted", False)})
    return records


def fixture_workstreams():
    snapshot = fleet()
    sessions = snapshot["sessions"] + snapshot["closed"]
    counts = {"needs_you": 0, "working": 0, "available": 0, "history": 0}
    for item in sessions:
        counts[item.get("ui_group") or "history"] += 1
    cost_values = [item.get("cost") for item in sessions if isinstance(item.get("cost"), (int, float))]
    return {"ok": True, "t": time.time(), "elapsed_ms": .2, "workstreams": [{
        "workstream_id": "ws-fleet", "kind": "git", "root": "/Users/test/fleet-dash",
        "worktree": "/Users/test/fleet-dash", "missing": False, "title": "fleet-dash",
        "counts": counts, "branches": sorted({item.get("branch") for item in sessions if item.get("branch")}),
        "worktrees": sorted({item.get("cwd") for item in sessions if item.get("cwd")}),
        "providers": sorted({item.get("provider") or "claude" for item in sessions}),
        "latest_at": time.time(), "latest_outcome": "Ready for the next task.",
        "cost": round(sum(cost_values), 4) if cost_values else None,
        "cost_scope": "partial" if len(cost_values) != len(sessions) else "exact",
        "context_tokens": sum(item.get("ctx_tokens") or 0 for item in sessions) or None,
        "repo_summary": {"changed_files": ("clean" if not STATE["repo"]["dirty"] else
                                             f"{len(STATE['repo']['files'])} files"),
                         "tests": STATE["repo"]["tests"]["state"],
                         "pull_request": (f"#{STATE['repo']['pr']['number']} draft"
                            if STATE["repo"]["pr"].get("state") == "ok" and
                               STATE["repo"]["pr"].get("is_draft") else
                            "none" if STATE["repo"]["pr"].get("state") == "none" else
                            STATE["repo"]["pr"].get("state"))},
        "repository": {key: copy.deepcopy(STATE["repo"].get(key)) for key in
            ("ok", "state", "worktree", "observed_at", "elapsed_ms", "cached", "error")
            if key in STATE["repo"]}, "budget_state": next((item.get("status") for item in
                fixture_budgets()["budgets"] if item.get("scope_type") == "workstream" and
                item.get("scope_id") == "ws-fleet"), "not_configured"),
        "sessions": sessions}]}


def fixture_budget_evaluations(sessions):
    evaluated = []
    for raw in STATE.get("budgets", []):
        matched = sessions if raw["scope_type"] == "fleet" else [item for item in sessions if
            (raw["scope_type"] == "provider" and item.get("provider") == raw.get("scope_id")) or
            (raw["scope_type"] == "session" and item.get("session_id") == raw.get("scope_id")) or
            (raw["scope_type"] == "workstream" and raw.get("scope_id") == "ws-fleet")]
        metric = raw["metric"]
        if metric == "usd":
            known = [item.get("cost") for item in matched if item.get("capabilities", {}).get("exact_cost") and isinstance(item.get("cost"), (int, float))]
        elif metric == "tokens":
            known = [item.get("total_tokens") for item in matched if isinstance(item.get("total_tokens"), (int, float))]
        elif metric == "runtime":
            known = [60 for item in matched]
        else:
            known = [(1 if item.get("ui_group") == "working" else 0) + int(item.get("agents_running") or 0) for item in matched]
        unknown = len(matched) - len(known) if metric != "concurrency" else 0
        value = sum(known) if known else None
        scope = "unavailable" if value is None else "partial" if unknown else "token_only" if metric == "tokens" and any(not item.get("capabilities", {}).get("exact_cost") for item in matched) else "exact"
        ratio = value / raw["limit_value"] if value is not None else None
        status = "unavailable" if value is None else "exceeded" if ratio >= 1 else "warning" if ratio >= .8 else "ok"
        evaluated.append({**copy.deepcopy(raw), "value": value, "headroom": max(0,raw["limit_value"]-value) if value is not None else None,
            "ratio": ratio, "status": status, "measurement_scope": scope,
            "matched_sessions": len(matched), "unknown_sessions": unknown,
            "label": raw.get("label") or f"{raw['scope_type'].title()} {metric} budget",
            "summary": "measurement unavailable" if value is None else f"{value:g} of {raw['limit_value']:g} · {scope.replace('_',' ')}",
            "alert_key": f"budget:{raw['id']}:{status}:1",
            "alert_created_at": time.time() - 120})
    return evaluated


def fixture_budget_actions(evaluated):
    records = []
    for item in evaluated:
        if item["status"] not in ("warning", "exceeded"):
            continue
        action_id = "act-budget-" + hashlib.sha256(item["alert_key"].encode()).hexdigest()[:20]
        blocking = bool(item.get("block_spawns") and item["status"] == "exceeded")
        scope = item.get("scope_type") or "fleet"
        target = item.get("scope_id") or "all sessions"
        records.append({"action_id": action_id, "session_id": None,
            "provider": target if scope == "provider" else "fleet", "kind": "budget",
            "request": (f"{item['label']} exceeded" if item["status"] == "exceeded" else
                        f"{item['label']} is nearing its limit"),
            "context": item["summary"], "created_at": item["alert_created_at"],
            "reason": "Budget exceeded" if item["status"] == "exceeded" else "Budget warning",
            "access": "measurement", "access_label": f"{scope.title()} scope",
            "primary_action": "view_budget", "primary_action_label": "Review budget",
            "delivery_state": "Future spawns blocked" if blocking else "Alert only",
            "safe_bulk": [], "revision": item["alert_key"], "title": item["label"],
            "project": target if scope in ("workstream", "session") else None,
            "muted": False, "status": item["status"],
            "measurement_scope": item["measurement_scope"], "budget_id": item["id"]})
    return records


def fixture_budgets(spawn=None):
    snapshot = fleet()
    sessions = snapshot["sessions"] + snapshot["closed"]
    evaluated = fixture_budget_evaluations(sessions)
    forecasts = {item["id"]: {"status": "forecast", "sample_size": 4,
        "confidence": "medium", "burn_per_hour": 1000, "seconds_to_limit": 7200}
        for item in evaluated if item.get("value") is not None}
    forecast = None
    if spawn is not None:
        provider = spawn.get("provider") or ""
        forecast = {"status": "forecast", "sample_size": 4, "confidence": "medium",
            "provider": provider, "model": spawn.get("model") or "",
            "project": spawn.get("project") or "fleet-dash",
            "median_usd": 1.25 if provider == "claude" else None,
            "median_tokens": 22000, "median_runtime_seconds": 900,
            "currency_scope": "exact" if provider == "claude" else "unavailable"}
    spawn_budgets = [item for item in evaluated if spawn and (
        item["scope_type"] == "fleet" or item["scope_type"] == "provider" and
        item.get("scope_id") == spawn.get("provider"))]
    return {"ok": True, "budgets": evaluated, "forecasts": forecasts,
        "spawn_forecast": forecast, "spawn_budgets": spawn_budgets,
        "measurement_labels": {
            "exact": "Exact provider measurement", "partial": "Partial measurement",
            "token_only": "Token-only", "unavailable": "Unavailable"}}


def fixture_briefing(device):
    reviewed = int(STATE.get("briefing_reviewed", {}).get(device, 0))
    sessions = [organize_session(item) for item in copy.deepcopy(STATE["sessions"])]
    actions = fixture_actions(sessions)
    attention = [{"id": "current:"+item["action_id"], "category": "attention",
        "severity": "warning", "title": item["request"], "summary": item["delivery_state"],
        "provider": item["provider"], "session_id": item["session_id"],
        "link_kind": "session", "link_id": item["session_id"], "muted": item["muted"],
        "current": True} for item in actions]
    completed = [] if reviewed >= 2 else [{"id": 2, "category": "completed",
        "severity": "success", "title": "Work completed", "summary": "Parser tests passed",
        "provider": "claude", "session_id": "claude-one", "link_kind": "session",
        "link_id": "claude-one", "muted": False}]
    outcomes = [] if reviewed >= 3 else [{"id": 3, "category": "outcome",
        "severity": "success", "title": "Artifacts delivered", "summary": "artifact.md available",
        "provider": "codex", "session_id": "codex:thread-one", "link_kind": "session",
        "link_id": "codex:thread-one", "muted": False}]
    reviewed_items = [] if reviewed < 3 else [
        {"id": 3, "category": "outcome", "severity": "success",
         "title": "Artifacts delivered", "summary": "artifact.md available",
         "provider": "codex", "session_id": "codex:thread-one",
         "link_kind": "session", "link_id": "codex:thread-one", "muted": False},
        {"id": 2, "category": "completed", "severity": "success",
         "title": "Work completed", "summary": "Parser tests passed",
         "provider": "claude", "session_id": "claude-one",
         "link_kind": "session", "link_id": "claude-one", "muted": False}]
    budgets = fixture_budgets()["budgets"]
    return {"ok": True, "device_id": device, "review_cursor": reviewed,
        "next_cursor": 3, "unread": max(0,3-reviewed), "muted_omitted": 0,
        "sections": {"attention": attention, "completed": completed, "slow": [],
            "outcomes": outcomes, "budgets": budgets, "measurements": [],
            "reviewed": reviewed_items}}


def organize_session(session):
    state = session.get("state") or "idle"
    pending = session.get("pending") or {}
    external = bool(session.get("headless") or session.get("read_only"))
    kind = pending.get("kind")
    if kind == "question":
        group, reason, action = "needs_you", "Question waiting", "respond"
    elif kind == "elicitation":
        group, reason, action = "needs_you", "Form waiting", "respond"
    elif kind == "permission":
        label = {"command": "Command approval", "file_change": "File approval",
                 "permissions": "Permission needed"}.get(
                     pending.get("approval_kind") or pending.get("tool"), "Permission needed")
        group, reason, action = "needs_you", label, "review"
    elif state == "error":
        group, reason, action = "needs_you", "Fix needed", "open"
    elif state == "stale":
        group, reason, action = "available", "Available", "view"
    elif state == "stalled_or_prompt":
        group, reason, action = "needs_you", "Check session", "open"
    elif state == "needs_you":
        group, reason, action = "needs_you", "Response needed", "respond"
    elif session.get("compacting") is not None:
        group, reason, action = "working", "Compacting", "open"
    elif state == "stalled":
        group, reason, action = "working", "Slow", "open"
    elif state == "running":
        group, reason, action = ("working", "Working elsewhere", "view") if external \
            else ("working", "Working", "open")
    elif session.get("reply_requested"):
        group, reason, action = "needs_you", "Reply requested", "respond"
    elif external:
        group, reason, action = "history", "External", "view"
    elif state == "dormant":
        group, reason, action = "history", "Inactive", "continue"
    else:
        group, reason, action = "available", "Available", "continue"
    if external or state == "stale":
        access = "view_only"
        if action != "view":
            action = "view"
    else:
        access = "interactive"
    session.update(ui_group=group, reason_label=reason, primary_action=action,
                   primary_action_label={"respond": "Respond", "review": "Review",
                       "open": "Open", "continue": "Continue", "view": "View"}[action],
                   access=access, access_label="View only" if access == "view_only" else "Interactive",
                   external=external, provider_stale=state == "stale",
                   activity_at=time.time() - float(session.get("quiet_s") or 0),
                   pinned=session["session_id"] in STATE["settings"]["pinned_sessions"])
    return session


def fleet():
    sessions = [organize_session(item) for item in copy.deepcopy(STATE["sessions"])]
    closed = [{**item, "ui_group": "history", "reason_label": "Closed",
        "primary_action": "reopen" if item.get("can_reopen") else "view",
        "primary_action_label": "Reopen" if item.get("can_reopen") else "View",
        "access": "reopen" if item.get("can_reopen") else "view_only",
        "access_label": "Reopen" if item.get("can_reopen") else "View only",
        "external": False, "provider_stale": False,
        "reply_requested": False, "new_response": False,
        "activity_at": item.get("last_seen") or item.get("closed_at") or time.time(),
        "pinned": item["session_id"] in STATE["settings"]["pinned_sessions"]}
        for item in [{"session_id": "codex:closed", "provider": "codex",
                "title": "Closed Codex", "project": "fleet-dash", "cwd": "/Users/test/fleet-dash",
                "branch": "old", "model": "gpt-5.4", "cost": None, "agent_cost": None,
                "total_tokens": 12000,
                "agents_total": 0, "closed_at": int(time.time()) - 60,
                "first_seen": int(time.time()) - 3600, "bridge_url": None},
                *copy.deepcopy(STATE["closed"])]]
    outbox_states = {}
    for item in STATE["outbox"]:
        outbox_states[item["state"]] = outbox_states.get(item["state"], 0) + 1
    outbox_pending = sum(outbox_states.get(state, 0) for state in
        ("scheduled", "waiting_availability", "waiting_usage_reset", "spawning", "sending"))
    outbox_attention = sum(outbox_states.get(state, 0) for state in
        ("blocked", "failed", "confirmation_unknown"))
    budget_evaluations = fixture_budget_evaluations(sessions + closed)
    actions = fixture_actions(sessions) + fixture_budget_actions(budget_evaluations)
    return {"t": time.time(), "sessions": sessions, "actions": actions,
            "outbox_summary": {"pending": outbox_pending, "attention": outbox_attention,
                               "states": outbox_states},
            "budget_summary": {"configured": len(budget_evaluations),
                "warning": sum(item["status"] == "warning" for item in budget_evaluations),
                "exceeded": sum(item["status"] == "exceeded" for item in budget_evaluations),
                "unavailable": sum(item["status"] == "unavailable" for item in budget_evaluations)},
            "totals": {"sessions": len(sessions),
                "busy": sum(item["ui_group"] == "working" for item in sessions),
                "needs_me": sum(item["ui_group"] == "needs_you" for item in sessions),
                "available": sum(item["ui_group"] == "available" for item in sessions),
                "history": sum(item["ui_group"] == "history" for item in sessions) + len(closed),
                "pinned": sum(item.get("pinned") for item in sessions + closed),
                "dormant": sum(item["state"] == "dormant" for item in sessions),
                "done": sum(item.get("new_response") for item in sessions),
                "agents_running": sum(item["agents_running"] for item in sessions),
                "session_cost": .12, "agent_cost": .03, "cost_partial": True},
            "usage": {"five_hour_pct": 20, "weekly_pct": 30,
                      "email": "claude@example.com", "lifetime_tokens": 59_591_487_086,
                      "lifetime_scope": "local_transcripts"},
            "provider_usage": {"claude": {"five_hour_pct": 20, "weekly_pct": 30,
                    "email": "claude@example.com", "lifetime_tokens": 59_591_487_086,
                    "lifetime_scope": "local_transcripts", "source": "claude_usage",
                    "show_week": True, "show_active": True, "refresh_seconds": 30,
                    "profiles": [{"id": "one", "email": "claude@example.com",
                        "active": True, "five_hour_pct": 20, "weekly_pct": 30,
                        "five_hour_reset": "2099-01-01T00:00:00Z",
                        "weekly_reset": "2099-01-07T00:00:00Z"},
                        {"id": "two", "email": "second@example.com", "active": False,
                         "five_hour_pct": 4, "weekly_pct": 8,
                         "five_hour_reset": "2099-01-01T02:00:00Z",
                         "weekly_reset": "2099-01-07T02:00:00Z"}]},
                "codex": {"provider": "codex", "email": "codex@example.com",
                    "account_id": "codex@example.com", "plan_type": "pro",
                    "lifetime_tokens": 12345, "buckets": [{"id": "codex:primary",
                    "label": "5-hour", "used_pct": 25, "reset": "2099-01-01T00:00:00Z"},
                    {"id": "codex:secondary", "label": "weekly", "used_pct": 30,
                     "reset": "2099-01-07T00:00:00Z"},
                    {"id": "spark:secondary", "label": "GPT-5.3-Codex-Spark weekly",
                     "used_pct": 0, "reset": "2099-01-07T00:00:00Z"}]}},
            "closed": closed,
            "recent_dirs": [{"path": "/Users/test/fleet-dash", "trusted": True}],
            "models": ["sonnet", "opus"], "efforts": ["low", "medium", "high"],
            "models_by_provider": {"claude": [{"id": "sonnet", "name": "sonnet",
                "efforts": ["low", "high"]}], "codex": [{"id": "gpt-5.4",
                "name": "GPT-5.4", "efforts": ["medium", "high"]}]},
            "providers": {"claude": {"ok": True},
                          "codex": {"ok": not bool(STATE.get("codex_error")),
                                    "error": STATE.get("codex_error")}},
            "notify": copy.deepcopy(STATE["notify"]),
            "settings": copy.deepcopy(STATE["settings"]),
            "page_v": 1}


def codex_session():
    return next(item for item in STATE["sessions"] if item["provider"] == "codex")


def claude_session():
    return next(item for item in STATE["sessions"] if item["provider"] == "claude")


def set_scenario(name):
    global STATE
    STATE = fresh_state()
    STATE["scenario"] = name
    session = claude_session() if name.startswith("claude-question") else codex_session()
    if name in ("single-question", "answer-failure", "claude-question-slow",
                "claude-question-failure"):
        session.update(state="needs_you", pending={"kind": "question", "nonce": "q1",
            "dismiss_action": "cancel_turn", "questions": [{"header": "Scope",
            "question": "How broad should the change be?", "multiSelect": False,
            "allowOther": True, "options": [{"label": "Focused", "description": "One area"},
                                             {"label": "Full", "description": "All areas"}]}]})
        if name in ("answer-failure", "claude-question-failure"):
            STATE["fail_answers"] = True
        if name == "claude-question-slow":
            STATE["delay_answers"] = 0.75
    elif name == "multi-question":
        session.update(state="needs_you", pending={"kind": "question", "nonce": "q2",
            "dismiss_action": "cancel_turn", "questions": [
                {"header": "Targets", "question": "Pick targets", "multiSelect": True,
                 "allowOther": True, "options": [{"label": "Desktop"}, {"label": "Mobile"}]},
                {"header": "Depth", "question": "Choose depth", "multiSelect": False,
                 "allowOther": False, "options": [{"label": "Full"}, {"label": "Small"}]}]})
    elif name in ("approval", "approval-slow"):
        session.update(state="needs_you", pending={"kind": "permission", "nonce": "p1",
            "tool": "command", "approval_kind": "command", "input_summary": "npm test",
            "decisions": ["allow", "always", "deny", "cancel"]})
        if name == "approval-slow":
            STATE["delay_quick_response"] = 0.75
    elif name == "elicitation":
        session.update(state="needs_you", pending={"kind": "elicitation", "nonce": "e1",
            "server": "deploy", "message": "Choose deployment targets", "fields": [
                {"name": "env", "label": "Environment", "type": "select",
                 "multiSelect": False, "required": True,
                 "options": [{"label": "Dev", "value": "dev"},
                             {"label": "Prod", "value": "prod"}]},
                {"name": "regions", "label": "Regions", "type": "select",
                 "multiSelect": True, "required": True,
                 "options": [{"label": "US", "value": "us"},
                             {"label": "EU", "value": "eu"}]}],
            "decisions": ["accept", "decline", "cancel"]})
    elif name == "reopenable":
        session.update(state="idle", pending=None, headless=True, read_only=True,
                       codex_source="vscode", read_only_reason=
                       "ChatGPT Desktop and VS Code use a different App Server; this transcript is view only")
        session["capabilities"] = capabilities(submit=False, takeover=False, close=False,
            archive=False, files=False, relay_agent=False, focus_terminal=False)
    elif name == "organization":
        session.update(state="idle", pending=None, headless=True, read_only=True,
                       codex_source="vscode", read_only_reason=
                       "ChatGPT Desktop and VS Code use a different App Server; this transcript is view only")
        session["capabilities"] = capabilities(submit=False, takeover=False, close=False,
            archive=False, files=False, relay_agent=False, focus_terminal=False)
        dormant = base_session("claude", "claude-dormant", "Dormant migration")
        dormant.update(state="dormant", quiet_s=8_000,
                       last_msg={"role": "assistant", "text": "Paused for later."})
        STATE["sessions"].append(dormant)
    elif name == "stale":
        session.update(state="stale", error="app-server exited", stale=True,
                       stale_reason="app-server exited")
        STATE["codex_error"] = "app-server exited"
        session["capabilities"] = capabilities(submit=False, interrupt=False, close=False)
    elif name == "cross-client-active":
        session.update(state="running", reg_status="running", quiet_s=1,
                       headless=True, read_only=True, codex_source="vscode",
                       read_only_reason=
                       "ChatGPT Desktop and VS Code use a different App Server; this transcript is view only",
                       last_msg={"role": "assistant", "text": "Working in ChatGPT desktop."})
        session["capabilities"] = capabilities(
            submit=False, interrupt=False, close=False, compact=False, review=False,
            focus_terminal=False)
    elif name == "markdown-peek":
        preview = (
            "### Default width\n\nUse **Fit the screen** with `compact code`.\n\n- Fast\n- Clear\n\n"+
            ("bounded preview content " * 30))
        session["last_msg"] = {"role": "assistant",
                               "text": preview[:499] + "…"}
    elif name == "send-failure":
        STATE["fail_text"] = True
    elif name == "handoff-failure":
        STATE["fail_handoff_once"] = True
    elif name == "reply-requested":
        session.update(state="turn_done", reg_status="idle", reply_requested=True,
                       convo_v="reply:1", quiet_s=12,
                       last_msg={"role": "assistant", "text":
                           "Which organization should we use?\n\nAnswer both before I continue."})
    elif name == "new-response":
        session.update(state="turn_done", reg_status="idle", new_response=True,
                       convo_v="response:1", quiet_s=12,
                       last_msg={"role": "assistant", "text": "The implementation is complete."})
    elif name == "subagent":
        session.update(state="running", reg_status="running", agents_running=1)
        session["agents"][0]["state"] = "running"
        session["capabilities"]["interrupt"] = True
    elif name == "stalled-agent":
        session.update(state="running", reg_status="running", agents_running=1)
        session["agents"][0]["state"] = "stalled"
        session["agents"][0]["quiet_s"] = 300
        session["capabilities"]["interrupt"] = True
    elif name == "provider-unavailable":
        STATE["sessions"] = [item for item in STATE["sessions"] if item["provider"] == "claude"]
        STATE["codex_error"] = "codex executable not found"
    elif name == "claude-archive":
        STATE["closed"].append({"session_id": "11111111-2222-3333-4444-555555555555",
            "provider": "claude", "title": "Historical Claude review",
            "project": "fleet-dash", "cwd": "/Users/test/fleet-dash",
            "branch": "codex-integration", "model": "claude-sonnet",
            "cost": 0.42, "agent_cost": 0.03, "agents_total": 1,
            "closed_at": int(time.time()) - 120, "first_seen": int(time.time()) - 3600,
            "last_seen": int(time.time()) - 120, "bridge_url": None,
            "can_reopen": True})
    elif name == "large-history":
        now = int(time.time())
        for index in range(205):
            STATE["closed"].append({
                "session_id": f"22222222-2222-2222-2222-{index:012d}",
                "provider": "claude", "title": f"Archived Claude session {index:03d}",
                "project": "fleet-dash", "cwd": "/Users/test/fleet-dash",
                "branch": "codex-integration", "model": "claude-sonnet",
                "cost": None, "agent_cost": None, "agents_total": None,
                "closed_at": now - index, "first_seen": now - 3600 - index,
                "last_seen": now - index, "bridge_url": None,
                "can_reopen": False})
    elif name == "large-conversation":
        STATE["contexts"]["codex:thread-one"] = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "text": f"Conversation message {index:03d}"}
            for index in range(205)]
        session["convo_v"] = "large:205"
    elif name in ("workstreams", "repo-action-failure"):
        session.update(cwd="/Users/test/fleet-dash-worktrees/ui",
                       branch="feature/action-inbox", project="fleet-dash")
        if name == "repo-action-failure":
            STATE["fail_repo_once"] = True


def authorized(handler):
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    return ((cookie.get("act_token") and cookie["act_token"].value == TOKEN) or
            handler.headers.get("X-Act-Token") == TOKEN)


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, ctype, data):
        if isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json_reply(self, data, code=200):
        self.reply(code, "application/json", json.dumps(data))

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        route = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        with LOCK:
            if route in ("/api/search", "/api/search/status", "/api/search/context",
                         "/api/handoff", "/api/repo", "/api/outbox"):
                if not authorized(self):
                    return self.json_reply({"ok": False, "error": "bad token"}, 403)
                if route == "/api/repo":
                    return self.json_reply(copy.deepcopy(STATE["repo"]))
                if route == "/api/outbox":
                    return self.json_reply({"ok": True, "items": copy.deepcopy(STATE["outbox"]),
                        "next_cursor": None, "summary": fleet()["outbox_summary"],
                        "usage_options": [
                            {"provider": "claude", "account_id": "one",
                             "label": "claude@example.com", "windows": [
                                {"id": "five_hour", "label": "5-hour", "reset": "2099-01-01T00:00:00Z"},
                                {"id": "weekly", "label": "Weekly", "reset": "2099-01-07T00:00:00Z"}]},
                            {"provider": "codex", "account_id": "codex@example.com",
                             "label": "codex@example.com", "windows": [
                                {"id": "codex:primary", "label": "5-hour", "reset": "2099-01-01T00:00:00Z"},
                                {"id": "codex:secondary", "label": "weekly", "reset": "2099-01-07T00:00:00Z"}]}]})
                if route == "/api/handoff":
                    sid = (query.get("sid") or [""])[0]
                    provider = (query.get("provider") or [""])[0]
                    source = next((item for item in STATE["sessions"] + STATE["closed"]
                                   if item.get("session_id") == sid), None)
                    if not source and sid == "codex:closed":
                        source = {"session_id": sid, "provider": "codex",
                            "title": "Closed Codex", "project": "fleet-dash",
                            "cwd": "/Users/test/fleet-dash", "branch": "old",
                            "model": "gpt-5.4"}
                    if not source or provider not in ("claude", "codex"):
                        return self.json_reply({"ok": False, "error": "session unavailable"})
                    return self.json_reply({"ok": True, "source": {
                        key: source.get(key) for key in ("session_id", "provider", "title",
                            "project", "cwd", "branch", "model", "effort", "collaboration_mode")},
                        "target_provider": provider, "independent_session": True,
                        "preview": f"Continue this work in a new, independent {provider.title()} coding session.\n\nSource session: {source.get('provider')} · {sid}\n\nObjective\nAudit parity\n\nRecent conversation\nAssistant: Working through the matrix.\n\nUnresolved work\n- Verify browser handoff\n\n[Selected artifact references]\n- /fixture/artifact.md — Codex updated this file\n[End artifact references]\n\nTreat this as an independent session.",
                        "artifacts": [{"name": "artifact.md", "path": "/fixture/artifact.md",
                            "caption": "Codex updated this file", "missing": False}],
                        "defaults": {"cwd": source.get("cwd"), "model":
                            (source.get("model") if source.get("provider") == provider else ""),
                            "effort": (source.get("effort") if source.get("provider") == provider else ""),
                            "mode": "plan" if provider == "codex" else None,
                            "worktree": False, "worktree_name": ""}})
                if route == "/api/search/status":
                    return self.json_reply({"ok": True, "state": "idle", "sources": 4,
                        "complete_sources": 4, "documents": 37, "bytes_total": 4096,
                        "pending_sources": 0, "bytes_done": 4096, "progress_pct": 100, "errors": 0,
                        "malformed_rows": 0, "unknown_rows": 1, "oversized_docs": 0,
                        "warnings": [{"provider": "codex", "source_kind": "session",
                            "session_id": "codex:thread-one", "title": "Codex parity work",
                            "error": None, "malformed_rows": 0, "unknown_rows": 1,
                            "oversized_docs": 0}], "last_error": None,
                        "last_discovery_at": time.time(), "parser_version": 1})
                if route == "/api/search/context":
                    document_id = int((query.get("id") or ["0"])[0])
                    if document_id not in (901, 902, 903):
                        return self.json_reply({"ok": False,
                                                "error": "search result no longer exists"})
                    claude = document_id == 902
                    artifact = document_id == 903
                    return self.json_reply({"ok": True, "document_id": document_id,
                        "source": {"provider": "claude" if claude else "codex",
                            "source_kind": "subagent" if claude else
                                           ("artifact" if artifact else "session"),
                            "session_id": "claude-one" if claude else "codex:thread-one",
                            "agent_id": "agent-review" if claude else None,
                            "project": "fleet-dash", "cwd": "/Users/test/fleet-dash",
                            "branch": "codex-integration",
                            "model": "claude-sonnet-4-5" if claude else "gpt-5.4",
                            "source_title": "Claude review agent" if claude else
                                            ("artifact.md" if artifact else "Codex parity work"),
                            "source_error": None,
                            "artifact_path": "/fixture/artifact.md" if artifact else None},
                        "messages": [{"id": document_id - 1, "role": "user",
                            "kind": "message", "timestamp": "2026-07-16T12:00:00Z",
                            "text": "Find the protocol regression", "artifact_path": None,
                            "hit": False}, {"id": document_id,
                            "role": "assistant", "kind": "message",
                            "timestamp": "2026-07-16T12:00:01Z",
                            "text": "Indexed artifact preview." if artifact else
                                    "Indexed exact context for the protocol regression.",
                            "artifact_path": "/fixture/artifact.md" if artifact else None,
                            "hit": True}]})
                q = ((query.get("q") or [""])[0]).lower()
                provider = (query.get("provider") or [""])[0]
                kind = (query.get("kind") or [""])[0]
                project = (query.get("project") or [""])[0]
                rows = [{"id": 901, "source_id": 41, "provider": "codex",
                    "source_kind": "session", "session_id": "codex:thread-one",
                    "agent_id": None, "project": "fleet-dash",
                    "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
                    "title": "Codex parity work", "role": "assistant", "kind": "message",
                    "timestamp": "2026-07-16T12:00:01Z", "timestamp_epoch": time.time(),
                    "artifact_path": None,
                    "snippet": "Indexed exact context for the protocol regression.", "rank": -1},
                    {"id": 902, "source_id": 42, "provider": "claude",
                    "source_kind": "subagent", "session_id": "claude-one",
                    "agent_id": "agent-review", "project": "fleet-dash",
                    "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
                    "title": "Claude review agent", "role": "assistant",
                    "kind": "reasoning", "timestamp": "2026-07-16T11:59:00Z",
                    "timestamp_epoch": time.time() - 60, "artifact_path": None,
                    "snippet": "Reviewed parser migration and protocol states.", "rank": -0.5},
                    {"id": 903, "source_id": 43, "provider": "codex",
                    "source_kind": "artifact", "session_id": "codex:thread-one",
                    "agent_id": None, "project": "fleet-dash",
                    "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
                    "title": "artifact.md", "role": "artifact", "kind": "artifact",
                    "timestamp": "2026-07-16T11:58:00Z",
                    "timestamp_epoch": time.time() - 120,
                    "artifact_path": "/fixture/artifact.md",
                    "snippet": "Indexed artifact preview.", "rank": -0.25}]
                rows = [item for item in rows if
                        (not q or q in (item["title"] + " " + item["snippet"]).lower()) and
                        (not provider or item["provider"] == provider) and
                        (not kind or item["kind"] == kind) and
                        (not project or item["project"] == project)]
                return self.json_reply({"ok": True, "query": q, "results": rows,
                    "next_cursor": None, "projects": ["fleet-dash"], "elapsed_ms": 1.2})
            if route == "/api/fleet":
                snapshot = fleet()
                closed = snapshot["closed"]
                snapshot["closed_total"] = len(closed)
                snapshot["closed_ids"] = [item["session_id"] for item in closed]
                snapshot["closed"] = [item for item in closed if item.get("pinned")]
                return self.json_reply(snapshot)
            if route == "/api/history":
                rows = [item for item in fleet()["closed"] if not item.get("pinned")]
                sid = (query.get("sid") or [""])[0]
                if sid:
                    item = next((item for item in fleet()["closed"]
                                 if item.get("session_id") == sid), None)
                    return self.json_reply({"ok": True, "item": item})
                q = (query.get("q") or [""])[0].strip().lower()
                provider = (query.get("provider") or [""])[0]
                access = (query.get("access") or [""])[0]
                try:
                    cursor = max(0, int((query.get("cursor") or ["0"])[0]))
                    limit = max(1, min(200, int((query.get("limit") or ["100"])[0])))
                except ValueError:
                    return self.json_reply({"ok": False, "error": "invalid pagination"})
                if provider:
                    rows = [item for item in rows if item.get("provider") == provider]
                if access:
                    rows = [item for item in rows if item.get("primary_action") == access]
                if q:
                    rows = [item for item in rows if q in " ".join(str(item.get(key) or "")
                        for key in ("title", "name", "project", "branch", "provider",
                                    "reason_label", "access_label", "state")).lower()]
                total = len(rows)
                items = rows[cursor:cursor + limit]
                next_cursor = cursor + len(items) if cursor + len(items) < total else None
                return self.json_reply({"ok": True, "items": rows[cursor:cursor + limit],
                    "cursor": cursor, "next_cursor": next_cursor, "total": total})
            if route == "/api/briefing":
                return self.json_reply(fixture_briefing((query.get("device") or ["default"])[0]))
            if route == "/api/budgets":
                spawn = {key: (query.get(key) or [""])[0]
                         for key in ("provider", "model", "project", "cwd")
                         if (query.get(key) or [""])[0]}
                return self.json_reply(fixture_budgets(spawn if spawn else None))
            if route == "/api/workstreams":
                return self.json_reply(fixture_workstreams())
            if route == "/api/evidence":
                sid = (query.get("sid") or [""])[0]
                try:
                    cursor = int((query.get("cursor") or ["0"])[0] or 0)
                    limit = int((query.get("limit") or ["40"])[0] or 40)
                except ValueError:
                    return self.json_reply({"ok": False, "error": "invalid evidence cursor or limit"})
                rows = copy.deepcopy(STATE.get("evidence", {}).get(sid, []))
                if cursor:
                    rows = [row for row in rows if row["id"] < cursor]
                more = len(rows) > limit
                rows = rows[:limit]
                current = next((item for item in STATE["sessions"] + STATE["closed"]
                                if item.get("session_id") == sid), None)
                return self.json_reply({"ok": True, "session_id": sid,
                    "current": current, "events": rows,
                    "next_cursor": rows[-1]["id"] if more and rows else None})
            if route == "/api/context":
                sid = (query.get("sid") or [""])[0]
                messages = copy.deepcopy(STATE.get("contexts", {}).get(sid, []))
                try:
                    limit = max(1, min(100, int((query.get("limit") or ["50"])[0])))
                    cursor = (len(messages) if "cursor" not in query else
                              int((query.get("cursor") or [str(len(messages))])[0]))
                    if cursor < 0 or cursor > len(messages):
                        raise ValueError
                except ValueError:
                    return self.json_reply({"ok": False,
                                            "error": "invalid conversation pagination"})
                start = max(0, cursor - limit)
                return self.json_reply({"ok": True,
                    "messages": messages[start:cursor], "message_total": len(messages),
                    "next_cursor": start if start > 0 else None,
                    "files": [{"name": "artifact.md", "path": "/fixture/artifact.md",
                    "kind": "text", "missing": False, "caption": "Codex updated this file",
                    "delivered": False}]})
            if route == "/api/agent_context":
                return self.json_reply({"ok": True, "messages": [{"role": "assistant",
                    "text": "Subagent report"}], "info": {"agent_id": "child-one",
                    "agent_type": "reviewer", "description": "Review protocol mapping",
                    "model": "gpt-5.4", "effort": "high", "tokens": {},
                    "total_tokens": None, "cost": None}})
            if route == "/api/closed_context":
                return self.json_reply({"ok": True, "closed": True, "messages": [
                    {"role": "assistant", "text": "Durable closed conversation"}],
                    "info": {"project": "fleet-dash", "branch": "old"}})
            if route == "/api/commands":
                if not authorized(self):
                    return self.json_reply({"ok": False, "error": "bad token"}, 403)
                return self.json_reply({"ok": True, "commands": [
                    {"name": "/compact", "desc": "Compact conversation", "scope": "app-server",
                     "danger": True, "execution": "action", "action": "compact"},
                    {"name": "/review", "desc": "Review changes", "scope": "app-server",
                     "danger": False, "execution": "action", "action": "review"},
                    {"name": "$reviewer", "desc": "Review code", "scope": "repo",
                     "danger": False, "execution": "skill"}]})
            if route == "/api/file":
                if not authorized(self):
                    return self.reply(403, "text/plain", "missing act token")
                return self.reply(200, "text/plain; charset=utf-8", "# Artifact\n\nSafe preview.")
            if route == "/api/insights":
                return self.json_reply({"ok": True, "totals": {"agent_cost": 0,
                    "session_cost": 0, "bust_cost": 0}, "token_mix": [], "cache_busts": [],
                    "agents": [], "skills": [], "tools": [], "models": [], "projects": [],
                    "by_day": [], "top_sessions": []})
            if route == "/test/state":
                return self.json_reply(copy.deepcopy(STATE))
        if route in ("/", "/index.html"):
            with open(os.path.join(ROOT, "dashboard.html"), "rb") as handle:
                return self.reply(200, "text/html; charset=utf-8", handle.read())
        if route in ("/static/fleet.css", "/static/app.js"):
            ctype = ("text/css; charset=utf-8" if route.endswith(".css")
                     else "text/javascript; charset=utf-8")
            with open(os.path.join(ROOT, route.removeprefix("/")), "rb") as handle:
                return self.reply(200, ctype, handle.read())
        return self.reply(404, "text/plain", "not found")

    def do_POST(self):
        global STATE
        route = urlparse(self.path).path
        payload = self.body()
        with LOCK:
            if route == "/test/reset":
                set_scenario(payload.get("scenario") or "base")
                return self.json_reply({"ok": True})
            if route == "/test/confirm":
                sid = payload.get("session_id")
                context = STATE.setdefault("contexts", {}).setdefault(sid, [])
                if payload.get("kind") == "answer":
                    context.append({"role": "event", "kind": "qa", "title": "You answered",
                        "level": "info", "detail": "", "qa": payload.get("answers") or []})
                else:
                    context.append({"role": "user", "text": payload.get("text") or ""})
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == sid), None)
                if session:
                    session["convo_v"] = "confirmed:" + str(time.time_ns())
                return self.json_reply({"ok": True})
            if route in ("/api/act", "/api/settings", "/api/search/rebuild") \
                    and not authorized(self):
                return self.json_reply({"ok": False, "error": "bad or missing act token"}, 403)
            if route == "/api/search/rebuild":
                STATE["actions"].append({"type": "search_rebuild"})
                return self.json_reply({"ok": True, "rebuilding": True})
            if route == "/api/settings":
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("mute_session")), None)
                if session:
                    session["muted"] = bool(payload.get("muted"))
                if isinstance(payload.get("notify"), dict):
                    STATE["notify"].update({key: bool(value) for key,value in payload["notify"].items()
                                            if key in STATE["notify"]})
                    payload["notify"] = copy.deepcopy(STATE["notify"])
                for key in ("reader_width", "preview_sessions", "preview_session_lines",
                            "preview_agents", "preview_agent_lines", "digest_schedule_time",
                            "digest_schedule_zone", "awaiting_input_notify_seconds",
                            "stall_seconds", "spend_threshold_usd", "fleet_quiet_minutes",
                            "dashboard_url"):
                    if key in payload:
                        STATE["settings"][key] = payload[key]
                if "budgets" in payload:
                    normalized=[]
                    for index,item in enumerate(payload.get("budgets") or []):
                        normalized.append({"id": item.get("id") or f"fixture-budget-{index+1}",
                            "scope_type": item.get("scope_type"), "scope_id": item.get("scope_id") or None,
                            "metric": item.get("metric"), "limit_value": float(item.get("limit_value")),
                            "block_spawns": bool(item.get("block_spawns")), "enabled": item.get("enabled") is not False,
                            "label": item.get("label") or None})
                    STATE["budgets"] = normalized
                    payload["budgets"] = copy.deepcopy(normalized)
                if payload.get("pin_session"):
                    sid=payload["pin_session"]
                    pins=[item for item in STATE["settings"]["pinned_sessions"] if item != sid]
                    if payload.get("pinned"):
                        pins.append(sid)
                    STATE["settings"]["pinned_sessions"] = pins
                    payload["pinned_sessions"] = pins
                if payload.get("mark_available_session"):
                    sid=payload["mark_available_session"]
                    target=next((item for item in STATE["sessions"] if item["session_id"] == sid),None)
                    if target:
                        target["reply_requested"] = False
                    STATE["reply_available"][sid] = payload.get("revision")
                if payload.get("mark_read_session"):
                    sid=payload["mark_read_session"]
                    target=next((item for item in STATE["sessions"] if item["session_id"] == sid),None)
                    if target:
                        target["new_response"] = False
                    STATE["read_sessions"][sid] = payload.get("revision")
                bulk = payload.get("bulk_triage") or {}
                if bulk:
                    operation = bulk.get("operation")
                    current = {item["action_id"]: item for item in fleet()["actions"]}
                    if operation != "mute":
                        for item in bulk.get("items") or []:
                            action = current.get(item.get("action_id"))
                            if (not action or action.get("session_id") != item.get("session_id") or
                                    operation not in action.get("safe_bulk", [])):
                                return self.json_reply({"ok": False,
                                    "error": "stale or ineligible bulk triage action"})
                    for item in bulk.get("items") or []:
                        sid = item.get("session_id")
                        target = next((session for session in STATE["sessions"]
                                       if session["session_id"] == sid), None)
                        if operation == "mark_read" and target:
                            target["new_response"] = False
                            STATE["read_sessions"][sid] = item.get("revision")
                        elif operation == "mark_available" and target:
                            target["reply_requested"] = False
                            STATE["reply_available"][sid] = item.get("revision")
                        elif operation == "mute" and target:
                            target["muted"] = True
                        elif operation == "dismiss":
                            STATE["dismissed_actions"][item.get("action_id")] = time.time()
                STATE["actions"].append(payload)
                return self.json_reply({"ok": True, **payload})
            if route == "/api/act":
                if payload.get("type") == "ping":
                    return self.json_reply({"ok": True})
                if payload.get("type") == "briefing_review":
                    STATE["briefing_reviewed"][payload.get("device_id")] = int(payload.get("cursor") or 0)
                    return self.json_reply({"ok": True, "cursor": int(payload.get("cursor") or 0)})
                STATE["actions"].append(payload)
                if str(payload.get("type") or "").startswith("outbox_"):
                    typ = payload["type"]
                    item = next((row for row in STATE["outbox"]
                                 if row["id"] == payload.get("outbox_id")), None)
                    if typ == "outbox_create":
                        kind = payload.get("kind")
                        state = {"when_available": "waiting_availability",
                                 "usage_reset": "waiting_usage_reset"}.get(kind, "scheduled")
                        now = time.time()
                        item = {"id": f"fixture-out-{len(STATE['outbox']) + 1}",
                            "created_at": now, "updated_at": now,
                            "created_zone": payload.get("created_zone") or "UTC",
                            "local_time": payload.get("local_time"),
                            "trigger_fold": payload.get("trigger_fold"),
                            "kind": kind, "state": state,
                            "state_label": {"scheduled": "Scheduled",
                                "waiting_availability": "Waiting for availability",
                                "waiting_usage_reset": "Waiting for usage reset"}[state],
                            "message": payload.get("message"),
                            "target_provider": payload.get("target_provider"),
                            "target_session_id": payload.get("target_session_id"),
                            "target_agent_id": payload.get("target_agent_id"),
                            "trigger_at": now + 3600 if kind in ("at_time", "new_session") else None,
                            "usage_account_id": payload.get("usage_account_id"),
                            "usage_window_id": payload.get("usage_window_id"),
                            "observed_reset_at": None, "spawn_spec": payload.get("spawn_spec"),
                            "destination_session_id": None, "provider_receipt": None,
                            "sent_at": None, "error": None, "blocked_reason": None,
                            "retry_of": None, "version": 1, "editable": True,
                            "cancellable": True, "retryable": False}
                        STATE["outbox"].append(item)
                    elif not item:
                        return self.json_reply({"ok": False, "error": "outbox message not found"})
                    elif typ == "outbox_update":
                        patch = payload.get("patch") or {}
                        item.update({key: value for key, value in patch.items()
                                     if key in ("message", "kind", "target_provider",
                                        "target_session_id", "target_agent_id", "created_zone",
                                        "local_time", "trigger_fold", "usage_account_id",
                                        "usage_window_id", "spawn_spec")})
                        item["updated_at"] = time.time()
                    elif typ == "outbox_cancel":
                        item.update(state="cancelled", state_label="Cancelled", editable=False,
                                    cancellable=False, retryable=False, updated_at=time.time())
                    elif typ == "outbox_send_now":
                        item.update(state="sent", state_label="Sent", editable=False,
                                    cancellable=False, retryable=False, sent_at=time.time(),
                                    provider_receipt={"accepted": True}, updated_at=time.time())
                    elif typ in ("outbox_retry", "outbox_retarget"):
                        patch = payload.get("patch") or {}
                        now = time.time()
                        item = {**item, **patch, "id": f"fixture-out-{len(STATE['outbox']) + 1}",
                            "retry_of": item["id"], "created_at": now, "updated_at": now,
                            "state": "waiting_availability", "state_label": "Waiting for availability",
                            "editable": True, "cancellable": True, "retryable": False,
                            "error": None, "blocked_reason": None}
                        STATE["outbox"].append(item)
                    return self.json_reply({"ok": True, "item": copy.deepcopy(item),
                                            "summary": fleet()["outbox_summary"]})
                if payload.get("type") in ("git_commit", "git_push", "pr_create_draft",
                                           "pr_mark_ready"):
                    repo = STATE["repo"]
                    if payload.get("revision") != repo.get("revision"):
                        return self.json_reply({"ok": False,
                            "error": "Repository changed since preview; review it again",
                            "stale": True})
                    typ = payload["type"]
                    if typ == "git_commit":
                        if STATE.pop("fail_repo_once", False):
                            return self.json_reply({"ok": False,
                                "error": "commit hook rejected the commit",
                                "snapshot": copy.deepcopy(repo)})
                        paths = payload.get("paths") or []
                        allowed = {item["path"] for item in repo["files"]}
                        if not paths or any(path not in allowed for path in paths):
                            return self.json_reply({"ok": False,
                                "error": "A selected file is stale or invalid"})
                        if not str(payload.get("message") or "").strip():
                            return self.json_reply({"ok": False,
                                "error": "Commit message must be 1–1,000 characters"})
                        repo.update(files=[], dirty=False, revision="repo-rev-2", ahead=3)
                        repo["actions"]["commit"].update(enabled=False,
                            reason="No changed files", files=[])
                        repo["actions"]["push"].update(enabled=True, ahead=3)
                        summary = "[codex-integration def456] " + payload["message"]
                    elif typ == "git_push":
                        if not repo["actions"]["push"]["enabled"]:
                            return self.json_reply({"ok": False,
                                "error": repo["actions"]["push"]["reason"]})
                        repo.update(ahead=0, revision="repo-rev-3")
                        repo["actions"]["push"].update(enabled=False, ahead=0,
                            reason="No local commits to push")
                        summary = "pushed to origin/codex-integration"
                    elif typ == "pr_create_draft":
                        if not str(payload.get("title") or "").strip():
                            return self.json_reply({"ok": False,
                                "error": "Pull-request title must be 1–200 characters"})
                        repo.update(revision="repo-rev-4")
                        repo["pr"] = {"state": "ok", "number": 7, "is_draft": True,
                            "url": "https://github.test/pull/7", "title": payload["title"],
                            "base": payload.get("base"), "head": repo["branch"],
                            "checks": {"total": 2, "passed": 1, "pending": 1, "failed": 0}}
                        repo["actions"]["pr_create_draft"].update(enabled=False,
                            reason="A pull request already exists")
                        repo["actions"]["pr_mark_ready"] = {"enabled": True,
                            "reason": None, "number": 7, "url": repo["pr"]["url"]}
                        summary = repo["pr"]["url"]
                    else:
                        if payload.get("number") != repo["pr"].get("number"):
                            return self.json_reply({"ok": False,
                                "error": "Pull request changed since preview"})
                        repo.update(revision="repo-rev-5")
                        repo["pr"]["is_draft"] = False
                        repo["actions"]["pr_mark_ready"].update(enabled=False,
                            reason="Pull request is already ready")
                        summary = "Pull request #7 is ready for review"
                    STATE["repo_actions"].append({"type": typ, "summary": summary})
                    repo["recent_actions"].insert(0, {"action_id": "repo-action-1",
                        "kind": typ, "started_at": time.time(), "finished_at": time.time(),
                        "status": "succeeded", "summary": summary, "error": None})
                    return self.json_reply({"ok": True, "action_id": "repo-action-1",
                        "kind": typ, "summary": summary,
                        "snapshot": copy.deepcopy(repo)})
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("session_id")), None)
                if payload.get("type") == "spawn":
                    exact = "codex:new" if payload.get("provider") == "codex" else "claude-new"
                    STATE["sessions"].append(base_session(payload.get("provider") or "claude",
                                                          exact, "New coding session"))
                    return self.json_reply({"ok": True, "session_id": exact,
                                            "cwd": payload.get("cwd")})
                if payload.get("type") == "handoff":
                    source = next((item for item in STATE["sessions"] + STATE["closed"]
                                   if item.get("session_id") == payload.get("session_id")), None)
                    if not source and payload.get("session_id") == "codex:closed":
                        source = {"session_id": "codex:closed", "provider": "codex",
                            "title": "Closed Codex", "project": "fleet-dash",
                            "cwd": "/Users/test/fleet-dash", "branch": "old",
                            "model": "gpt-5.4", "handoff_links": []}
                    if not source:
                        return self.json_reply({"ok": False, "error": "source unavailable"})
                    provider = payload.get("provider")
                    destination = payload.get("destination_session_id")
                    created = False
                    if destination:
                        target = next((item for item in STATE["sessions"]
                                       if item.get("session_id") == destination), None)
                        if not target:
                            return self.json_reply({"ok": False,
                                "error": "stale or mismatched handoff destination"})
                    else:
                        destination = ("codex:" if provider == "codex" else "") + "handoff-1"
                        target = base_session(provider, destination,
                            f"Continued from {source.get('title') or 'session'}")
                        target.update(cwd=payload.get("cwd") or source.get("cwd"),
                                      model=payload.get("model") or target.get("model"),
                                      effort=payload.get("effort") or target.get("effort"),
                                      collaboration_mode=payload.get("mode") if provider == "codex" else None)
                        STATE["sessions"].append(target);created=True
                        STATE.setdefault("contexts", {})[destination] = [
                            {"role": "user", "text": payload.get("preview") or ""}]
                    status = "delivery_failed" if STATE.pop("fail_handoff_once", False) else "delivered"
                    source["handoff_links"] = [{"direction": "from", "session_id": destination,
                        "provider": provider, "status": status, "created_at": time.time(),
                        "error": "fixture delivery failure" if status == "delivery_failed" else None}]
                    target["handoff_links"] = [{"direction": "to",
                        "session_id": source.get("session_id"), "provider": source.get("provider"),
                        "status": status, "created_at": time.time(),
                        "error": "fixture delivery failure" if status == "delivery_failed" else None}]
                    if status == "delivery_failed":
                        return self.json_reply({"ok": False, "error": "fixture delivery failure",
                            "destination_session_id": destination, "created": created,
                            "retryable": True})
                    return self.json_reply({"ok": True, "destination_session_id": destination,
                        "session_id": destination, "provider": provider, "created": created})
                if payload.get("type") == "reopen":
                    closed = next((item for item in STATE["closed"]
                                   if item["session_id"] == payload.get("session_id")), None)
                    if not closed or not closed.get("can_reopen"):
                        return self.json_reply({"ok": False,
                                                "error": "session is not reopenable"})
                    return self.json_reply({"ok": True, "reopened": True})
                if not session:
                    return self.json_reply({"ok": False, "error": "stale session"})
                typ = payload.get("type")
                if typ == "text":
                    if STATE.get("fail_text"):
                        return self.json_reply({"ok": False, "error": "terminal rejected input"})
                    session.update(state="running", reg_status="running")
                    session["capabilities"].update(
                        interrupt=True, focus_terminal=False,
                        focus_terminal_mode=None, focus_terminal_label="turn active",
                        focus_terminal_reason=
                        "Wait for the current Codex turn to finish before attaching")
                elif typ == "interrupt":
                    session.update(state="turn_done", reg_status="idle")
                    session["capabilities"].update(
                        interrupt=False, focus_terminal=True,
                        focus_terminal_mode="attach", focus_terminal_label="attach",
                        focus_terminal_reason=
                        "Open a Codex TUI attached to Fleet's shared App Server")
                elif typ == "mode":
                    session["collaboration_mode"] = payload.get("mode")
                    return self.json_reply({"ok": True, "mode": payload.get("mode")})
                elif typ == "close":
                    STATE["sessions"] = [item for item in STATE["sessions"]
                                         if item["session_id"] != session["session_id"]]
                    STATE["closed"].append({"session_id": session["session_id"],
                        "provider": session["provider"], "title": session["title"],
                        "project": session["project"], "cwd": session["cwd"],
                        "branch": session["branch"], "model": session["model"],
                        "cost": session["cost"], "agent_cost": session["agent_cost"],
                        "agents_total": session["agents_total"],
                        "closed_at": int(time.time()), "first_seen": int(time.time()) - 60,
                        "bridge_url": session["bridge_url"]})
                    return self.json_reply({"ok": True, "closed": True,
                                            "interrupted": session["state"] == "running"})
                elif typ in ("option", "multiq", "permission", "dismiss", "elicitation"):
                    if not session.get("pending") or payload.get("nonce") != session["pending"].get("nonce"):
                        return self.json_reply({"ok": False, "error": "stale request"})
                    if typ in ("permission", "dismiss", "elicitation") \
                            and STATE.get("delay_quick_response"):
                        time.sleep(STATE["delay_quick_response"])
                    if typ == "option" and not payload.get("digits") and not payload.get("other"):
                        return self.json_reply({"ok": False, "error": "invalid response"})
                    if typ in ("option", "multiq") and STATE.get("delay_answers"):
                        time.sleep(STATE["delay_answers"])
                    if typ in ("option", "multiq") and STATE.get("fail_answers"):
                        return self.json_reply({"ok": False,
                                                "error": "provider rejected answer"})
                    session.update(state="turn_done", pending=None)
                return self.json_reply({"ok": True})
        return self.reply(404, "text/plain", "not found")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8399), Handler).serve_forever()
