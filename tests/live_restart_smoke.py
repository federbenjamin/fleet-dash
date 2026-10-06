#!/usr/bin/env python3
"""Paid live smoke: restart Fleet Dash during a Codex turn and recover it."""
import atexit
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import sys
import urllib.request


ROOT = "http://127.0.0.1:8377"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from fleetdash import launchd


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
    for _ in range(120):
        try:
            ready = request("/api/fleet")
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
            continue
        if ready["providers"]["codex"]["ok"]:
            break
        time.sleep(0.5)
    else:
        raise AssertionError("Fleet Dash did not become ready")
    spawned = request("/api/act", {"type": "spawn", "provider": "codex",
        "cwd": BASE, "model": "", "effort": "", "mode": "default"}, token)
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
        "text": "Run exactly this Bash command: GUARD_OK=sleep-poll sleep 45. "
                "After it finishes reply PRE_RESTART_FINISHED."}, token)
    assert sent["ok"], sent
    for _ in range(60):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next((item for item in fleet["sessions"]
                        if item["session_id"] == sid), None)
        if session and session["state"] == "running":
            break
    assert session and session["state"] == "running", session

    subprocess.run(["launchctl", "kickstart", "-k",
                    f"gui/{os.getuid()}/{launchd.label('production')}"], check=True)
    for _ in range(120):
        time.sleep(0.5)
        try:
            fleet = request("/api/fleet")
        except (OSError, urllib.error.URLError):
            continue
        session = next((item for item in fleet["sessions"]
                        if item["session_id"] == sid), None)
        if (fleet["providers"]["claude"]["ok"] and fleet["providers"]["codex"]["ok"]
                and session):
            break
    assert fleet["providers"]["claude"]["ok"], fleet["providers"]
    assert fleet["providers"]["codex"]["ok"], fleet["providers"]
    assert session, f"managed thread {sid} disappeared after restart"

    recovered = request("/api/act", {"type": "text", "session_id": sid,
        "text": "Reply exactly RESTART_OK. Do not use tools."}, token)
    assert recovered["ok"], recovered
    assistant_text = ""
    for _ in range(180):
        time.sleep(0.5)
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        assistant_text = "\n".join(str(item.get("text") or "")
                                   for item in context.get("messages", [])
                                   if item.get("role") == "assistant")
        fleet = request("/api/fleet")
        session = next((item for item in fleet["sessions"]
                        if item["session_id"] == sid), None)
        if session and session["state"] != "running" and "RESTART_OK" in assistant_text:
            break
    assert "RESTART_OK" in assistant_text, assistant_text
    archive_created_thread()
    print(json.dumps({"ok": True, "restart_during_turn": True,
                      "both_providers_recovered": True, "thread_recovered": True,
                      "archived": True}, indent=2))


if __name__ == "__main__":
    main()
