#!/usr/bin/env python3
"""Non-mutating live HTTP contract smoke test."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import time


ROOT = "http://127.0.0.1:8377"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(path, token=None):
    headers = {"X-Act-Token": token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(ROOT + path, headers=headers), timeout=20) as r:
        return r.status, r.read(), r.headers.get_content_type()


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    status, html, kind = get("/")
    assert status == 200 and kind == "text/html"
    assert b'<div id="appshell">' in html
    assert b'<div id="outboxview">' in html
    assert b'<div id="briefing">' in html
    assert b'<div id="budgets">' in html
    assert b'<script src="/static/app.js"></script>' in html

    fleet = None
    for _ in range(30):
        status, raw, kind = get("/api/fleet")
        fleet = json.loads(raw)
        if (fleet["providers"]["codex"]["ok"] and
                fleet["models_by_provider"]["codex"]):
            break
        time.sleep(0.5)
    assert fleet is not None
    assert status == 200 and kind == "application/json"
    assert fleet["providers"]["codex"]["ok"] is True
    assert fleet["models_by_provider"]["codex"]
    assert any(s.get("provider") == "claude" for s in fleet["sessions"])
    codex = next(s for s in fleet["sessions"] if s.get("provider") == "codex")

    query = urllib.parse.urlencode({"sid": codex["session_id"]})
    assert json.loads(get("/api/context?" + query)[1])["ok"] is True
    commands = json.loads(get("/api/commands?" + query, token)[1])
    names = {c["name"] for c in commands["commands"]}
    assert commands["ok"] and {"/compact", "/review"} <= names, commands
    insights = json.loads(get("/api/insights?days=7")[1])
    assert insights["ok"] is True
    briefing = json.loads(get("/api/briefing?device=live-api-smoke&limit=20")[1])
    assert briefing["ok"] is True
    assert {"attention", "completed", "slow", "outcomes", "budgets",
            "measurements", "reviewed"} <= set(briefing["sections"])
    budget_query = urllib.parse.urlencode({
        "provider": "codex", "model": codex.get("model") or "",
        "project": codex.get("project") or "", "cwd": codex.get("cwd") or "",
    })
    budgets = json.loads(get("/api/budgets?" + budget_query)[1])
    assert budgets["ok"] is True
    assert budgets["spawn_forecast"] is not None
    assert set(budgets["measurement_labels"]) == {
        "exact", "partial", "token_only", "unavailable"}
    assert "budget_summary" in fleet
    assert "scheduled_digest" in fleet["notify"]
    assert "digest_schedule_time" in fleet["settings"]
    assert "digest_schedule_zone" in fleet["settings"]
    for action in fleet.get("actions") or []:
        if action.get("kind") == "budget":
            assert action.get("primary_action") == "view_budget"
            assert action.get("safe_bulk") == []

    try:
        urllib.request.urlopen(urllib.request.Request(ROOT + "/api/act", data=b'{"type":"ping"}',
            headers={"Content-Type": "application/json"}), timeout=20)
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError("unauthenticated /api/act was accepted")
    print(json.dumps({"ok": True, "sessions": len(fleet["sessions"]),
                      "codex_models": len(fleet["models_by_provider"]["codex"]),
                      "codex_commands": len(commands["commands"]),
                      "briefing_unread": briefing["unread"],
                      "budgets": len(budgets["budgets"])}, indent=2))


if __name__ == "__main__":
    main()
