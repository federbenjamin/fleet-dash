#!/usr/bin/env python3
"""Read-only smoke for the running Fleet PWA and redacted push-device APIs."""
import json
import os
import stat
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGIN = "http://127.0.0.1:8377"


def fetch(path, token=None):
    headers = {"X-Act-Token": token} if token else {}
    request = urllib.request.Request(ORIGIN + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()


with open(os.path.join(ROOT, "config.json")) as handle:
    TOKEN = json.load(handle)["act_token"]

status, _, _ = fetch("/api/push/config?device=live-smoke")
assert status == 403, status
status, _, body = fetch("/api/push/config?device=live-smoke", TOKEN)
assert status == 200, status
config = json.loads(body)
assert config["ok"] is True
assert config["configured"] is True
assert isinstance(config["public_key"], str) and 80 <= len(config["public_key"]) <= 100
assert config["delivery"] == "ready", config
assert set(config["helper"]) == {"ready", "restarts", "state"}
assert config["helper"]["ready"] is True
assert set(config["queue"]) == {
    "queued", "failed", "subscription_expired", "oldest_pending_seconds", "statuses"}

status, _, body = fetch("/api/push/devices?device=live-smoke", TOKEN)
assert status == 200, status
devices = json.loads(body)
projected = json.dumps(devices, sort_keys=True).lower()
for secret_name in ("subscription_json", "endpoint_origin", "p256dh", '"auth"', '"endpoint"'):
    assert secret_name not in projected, secret_name

status, _, body = fetch("/api/diagnostics", TOKEN)
assert status == 200, status
diagnostics = json.loads(body)["web_push"]
assert set(diagnostics) == {"configured", "delivery", "helper", "queue"}
assert diagnostics["configured"] is True and diagnostics["delivery"] == "ready"
redacted = json.dumps(diagnostics, sort_keys=True).lower()
for secret_name in ("private", "public_key", "subscription_json", '"subscription":',
                    "endpoint_origin", '"endpoint":', "p256dh", '"auth"'):
    assert secret_name not in redacted, secret_name

secret_path = os.path.join(ROOT, "push-secrets.json")
secret_info = os.lstat(secret_path)
assert stat.S_ISREG(secret_info.st_mode)
assert stat.S_IMODE(secret_info.st_mode) == 0o600

expected = {
    "/static/manifest.webmanifest": "application/manifest+json",
    "/static/offline.html": "text/html",
    "/static/icons/fleet-192.png": "image/png",
    "/static/icons/fleet-512.png": "image/png",
    "/static/icons/fleet-maskable-512.png": "image/png",
    "/sw.js": "text/javascript",
}
for path, content_type in expected.items():
    status, actual, body = fetch(path)
    assert status == 200, (path, status)
    assert actual.startswith(content_type), (path, actual)
    assert body, path

manifest = json.loads(fetch("/static/manifest.webmanifest")[2])
assert manifest["id"] == "/" and manifest["scope"] == "/"
print(json.dumps({"ok": True, "configured": config["configured"],
                  "registered_devices": devices["registered"],
                  "assets": len(expected)}, sort_keys=True))
