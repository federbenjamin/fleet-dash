#!/usr/bin/env python3
"""Paid live smoke: one safely denied Codex approval and stale validation."""
import atexit
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
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
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
    prompt = ("Run exactly this shell command: touch /etc/fleet-dash-live-approval. "
              "Do not substitute another path or method. If permission is denied, "
              "reply exactly APPROVAL_DENIED_OK.")
    assert request("/api/act", {"type": "text", "session_id": sid,
                                "text": prompt}, token)["ok"]
    pending = None
    auto_denied = False
    for _ in range(180):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next((item for item in fleet["sessions"]
                        if item["session_id"] == sid), None)
        if session and (session.get("pending") or {}).get("kind") == "permission":
            pending = session["pending"]
            break
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        assistant_text = "\n".join(str(item.get("text") or "")
                                   for item in context.get("messages", [])
                                   if item.get("role") == "assistant")
        if (session and session["state"] != "running"
                and "APPROVAL_DENIED_OK" in assistant_text):
            auto_denied = True
            break
    if auto_denied:
        assert not os.path.exists("/etc/fleet-dash-live-approval")
        archive_created_thread()
        print(json.dumps({"ok": True, "provider_request": False,
                          "auto_denied_before_request": True,
                          "archived": True}, indent=2))
        return
    assert pending, session
    assert pending["approval_kind"] in ("command", "permissions", "file_change")

    invalid = request("/api/act", {"type": "permission", "session_id": sid,
        "nonce": pending["nonce"], "choice": "bogus"}, token)
    assert invalid["ok"] is False, invalid
    fleet = request("/api/fleet")
    session = next(item for item in fleet["sessions"] if item["session_id"] == sid)
    assert (session.get("pending") or {}).get("nonce") == pending["nonce"]

    denied = request("/api/act", {"type": "permission", "session_id": sid,
        "nonce": pending["nonce"], "choice": "deny"}, token)
    assert denied["ok"], denied
    duplicate = request("/api/act", {"type": "permission", "session_id": sid,
        "nonce": pending["nonce"], "choice": "deny"}, token)
    assert duplicate["ok"] is False and "stale" in duplicate["error"].lower(), duplicate

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
        if (session and session["state"] != "running" and not session.get("pending")
                and "APPROVAL_DENIED_OK" in assistant_text):
            break
    assert "APPROVAL_DENIED_OK" in assistant_text, assistant_text
    assert not os.path.exists("/etc/fleet-dash-live-approval")
    archive_created_thread()
    print(json.dumps({"ok": True, "approval_kind": pending["approval_kind"],
                      "invalid_retained": True, "duplicate_stale": True,
                      "denied": True, "archived": True}, indent=2))


if __name__ == "__main__":
    main()
