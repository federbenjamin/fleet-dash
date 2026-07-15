#!/usr/bin/env python3
"""Paid live smoke: native file artifact and compact action, with cleanup."""
import atexit
import json
import os
import time
import urllib.parse
import urllib.request


ROOT = "http://127.0.0.1:8377"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELATIVE_ARTIFACT = "test-results/live-codex-artifact.md"
ARTIFACT = os.path.join(BASE, RELATIVE_ARTIFACT)


def request(path, payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Act-Token"] = token
    req = urllib.request.Request(ROOT + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def file_bytes(sid, path, token):
    query = urllib.parse.urlencode({"sid": sid, "p": path})
    req = urllib.request.Request(ROOT + "/api/file?" + query,
                                 headers={"X-Act-Token": token})
    with urllib.request.urlopen(req, timeout=30) as response:
        assert response.status == 200
        return response.read()


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    spawned = request("/api/act", {"type": "spawn", "provider": "codex",
        "cwd": BASE, "model": "", "effort": "", "mode": "default"}, token)
    assert spawned["ok"], spawned
    sid = spawned["session_id"]
    cleanup = {"done": False}

    def clean():
        if not cleanup["done"]:
            try:
                request("/api/act", {"type": "archive", "session_id": sid}, token)
            except Exception:
                pass
            cleanup["done"] = True
        try:
            os.remove(ARTIFACT)
        except FileNotFoundError:
            pass

    atexit.register(clean)
    os.makedirs(os.path.dirname(ARTIFACT), exist_ok=True)
    try:
        os.remove(ARTIFACT)
    except FileNotFoundError:
        pass

    prompt = (f"Use the apply_patch tool to create {RELATIVE_ARTIFACT} with exactly this "
              "Markdown content: a heading 'Fleet live artifact', a blank line, then "
              "ARTIFACT_OK. Do not edit any other file. Reply exactly FILE_OK when done.")
    sent = request("/api/act", {"type": "text", "session_id": sid,
                                 "text": prompt}, token)
    assert sent["ok"], sent
    context = None
    session = None
    artifact = None
    assistant_text = ""
    for _ in range(240):
        time.sleep(0.5)
        fleet = request("/api/fleet")
        session = next((item for item in fleet["sessions"]
                        if item["session_id"] == sid), None)
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        assistant_text = "\n".join(str(item.get("text") or "")
                                   for item in context.get("messages", [])
                                   if item.get("role") == "assistant")
        artifact = next((item for item in context.get("files", [])
                         if item.get("path") == ARTIFACT), None)
        if (session and session["state"] != "running" and artifact
                and "FILE_OK" in assistant_text):
            break
    assert session and session["state"] != "running", session
    assert "FILE_OK" in assistant_text, assistant_text
    assert artifact, context
    assert artifact["delivered"] is False
    assert artifact["source"] == "codex"
    body = file_bytes(sid, ARTIFACT, token)
    assert b"Fleet live artifact" in body and b"ARTIFACT_OK" in body

    compacted = request("/api/act", {"type": "compact", "session_id": sid}, token)
    assert compacted["ok"], compacted
    saw_compaction = False
    for _ in range(120):
        time.sleep(0.5)
        context = request("/api/context?" + urllib.parse.urlencode({"sid": sid}))
        saw_compaction = any(item.get("kind") == "compact"
                             for item in context.get("messages", []))
        if saw_compaction:
            break
    assert saw_compaction, context
    clean()
    print(json.dumps({"ok": True, "artifact_preview": True,
                      "native_compact": True, "archived": True}, indent=2))


if __name__ == "__main__":
    main()
