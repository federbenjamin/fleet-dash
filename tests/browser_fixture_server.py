#!/usr/bin/env python3
"""Deterministic HTTP fixture for the repository-native browser suite."""
import copy
import gzip
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


def file_id(sid, path):
    return hashlib.sha256((str(sid) + "\0" + os.path.realpath(path)).encode()).hexdigest()[:24]


def fixture_file_rows():
    return [
        ("/fixture/artifact.md", "Codex updated this file"),
        ("/fixture/preview.html", "Interactive preview"),
        ("/fixture/data.json", "Structured output"),
        ("/fixture/report.pdf", "Report"),
    ]


def fixture_files(sid):
    return [{"name": os.path.basename(path),
             "file_id": file_id(sid, path),
             "kind": "text", "missing": False, "caption": caption,
             "delivered": False} for path, caption in fixture_file_rows()]


def capabilities(**overrides):
    out = {"submit": True, "interrupt": False, "takeover": False,
           "close": True,
           "focus_terminal": False, "answer_structured": False,
           "decide_approval": False, "spawn_agent": True, "relay_agent": True,
           "relay_agent_direct": False, "account_usage": True, "exact_cost": False,
           "measured_throughput": False, "archive": True, "compact": True,
           "review": True, "files": True, "change_permission_mode": False,
           "model_effort_settings": False, "change_model_effort": False,
           "change_model_effort_reason": ""}
    out.update(overrides)
    return out


def fixture_status_line(provider, frozen=False):
    common = {"branch": "status-strip", "worktree": "/Users/test/fleet-dash",
        "worktree_label": "fleet-dash", "ahead": 3, "behind": 10,
        "git_observed_at": time.time(), "effort": "high", "frozen": frozen}
    if provider == "codex":
        return {**common, "model": "GPT-5.4", "context_tokens": 1200,
            "context_window": 10000, "context_pct": 12, "compact_remaining": None,
            "cost_scope": "unavailable", "cost_label": "tree",
            "cost_breakdown": [], "cost_breakdown_omitted": 0}
    history = [500 + (index % 8) * 900 for index in range(50)]
    return {**common, "model": "Opus 4.8", "context_tokens": 470000,
        "context_window": 1000000, "context_pct": 47, "compact_remaining": 174000,
        "cache_read_pct": 99, "cache_write": 855,
        "cache_write_history": history, "cache_write_spikes": 9,
        "cache_write_peak": 278000, "turn_cost": .254,
        "session_cost": 80.0, "agent_cost": 4.12, "tree_cost": 84.12,
        "cost_scope": "estimated", "cost_label": "tree",
        "cost_breakdown": [{"kind": "main", "label": "Main session", "cost": 80.0},
            {"kind": "agent", "label": "Review protocol mapping", "cost": 4.12}],
        "cost_breakdown_omitted": 0}


def base_session(provider, sid, title):
    codex = provider == "codex"
    session = {"session_id": sid, "native_session_id": sid.split(":")[-1],
            "provider": provider, "pid": None if codex else 4242,
            "name": title, "title": title, "project": "fleet-dash",
            "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
            "model": "gpt-5.4" if codex else "claude-sonnet-4-5",
            "family": "codex" if codex else "sonnet", "effort": "high",
            "collaboration_mode": "plan" if codex else None,
            "permission_mode": None if codex else "default",
            "permission_modes": [] if codex else ["default", "acceptEdits", "plan",
                                                     "bypassPermissions"],
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
                focus_terminal=not codex,
                measured_throughput=not codex, change_permission_mode=not codex,
                model_effort_settings=True, change_model_effort=True)}
    session["status_line"] = fixture_status_line(provider)
    return session


def fixture_notification_policy(now):
    kinds = ("question", "approval", "form", "reply", "failure", "stall",
             "completion", "artifact", "outcome", "budget", "measurement",
             "notification")
    active = {"question": 1, "failure": 1}
    rules = []
    for kind in kinds:
        attention = kind in {"question", "approval", "form", "reply", "failure"}
        rules.append({"kind": kind,
            "mode": "remind_once" if attention else "once" if kind == "stall" else "off",
            "minimum_severity": "info", "initial_delay_seconds": 0,
            "repeat_interval_seconds": 900,
            "max_deliveries": 2 if attention else 1,
            "allow_during_quiet_hours": False, "effective_after": 0,
            "revision": 1, "updated_at": now,
            "active_matches": active.get(kind, 0)})
    return {"ok": True, "global": {"singleton_id": 1, "enabled": True,
        "quiet_hours_enabled": False, "quiet_start_minute": 1320,
        "quiet_end_minute": 420, "timezone": "America/New_York",
        "revision": 1, "updated_at": now, "quiet_now": False,
        "quiet_ends_at": None}, "kinds": rules, "muted_sessions": [],
        "next_deliveries": [{"id": "push-next", "event_id": "evt-1-question",
            "device_id": "phone", "purpose": "reminder",
            "next_attempt_at": now + 900, "status": "queued", "kind": "question",
            "title": "Claude needs a decision"}],
        "recent_deliveries": [{"id": "push-recent", "event_id": "evt-2-completion",
            "device_id": "phone", "purpose": "initial", "status": "sent", "attempt": 1,
            "remote_status": 201, "updated_at": now - 45, "kind": "completion",
            "title": "Quick build finished"}]}


def fixture_notification(sequence, kind, state, title, summary, provider="claude",
                         session_id=None, *, unread=False, snoozed_until=None,
                         link_kind=None, link_id=None):
    now = time.time()
    event_id = f"evt-{sequence}-{kind}"
    return {"id": event_id, "event_key": f"fixture:{kind}:{sequence}",
        "sequence": sequence, "kind": kind, "state": state,
        "severity": "watch" if kind in ("failure", "stall") else "info",
        "title": title, "summary": summary, "provider": provider,
        "session_id": session_id, "workstream_id": "ws-fleet" if session_id else None,
        "source_type": "action" if state != "resolved" else "briefing",
        "source_id": session_id or event_id, "source_revision": f"rev-{sequence}",
        "opened_at": now - sequence * 65, "changed_at": now - sequence * 30,
        "resolved_at": now - sequence * 30 if state == "resolved" else None,
        "snoozed_until": snoozed_until, "reminder_budget": 1,
        "reminders_sent": 0, "payload": ({"link_kind": link_kind,
            "link_id": link_id} if link_kind else {}), "unread": unread,
        "muted": False}


def fresh_state():
    claude = base_session("claude", "claude-one", "Claude parser fix")
    codex = base_session("codex", "codex:thread-one", "Codex parity work")
    codex["agents"] = [{"agent_id": "agent-child-one", "session_id": codex["session_id"],
        "agent_type": "reviewer", "description": "Review protocol mapping", "depth": 0,
        "model": "gpt-5.4", "family": "codex", "effort": "high",
        "state": "done", "total_tokens": None, "cost": None,
        "tokens": {}, "spark": [], "tok_per_s": None, "started": None,
        "last": None, "last_msg": None, "convo_v": 1}]
    codex.update(agents_running=0, agents_total=1)
    context = [{"role": "user", "text": "Audit parity"},
               {"role": "assistant", "text": "Working through the matrix."},
               # a quiet tool with a result to expand, and a loud one without
               {"role": "tool", "name": "Read", "arg": "~/fleet-dash/tail.py",
                "quiet": True, "tool_id": "tu-read", "result": "line one",
                "result_lines": 3, "result_chars": 28},
               {"role": "tool", "name": "Write", "arg": "~/fleet-dash/out.txt",
                "tool_id": "tu-none"},
               {"role": "event", "kind": "reasoning", "title": "Reasoning",
                "detail": "Compared protocol states", "level": "info"}]
    repo = {"ok": True, "state": "ok", "root": "/Users/test/fleet-dash",
        "worktree": "/Users/test/fleet-dash", "worktrees": ["/Users/test/fleet-dash",
            "/Users/test/fleet-dash-worktrees/ui"], "title": "fleet-dash",
        "branch": "codex-integration", "detached": False, "head_oid": "abc123",
        "upstream": "origin/codex-integration", "ahead": 2, "behind": 0,
        "dirty": True, "conflicts": 0, "remotes": ["origin"], "remote": "origin",
        "remote_branch": "codex-integration", "default_base": "main",
        "repo_slug": "federbenjamin/fleet-dash",
        "github_url": "https://github.com/federbenjamin/fleet-dash",
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
    now = time.time()
    notifications = [
        fixture_notification(6, "question", "active", "Choose a release target",
            "Claude needs one answer before it can continue.", "claude", "claude-one",
            unread=True),
        fixture_notification(5, "failure", "active", "Codex connection interrupted",
            "The shared runtime stopped responding.", "codex", "codex:thread-one",
            unread=True),
        fixture_notification(4, "completion", "resolved", "Build finished",
            "The notification persistence milestone completed.", "claude", "claude-one",
            unread=True, link_kind="session", link_id="claude-one"),
        fixture_notification(3, "stall", "snoozed", "Parser audit is quiet",
            "No new progress has appeared for four minutes.", "codex", "codex:thread-one",
            snoozed_until=now + 900),
        fixture_notification(2, "outcome", "resolved", "Artifact delivered",
            "A browser verification report is ready.", "codex", "codex:thread-one",
            link_kind="session", link_id="codex:thread-one"),
        fixture_notification(1, "notification", "resolved", "Push delivery recovered",
            "The test device accepted its next delivery.", "claude")]
    return {"sessions": [claude, codex], "closed": [], "actions": [], "uploads": [],
            "hidden_action_sessions": [], "act_receipts": {},
            "ledger": {"ok": True, "recovered": False},
            "contexts": {"claude-one": copy.deepcopy(context),
                         "codex:thread-one": copy.deepcopy(context)},
            "tool_results": {"tu-read": "line one\nline two\nline three",
                             "tu-big": "x" * 9000},
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
            "settings": {"stall_seconds": 240,
                "preview_sessions": True, "preview_session_lines": 2,
                "preview_agents": False, "preview_agent_lines": 1,
                "reader_width": "fit", "pinned_sessions": [],
                "legacy_ntfy_enabled": False, "legacy_ntfy_configured": True},
            "reply_available": {}, "read_sessions": {}, "dismissed_actions": {},
            "repo": repo, "repo_actions": [], "outbox": [], "budgets": [],
            "briefing_reviewed": {}, "push_devices": {},
            "notification_events": notifications,
            "notification_read_cursors": {}, "muted_notification_sessions": [],
            "notification_policy": fixture_notification_policy(now),
            "notification_delivery_problems": [{"id": "delivery-fixture-1",
                "event_id": "evt-5-failure", "device_id": "expired-phone",
                "generation": 1, "status": "subscription_expired", "attempt": 2,
                "remote_status": 410, "created_at": now - 300, "updated_at": now - 60,
                "display_name": "Benjamin’s iPhone", "platform": "iOS",
                "permission_state": "expired", "last_success_at": now - 3600,
                "last_failure_at": now - 60, "enabled": False, "can_retry": False},
                {"id": "delivery-fixture-2", "event_id": "evt-4-completion",
                "device_id": "desktop", "generation": 1, "status": "failed",
                "attempt": 1, "remote_status": 503, "created_at": now - 90,
                "updated_at": now - 45, "display_name": "Studio Mac",
                "platform": "macOS", "permission_state": "granted",
                "last_success_at": now - 600, "last_failure_at": now - 45,
                "enabled": True, "can_retry": True}]}


STATE = fresh_state()


def fixture_notifications(query):
    device_id = (query.get("device") or ["default"])[0]
    read_cursor = STATE["notification_read_cursors"].setdefault(device_id, 3)
    event_id = (query.get("id") or [""])[0]
    try:
        limit = max(1, min(200, int((query.get("limit") or ["100"])[0])))
        cursor = int((query.get("cursor") or ["0"])[0] or 0)
    except ValueError:
        return {"ok": False, "error": "invalid notification pagination"}
    states = {item for item in (query.get("state") or [""])[0].split(",") if item}
    kinds = {item for item in (query.get("kind") or [""])[0].split(",") if item}
    events = copy.deepcopy(STATE["notification_events"])
    maximum = max((item["sequence"] for item in events), default=0)
    for item in events:
        item["unread"] = item["sequence"] > read_cursor
        item["muted"] = item.get("session_id") in STATE["muted_notification_sessions"]
    if event_id:
        rows = [item for item in events if item["id"] == event_id]
        next_cursor = None
    else:
        before = cursor or maximum + 1
        rows = [item for item in events if item["sequence"] < before and
                (not states or item["state"] in states) and
                (not kinds or item["kind"] in kinds)]
        rows.sort(key=lambda item: item["sequence"], reverse=True)
        next_cursor = rows[limit - 1]["sequence"] if len(rows) > limit else None
        rows = rows[:limit]
    return {"ok": True, "device_id": device_id, "read_cursor": read_cursor,
        "event_cursor": maximum,
        "unread": sum(item["sequence"] > read_cursor for item in events),
        "active": sum(item["state"] in ("active", "snoozed") for item in events),
        "events": rows,
        "delivery_problems": copy.deepcopy(STATE["notification_delivery_problems"]),
        "next_cursor": next_cursor}


def fixture_action_identity(session, kind, revision):
    raw = "\0".join((session.get("provider") or "claude", session["session_id"],
                      kind, str(revision)))
    return "act-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def fixture_attention(session):
    pending = session.get("pending") or {}
    revision = str(session.get("convo_v") or "")
    raw_state = str(session.get("state") or "idle")
    state = str(session.get("stale_previous_state") or "idle") \
        if raw_state == "stale" else raw_state
    kind = {"question": "question", "permission": "approval",
            "elicitation": "form"}.get(pending.get("kind"))
    if kind:
        action_revision = str(pending.get("nonce") or revision)
    elif session.get("reply_requested"):
        kind, action_revision = "reply", revision
    elif state in ("error", "blocked", "stalled_or_prompt"):
        detail = str(session.get("error") or "").strip()[:600]
        kind, action_revision = "problem", "\0".join((revision, state, detail))
    elif state == "needs_you":
        kind, action_revision = "attention", revision
    else:
        return None
    return kind, action_revision, fixture_action_identity(session, kind, action_revision)


def fixture_actions(sessions):
    records = []
    for session in sessions:
        if session["session_id"] in STATE.get("hidden_action_sessions", []):
            continue
        if session.get("ui_group") == "history":
            continue
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
        elif session.get("new_response"):
            kind, request, delivery = "outcome", "Completed work is ready to review", "Unreviewed"
        elif session.get("ui_group") == "needs_you":
            kind = "problem" if session.get("state") in ("error", "stalled_or_prompt") else "attention"
            request, delivery = session.get("error") or session.get("reason_label"), "Intervention needed"
        if not kind:
            continue
        exact = fixture_attention(session)
        action_id = exact[2] if exact else fixture_action_identity(session, kind, revision)
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
            "dismissible": bool(exact),
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
            ("ok", "state", "worktree", "observed_at", "elapsed_ms", "cached", "error",
             "repo_slug", "github_url")
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
            "median_tokens": (53000 if spawn.get("model") == "gpt-5.3-codex" else
                              54000 if spawn.get("model") == "gpt-5.4" else 22000),
            "median_runtime_seconds": 900,
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
    attention = fixture_attention(session)
    attention_dismissed = bool(attention and
                               attention[2] in STATE.get("dismissed_actions", {}))
    if state == "dormant":
        group, reason, action = "history", "External" if external else "Inactive", \
            "view" if external else "continue"
    elif attention_dismissed:
        group, reason, action = "available", "Available", "view" if external else "continue"
    elif kind == "question":
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
                   unresolved_attention=bool(attention),
                   attention_dismissed=attention_dismissed,
                   attention_action_id=attention[2] if attention else None,
                   activity_at=time.time() - float(session.get("quiet_s") or 0),
                   pinned=session["session_id"] in STATE["settings"]["pinned_sessions"])
    return session


def fleet():
    sessions = [organize_session(item) for item in copy.deepcopy(STATE["sessions"])]
    if STATE.get("scenario") == "staging":
        for session in sessions:
            owned = session["session_id"] == "codex:thread-one"
            session["staging_owned"] = owned
            session["staging_observer"] = not owned
            if not owned:
                session.update(read_only=True, access="view_only", access_label="View only",
                               primary_action="view", primary_action_label="View",
                               read_only_reason=
                               "Production session; staging can observe but not control it")
                for key in ("submit", "interrupt", "close", "compact", "review",
                            "focus_terminal", "answer_structured", "decide_approval",
                            "spawn_agent", "relay_agent", "relay_agent_direct",
                            "change_permission_mode", "model_effort_settings",
                            "change_model_effort"):
                    session["capabilities"][key] = False
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
        state = "cancelled" if item.get("cancelled") else item["state"]
        outbox_states[state] = outbox_states.get(state, 0) + 1
    outbox_pending = sum(outbox_states.get(state, 0) for state in
        ("scheduled", "waiting_availability", "waiting_usage_reset", "spawning", "sending"))
    outbox_attention = sum(outbox_states.get(state, 0) for state in
        ("blocked", "failed", "confirmation_unknown"))
    budget_evaluations = fixture_budget_evaluations(sessions + closed)
    actions = fixture_actions(sessions) + fixture_budget_actions(budget_evaluations)
    if STATE.get("scenario") == "staging":
        actions = [item for item in actions
                   if item.get("session_id") == "codex:thread-one"]
    return {"t": time.time(), "sessions": sessions, "actions": actions,
            "instance": ({"mode": "staging", "name": "Fleet Staging",
                "controls": "staging_owned_only", "owned_sessions": 1,
                "source_root": "/Users/test/fleet-dash"}
                if STATE.get("scenario") == "staging" else
                {"mode": "production", "name": "Fleet Dash", "controls": "production",
                 "owned_sessions": None, "source_root": None}),
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
                        "active": STATE.get("scenario") != "missing-active-warning",
                        "five_hour_pct": 20, "weekly_pct": 30,
                        "fable_weekly_pct": (96 if STATE.get("scenario") in
                        {"usage-warning", "missing-active-warning"} else
                        75 if STATE.get("scenario") == "usage-amber" else 41),
                        "five_hour_reset": "2099-01-01T00:00:00Z",
                        "weekly_reset": "2099-01-07T00:00:00Z",
                        "fable_weekly_reset": "2099-01-08T00:00:00Z"},
                        {"id": "two", "email": "second@example.com", "active": False,
                         "five_hour_pct": 94 if STATE.get("scenario") in
                         {"usage-warning", "inactive-usage-warning"} else 4,
                         "weekly_pct": 8, "fable_weekly_pct": 12,
                         "five_hour_reset": "2099-01-01T02:00:00Z",
                         "weekly_reset": "2099-01-07T02:00:00Z",
                         "fable_weekly_reset": "2099-01-08T02:00:00Z"}]},
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
            "models_by_provider": {"claude": [{"id": "sonnet", "name": "Sonnet",
                "efforts": ["low", "high"]}, {"id": "opus", "name": "Opus",
                "efforts": ["medium"]}], "codex": [{"id": "gpt-5.4",
                "name": "GPT-5.4", "efforts": ["medium", "high"]},
                {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex",
                 "efforts": ["medium", "high"]}]},
            "providers": {"claude": {"ok": True},
                          "codex": {"ok": not bool(STATE.get("codex_error")),
                                    "error": STATE.get("codex_error"),
                                    "runtime": ({"mode": "migrating", "phase": "draining",
                                        "blockers": ["turn active"], "error": None,
                                        "launcher": {"state": "ready"}}
                                        if STATE.get("scenario") == "runtime-migrating" else
                                        {"mode": "managed", "phase": "committed",
                                         "blockers": [], "error": None,
                                         "launcher": {"state": "ready"}})}},
            "ledger": copy.deepcopy(STATE.get("ledger") or {"ok": True}),
            "settings": {**copy.deepcopy(STATE["settings"]),
                         "dismissed_actions": copy.deepcopy(
                             STATE.get("dismissed_actions", {}))},
            "page_v": 1}


def codex_session():
    return next(item for item in STATE["sessions"] if item["provider"] == "codex")


def claude_session():
    return next(item for item in STATE["sessions"] if item["provider"] == "claude")


def set_scenario(name):
    global STATE
    STATE = fresh_state()
    STATE["scenario"] = name
    session = claude_session() if (name.startswith("claude-question") or
        name.startswith("mobile-needs-you") or name.startswith("close-worktree") or
        name in ("notification-request", "send-while-busy", "claude-prompt-gate",
                 "screen-permission")) else codex_session()
    if name == "screen-permission":
        # a permission Fleet read off the pane: no tool name, no input
        # summary, and its words fetched from the screen route instead
        session.update(state="needs_you", normalized_state="needs_you",
            reg_status="waiting", ui_group="needs_you",
            pending={"kind": "permission", "nonce": "screen-1785000000000-1",
                     "source": "screen", "tool": "requested tool",
                     "input_summary": ""})
    if name == "claude-starting":
        session = claude_session()
        session.update(title="New Claude session", name="New Claude session",
                       last_msg=None, ctx_tokens=None, ctx_pct=None,
                       total_tokens=None, cost=None, cost_source="unavailable",
                       convo_v="starting:4242:idle")
        session["status_line"] = {"model": "claude", "effort": "high",
            "context_tokens": None, "context_window": None, "context_pct": None,
            "compact_remaining": None, "cache_read_pct": None, "cache_write": None,
            "tree_cost": None, "cost_scope": "unavailable", "frozen": False}
        STATE["contexts"]["claude-one"] = []
    elif name == "notification-request":
        session.update(state="needs_you", normalized_state="needs_you", reg_status="waiting",
            pending={"kind": "question", "nonce": "rev-6", "dismiss_action": "cancel_turn",
                "questions": [{"header": "Release target",
                    "question": "Which release target should Fleet use?", "multiSelect": False,
                    "allowOther": True, "options": [
                        {"label": "Staging", "description": "Verify before production."},
                    {"label": "Production", "description": "Ship the approved build."}]}]})
    elif name == "claude-prompt-gate":
        session.update(state="needs_you", normalized_state="needs_you", reg_status="idle",
            pending={"kind": "question", "nonce": "native-gate", "questions": [{
                "header": "Native gate", "question": "Can Fleet answer yet?",
                "multiSelect": False, "allowOther": False,
                "options": [{"label": "Yes"}, {"label": "No"}]}]})
        session["capabilities"].update(answer_structured=False, decide_approval=False,
            answer_reason="Waiting for Claude's native prompt state")
    elif name in ("mobile-needs-you", "mobile-needs-you-missing-action"):
        session.update(title="Get 429 into a mergable state", name="hazy-hatching-curry-d8",
            project="hazy-hatching-curry", branch="fix/pr-429", state="needs_you",
            normalized_state="needs_you", reg_status="waiting", quiet_s=28 * 60,
            last_msg={"role": "assistant", "text": "I'll run the statusline fix script."},
            pending={"kind": "question", "nonce": "mobile-needs-you-q1",
                "dismiss_action": "cancel_turn", "questions": [{"header": "Update branch",
                "question": "How should I bring PR #429 up to date with main (required by strict branch protection)?",
                "multiSelect": False, "allowOther": True, "options": [
                    {"label": "Remote update-branch", "description": "Merge main into the head branch on GitHub."},
                    {"label": "Wait on current CI first", "description": "Let the current checks finish first."},
                    {"label": "I'll do it in the worktree", "description": "Leave the manual merge to the owner."}]}]})
        if name.endswith("missing-action"):
            STATE["hidden_action_sessions"].append(session["session_id"])
    elif name in ("single-question", "answer-failure", "claude-question-slow",
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
    elif name == "long-multi-question":
        long_options = [{"label": f"Target {index}",
            "description": (f"Detailed explanation for target {index}. " * 4).strip()}
            for index in range(1, 19)]
        session.update(state="needs_you", pending={"kind": "question", "nonce": "q-long",
            "dismiss_action": "cancel_turn", "questions": [
                {"header": "Targets", "question": "Read every target before choosing.",
                 "multiSelect": True, "allowOther": True, "options": long_options},
                {"header": "Depth", "question": "Choose the verification depth.",
                 "multiSelect": False, "allowOther": False,
                 "options": [{"label": "Full"}, {"label": "Focused"}]}]})
    elif name in ("approval", "approval-slow"):
        session.update(state="needs_you", pending={"kind": "permission", "nonce": "p1",
            "tool": "command", "approval_kind": "command", "input_summary": "npm test",
            "decisions": ["allow", "always", "deny", "cancel"]})
        if name == "approval-slow":
            STATE["delay_quick_response"] = 0.75
    elif name in ("claude-permission", "claude-permission-blind",
                  "claude-permission-nogrant"):
        session = claude_session()
        session.update(state="needs_you", normalized_state="needs_you",
                       reg_status="waiting", ui_group="needs_you",
                       reason_label="Permission needed",
                       # no `decisions`: that field is Codex's statement of which
                       # approval values its App Server accepts, and a Claude
                       # pending has never carried it. The fixture claiming it
                       # hid the difference the always-button gate turns on.
                       pending={"kind": "permission", "nonce": "cp1",
                                "request_id": "req-cp1", "tool": "Bash",
                                "input_summary": "touch probe.txt"})
        if name.endswith("blind"):
            STATE["prompt_options_blind"] = True
        if name.endswith("nogrant"):
            # the Bash variant whose command cannot be statically analyzed:
            # Claude offers no rule to write, so row 2 is "No"
            STATE["prompt_options_no_grant"] = True
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
            archive=False, files=False, relay_agent=False, focus_terminal=False,
            model_effort_settings=False, change_model_effort=False)
    elif name == "organization":
        session.update(state="idle", pending=None, headless=True, read_only=True,
                       codex_source="vscode", read_only_reason=
                       "ChatGPT Desktop and VS Code use a different App Server; this transcript is view only")
        session["capabilities"] = capabilities(submit=False, takeover=False, close=False,
            archive=False, files=False, relay_agent=False, focus_terminal=False,
            model_effort_settings=False, change_model_effort=False)
        dormant = base_session("claude", "claude-dormant", "Dormant migration")
        dormant.update(state="dormant", quiet_s=8_000,
                       last_msg={"role": "assistant", "text": "Paused for later."})
        STATE["sessions"].append(dormant)
    elif name == "stale":
        session.update(state="stale", error="app-server exited", stale=True,
                       stale_reason="app-server exited")
        STATE["codex_error"] = "app-server exited"
        session["capabilities"] = capabilities(submit=False, interrupt=False, close=False,
            model_effort_settings=True, change_model_effort=False,
            change_model_effort_reason="Provider state is stale")
    elif name == "cross-client-active":
        session.update(state="running", reg_status="running", quiet_s=1,
                       headless=True, read_only=True, codex_source="vscode",
                       read_only_reason=
                       "ChatGPT Desktop and VS Code use a different App Server; this transcript is view only",
                       last_msg={"role": "assistant", "text": "Working in ChatGPT desktop."})
        session["capabilities"] = capabilities(
            submit=False, interrupt=False, close=False, compact=False, review=False,
            focus_terminal=False, model_effort_settings=False, change_model_effort=False)
    elif name == "codex-terminal":
        session.update(terminal_attached=True, control_state="terminal_idle")
        session["capabilities"].update(
            focus_terminal=True, focus_terminal_mode="focus",
            focus_terminal_label="open",
            focus_terminal_reason="Bring the attached Codex terminal to the front")
    elif name == "markdown-peek":
        preview = (
            "### Default width\n\nUse **Fit the screen** with `compact code` and "
            "[Fleet docs](https://example.com/docs).\n\n- Fast\n- Clear\n\n"+
            ("bounded preview content " * 45))
        session["last_msg"] = {"role": "assistant",
                               "text": preview[:799] + "…"}
    elif name == "large-message":
        large = "Large response begins.\n\n" + ("complete-message-segment " * 500) + "\n\nEND-OF-LARGE-RESPONSE"
        STATE["contexts"]["codex:thread-one"] = [
            {"role": "assistant", "text": large}]
    elif name == "send-failure":
        STATE["fail_text"] = True
    elif name == "send-while-busy":
        session.update(state="running", normalized_state="running",
                       reg_status="running", ui_group="working",
                       reason_label="Working")
        session["capabilities"]["interrupt"] = True
        session["capabilities"]["change_model_effort"] = False
        session["capabilities"]["change_model_effort_reason"] = "Available when Claude is idle"
    elif name in ("active-tool", "active-tool-stalled"):
        stalled = name.endswith("stalled")
        session = claude_session()
        session.update(state="stalled" if stalled else "running",
                       normalized_state="stalled" if stalled else "running",
                       reg_status="running", ui_group="working",
                       reason_label="Stalled" if stalled else "Working",
                       active_tool={"name": "Bash", "count": 2 if stalled else 1,
                                    "seconds": 245 if stalled else 3})
    elif name in ("compacting-hook", "compacting-screen"):
        session = claude_session()
        screen = name.endswith("screen")
        session.update(state="running", normalized_state="running",
                       reg_status="running", ui_group="working",
                       reason_label="Working", compacting=42,
                       compacting_source="screen" if screen else "hook")
        if screen:
            session["screen_state"] = "compacting"
    elif name == "handoff-failure":
        STATE["fail_handoff_once"] = True
    elif name.startswith("close-worktree"):
        session.update(cwd="/Users/test/fleet-dash-worktrees/close-me",
                       branch="feature/close-me", project="fleet-dash")
        if name == "close-worktree-shared":
            codex_session()["cwd"] = session["cwd"]
        if name == "close-worktree-cleanup-failure":
            STATE["fail_cleanup"] = True
    elif name == "reply-requested":
        session.update(state="turn_done", reg_status="idle", reply_requested=True,
                       convo_v="reply:1", quiet_s=12,
                       last_msg={"role": "assistant", "text":
                           "Which organization should we use?\n\nAnswer both before I continue."})
    elif name == "new-response":
        session.update(state="turn_done", reg_status="idle", new_response=True,
                       convo_v="response:1", quiet_s=12,
                       last_msg={"role": "assistant", "text": "Done.\n\n- Updated the dashboard\n- Tests passed"})
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
    elif name == "ledger-recovery":
        STATE["ledger"] = {"ok": False, "recovered": True,
            "error": "Fleet's local ledger was corrupt. It was preserved and a clean ledger was started; Session History, Outbox, budgets, and local usage totals may be incomplete.",
            "quarantine": "ledger.db.corrupt-test"}
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
        STATE["closed"].append({
            "session_id": "closed-large", "provider": "claude",
            "title": "Large closed conversation", "project": "fleet-dash",
            "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
            "model": "claude-sonnet", "closed_at": int(time.time()) - 60,
            "first_seen": int(time.time()) - 3600, "last_seen": int(time.time()) - 60,
            "can_reopen": False, "primary_action": "view",
            "primary_action_label": "View", "ui_group": "history",
            "reason_label": "Closed"})
    elif name == "agent-collision":
        other = base_session("codex", "codex:thread-two", "Second Codex parent")
        other["agents"] = copy.deepcopy(session["agents"])
        for agent in other["agents"]:
            agent["session_id"] = other["session_id"]
        other.update(agents_running=0, agents_total=1)
        STATE["sessions"].append(other)
    elif name in ("workstreams", "repo-action-failure"):
        session.update(cwd="/Users/test/fleet-dash-worktrees/ui",
                       branch="feature/action-inbox", project="fleet-dash")
        if name == "repo-action-failure":
            STATE["fail_repo_once"] = True

    # F50 made native prompt authority explicit. Ordinary waiting fixtures must
    # opt into the capability they are intended to exercise; the prompt-gate
    # scenario intentionally stays disabled until its test advances the native
    # registry state through /test/native-prompt-state.
    pending_kind = (session.get("pending") or {}).get("kind")
    if name != "claude-prompt-gate" and pending_kind == "question":
        session["capabilities"].update(answer_structured=True, answer_reason="")
    if pending_kind == "permission":
        session["capabilities"].update(decide_approval=True, answer_reason="")


def authorized(handler):
    cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    mode = "staging" if STATE.get("scenario") == "staging" else "production"
    cookie_name = "act_token_" + mode
    return ((cookie.get(cookie_name) and cookie[cookie_name].value == TOKEN) or
            handler.headers.get("X-Act-Token") == TOKEN)


def paginate_messages(query, messages):
    messages = copy.deepcopy(messages)
    try:
        limit = max(1, min(100, int((query.get("limit") or ["50"])[0])))
        cursor = (len(messages) if "cursor" not in query else
                  int((query.get("cursor") or [str(len(messages))])[0]))
        if cursor < 0 or cursor > len(messages):
            raise ValueError
    except ValueError:
        return None
    start = max(0, cursor - limit)
    return {"messages": messages[start:cursor], "message_total": len(messages),
            "next_cursor": start if start > 0 else None}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, ctype, data):
        if isinstance(data, str):
            data = data.encode()
        payload_bytes = len(data)
        # Mirror server.py: compress the same responses the real daemon does, so
        # the PWA/offline specs actually exercise a service worker caching and
        # replaying a gzip-encoded snapshot.
        encoded = False
        if (payload_bytes >= 1400 and
                ctype.startswith(("application/json", "text/")) and
                "gzip" in (self.headers.get("Accept-Encoding") or "")):
            data = gzip.compress(data, 4)
            encoded = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if encoded:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("X-Fleet-Payload-Bytes", str(payload_bytes))
        self.send_header("Cache-Control", "no-store")
        if urlparse(self.path).path == "/api/file":
            self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Superseded-request tests intentionally abort in-flight responses.
            return

    def json_reply(self, data, code=200):
        self.reply(code, "application/json", json.dumps(data))

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        route = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        with LOCK:
            if route in ("/api/search", "/api/search/status", "/api/search/context",
                         "/api/handoff", "/api/repo", "/api/outbox",
                         "/api/notifications", "/api/push/config", "/api/push/devices",
                         "/api/notification-policy"):
                if not authorized(self):
                    return self.json_reply({"ok": False, "error": "bad token"}, 403)
                if route == "/api/notifications":
                    return self.json_reply(fixture_notifications(query))
                if route == "/api/notification-policy":
                    policy = copy.deepcopy(STATE["notification_policy"])
                    policy["muted_sessions"] = [{"session_id": sid, "provider": "claude",
                        "muted_at": time.time() - 60}
                        for sid in STATE["muted_notification_sessions"]]
                    return self.json_reply(policy)
                if route in ("/api/push/config", "/api/push/devices"):
                    device_id = (query.get("device") or
                        [self.headers.get("X-Fleet-Device-ID") or ""])[0]
                    devices = [copy.deepcopy(item) for item in STATE["push_devices"].values()]
                    current = next((item for item in devices if item["id"] == device_id), None)
                    if route == "/api/push/devices":
                        return self.json_reply({"ok": True, "devices": devices,
                            "current_device": current, "registered": len(devices),
                            "enabled": sum(item.get("enabled") is True for item in devices)})
                    return self.json_reply({"ok": True, "feature": "production", "configured": True,
                        "public_key": "BErt812-TgTDdRIdV-OWO4wuFeaDBBA3j8jvS4JtMHcY1sxINPdwC3iYMeJOV245gdZZoyYWQ_Mh48zA64LKNns",
                        "delivery": "ready", "helper": {"ready": True, "restarts": 0,
                            "state": "ready"},
                        "queue": {"queued": 0, "failed": 0, "subscription_expired": 0},
                        "current_device": current,
                        "devices": devices,
                        "registered_devices": len(devices),
                        "enabled_devices": sum(item.get("enabled") is True for item in devices)})
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
                            "file_id": file_id("codex:thread-one", "/fixture/artifact.md")
                                if artifact else None},
                        "messages": [{"id": document_id - 1, "role": "user",
                            "kind": "message", "timestamp": "2026-07-16T12:00:00Z",
                            "text": "Find the protocol regression", "file_id": None,
                            "hit": False}, {"id": document_id,
                            "role": "assistant", "kind": "message",
                            "timestamp": "2026-07-16T12:00:01Z",
                            "text": "Indexed artifact preview." if artifact else
                                    "Indexed exact context for the protocol regression.",
                            "file_id": file_id("codex:thread-one", "/fixture/artifact.md")
                                if artifact else None,
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
                    "file_id": None,
                    "snippet": "Indexed exact context for the protocol regression.", "rank": -1},
                    {"id": 902, "source_id": 42, "provider": "claude",
                    "source_kind": "subagent", "session_id": "claude-one",
                    "agent_id": "agent-review", "project": "fleet-dash",
                    "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
                    "title": "Claude review agent", "role": "assistant",
                    "kind": "reasoning", "timestamp": "2026-07-16T11:59:00Z",
                    "timestamp_epoch": time.time() - 60, "file_id": None,
                    "snippet": "Reviewed parser migration and protocol states.", "rank": -0.5},
                    {"id": 903, "source_id": 43, "provider": "codex",
                    "source_kind": "artifact", "session_id": "codex:thread-one",
                    "agent_id": None, "project": "fleet-dash",
                    "cwd": "/Users/test/fleet-dash", "branch": "codex-integration",
                    "title": "artifact.md", "role": "artifact", "kind": "artifact",
                    "timestamp": "2026-07-16T11:58:00Z",
                    "timestamp_epoch": time.time() - 120,
                    "file_id": file_id("codex:thread-one", "/fixture/artifact.md"),
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
                project = (query.get("project") or [""])[0]
                try:
                    cursor = max(0, int((query.get("cursor") or ["0"])[0]))
                    limit = max(1, min(200, int((query.get("limit") or ["100"])[0])))
                except ValueError:
                    return self.json_reply({"ok": False, "error": "invalid pagination"})
                if provider:
                    rows = [item for item in rows if item.get("provider") == provider]
                if access:
                    rows = [item for item in rows if item.get("primary_action") == access]
                if project:
                    rows = [item for item in rows if item.get("project") == project]
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
                if (STATE.get("scenario") == "forecast-race" and
                        spawn.get("model") == "gpt-5.4"):
                    time.sleep(.35)
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
                page = paginate_messages(query, STATE.get("contexts", {}).get(sid, []))
                if page is None:
                    return self.json_reply({"ok": False,
                                            "error": "invalid conversation pagination"})
                return self.json_reply({"ok": True, **page, "files": fixture_files(sid)})
            if route == "/api/agent_context":
                sid = (query.get("sid") or [""])[0]
                messages = ([{"role": "assistant", "text": f"Subagent report {index:03d}"}
                            for index in range(205)] if STATE["scenario"] == "large-conversation" else
                            [{"role": "assistant", "text": (
                                "Second parent report" if STATE["scenario"] == "agent-collision" and
                                sid == "codex:thread-two" else
                                "First parent report" if STATE["scenario"] == "agent-collision" else
                                "Subagent report")}])
                page = paginate_messages(query, messages)
                if page is None:
                    return self.json_reply({"ok": False,
                                            "error": "invalid conversation pagination"})
                return self.json_reply({"ok": True, **page, "info": {"agent_id": "agent-child-one",
                    "agent_type": "reviewer", "description": "Review protocol mapping",
                    "model": "gpt-5.4", "effort": "high", "tokens": {},
                    "total_tokens": None, "cost": None, "state": "done",
                    "status_line": {**fixture_status_line("codex", frozen=True),
                        "model": "GPT-5.4", "cost_label": "agent"}}})
            if route == "/api/closed_context":
                sid = (query.get("sid") or [""])[0]
                messages = ([{"role": "assistant", "text": f"Closed report {index:03d}"}
                            for index in range(205)] if STATE["scenario"] == "large-conversation" else
                            [{"role": "assistant", "text": "Durable closed conversation"}])
                page = paginate_messages(query, messages)
                if page is None:
                    return self.json_reply({"ok": False,
                                            "error": "invalid conversation pagination"})
                return self.json_reply({"ok": True, "closed": True, **page,
                    "files": fixture_files(sid), "agents": [],
                    "info": {"project": "fleet-dash", "branch": "old",
                        "can_resume_and_send": False,
                        "resume_disabled_reason": "Fixture session is view only",
                        "status_line": fixture_status_line("claude", frozen=True)}})
            if route == "/api/tool-result":
                if not authorized(self):
                    return self.json_reply({"ok": False, "error": "bad token"}, 403)
                tuid = (query.get("tuid") or [""])[0]
                body = STATE.get("tool_results", {}).get(tuid)
                if body is None:
                    return self.json_reply({"ok": False,
                                            "error": "no result recorded for this call"})
                return self.json_reply({"ok": True, "tool_id": tuid, "failed": False,
                                        "text": body[:4096], "chars": len(body),
                                        "truncated": len(body) > 4096})
            if route == "/api/screen":
                if not authorized(self):
                    return self.json_reply({"ok": False, "error": "bad token"}, 403)
                sid = (query.get("sid") or [""])[0]
                if not sid.startswith("claude"):
                    return self.json_reply({
                        "ok": False, "code": "screen_unavailable",
                        "error": "this session is not running in a tmux pane — only "
                                 "the tmux transport can read a terminal screen"})
                lines = (["  touch probe.txt", "", " Do you want to proceed?",
                          " ❯ 1. Yes", "   2. No", "",
                          " Esc to cancel · Tab to amend"]
                         if STATE["scenario"] == "screen-permission" else
                         ["❯ 1. [ ] Red", "  2. [ ] Green", "  <script>x</script>"])
                return self.json_reply({
                    "ok": True, "session_id": sid, "transport": "tmux",
                    "truncated": False, "captured_at": time.time(),
                    "lines": lines})
            if route == "/api/prompt-options":
                if not authorized(self):
                    return self.json_reply({"ok": False, "error": "bad token"}, 403)
                sid = (query.get("sid") or [""])[0]
                if not sid.startswith("claude") or STATE.get("prompt_options_blind"):
                    return self.json_reply({
                        "ok": False, "code": "screen_unavailable",
                        "error": "this session is not running in a tmux pane"})
                grant = "Yes, and always allow access to fleet-dash/ from this project"
                # `always_key` is the digit the SCREEN showed, which is what the
                # client presses — the server derives it rather than assuming 2,
                # because one captured variant puts "No" on row 2.
                if STATE.get("prompt_options_no_grant"):
                    return self.json_reply({
                        "ok": True, "session_id": sid, "kind": "permission",
                        "options": ["Yes", "No"]})
                return self.json_reply({
                    "ok": True, "session_id": sid, "kind": "permission",
                    "options": ["Yes", grant, "No"],
                    "always_key": "2", "always_label": grant})
            if route == "/api/act-receipt":
                if not authorized(self):
                    return self.json_reply({"ok": False, "error": "bad token"}, 403)
                rid = (query.get("rid") or [""])[0]
                receipt = STATE.setdefault("act_receipts", {}).get(rid)
                if not receipt:
                    return self.json_reply({"ok": True, "found": False,
                        "reason": "no receipt: the request never reached Fleet, or it "
                                  "is older than 24 hours"})
                return self.json_reply({"ok": True, "found": True, "receipt": receipt})
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
                sid = (query.get("sid") or [""])[0]
                selector = (query.get("fid") or [""])[0]
                path = next((path for path, _ in fixture_file_rows()
                             if file_id(sid, path) == selector), "")
                if not path:
                    return self.reply(404, "text/plain", "not a file owned by this session")
                if path.endswith("preview.html"):
                    return self.reply(200, "text/plain; charset=utf-8", """<!doctype html>
<html><head><style>h1{color:rgb(35,120,70)}</style></head><body>
<h1>Rendered HTML</h1><form action=\"https://example.test/submit\"><input value=\"private\"><button>Submit</button></form>
<div id=\"generated\"></div><img src=\"https://example.test/tracker.png\">
<script>document.getElementById('generated').textContent='Inline chart rendered';try{parent.__unsafeHtmlRan=true}catch{}</script>
<script src=\"https://example.test/external.js\"></script></body></html>""")
                if path.endswith("data.json"):
                    return self.reply(200, "application/json; charset=utf-8",
                                      '{"status":"ready","items":[1,2]}')
                if path.endswith("report.pdf"):
                    return self.reply(200, "application/pdf",
                                      b"%PDF-1.4\n% Fleet fixture\n%%EOF\n")
                return self.reply(200, "text/plain; charset=utf-8", "# Artifact\n\nSafe preview.")
            if route == "/api/session_files":
                sid = (query.get("sid") or [""])[0]
                return self.json_reply({"ok": True, "files": fixture_files(sid),
                                        "next_cursor": None})
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
        assets = {
            "/static/fleet.css": ("static/fleet.css", "text/css; charset=utf-8"),
            **{f"/static/fonts/{name}": (f"static/fonts/{name}", "font/woff2")
               for name in ("SpaceGrotesk-var.woff2", "IBMPlexMono-Regular.woff2",
                            "IBMPlexMono-Medium.woff2", "IBMPlexMono-SemiBold.woff2")},
            **{f"/static/js/{name}": (f"static/js/{name}", "text/javascript; charset=utf-8")
               for name in ("main.js", "state-store.js", "nav.js", "search.js",
                            "ui-utils.js", "outbox.js", "push.js", "notifications.js",
                            "context.js", "viewer-handoff.js", "overlays.js",
                            "workspace.js", "cards.js", "settings-actions.js",
                            "history-spawn.js", "insights.js")},
            "/static/manifest.webmanifest": ("static/manifest.webmanifest",
                                                "application/manifest+json"),
            "/static/offline.html": ("static/offline.html", "text/html; charset=utf-8"),
            "/static/icons/fleet.svg": ("static/icons/fleet.svg", "image/svg+xml"),
            "/static/icons/fleet-192.png": ("static/icons/fleet-192.png", "image/png"),
            "/static/icons/fleet-512.png": ("static/icons/fleet-512.png", "image/png"),
            "/static/icons/fleet-maskable-512.png": ("static/icons/fleet-maskable-512.png",
                                                       "image/png"),
            "/sw.js": ("static/sw.js", "text/javascript; charset=utf-8"),
        }
        if route in assets:
            path, ctype = assets[route]
            with open(os.path.join(ROOT, path), "rb") as handle:
                return self.reply(200, ctype, handle.read())
        return self.reply(404, "text/plain", "not found")

    def do_POST(self):
        global STATE
        route = urlparse(self.path).path
        if route == "/api/upload-image":
            if not authorized(self):
                return self.json_reply({"ok": False, "error": "bad or missing act token"}, 403)
            query = parse_qs(urlparse(self.path).query)
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            upload_id = (query.get("id") or [""])[0]
            with LOCK:
                STATE["uploads"].append({"id": upload_id,
                    "sid": (query.get("sid") or [""])[0],
                    "name": (query.get("name") or [""])[0],
                    "content_type": self.headers.get("Content-Type"), "size": len(data)})
            return self.json_reply({"ok": True, "upload_id": upload_id,
                "name": (query.get("name") or ["image"])[0],
                "content_type": "image/jpeg", "size": len(data)})
        payload = self.body()
        with LOCK:
            if route == "/test/reset":
                set_scenario(payload.get("scenario") or "base")
                return self.json_reply({"ok": True})
            if route == "/test/canonical-session-settings":
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("session_id")), None)
                if not session:
                    return self.json_reply({"ok": False, "error": "unknown session"}, 404)
                session["model"] = str(payload.get("model") or "")
                session["effort"] = str(payload.get("effort") or "")
                session["status_line"].update(model=session["model"], effort=session["effort"])
                return self.json_reply({"ok": True, "model": session["model"],
                                        "effort": session["effort"]})
            if route == "/test/native-prompt-state":
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("session_id")), None)
                if not session:
                    return self.json_reply({"ok": False, "error": "unknown session"}, 404)
                waiting = payload.get("waiting") is True
                session["reg_status"] = "waiting" if waiting else "idle"
                session["capabilities"].update(answer_structured=waiting,
                    decide_approval=waiting,
                    answer_reason="" if waiting else
                        "Waiting for Claude's native prompt state")
                return self.json_reply({"ok": True})
            if route == "/test/act-receipt":
                # Stand in for the durable receipt an act() left behind
                # (invariant 76) so a spec can replay the reconnect path.
                rid = str(payload.get("client_request_id") or "")
                if not rid:
                    return self.json_reply({"ok": False, "error": "rid required"}, 400)
                STATE.setdefault("act_receipts", {})[rid] = {
                    "receipt_id": f"rcpt-{rid}", "client_request_id": rid,
                    "session_id": str(payload.get("session_id") or ""),
                    "action_type": str(payload.get("action_type") or "text"),
                    "state": str(payload.get("state") or "delivered"),
                    "code": str(payload.get("code") or ""),
                    "error": str(payload.get("error") or ""),
                    "created_at": time.time(), "updated_at": time.time()}
                return self.json_reply({"ok": True})
            if route == "/test/screen-state":
                # The label the scan derives from a batched capture-pane look
                # (invariant 78). Never screen text — only the classification.
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("session_id")), None)
                if not session:
                    return self.json_reply({"ok": False, "error": "unknown session"}, 404)
                state = payload.get("state")
                if state:
                    session["screen_state"] = str(state)
                else:
                    session.pop("screen_state", None)
                return self.json_reply({"ok": True})
            if route == "/test/prompt-identity":
                # A prompt's nonce flips when its evidence source changes (hook
                # capture -> transcript fallback). The server-owned request_id
                # does not (invariant 75); this drives that transition.
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("session_id")), None)
                if not session or not session.get("pending"):
                    return self.json_reply({"ok": False, "error": "no pending request"}, 404)
                if payload.get("request_id") is not None:
                    session["pending"]["request_id"] = str(payload["request_id"])
                if payload.get("nonce") is not None:
                    session["pending"]["nonce"] = str(payload["nonce"])
                return self.json_reply({"ok": True, "pending": session["pending"]})
            if route == "/test/confirm":
                sid = payload.get("session_id")
                context = STATE.setdefault("contexts", {}).setdefault(sid, [])
                if payload.get("kind") == "answer":
                    context.append({"role": "event", "kind": "qa", "title": "You answered",
                        "level": "info", "detail": "", "qa": payload.get("answers") or []})
                else:
                    context.append({"role": "assistant" if payload.get("role") == "assistant" else "user",
                                    "text": payload.get("text") or ""})
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == sid), None)
                if session:
                    session["convo_v"] = "confirmed:" + str(time.time_ns())
                return self.json_reply({"ok": True})
            if route == "/test/outbox-state":
                item = next((row for row in STATE["outbox"]
                             if row["id"] == payload.get("outbox_id")), None)
                if not item:
                    return self.json_reply({"ok": False, "error": "outbox message not found"}, 404)
                state = payload.get("state")
                if state not in ("waiting_availability", "blocked", "failed",
                                 "confirmation_unknown", "sent"):
                    return self.json_reply({"ok": False, "error": "invalid outbox state"}, 400)
                item.update(state=state, state_label={
                    "waiting_availability": "Waiting for availability", "blocked": "Blocked",
                    "failed": "Failed", "confirmation_unknown": "Delivery unconfirmed",
                    "sent": "Sent"}[state], updated_at=time.time(),
                    error=payload.get("error") if state in ("failed", "confirmation_unknown") else None,
                    blocked_reason=payload.get("error") if state == "blocked" else None,
                    editable=state == "waiting_availability",
                    cancellable=state == "waiting_availability",
                    retryable=state in ("blocked", "failed", "confirmation_unknown"))
                return self.json_reply({"ok": True, "item": copy.deepcopy(item)})
            if route in ("/api/act", "/api/upload-image", "/api/settings", "/api/search/rebuild",
                         "/api/notifications/read", "/api/notifications/snooze",
                         "/api/notifications/wake", "/api/notifications/mute",
                         "/api/notifications/retry", "/api/push/subscription",
                         "/api/push/test", "/api/push/device-settings",
                         "/api/legacy-ntfy/test", "/api/notification-policy") \
                    and not authorized(self):
                return self.json_reply({"ok": False, "error": "bad or missing act token"}, 403)
            if route == "/api/push/subscription":
                device_id = str(payload.get("device_id") or "")
                if payload.get("forget"):
                    device = STATE["push_devices"].pop(device_id, None)
                    if not device:
                        return self.json_reply({"ok": False, "error": "device not registered"})
                    device = {"id": device_id, "removed": True}
                elif payload.get("remove"):
                    device = STATE["push_devices"].get(device_id)
                    if not device:
                        return self.json_reply({"ok": False, "error": "device not registered"})
                    device.update(enabled=False,
                        permission_state=payload.get("permission_state") or "expired",
                        health="disabled", last_registered_at=time.time())
                else:
                    device = STATE["push_devices"].get(device_id) or {
                        "id": device_id, "created_at": time.time(), "read_cursor": 0,
                        "preferences": {}}
                    device.update(display_name=str(payload.get("display_name") or "Fleet device")[:80],
                        platform=str(payload.get("platform") or "browser")[:80], enabled=True,
                        permission_state="granted", health="registered",
                        last_registered_at=time.time(), last_success_at=None,
                        last_failure_at=None)
                    STATE["push_devices"][device_id] = device
                return self.json_reply({"ok": True, "device": copy.deepcopy(device)})
            if route == "/api/notification-policy":
                policy = STATE["notification_policy"]
                scope = payload.get("scope")
                if scope == "global":
                    target = policy["global"]
                elif scope == "kind":
                    target = next((item for item in policy["kinds"]
                                   if item["kind"] == payload.get("kind")), None)
                else:
                    target = None
                if not target:
                    return self.json_reply({"ok": False, "error": "unknown policy target"}, 400)
                if int(payload.get("expected_revision", -1)) != int(target["revision"]):
                    return self.json_reply({"ok": False,
                        "error": "notification policy changed; refresh and try again"}, 409)
                patch = payload.get("patch") or {}
                if scope == "kind" and patch.get("mode", target.get("mode")) == "repeat":
                    interval = int(patch.get("repeat_interval_seconds",
                                             target["repeat_interval_seconds"]))
                    maximum = int(patch.get("max_deliveries", target["max_deliveries"]))
                    if min(maximum, 1 + 86400 // max(1, interval)) > 12 and \
                            payload.get("confirm_aggressive") is not True:
                        return self.json_reply({"ok": False,
                            "error": "this rule can send more than 12 pushes per day; confirm the high cadence"}, 409)
                target.update(copy.deepcopy(patch), revision=int(target["revision"]) + 1,
                              updated_at=time.time())
                STATE["actions"].append({"type": "notification_policy", "scope": scope,
                    "kind": payload.get("kind"), "patch": copy.deepcopy(patch),
                    "apply_current": payload.get("apply_current") is True})
                return self.json_reply(copy.deepcopy(policy))
            if route == "/api/push/device-settings":
                device = STATE["push_devices"].get(str(payload.get("device_id") or ""))
                if not device:
                    return self.json_reply({"ok": False, "error": "device not registered"})
                if "display_name" in payload:
                    device["display_name"] = str(payload["display_name"])[:80]
                if "enabled" in payload:
                    device["enabled"] = payload["enabled"] is True
                    device["health"] = "registered" if device["enabled"] else "disabled"
                if "preferences" in payload:
                    device["preferences"] = copy.deepcopy(payload["preferences"])
                return self.json_reply({"ok": True, "device": copy.deepcopy(device)})
            if route == "/api/push/test":
                device = STATE["push_devices"].get(str(payload.get("device_id") or ""))
                if not device or not device.get("enabled"):
                    return self.json_reply({"ok": False, "error": "device not available"})
                device["health"] = "healthy"
                device["last_success_at"] = time.time()
                STATE["actions"].append({"type": "push_test", "device_id": device["id"]})
                return self.json_reply({"ok": True, "delivery": {"id": "push-fixture",
                    "event_id": "evt-fixture", "device_id": device["id"],
                    "generation": 1, "status": "queued", "attempt": 0}})
            if route == "/api/legacy-ntfy/test":
                if STATE["settings"].get("legacy_ntfy_enabled") is not True:
                    return self.json_reply(
                        {"ok": False, "error": "Legacy ntfy is disabled"}, 409)
                STATE["actions"].append({"type": "legacy_ntfy_test"})
                return self.json_reply({"ok": True, "queued": True})
            if route == "/api/notifications/read":
                device_id = str(payload.get("device_id") or "default")
                cursor = int(payload.get("cursor") or 0)
                STATE["notification_read_cursors"][device_id] = max(
                    cursor, STATE["notification_read_cursors"].get(device_id, 0))
                STATE["actions"].append({"type": "notifications_read",
                    "device_id": device_id, "cursor": cursor})
                return self.json_reply({"ok": True,
                    "cursor": STATE["notification_read_cursors"][device_id]})
            if route in ("/api/notifications/snooze", "/api/notifications/wake",
                         "/api/notifications/mute"):
                event = next((item for item in STATE["notification_events"]
                    if item["id"] == payload.get("event_id")), None)
                if not event or event["source_revision"] != payload.get("source_revision"):
                    return self.json_reply({"ok": False,
                        "error": "notification changed; refresh and try again"}, 409)
                if route == "/api/notifications/snooze":
                    event.update(state="snoozed", snoozed_until=float(payload.get("until") or 0),
                        changed_at=time.time())
                    result = {"ok": True, "event": copy.deepcopy(event)}
                elif route == "/api/notifications/wake":
                    event.update(state="active", snoozed_until=None, changed_at=time.time())
                    result = {"ok": True, "event": copy.deepcopy(event)}
                else:
                    sid = event.get("session_id")
                    if not sid:
                        return self.json_reply({"ok": False,
                            "error": "notification has no session"}, 409)
                    muted = payload.get("muted") is True
                    sessions = set(STATE["muted_notification_sessions"])
                    sessions.add(sid) if muted else sessions.discard(sid)
                    STATE["muted_notification_sessions"] = sorted(sessions)
                    for session in STATE["sessions"]:
                        if session["session_id"] == sid:
                            session["muted"] = muted
                    result = {"ok": True, "session_id": sid, "muted": muted}
                STATE["actions"].append({"type": route.rsplit("/", 1)[-1],
                    "event_id": event["id"]})
                return self.json_reply(result)
            if route == "/api/notifications/retry":
                delivery_id = str(payload.get("delivery_id") or "")
                before = len(STATE["notification_delivery_problems"])
                STATE["notification_delivery_problems"] = [item for item in
                    STATE["notification_delivery_problems"] if item["id"] != delivery_id]
                if len(STATE["notification_delivery_problems"]) == before:
                    return self.json_reply({"ok": False,
                        "error": "delivery is no longer retryable"}, 409)
                STATE["actions"].append({"type": "notification_retry",
                    "delivery_id": delivery_id})
                return self.json_reply({"ok": True, "delivery": {"id": delivery_id,
                    "status": "queued"}})
            if route == "/api/search/rebuild":
                STATE["actions"].append({"type": "search_rebuild"})
                return self.json_reply({"ok": True, "rebuilding": True})
            if route == "/api/settings":
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("mute_session")), None)
                if session:
                    session["muted"] = bool(payload.get("muted"))
                for key in ("reader_width", "preview_sessions", "preview_session_lines",
                            "preview_agents", "preview_agent_lines", "stall_seconds",
                            "legacy_ntfy_enabled"):
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
                dismiss = payload.get("dismiss_action")
                if dismiss:
                    current = {item["action_id"]: item for item in fleet()["actions"]}
                    action = current.get(dismiss.get("action_id"))
                    if (not action or action.get("session_id") != dismiss.get("session_id") or
                            not action.get("dismissible")):
                        return self.json_reply({"ok": False,
                            "error": "stale or ineligible attention action"})
                    STATE["dismissed_actions"][dismiss["action_id"]] = time.time()
                    payload["dismissed_actions"] = copy.deepcopy(STATE["dismissed_actions"])
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
                            "cancellable": True, "retryable": False,
                            "cancelled": False, "deletable": True,
                            "cancelled_at": None, "cancelled_from_state": None,
                            "cancelled_from_label": None}
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
                                    cancellable=False, retryable=False, cancelled=True,
                                    deletable=False, cancelled_at=time.time(),
                                    cancelled_from_state=item["state"],
                                    cancelled_from_label=item["state_label"],
                                    updated_at=time.time())
                    elif typ == "outbox_delete":
                        if item["state"] in ("spawning", "sending"):
                            return self.json_reply({"ok": False,
                                "error": "a message being delivered cannot be deleted"})
                        previous_state, previous_label = item["state"], item["state_label"]
                        if previous_state in ("scheduled", "waiting_availability",
                                              "waiting_usage_reset", "waiting_provider"):
                            item["state"] = "cancelled"
                        item.update(state_label=("Cancelled" if item["state"] == "cancelled"
                                                 else previous_label),
                                    editable=False, cancellable=False, retryable=False,
                                    cancelled=True, deletable=False, cancelled_at=time.time(),
                                    cancelled_from_state=previous_state,
                                    cancelled_from_label=previous_label, updated_at=time.time())
                    elif typ == "outbox_send_now":
                        item.update(state="sent", state_label="Sent", editable=False,
                                    cancellable=False, retryable=False, deletable=True,
                                    sent_at=time.time(),
                                    provider_receipt={"accepted": True}, updated_at=time.time())
                    elif typ in ("outbox_retry", "outbox_retarget"):
                        patch = payload.get("patch") or {}
                        now = time.time()
                        item = {**item, **patch, "id": f"fixture-out-{len(STATE['outbox']) + 1}",
                            "retry_of": item["id"], "created_at": now, "updated_at": now,
                            "state": "waiting_availability", "state_label": "Waiting for availability",
                            "editable": True, "cancellable": True, "retryable": False,
                            "cancelled": False, "deletable": True, "cancelled_at": None,
                            "cancelled_from_state": None, "cancelled_from_label": None,
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
                    if STATE.get("scenario") == "spawn-failure":
                        time.sleep(.2)
                        return self.json_reply({"ok": False,
                                                "error": "fixture startup rejected"})
                    if STATE.get("scenario") == "spawn-slow":
                        time.sleep(.5)
                    exact = "codex:new" if payload.get("provider") == "codex" else "claude-new"
                    spawned = base_session(payload.get("provider") or "claude",
                                           exact, "New coding session")
                    if spawned["provider"] == "claude":
                        spawned["permission_mode"] = payload.get("permission_mode") or "default"
                    STATE["sessions"].append(spawned)
                    STATE.setdefault("contexts", {})[exact] = []
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
                if payload.get("type") == "worktree_cleanup":
                    if payload.get("cleanup_ticket") != "fixture-cleanup-ticket":
                        return self.json_reply({"ok": False, "error": "cleanup preview expired"})
                    if STATE.get("fail_cleanup"):
                        return self.json_reply({"ok": False,
                            "error": "fixture Git removal failed", "preserved": True,
                            "worktree": "/Users/test/fleet-dash-worktrees/close-me"})
                    STATE["worktree_removed"] = True
                    return self.json_reply({"ok": True, "removed": True,
                        "forced": payload.get("force") is True,
                        "worktree": "/Users/test/fleet-dash-worktrees/close-me",
                        "branch_preserved": True})
                if not session:
                    return self.json_reply({"ok": False, "error": "stale session"})
                typ = payload.get("type")
                if typ == "dismiss_then_send":
                    pending = session.get("pending") or {}
                    if pending.get("kind") != "question" or \
                            payload.get("nonce") != pending.get("nonce"):
                        return self.json_reply({"ok": False,
                                                "error": "stale request"})
                    if STATE.get("fail_dismiss"):
                        return self.json_reply({"ok": False,
                                                "error": "provider rejected dismiss"})
                    session.update(state="turn_done", reg_status="idle", pending=None)
                    request_id = payload.get("client_request_id")
                    item = next((row for row in STATE["outbox"]
                                 if row.get("idempotency_key") == request_id), None)
                    if not item:
                        now = time.time()
                        item = {"id": f"fixture-out-{len(STATE['outbox']) + 1}",
                            "created_at": now, "updated_at": now,
                            "created_zone": "UTC", "local_time": None,
                            "trigger_fold": None, "kind": "when_available",
                            "state": "waiting_availability",
                            "state_label": "Waiting for availability",
                            "message": payload.get("text"),
                            "target_provider": session.get("provider"),
                            "target_session_id": session.get("session_id"),
                            "target_agent_id": None, "trigger_at": None,
                            "usage_account_id": None, "usage_window_id": None,
                            "observed_reset_at": None, "spawn_spec": None,
                            "destination_session_id": None, "provider_receipt": None,
                            "sent_at": None, "error": None,
                            "blocked_reason": "Question dismissed; waiting for the session to become available",
                            "retry_of": None, "origin": "automatic_fallback",
                            "idempotency_key": request_id,
                            "image_count": len(payload.get("upload_ids") or []),
                            "version": 1, "editable": True,
                            "cancellable": True, "retryable": False}
                        STATE["outbox"].append(item)
                    return self.json_reply({"ok": True, "queued": True,
                        "dismissed": True, "dismissed_nonce": payload.get("nonce"),
                        "outbox_id": item["id"], "queue_state": item["state"],
                        "message": "Queued · waiting for session",
                        "queue_reason": item["blocked_reason"]})
                if typ == "send_message":
                    if STATE.get("fail_text"):
                        return self.json_reply({"ok": False,
                                                "error": "terminal rejected input"})
                    immediately_steerable = (session.get("provider") == "codex" and
                        session.get("control_state") == "connected_active")
                    if session.get("state") in ("running", "stalled", "needs_you") \
                            and not immediately_steerable:
                        request_id = payload.get("client_request_id")
                        item = next((row for row in STATE["outbox"]
                                     if row.get("idempotency_key") == request_id), None)
                        if not item:
                            now = time.time()
                            item = {"id": f"fixture-out-{len(STATE['outbox']) + 1}",
                                "created_at": now, "updated_at": now,
                                "created_zone": "UTC", "local_time": None,
                                "trigger_fold": None, "kind": "when_available",
                                "state": "waiting_availability",
                                "state_label": "Waiting for availability",
                                "message": payload.get("text"),
                                "target_provider": session.get("provider"),
                                "target_session_id": session.get("session_id"),
                                "target_agent_id": None, "trigger_at": None,
                                "usage_account_id": None, "usage_window_id": None,
                                "observed_reset_at": None, "spawn_spec": None,
                                "destination_session_id": None, "provider_receipt": None,
                                "sent_at": None, "error": None,
                                "blocked_reason": "Waiting for the target to become available",
                                "retry_of": None, "origin": "automatic_fallback",
                                "idempotency_key": request_id,
                                "image_count": len(payload.get("upload_ids") or []),
                                "version": 1, "editable": True,
                                "cancellable": True, "retryable": False}
                            STATE["outbox"].append(item)
                        return self.json_reply({"ok": True, "queued": True,
                            "outbox_id": item["id"], "queue_state": item["state"],
                            "message": "Queued · waiting for session",
                            "queue_reason": item["blocked_reason"]})
                if typ == "close_preview":
                    secondary = "fleet-dash-worktrees" in session.get("cwd", "")
                    if not secondary:
                        return self.json_reply({"ok": True, "secondary_worktree": False,
                            "remove_allowed": False, "force_remove_allowed": False})
                    dirty = STATE.get("scenario") != "close-worktree-clean"
                    shared = ([{"session_id": "codex:thread-one", "provider": "codex",
                                "title": "Codex parity work"}]
                              if STATE.get("scenario") == "close-worktree-shared" else [])
                    return self.json_reply({"ok": True, "secondary_worktree": True,
                        "inspect_ok": True, "root": "/Users/test/fleet-dash",
                        "worktree": session["cwd"], "branch": session["branch"],
                        "revision": "fixture-close-revision",
                        "cleanup_ticket": "fixture-cleanup-ticket", "dirty": dirty,
                        "dirty_counts": {"staged": 1 if dirty else 0,
                            "unstaged": 1 if dirty else 0, "untracked": 1 if dirty else 0,
                            "conflicts": 0}, "dirty_total": 3 if dirty else 0,
                        "dirty_files": ([
                            {"path": "engine.py", "category": "staged", "status": "M."},
                            {"path": "static/js/cards.js", "category": "unstaged", "status": ".M"},
                            {"path": "notes.txt", "category": "untracked", "status": "??"}]
                            if dirty else []),
                        "ignored_count": 0,
                        "ignored_files": [],
                        "shared_sessions": shared,
                        "remove_allowed": not dirty and not shared,
                        "force_remove_allowed": dirty and not shared,
                        "reason": ("Another live Fleet session is using this worktree."
                                   if shared else "The worktree contains files that removal would erase."
                                   if dirty else None)})
                if typ in ("text", "send_message"):
                    if STATE.get("fail_text"):
                        return self.json_reply({"ok": False, "error": "terminal rejected input"})
                    session.update(state="running", reg_status="running")
                    session["capabilities"].update(
                        interrupt=True, focus_terminal=False,
                        change_model_effort=False,
                        change_model_effort_reason="Available when the turn is idle",
                        focus_terminal_mode=None)
                elif typ == "interrupt":
                    session.update(state="turn_done", reg_status="idle")
                    session["capabilities"].update(
                        interrupt=False, focus_terminal=False,
                        change_model_effort=True, change_model_effort_reason="",
                        focus_terminal_mode=None)
                elif typ == "mode":
                    session["collaboration_mode"] = payload.get("mode")
                    return self.json_reply({"ok": True, "mode": payload.get("mode")})
                elif typ == "session_settings":
                    if not session.get("capabilities", {}).get("change_model_effort"):
                        return self.json_reply({"ok": False,
                            "error": "model and effort can change only while idle"})
                    expected = (str(payload.get("expected_model") or ""),
                                str(payload.get("expected_effort") or ""))
                    current = (str(session.get("model") or ""),
                               str(session.get("effort") or ""))
                    if expected != current:
                        return self.json_reply({"ok": False,
                            "error": "settings changed in another view — refresh and try again"})
                    catalogs = {
                        "claude": {"sonnet": ["low", "high"], "opus": ["medium"]},
                        "codex": {"gpt-5.4": ["medium", "high"],
                                  "gpt-5.3-codex": ["medium", "high"]},
                    }
                    model = str(payload.get("model") or "")
                    effort = str(payload.get("effort") or "")
                    if model not in catalogs.get(session.get("provider"), {}):
                        return self.json_reply({"ok": False, "error": "unknown model"})
                    if effort not in catalogs[session["provider"]][model]:
                        return self.json_reply({"ok": False, "error": "unsupported effort"})
                    session.update(model=model, effort=effort)
                    session["status_line"].update(model=model, effort=effort)
                    return self.json_reply({"ok": True, "model": model, "effort": effort})
                elif typ == "permission_mode":
                    if not session.get("capabilities", {}).get("change_permission_mode"):
                        return self.json_reply({"ok": False, "error": "Claude is not idle"})
                    if payload.get("mode") not in session.get("permission_modes", []):
                        return self.json_reply({"ok": False, "error": "mode unavailable"})
                    session["permission_mode"] = payload.get("mode")
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
