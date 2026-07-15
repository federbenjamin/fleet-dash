#!/usr/bin/env python3
"""Paid live smoke: isolated Claude discovery, send, question, stop, focus, mute."""
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
    with urllib.request.urlopen(req, timeout=35) as response:
        return json.load(response)


def assistant_text(sid):
    context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
    return "\n".join(str(item.get("text") or "") for item in context.get("messages", [])
                     if item.get("role") == "assistant"), context


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    before = request("/api/fleet")
    existing = {item["session_id"] for item in before["sessions"]
                if item.get("provider") == "claude"}
    spawned = request("/api/act", {"type": "spawn", "provider": "claude",
        "cwd": BASE, "model": "haiku", "effort": "low", "worktree": False}, token)
    assert spawned["ok"], spawned
    sid = None
    session = None
    for _ in range(120):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next((item for item in fleet["sessions"]
                        if item.get("provider") == "claude"
                        and item["session_id"] not in existing
                        and os.path.realpath(item.get("cwd") or "") == os.path.realpath(BASE)), None)
        if session:
            sid = session["session_id"]
            break
    assert sid, "spawned Claude session was not independently discovered"
    cleanup = {"done": False}

    def close_created_session():
        if cleanup["done"]:
            return
        try:
            fleet = request("/api/fleet")
            current = next((item for item in fleet["sessions"]
                            if item["session_id"] == sid), None)
            if current and current.get("pending"):
                request("/api/act", {"type": "dismiss", "session_id": sid,
                    "nonce": current["pending"]["nonce"]}, token)
                time.sleep(0.5)
            if current and current.get("state") == "running":
                request("/api/act", {"type": "interrupt", "session_id": sid}, token)
                time.sleep(0.5)
            request("/api/act", {"type": "text", "session_id": sid,
                                  "text": "/exit"}, token)
        except Exception:
            pass
        cleanup["done"] = True

    atexit.register(close_created_session)
    focused = request("/api/act", {"type": "focus", "session_id": sid}, token)
    assert focused["ok"], focused

    sent = request("/api/act", {"type": "text", "session_id": sid,
        "text": "Reply exactly CLAUDE_OK. Do not use tools."}, token)
    assert sent["ok"], sent
    text = ""
    context = None
    for _ in range(180):
        time.sleep(0.5)
        text, context = assistant_text(sid)
        fleet = request("/api/fleet")
        session = next((item for item in fleet["sessions"] if item["session_id"] == sid), None)
        if session and session["state"] != "running" and "CLAUDE_OK" in text:
            break
    assert "CLAUDE_OK" in text, text
    assert session and session["capabilities"]["exact_cost"] is True
    assert session["cost"] > 0 and session["ctx_tokens"] > 0, session

    muted = request("/api/settings", {"mute_session": sid, "muted": True}, token)
    assert muted["ok"], muted
    for _ in range(20):
        time.sleep(0.25)
        fleet = request("/api/fleet")
        session = next(item for item in fleet["sessions"] if item["session_id"] == sid)
        if session["muted"]:
            break
    assert session["muted"] is True
    unmuted = request("/api/settings", {"mute_session": sid, "muted": False}, token)
    assert unmuted["ok"], unmuted

    asked = request("/api/act", {"type": "text", "session_id": sid,
        "text": "Use AskUserQuestion now. Ask one question with header Scope and exactly "
                "two options. After the answer, reply exactly CLAUDE_QUESTION_OK."}, token)
    assert asked["ok"], asked
    pending = None
    for _ in range(180):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next(item for item in fleet["sessions"] if item["session_id"] == sid)
        if (session.get("pending") or {}).get("kind") == "question":
            pending = session["pending"]
            break
    assert pending, session
    question = pending["questions"][0]
    answered = request("/api/act", {"type": "option", "session_id": sid,
        "nonce": pending["nonce"], "digits": [1],
        "n_options": len(question["options"])}, token)
    assert answered["ok"], answered
    for _ in range(180):
        time.sleep(0.5)
        text, _ = assistant_text(sid)
        if "CLAUDE_QUESTION_OK" in text:
            break
    assert "CLAUDE_QUESTION_OK" in text, text

    running = request("/api/act", {"type": "text", "session_id": sid,
        "text": "Run exactly this Bash command: GUARD_OK=sleep-poll sleep 30. "
                "After it finishes reply CLAUDE_INTERRUPT_FAILED."}, token)
    assert running["ok"], running
    for _ in range(60):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next(item for item in fleet["sessions"] if item["session_id"] == sid)
        if session["state"] == "running":
            break
    assert session["state"] == "running", session
    stopped = request("/api/act", {"type": "interrupt", "session_id": sid}, token)
    assert stopped["ok"], stopped
    for _ in range(60):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next(item for item in fleet["sessions"] if item["session_id"] == sid)
        if session["state"] != "running":
            break
    text, _ = assistant_text(sid)
    assert "CLAUDE_INTERRUPT_FAILED" not in text
    close_created_session()
    print(json.dumps({"ok": True, "discovery": True, "send": True,
                      "focus": True, "question": True, "interrupt": True,
                      "usage_cost": True, "mute": True,
                      "closed": True}, indent=2))


if __name__ == "__main__":
    main()
