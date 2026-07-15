#!/usr/bin/env python3
"""Paid live smoke: Plan mode structured question round-trip through Fleet Dash."""
import json
import os
import time
import atexit
import urllib.request


ROOT = "http://127.0.0.1:8377"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def request(path, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Act-Token"] = token
    req = urllib.request.Request(ROOT + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    spawned = request("/api/act", {"type": "spawn", "provider": "codex",
        "cwd": BASE, "model": "", "effort": "medium", "mode": "plan"}, token)
    assert spawned["ok"], spawned
    sid = spawned["session_id"]
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
    sent = request("/api/act", {"type": "text", "session_id": sid,
        "text": "Use request_user_input now. Ask exactly one question with header Scope "
                "and exactly two options. Do not answer it yourself."}, token)
    assert sent["ok"], sent

    session = None
    for _ in range(120):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        if session and (session.get("pending") or {}).get("kind") == "question":
            break
    assert session, sid
    assert session["collaboration_mode"] == "plan", session
    pending = session.get("pending") or {}
    assert pending.get("kind") == "question", session
    assert len(pending.get("questions") or []) == 1, pending
    question = pending["questions"][0]
    assert len(question.get("options") or []) == 2, question

    answered = request("/api/act", {"type": "option", "session_id": sid,
        "nonce": pending["nonce"], "digits": [1],
        "n_options": len(question["options"])}, token)
    assert answered["ok"], answered
    for _ in range(120):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        if session and not session.get("pending") and session["state"] != "running":
            break
    assert session and not session.get("pending"), session
    archived = request("/api/act", {"type": "archive", "session_id": sid}, token)
    assert archived["ok"], archived
    cleanup["done"] = True
    print(json.dumps({"ok": True,
                      "mode": session["collaboration_mode"],
                      "state": session["state"], "archived": True}, indent=2))


if __name__ == "__main__":
    main()
