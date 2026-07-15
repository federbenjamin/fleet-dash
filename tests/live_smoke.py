#!/usr/bin/env python3
"""Live Fleet Dash smoke test. Creates a Codex thread but starts no paid turn."""
import json
import os
import time
import atexit
import urllib.parse
import urllib.request


ROOT = "http://127.0.0.1:8377"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def request(path, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Act-Token"] = token
    req = urllib.request.Request(ROOT + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        assert response.status == 200, (path, response.status)
        return json.load(response)


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]

    fleet = None
    for _ in range(30):
        fleet = request("/api/fleet")
        if (fleet["providers"]["codex"]["ok"] and
                fleet["models_by_provider"]["codex"]):
            break
        time.sleep(0.5)
    assert fleet is not None
    assert fleet["providers"]["claude"]["ok"] is True
    assert fleet["providers"]["codex"]["ok"] is True, fleet["providers"]["codex"]
    assert fleet["models_by_provider"]["codex"], "Codex models were not discovered"
    assert all("provider" in session for session in fleet["sessions"])

    assert request("/api/act", {"type": "ping"}, token)["ok"] is True
    spawned = request("/api/act", {"type": "spawn", "provider": "codex",
                                    "cwd": BASE, "model": "", "effort": "",
                                    "mode": "plan"}, token)
    assert spawned["ok"] is True, spawned
    sid = spawned["session_id"]
    assert sid.startswith("codex:")
    cleanup = {"done": False}

    def archive_created_thread():
        if cleanup["done"]:
            return
        try:
            request("/api/act", {"type": "archive", "session_id": sid}, token)
        except Exception:
            pass
        cleanup["done"] = True

    atexit.register(archive_created_thread)

    session = None
    for _ in range(15):
        time.sleep(1)
        fleet = request("/api/fleet")
        session = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        if session:
            break
    assert session, f"spawned thread {sid} never appeared"
    assert session["provider"] == "codex"
    assert session["cost_source"] == "unavailable"
    assert session["capabilities"]["submit"] is True
    assert session["capabilities"]["focus_terminal"] is False
    assert session["collaboration_mode"] == "plan", session
    assert session["capabilities"]["answer_structured"] is False
    assert session["model"], session
    codex_usage = (fleet.get("provider_usage") or {}).get("codex") or {}
    assert codex_usage.get("buckets"), codex_usage

    changed = request("/api/act", {"type": "mode", "session_id": sid,
                                    "mode": "default"}, token)
    assert changed == {"ok": True, "mode": "default"}, changed
    changed = request("/api/act", {"type": "mode", "session_id": sid,
                                    "mode": "plan"}, token)
    assert changed == {"ok": True, "mode": "plan"}, changed

    query = urllib.parse.urlencode({"sid": sid})
    context = request("/api/context?" + query)
    assert context["ok"] is True, context
    commands = request("/api/commands?" + query, token=token)
    assert commands["ok"] is True
    names = {item["name"] for item in commands["commands"]}
    assert {"/compact", "/review"} <= names, names
    archived = request("/api/act", {"type": "archive", "session_id": sid}, token)
    assert archived["ok"], archived
    cleanup["done"] = True
    print(json.dumps({"ok": True,
                      "codex_models": len(fleet["models_by_provider"]["codex"]),
                      "commands": len(commands["commands"]), "archived": True}, indent=2))


if __name__ == "__main__":
    main()
