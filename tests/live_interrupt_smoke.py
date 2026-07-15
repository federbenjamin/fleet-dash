#!/usr/bin/env python3
"""Paid live smoke: interrupt an active Codex turn through Fleet Dash."""
import json
import os
import sys
import time
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
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    sid = sys.argv[1]
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    prompt = "Use the sleep tool for 30 seconds. After it completes, reply INTERRUPT_FAILED."
    assert request("/api/act", {"type": "text", "session_id": sid, "text": prompt}, token)["ok"]
    running = False
    for _ in range(30):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next(s for s in fleet["sessions"] if s["session_id"] == sid)
        if session["state"] == "running":
            running = True
            break
    assert running, "turn never entered running state"
    stopped = request("/api/act", {"type": "interrupt", "session_id": sid}, token)
    assert stopped["ok"], stopped
    for _ in range(30):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next(s for s in fleet["sessions"] if s["session_id"] == sid)
        if session["state"] != "running":
            break
    assert session["state"] != "running", session
    context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
    recent = "\n".join((m.get("text") or "") for m in context["messages"][-3:]
                       if m.get("role") == "assistant")
    assert "INTERRUPT_FAILED" not in recent, recent
    print(json.dumps({"ok": True, "state": session["state"]}, indent=2))


if __name__ == "__main__":
    main()
