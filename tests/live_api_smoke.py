#!/usr/bin/env python3
"""Non-mutating live HTTP contract smoke test."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


ROOT = "http://127.0.0.1:8377"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(path, token=None):
    headers = {"X-Act-Token": token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(ROOT + path, headers=headers), timeout=20) as r:
        return r.status, r.read(), r.headers.get_content_type()


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    status, html, kind = get("/")
    assert status == 200 and kind == "text/html"
    assert b"new coding session" in html and b"Codex CLI" in html

    status, raw, kind = get("/api/fleet")
    fleet = json.loads(raw)
    assert status == 200 and kind == "application/json"
    assert fleet["providers"]["codex"]["ok"] is True
    assert fleet["models_by_provider"]["codex"]
    assert any(s.get("provider") == "claude" for s in fleet["sessions"])
    codex = next(s for s in fleet["sessions"] if s.get("provider") == "codex")

    query = urllib.parse.urlencode({"sid": codex["session_id"]})
    assert json.loads(get("/api/context?" + query)[1])["ok"] is True
    commands = json.loads(get("/api/commands?" + query, token)[1])
    assert commands["ok"] and any(c["name"] == "/model" for c in commands["commands"])
    insights = json.loads(get("/api/insights?days=7")[1])
    assert insights["ok"] is True

    try:
        urllib.request.urlopen(urllib.request.Request(ROOT + "/api/act", data=b'{"type":"ping"}',
            headers={"Content-Type": "application/json"}), timeout=20)
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError("unauthenticated /api/act was accepted")
    print(json.dumps({"ok": True, "sessions": len(fleet["sessions"]),
                      "codex_models": len(fleet["models_by_provider"]["codex"]),
                      "codex_commands": len(commands["commands"])}, indent=2))


if __name__ == "__main__":
    main()
