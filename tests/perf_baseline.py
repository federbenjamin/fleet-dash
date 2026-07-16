#!/usr/bin/env python3
"""Repeatable read-only latency and local transcript-corpus baseline."""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return round(ordered[index], 3)


def request_json(url, timeout=60, headers=None):
    started = time.perf_counter()
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.headers.items()}
    elapsed = (time.perf_counter() - started) * 1000
    return json.loads(raw), elapsed, len(raw), response_headers


def server_duration(headers):
    value = str((headers or {}).get("server-timing") or "")
    for part in value.split(","):
        if "dur=" not in part:
            continue
        try:
            return float(part.split("dur=", 1)[1].split(";", 1)[0])
        except ValueError:
            pass
    return 0.0


def corpus(roots):
    result = {"sources": 0, "bytes": 0, "rows": 0, "roots": []}
    for root in roots:
        root = os.path.realpath(os.path.expanduser(root))
        item = {"root": root, "sources": 0, "bytes": 0, "rows": 0}
        if not os.path.isdir(root):
            item["missing"] = True
            result["roots"].append(item)
            continue
        for base, _, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(base, name)
                try:
                    size = os.path.getsize(path)
                    rows = 0
                    with open(path, "rb") as handle:
                        while True:
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            rows += chunk.count(b"\n")
                except OSError:
                    continue
                item["sources"] += 1
                item["bytes"] += size
                item["rows"] += rows
        result["sources"] += item["sources"]
        result["bytes"] += item["bytes"]
        result["rows"] += item["rows"]
        result["roots"].append(item)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8377")
    parser.add_argument("--sid", help="session used for /api/context timing")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--context-samples", type=int, default=3)
    parser.add_argument("--search-samples", type=int, default=0)
    parser.add_argument("--history-samples", type=int, default=10)
    parser.add_argument("--search-query", default="parity")
    parser.add_argument("--local-action-auth", action="store_true",
                        help="read Fleet's local action token without printing it")
    parser.add_argument("--skip-corpus", action="store_true")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    fleet_times, fleet_server_times, fleet_sizes, fleet = [], [], [], None
    for _ in range(max(1, args.samples)):
        fleet, elapsed, size, response_headers = request_json(base + "/api/fleet")
        fleet_times.append(elapsed)
        fleet_server_times.append(server_duration(response_headers))
        fleet_sizes.append(size)
    sid = args.sid
    if not sid:
        sessions = (fleet or {}).get("sessions") or []
        sid = sessions[0].get("session_id") if sessions else None

    context_times, context_sizes = [], []
    if sid:
        url = base + "/api/context?" + urllib.parse.urlencode({"sid": sid})
        for _ in range(max(1, args.context_samples)):
            _, elapsed, size, _ = request_json(url)
            context_times.append(elapsed)
            context_sizes.append(size)

    history_times, history_server_times, history_sizes = [], [], []
    for _ in range(max(0, args.history_samples)):
        _, elapsed, size, response_headers = request_json(base + "/api/history?limit=100")
        history_times.append(elapsed)
        history_server_times.append(server_duration(response_headers))
        history_sizes.append(size)

    search_times, search_server_times, search_sizes, search_status = [], [], [], None
    headers = {}
    if args.local_action_auth:
        from engine import load_config
        token = load_config().get("act_token")
        if token:
            headers["Cookie"] = "act_token=" + str(token)
    if args.search_samples:
        search_url = base + "/api/search?" + urllib.parse.urlencode(
            {"q": args.search_query, "limit": 30})
        for _ in range(max(1, args.search_samples)):
            result, elapsed, size, _ = request_json(search_url, headers=headers)
            search_times.append(elapsed)
            search_server_times.append(float(result.get("elapsed_ms") or 0))
            search_sizes.append(size)
        search_status, _, _, _ = request_json(base + "/api/search/status", headers=headers)

    diagnostics = None
    if headers:
        try:
            diagnostics, _, _, _ = request_json(base + "/api/diagnostics", headers=headers)
        except Exception as exc:
            diagnostics = {"ok": False, "error": str(exc)}

    out = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": base,
        "fleet": {"samples": len(fleet_times), "p50_ms": percentile(fleet_times, .50),
                  "p95_ms": percentile(fleet_times, .95),
                  "server_p95_ms": percentile(fleet_server_times, .95),
                  "response_bytes_p50": percentile(fleet_sizes, .50)},
        "context": {"session_id": sid, "samples": len(context_times),
                    "p50_ms": percentile(context_times, .50),
                    "p95_ms": percentile(context_times, .95),
                    "response_bytes_p50": percentile(context_sizes, .50)},
        "engine": (fleet or {}).get("diagnostics") or {},
    }
    if history_times:
        out["history"] = {"samples": len(history_times),
                          "p50_ms": percentile(history_times, .50),
                          "p95_ms": percentile(history_times, .95),
                          "server_p95_ms": percentile(history_server_times, .95),
                          "response_bytes_p50": percentile(history_sizes, .50)}
    if diagnostics is not None:
        out["diagnostics"] = diagnostics
    if search_times:
        out["search"] = {"query": args.search_query, "samples": len(search_times),
                         "p50_ms": percentile(search_times, .50),
                         "p95_ms": percentile(search_times, .95),
                         "server_p95_ms": percentile(search_server_times, .95),
                         "response_bytes_p50": percentile(search_sizes, .50),
                         "status": search_status}
    if not args.skip_corpus:
        out["corpus"] = corpus(["~/.claude/projects", "~/.codex/sessions"])
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
