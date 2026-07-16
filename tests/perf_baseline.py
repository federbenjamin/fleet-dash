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


def request_json(url, timeout=60, headers=None, payload=None):
    started = time.perf_counter()
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=request_headers, data=body)
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
    parser.add_argument("--action-samples", type=int, default=10,
                        help="authenticated, non-mutating ping samples")
    parser.add_argument("--assert-contract", action="store_true",
                        help="fail when routine local p95 exceeds 250 ms")
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

    context_times, context_server_times, context_sizes = [], [], []
    if sid:
        url = base + "/api/context?" + urllib.parse.urlencode({"sid": sid})
        for _ in range(max(1, args.context_samples)):
            _, elapsed, size, response_headers = request_json(url)
            context_times.append(elapsed)
            context_server_times.append(server_duration(response_headers))
            context_sizes.append(size)

    agent_target = next(((session.get("session_id"), agent.get("agent_id"))
                         for session in (fleet or {}).get("sessions") or []
                         for agent in session.get("agents") or []
                         if session.get("session_id") and agent.get("agent_id")), None)
    agent_context_times, agent_context_server_times, agent_context_sizes = [], [], []
    if agent_target:
        url = base + "/api/agent_context?" + urllib.parse.urlencode(
            {"sid": agent_target[0], "aid": agent_target[1]})
        for _ in range(max(1, args.context_samples)):
            _, elapsed, size, response_headers = request_json(url)
            agent_context_times.append(elapsed)
            agent_context_server_times.append(server_duration(response_headers))
            agent_context_sizes.append(size)

    history_times, history_server_times, history_sizes = [], [], []
    for _ in range(max(0, args.history_samples)):
        _, elapsed, size, response_headers = request_json(base + "/api/history?limit=100")
        history_times.append(elapsed)
        history_server_times.append(server_duration(response_headers))
        history_sizes.append(size)

    search_times, search_server_times, search_sizes, search_status = [], [], [], None
    headers = {}
    if args.local_action_auth:
        try:
            with open(os.path.join(ROOT, "config.json")) as handle:
                token = json.load(handle).get("act_token")
        except (OSError, ValueError, TypeError):
            token = None
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

    action_times, action_server_times = [], []
    if headers:
        for _ in range(max(0, args.action_samples)):
            _, elapsed, _, response_headers = request_json(
                base + "/api/act", headers=headers, payload={"type": "ping"})
            action_times.append(elapsed)
            action_server_times.append(server_duration(response_headers))

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
                    "server_p95_ms": percentile(context_server_times, .95),
                    "response_bytes_p50": percentile(context_sizes, .50)},
        "engine": (fleet or {}).get("diagnostics") or {},
    }
    if agent_context_times:
        out["agent_context"] = {
            "session_id": agent_target[0], "agent_id": agent_target[1],
            "samples": len(agent_context_times),
            "p50_ms": percentile(agent_context_times, .50),
            "p95_ms": percentile(agent_context_times, .95),
            "server_p95_ms": percentile(agent_context_server_times, .95),
            "response_bytes_p50": percentile(agent_context_sizes, .50),
        }
    if history_times:
        out["history"] = {"samples": len(history_times),
                          "p50_ms": percentile(history_times, .50),
                          "p95_ms": percentile(history_times, .95),
                          "server_p95_ms": percentile(history_server_times, .95),
                          "response_bytes_p50": percentile(history_sizes, .50)}
    if diagnostics is not None:
        out["diagnostics"] = diagnostics
    if action_times:
        out["action_ping"] = {"samples": len(action_times),
                              "p50_ms": percentile(action_times, .50),
                              "p95_ms": percentile(action_times, .95),
                              "server_p95_ms": percentile(action_server_times, .95)}
    if search_times:
        out["search"] = {"query": args.search_query, "samples": len(search_times),
                         "p50_ms": percentile(search_times, .50),
                         "p95_ms": percentile(search_times, .95),
                         "server_p95_ms": percentile(search_server_times, .95),
                         "response_bytes_p50": percentile(search_sizes, .50),
                         "status": search_status}
    if not args.skip_corpus:
        out["corpus"] = corpus(["~/.claude/projects", "~/.codex/sessions"])
    contract = {}
    for name in ("fleet", "context", "agent_context", "history", "search", "action_ping"):
        measurement = out.get(name)
        if measurement and measurement.get("p95_ms") is not None:
            contract[name] = {"budget_ms": 250,
                              "p95_ms": measurement["p95_ms"],
                              "pass": measurement["p95_ms"] < 250}
    out["contract"] = contract
    print(json.dumps(out, indent=2, sort_keys=True))
    if args.assert_contract:
        undersampled = [name for name, measurement in out.items()
                        if name in contract and measurement.get("samples", 0) < 10]
        failed = [name for name, result in contract.items() if not result["pass"]]
        if undersampled or failed:
            raise SystemExit("latency contract failed: " + "; ".join(filter(None, [
                "need at least 10 samples for " + ", ".join(undersampled)
                if undersampled else "",
                "p95 >= 250 ms for " + ", ".join(failed) if failed else ""])))


if __name__ == "__main__":
    main()
