#!/usr/bin/env python3
"""Deterministic HTTP fixture for the repository-native browser suite."""
import copy
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
            "activity_at": time.time() - 3, "pinned": False,
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
    return {"sessions": [claude, codex], "closed": [], "actions": [],
            "contexts": {"claude-one": copy.deepcopy(context),
                         "codex:thread-one": copy.deepcopy(context)},
            "scenario": "base", "codex_error": None,
            "settings": {"awaiting_input_notify_seconds": 180, "stall_seconds": 240,
                "spend_threshold_usd": 5, "fleet_quiet_minutes": 0, "dashboard_url": "",
                "preview_sessions": True, "preview_session_lines": 2,
                "preview_agents": False, "preview_agent_lines": 1,
                "reader_width": "fit", "pinned_sessions": []},
            "reply_available": {}, "read_sessions": {}}


STATE = fresh_state()


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
                "agents_total": 0, "closed_at": int(time.time()) - 60,
                "first_seen": int(time.time()) - 3600, "bridge_url": None},
                *copy.deepcopy(STATE["closed"])]]
    return {"t": time.time(), "sessions": sessions,
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
                        "active": True, "five_hour_pct": 20, "weekly_pct": 30},
                        {"id": "two", "email": "second@example.com", "active": False,
                         "five_hour_pct": 4, "weekly_pct": 8}]},
                "codex": {"provider": "codex", "email": "codex@example.com",
                    "plan_type": "pro",
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
            "notify": {"needs_you": True, "stall": True, "spend": True,
                       "fleet_quiet": True},
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
            if route == "/api/fleet":
                return self.json_reply(fleet())
            if route == "/api/context":
                sid = (query.get("sid") or [""])[0]
                return self.json_reply({"ok": True,
                    "messages": copy.deepcopy(STATE.get("contexts", {}).get(sid, [])),
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
            if route in ("/api/act", "/api/settings") and not authorized(self):
                return self.json_reply({"ok": False, "error": "bad or missing act token"}, 403)
            if route == "/api/settings":
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("mute_session")), None)
                if session:
                    session["muted"] = bool(payload.get("muted"))
                for key in ("reader_width", "preview_sessions", "preview_session_lines",
                            "preview_agents", "preview_agent_lines"):
                    if key in payload:
                        STATE["settings"][key] = payload[key]
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
                STATE["actions"].append(payload)
                return self.json_reply({"ok": True, **payload})
            if route == "/api/act":
                if payload.get("type") == "ping":
                    return self.json_reply({"ok": True})
                STATE["actions"].append(payload)
                session = next((item for item in STATE["sessions"]
                                if item["session_id"] == payload.get("session_id")), None)
                if payload.get("type") == "spawn":
                    return self.json_reply({"ok": True, "session_id": "codex:new",
                                            "cwd": payload.get("cwd")})
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
