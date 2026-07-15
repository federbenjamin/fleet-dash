#!/usr/bin/env python3
"""Paid live smoke: Codex subagent discovery and conversation retrieval."""
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
    if len(sys.argv) != 2 or not sys.argv[1].startswith("codex:"):
        raise SystemExit("usage: live_subagent_smoke.py codex:THREAD_ID")
    sid = sys.argv[1]
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    sent = request("/api/act", {"type": "text", "session_id": sid,
        "text": "Spawn exactly one subagent. Tell it to reply exactly AGENT_OK without using tools. "
                "Wait for it, then reply exactly PARENT_OK."}, token)
    assert sent["ok"], sent

    parent = None
    for _ in range(150):
        time.sleep(1)
        fleet = request("/api/fleet")
        parent = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        text = "\n".join(str(m.get("text") or "") for m in context.get("messages", []))
        if parent and parent["agents_total"] and "PARENT_OK" in text:
            break
    assert parent and parent["agents_total"] >= 1, parent
    agent = parent["agents"][0]
    query = urllib.parse.urlencode({"sid": sid, "aid": agent["agent_id"]})
    child = request("/api/agent_context?" + query)
    assert child["ok"], child
    child_text = "\n".join(str(m.get("text") or "") for m in child.get("messages", []))
    assert "AGENT_OK" in child_text, child_text
    print(json.dumps({"ok": True, "agent_id": agent["agent_id"],
                      "agent_state": agent["state"],
                      "child_messages": len(child["messages"])}, indent=2))


if __name__ == "__main__":
    main()
