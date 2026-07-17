#!/usr/bin/env python3
"""Authenticated, read-only smoke for the canonical notification stream."""
import json
import os
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("FLEET_URL", "http://127.0.0.1:8377").rstrip("/")


def request(headers=None):
    req = urllib.request.Request(BASE + "/api/notifications?device=live-smoke&limit=20",
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.status, json.loads(response.read())


def main():
    try:
        request()
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, exc.code
    else:
        raise AssertionError("notifications endpoint must require authentication")

    with open(os.path.join(ROOT, "config.json")) as handle:
        token = json.load(handle).get("act_token")
    assert token
    status, result = request({"X-Act-Token": token})
    assert status == 200 and result.get("ok") is True, result
    assert isinstance(result.get("events"), list)
    raw = json.dumps(result).lower()
    for forbidden in ("subscription_json", "p256dh", '"auth"', "endpoint_origin",
                      '"event_key"', '"source_id"', '"reminder_budget"',
                      '"last_push_at"', '"payload_json"'):
        assert forbidden not in raw, forbidden
    print("live notifications smoke: ok · events", len(result["events"]),
          "· unread", result.get("unread"), "· active", result.get("active"))


if __name__ == "__main__":
    main()
