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
    return {"sessions": [claude, codex], "closed": [], "actions": [],
            "scenario": "base", "codex_error": None,
            "settings": {"awaiting_input_notify_seconds": 180, "stall_seconds": 240,
                "spend_threshold_usd": 5, "fleet_quiet_minutes": 0, "dashboard_url": "",
                "preview_sessions": True, "preview_session_lines": 2,
                "preview_agents": False, "preview_agent_lines": 1,
                "reader_width": "fit"}}


STATE = fresh_state()


def fleet():
    sessions = copy.deepcopy(STATE["sessions"])
    return {"t": time.time(), "sessions": sessions,
            "totals": {"sessions": len(sessions),
                "busy": sum(item["state"] == "running" for item in sessions),
                "needs_me": sum(item["state"] in ("needs_you", "stalled") for item in sessions),
                "dormant": sum(item["state"] == "dormant" for item in sessions),
                "done": sum(item["state"] == "turn_done" for item in sessions),
                "agents_running": sum(item["agents_running"] for item in sessions),
                "session_cost": .12, "agent_cost": .03, "cost_partial": True},
            "usage": {"five_hour_pct": 20, "weekly_pct": 30,
                      "email": "claude@example.com", "lifetime_tokens": 59_591_487_086,
                      "lifetime_scope": "local_transcripts"},
            "provider_usage": {"claude": {"five_hour_pct": 20, "weekly_pct": 30,
                    "email": "claude@example.com", "lifetime_tokens": 59_591_487_086,
                    "lifetime_scope": "local_transcripts"},
                "codex": {"provider": "codex", "email": "codex@example.com",
                    "plan_type": "pro",
                    "lifetime_tokens": 12345, "buckets": [{"id": "codex:primary",
                    "label": "5-hour", "used_pct": 25, "reset": "2099-01-01T00:00:00Z"},
                    {"id": "codex:secondary", "label": "weekly", "used_pct": 30,
                     "reset": "2099-01-07T00:00:00Z"},
                    {"id": "spark:secondary", "label": "GPT-5.3-Codex-Spark weekly",
                     "used_pct": 0, "reset": "2099-01-07T00:00:00Z"}]}},
            "closed": [{"session_id": "codex:closed", "provider": "codex",
                "title": "Closed Codex", "project": "fleet-dash", "cwd": "/Users/test/fleet-dash",
                "branch": "old", "model": "gpt-5.4", "cost": None, "agent_cost": None,
                "agents_total": 0, "closed_at": int(time.time()) - 60,
                "first_seen": int(time.time()) - 3600, "bridge_url": None},
                *copy.deepcopy(STATE["closed"])],
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


def set_scenario(name):
    global STATE
    STATE = fresh_state()
    STATE["scenario"] = name
    session = codex_session()
    if name == "single-question":
        session.update(state="needs_you", pending={"kind": "question", "nonce": "q1",
            "dismiss_action": "cancel_turn", "questions": [{"header": "Scope",
            "question": "How broad should the change be?", "multiSelect": False,
            "allowOther": True, "options": [{"label": "Focused", "description": "One area"},
                                             {"label": "Full", "description": "All areas"}]}]})
    elif name == "multi-question":
        session.update(state="needs_you", pending={"kind": "question", "nonce": "q2",
            "dismiss_action": "cancel_turn", "questions": [
                {"header": "Targets", "question": "Pick targets", "multiSelect": True,
                 "allowOther": True, "options": [{"label": "Desktop"}, {"label": "Mobile"}]},
                {"header": "Depth", "question": "Choose depth", "multiSelect": False,
                 "allowOther": False, "options": [{"label": "Full"}, {"label": "Small"}]}]})
    elif name == "approval":
        session.update(state="needs_you", pending={"kind": "permission", "nonce": "p1",
            "tool": "command", "approval_kind": "command", "input_summary": "npm test",
            "decisions": ["allow", "always", "deny", "cancel"]})
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
        session["last_msg"] = {"role": "assistant", "text":
            "### Default width\n\nUse **Fit the screen** with `compact code`.\n\n- Fast\n- Clear"}
    elif name == "subagent":
        session.update(state="running", reg_status="running", agents_running=1)
        session["agents"][0]["state"] = "running"
        session["capabilities"]["interrupt"] = True
    elif name == "provider-unavailable":
        STATE["sessions"] = [item for item in STATE["sessions"] if item["provider"] == "claude"]
        STATE["codex_error"] = "codex executable not found"


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
                return self.json_reply({"ok": True, "messages": [
                    {"role": "user", "text": "Audit parity"},
                    {"role": "assistant", "text": "Working through the matrix."},
                    {"role": "event", "kind": "reasoning", "title": "Reasoning",
                     "detail": "Compared protocol states", "level": "info"}],
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
        return self.reply(404, "text/plain", "not found")

    def do_POST(self):
        global STATE
        route = urlparse(self.path).path
        payload = self.body()
        with LOCK:
            if route == "/test/reset":
                set_scenario(payload.get("scenario") or "base")
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
                if not session:
                    return self.json_reply({"ok": False, "error": "stale session"})
                typ = payload.get("type")
                if typ == "text":
                    session.update(state="running", reg_status="running")
                    session["capabilities"]["interrupt"] = True
                elif typ == "interrupt":
                    session.update(state="turn_done", reg_status="idle")
                    session["capabilities"]["interrupt"] = False
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
                    if typ == "option" and not payload.get("digits") and not payload.get("other"):
                        return self.json_reply({"ok": False, "error": "invalid response"})
                    session.update(state="turn_done", pending=None)
                return self.json_reply({"ok": True})
        return self.reply(404, "text/plain", "not found")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8399), Handler).serve_forever()
