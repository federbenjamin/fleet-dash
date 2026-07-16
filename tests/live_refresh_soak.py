#!/usr/bin/env python3
"""Bounded, read-only concurrent refresh soak for a running Fleet Dash daemon."""
import concurrent.futures
import json
import os
import time
import urllib.parse
import urllib.request


ROOT = os.environ.get("FLEET_DASH_URL", "http://127.0.0.1:8377").rstrip("/")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUNDS = max(1, min(100, int(os.environ.get("FLEET_DASH_SOAK_ROUNDS", "20"))))
WORKERS = max(1, min(16, int(os.environ.get("FLEET_DASH_SOAK_WORKERS", "8"))))


def request(path, token=None):
    headers = {"X-Act-Token": token} if token else {}
    started = time.perf_counter()
    with urllib.request.urlopen(
            urllib.request.Request(ROOT + path, headers=headers), timeout=20) as response:
        payload = json.loads(response.read())
    return path.split("?", 1)[0], payload, (time.perf_counter() - started) * 1000


def main():
    with open(os.path.join(BASE, "config.json")) as handle:
        token = json.load(handle)["act_token"]
    _, fleet, _ = request("/api/fleet")
    sessions = fleet.get("sessions") or []
    assert any(item.get("provider") == "claude" for item in sessions), fleet
    assert fleet.get("providers", {}).get("codex") is not None, fleet
    sid = sessions[0]["session_id"] if sessions else ""
    routes = [
        ("/api/fleet", None),
        ("/api/insights?days=7", None),
        ("/api/history?limit=100", None),
        ("/api/briefing?device=live-refresh-soak&limit=20", None),
        ("/api/search?q=fleet&limit=10", token),
    ]
    if sid:
        routes.append(("/api/context?" + urllib.parse.urlencode({"sid": sid}), None))
    jobs = routes * ROUNDS
    timings = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(request, path, auth) for path, auth in jobs]
        for future in concurrent.futures.as_completed(futures):
            route, payload, elapsed = future.result()
            assert payload.get("ok", True) is True, {"route": route, "payload": payload}
            timings.setdefault(route, []).append(elapsed)
    _, final_fleet, _ = request("/api/fleet")
    assert any(item.get("provider") == "claude"
               for item in final_fleet.get("sessions") or []), final_fleet
    print(json.dumps({
        "ok": True,
        "requests": len(jobs),
        "workers": WORKERS,
        "routes": {route: {"samples": len(values),
                           "max_ms": round(max(values), 3)}
                   for route, values in sorted(timings.items())},
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
