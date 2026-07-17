#!/usr/bin/env python3
"""Read-only live privacy gate for Notification Center and Web Push."""
import json
import os
import stat
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("FLEET_URL", "http://127.0.0.1:8377").rstrip("/")


def get(path, token=None):
    headers = {"X-Act-Token": token} if token else {}
    with urllib.request.urlopen(
            urllib.request.Request(BASE + path, headers=headers), timeout=20) as response:
        return response.status, response.read()


def main():
    config_path = os.path.join(ROOT, "config.json")
    with open(config_path) as stream:
        config = json.load(stream)
    token = str(config.get("act_token") or "")
    assert token
    secrets_to_exclude = [token]
    for key in ("ntfy_topic", "dashboard_url"):
        value = str(config.get(key) or "")
        if value:
            secrets_to_exclude.append(value)

    secret_path = os.path.join(ROOT, "push-secrets.json")
    with open(secret_path) as stream:
        push_secrets = json.load(stream)
    for key in ("vapid_private_key", "action_secret"):
        value = str(push_secrets.get(key) or "")
        if value:
            secrets_to_exclude.append(value)

    public_status, public_fleet = get("/api/fleet")
    assert public_status == 200
    authenticated = []
    for path in ("/api/notifications?device=privacy-gate&limit=50", "/api/push/config",
                 "/api/push/devices", "/api/diagnostics"):
        status, body = get(path, token)
        assert status == 200, path
        authenticated.append(body)
    invalid = urllib.request.Request(
        BASE + "/api/push/capability-action", data=b'{"capability":"invalid"}',
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(invalid, timeout=20)
    except urllib.error.HTTPError as exc:
        assert exc.code == 409
        authenticated.append(exc.read())
    else:
        raise AssertionError("invalid capability must fail")

    responses = b"\n".join([public_fleet, *authenticated])
    log_path = os.path.join(ROOT, "fleet-dash.log")
    with open(log_path, "rb") as stream:
        log = stream.read()
    ledger_path = os.path.join(ROOT, "ledger.db")
    with open(ledger_path, "rb") as stream:
        ledger = stream.read()
    for secret in secrets_to_exclude:
        encoded = secret.encode()
        assert encoded not in responses
        assert encoded not in log
        assert encoded not in ledger

    decoded = json.loads(public_fleet)
    assert "notify" not in decoded
    assert "dashboard_url" not in decoded.get("settings", {})
    for path in (config_path, secret_path, ledger_path, log_path):
        info = os.lstat(path)
        assert stat.S_ISREG(info.st_mode), path
        assert stat.S_IMODE(info.st_mode) == 0o600, path
    for suffix in ("-wal", "-shm"):
        path = ledger_path + suffix
        if os.path.exists(path):
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600, path

    print("live notification privacy: ok · fleet · APIs · errors · log · database · modes")


if __name__ == "__main__":
    main()
