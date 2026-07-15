#!/usr/bin/env python3
"""Paid live smoke: one minimal Codex turn through Fleet Dash."""
import json
import os
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
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    spawned = request("/api/act", {"type": "spawn", "provider": "codex",
                                    "cwd": BASE, "model": "", "effort": ""}, token)
    assert spawned["ok"], spawned
    sid = spawned["session_id"]
    sent = request("/api/act", {"type": "text", "session_id": sid,
                                 "text": "Reply with exactly TEST_OK. Do not use tools."}, token)
    assert sent["ok"], sent

    context = None
    session = None
    for _ in range(90):
        time.sleep(1)
        fleet = request("/api/fleet")
        session = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        text = "\n".join(str(m.get("text") or "") for m in context.get("messages", []))
        if "TEST_OK" in text and session and session["state"] != "running":
            break
    assert context and context.get("ok"), context
    text = "\n".join(str(m.get("text") or "") for m in context["messages"])
    assert "TEST_OK" in text, text
    assert session, sid
    assert session["provider"] == "codex"
    print(json.dumps({"ok": True, "session_id": sid, "state": session["state"],
                      "ctx_tokens": session["ctx_tokens"],
                      "messages": len(context["messages"])}, indent=2))


if __name__ == "__main__":
    main()
