#!/usr/bin/env python3
"""Opt-in live Outbox check that schedules only future work, then cancels it."""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("FLEET_DASH_URL", "http://127.0.0.1:8377")


def request(path, *, token=None, body=None):
    headers = {}
    data = None
    if token:
        headers["X-Act-Token"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.status, json.load(response)


def main():
    with open(os.path.join(ROOT, "config.json")) as handle:
        token = json.load(handle).get("act_token")
    if not token:
        raise AssertionError("live config has no act token")
    _, fleet = request("/api/fleet")
    target = next((item for item in fleet.get("sessions") or []
                   if item.get("access") == "interactive" and
                   (item.get("capabilities") or {}).get("submit")), None)
    if not target:
        print("SKIP: no interactive live session")
        return
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    payload = {"type": "outbox_create", "kind": "at_time",
        "message": "Fleet Dash live Outbox smoke — this record must be cancelled",
        "created_zone": "UTC", "trigger_at": future.isoformat(),
        "target_provider": target.get("provider"),
        "target_session_id": target.get("session_id")}
    _, created = request("/api/act", token=token, body=payload)
    assert created.get("ok"), created
    item = created["item"]
    assert item["state"] == "scheduled", item
    _, listing = request("/api/outbox?state=scheduled&limit=200", token=token)
    assert any(row["id"] == item["id"] for row in listing.get("items") or []), listing
    _, cancelled = request("/api/act", token=token, body={
        "type": "outbox_cancel", "outbox_id": item["id"]})
    assert cancelled.get("ok"), cancelled
    assert cancelled["item"]["state"] == "cancelled", cancelled
    print(json.dumps({"ok": True, "target_provider": target.get("provider"),
                      "state": "cancelled", "message_sent": False}))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, urllib.error.URLError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
