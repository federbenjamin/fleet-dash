#!/usr/bin/env python3
"""Paid live Fleet Dash smoke for Codex bootstrap and shared-runtime attachment."""
import json
import os
import sys
import time
import atexit
import urllib.parse
import urllib.request

ROOT = "http://127.0.0.1:8377"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from codex_adapter import (CodexAppServer, UnixWebSocketProcess,
                           codex_control_socket)


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
    context = None
    for _ in range(120):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        has_hi = any(m.get("role") == "user" and m.get("text") == "hi"
                     for m in context.get("messages") or [])
        if (session and has_hi and session["state"] not in
                ("running", "stalled", "needs_you") and
                session["capabilities"]["focus_terminal"]):
            break
    assert session, f"spawned thread {sid} never appeared"
    assert context and any(m.get("role") == "user" and m.get("text") == "hi"
                           for m in context.get("messages") or []), context
    assert session["state"] not in ("running", "stalled", "needs_you"), session
    assert session["provider"] == "codex"
    assert session["cost_source"] == "unavailable"
    assert session["capabilities"]["submit"] is True
    assert session["capabilities"]["focus_terminal"] is True
    assert session["capabilities"]["focus_terminal_mode"] == "attach"
    assert session["headless"] is False
    assert session["read_only"] is False
    assert session["collaboration_mode"] == "plan", session
    assert session["capabilities"]["answer_structured"] is False
    assert session["model"], session
    codex_usage = (fleet.get("provider_usage") or {}).get("codex") or {}
    assert codex_usage.get("buckets"), codex_usage

    # A second client must see and resume the exact same now-idle thread on the
    # canonical Unix runtime. Resuming while a turn is active aborts that turn,
    # which is why Fleet disables Attach until the bootstrap turn completes.
    socket_path = codex_control_socket()
    peer = CodexAppServer(
        command=["unix", socket_path], timeout=8,
        process_factory=lambda *args, **kwargs: UnixWebSocketProcess(socket_path, 8))
    try:
        loaded = peer.loaded_thread_ids()
        loaded_ids = {item.get("id") if isinstance(item, dict) else item for item in loaded}
        assert sid.split(":", 1)[1] in loaded_ids, loaded
        peer_thread = (peer.request("thread/read", {
            "threadId": sid.split(":", 1)[1], "includeTurns": False}).get("thread") or {})
        assert peer_thread["id"] == sid.split(":", 1)[1]
        resumed = peer.resume_thread(sid.split(":", 1)[1])
        assert resumed["id"] == sid.split(":", 1)[1], resumed
    finally:
        peer.close()

    changed = request("/api/act", {"type": "mode", "session_id": sid,
                                    "mode": "default"}, token)
    assert changed == {"ok": True, "mode": "default", "durable": True}, changed
    changed = request("/api/act", {"type": "mode", "session_id": sid,
                                    "mode": "plan"}, token)
    assert changed == {"ok": True, "mode": "plan", "durable": True}, changed

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
                      "commands": len(commands["commands"]), "shared_peer": True,
                      "archived": True}, indent=2))


if __name__ == "__main__":
    main()
