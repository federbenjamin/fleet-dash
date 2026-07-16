#!/usr/bin/env python3
"""Repeatable read-only latency and local transcript-corpus baseline."""
import argparse
import json
import os
import statistics
import time
import urllib.parse
import urllib.request


def percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return round(ordered[index], 3)


def request_json(url, timeout=60):
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read()
    elapsed = (time.perf_counter() - started) * 1000
    return json.loads(raw), elapsed, len(raw)


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
    parser.add_argument("--skip-corpus", action="store_true")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    fleet_times, fleet_sizes, fleet = [], [], None
    for _ in range(max(1, args.samples)):
        fleet, elapsed, size = request_json(base + "/api/fleet")
        fleet_times.append(elapsed)
        fleet_sizes.append(size)
    sid = args.sid
    if not sid:
        sessions = (fleet or {}).get("sessions") or []
        sid = sessions[0].get("session_id") if sessions else None

    context_times, context_sizes = [], []
    if sid:
        url = base + "/api/context?" + urllib.parse.urlencode({"sid": sid})
        for _ in range(max(1, args.context_samples)):
            _, elapsed, size = request_json(url)
            context_times.append(elapsed)
            context_sizes.append(size)

    out = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": base,
        "fleet": {"samples": len(fleet_times), "p50_ms": percentile(fleet_times, .50),
                  "p95_ms": percentile(fleet_times, .95),
                  "response_bytes_p50": percentile(fleet_sizes, .50)},
        "context": {"session_id": sid, "samples": len(context_times),
                    "p50_ms": percentile(context_times, .50),
                    "p95_ms": percentile(context_times, .95),
                    "response_bytes_p50": percentile(context_sizes, .50)},
        "engine": (fleet or {}).get("diagnostics") or {},
    }
    if not args.skip_corpus:
        out["corpus"] = corpus(["~/.claude/projects", "~/.codex/sessions"])
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
