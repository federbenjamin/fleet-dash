#!/usr/bin/env python3
"""Paid live smoke: Codex subagent discovery and conversation retrieval."""
import json
import os
import sys
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
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and not sys.argv[1].startswith("codex:")):
        raise SystemExit("usage: live_subagent_smoke.py codex:THREAD_ID")
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    created = len(sys.argv) == 1
    if created:
        model = os.environ.get("FLEET_DASH_CODEX_SUBAGENT_MODEL", "")
        spawned = request("/api/act", {"type": "spawn", "provider": "codex",
            "cwd": BASE, "model": model, "effort": "", "mode": "default"}, token)
        assert spawned["ok"], spawned
        sid = spawned["session_id"]
    else:
        sid = sys.argv[1]
    cleanup = {"done": not created}

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
        "text": "You must call the spawn_agent tool exactly once; do not simulate or skip the "
                "tool call. Tell that child to reply exactly AGENT_OK without using tools. Wait "
                "for the real child result. Only after receiving it, reply exactly PARENT_OK. "
                "If spawn_agent is unavailable, reply exactly SUBAGENT_UNAVAILABLE instead."}, token)
    assert sent["ok"], sent

    parent = None
    unsupported_since = None
    for _ in range(150):
        time.sleep(1)
        fleet = request("/api/fleet")
        parent = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        text = "\n".join(str(m.get("text") or "") for m in context.get("messages", [])
                         if m.get("role") == "assistant")
        if "SUBAGENT_UNAVAILABLE" in text:
            archive_created_thread()
            print(json.dumps({"ok": True, "supported": False,
                              "reason": "spawn_agent unavailable in this App Server session",
                              "archived": created}, indent=2))
            return
        if parent and not parent.get("agents_total") and "PARENT_OK" in text:
            unsupported_since = unsupported_since or time.time()
            if time.time() - unsupported_since >= 10:
                archive_created_thread()
                print(json.dumps({"ok": True, "supported": False,
                                  "reason": "no subagent lifecycle was emitted",
                                  "archived": created}, indent=2))
                return
        if parent and parent["agents_total"] and "PARENT_OK" in text:
            break
    assert parent and parent["agents_total"] >= 1, parent
    agent = parent["agents"][0]
    query = urllib.parse.urlencode({"sid": sid, "aid": agent["agent_id"]})
    child = request("/api/agent_context?" + query)
    assert child["ok"], child
    child_text = "\n".join(str(m.get("text") or "") for m in child.get("messages", [])
                           if m.get("role") == "assistant")
    assert "AGENT_OK" in child_text, child_text
    for _ in range(60):
        if agent["state"] in ("done", "ended"):
            break
        time.sleep(0.5)
        fleet = request("/api/fleet")
        parent = next((s for s in fleet["sessions"] if s["session_id"] == sid), None)
        if parent and parent.get("agents"):
            agent = next((item for item in parent["agents"]
                          if item["agent_id"] == agent["agent_id"]), agent)
    assert agent["state"] in ("done", "ended"), agent
    archive_created_thread()
    print(json.dumps({"ok": True,
                      "supported": True,
                      "agent_state": agent["state"],
                      "child_messages": len(child["messages"]),
                      "archived": created}, indent=2))


if __name__ == "__main__":
    main()
