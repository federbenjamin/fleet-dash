#!/usr/bin/env python3
"""Repeatable 100k-message/2k-source benchmark for Fleet transcript search."""
import argparse
import json
import os
import resource
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fleetdash.search_index import SearchIndex


def percentile(values, quantile):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return round(ordered[index], 3)


def timed(call):
    started = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - started) * 1000


def create_corpus(root, sources, messages):
    project = os.path.join(root, "-benchmark-fleet-search")
    os.makedirs(project)
    per_source = max(1, messages // sources)
    written = 0
    for source_index in range(sources):
        path = os.path.join(project, "bench-%05d.jsonl" % source_index)
        with open(path, "w", encoding="utf-8") as handle:
            for message_index in range(per_source):
                if written >= messages:
                    break
                marker = " lighthouse" if written % 20 == 0 else ""
                row = {"type": "user", "timestamp": "2026-07-16T12:00:00Z",
                       "cwd": "/benchmark/fleet-search", "gitBranch": "benchmark",
                       "message": {"role": "user", "content":
                                   "search benchmark message %d%s" % (written, marker)}}
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                written += 1
    return project, written


def drain(index, limit):
    calls = 0
    idle = 0
    while calls < limit:
        calls += 1
        if index.run_once():
            idle = 0
        else:
            idle += 1
            if idle >= 2:
                return calls
    raise RuntimeError("index did not settle")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=int, default=2000)
    parser.add_argument("--messages", type=int, default=100000)
    parser.add_argument("--warm-samples", type=int, default=50)
    parser.add_argument("--cold-samples", type=int, default=10)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        claude = os.path.join(tmp, "claude")
        codex = os.path.join(tmp, "codex")
        os.makedirs(claude)
        os.makedirs(codex)
        project, written = create_corpus(claude, args.sources, args.messages)
        db_path = os.path.join(tmp, "search.db")
        index = SearchIndex(db_path, claude, codex, discover_seconds=60, batch_rows=1000)
        started = time.perf_counter()
        calls = drain(index, args.sources * 3 + 100)
        build_seconds = time.perf_counter() - started

        warm = []
        for _ in range(args.warm_samples):
            result, elapsed = timed(lambda: index.search("lighthouse benchmark", limit=30))
            if not result["ok"] or not result["results"]:
                raise RuntimeError("warm benchmark query returned no results")
            warm.append(elapsed)
        status = index.status()
        index.close()

        cold = []
        for _ in range(args.cold_samples):
            opened = SearchIndex(db_path, claude, codex, discover_seconds=60,
                                 batch_rows=1000)
            result, elapsed = timed(lambda: opened.search("lighthouse benchmark", limit=30))
            opened.close()
            if not result["ok"] or not result["results"]:
                raise RuntimeError("cold benchmark query returned no results")
            cold.append(elapsed)

        index = SearchIndex(db_path, claude, codex, discover_seconds=60, batch_rows=1000)
        append_path = os.path.join(project, "bench-00000.jsonl")
        started = time.perf_counter()
        with open(append_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "timestamp": "2026-07-16T12:01:00Z",
                "message": {"role": "user", "content": "live lag unique canary"}}) + "\n")
        index.last_discovery = 0
        drain(index, args.sources * 2 + 100)
        live_lag_seconds = time.perf_counter() - started
        if not index.search("live lag unique canary")["results"]:
            raise RuntimeError("appended message was not searchable")
        index.close()

        warm_p95 = percentile(warm, .95)
        cold_p95 = percentile(cold, .95)
        out = {"sources": args.sources, "messages": written,
               "build_seconds": round(build_seconds, 3), "worker_calls": calls,
               "documents": status["documents"], "database_bytes": os.path.getsize(db_path),
               "warm_p50_ms": percentile(warm, .5), "warm_p95_ms": warm_p95,
               "cold_p50_ms": percentile(cold, .5), "cold_p95_ms": cold_p95,
               "live_lag_seconds": round(live_lag_seconds, 3),
               "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss /
                                   (1024 * 1024 if os.uname().sysname == "Darwin" else 1024), 1),
               "targets": {"warm_p95_under_75_ms": warm_p95 < 75,
                           "cold_p95_under_150_ms": cold_p95 < 150,
                           "live_lag_under_4_seconds": live_lag_seconds < 4}}
        print(json.dumps(out, indent=2, sort_keys=True))
        if not all(out["targets"].values()):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
