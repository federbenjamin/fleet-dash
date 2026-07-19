"""Read-only observation of Codex rollout events owned by another runtime.

Fleet's supported interactive Codex surface remains App Server. ChatGPT Desktop
and VS Code use independent runtimes, so App Server can list their threads while
reporting them ``notLoaded`` and omitting current turns. This module supplies a
strictly observational fallback for explicitly tracked external threads. It
never creates, resumes, mutates, or takes ownership of a Codex thread.

Rollout JSONL is a local implementation detail rather than a public protocol.
Only a small, defensive event allowlist is consumed. Unknown additions are
counted and ignored; malformed rows remain visible as an observation warning.
"""
from collections import deque
import glob
import json
import os
import re
import threading
import time


THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,79}")
MAX_ROW_BYTES = 2 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
MAX_READ_BYTES_PER_OBSERVE = 4 * 1024 * 1024
MAX_READ_SECONDS_PER_OBSERVE = 0.05


def _epoch(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class CodexRolloutObserver:
    """Incrementally fold lifecycle/messages for external Codex threads."""

    def __init__(self, root=None, clock=None, max_messages=300):
        self.root = os.path.realpath(root or os.path.expanduser("~/.codex/sessions"))
        self.clock = clock or time.time
        self.max_messages = max(10, int(max_messages))
        self._entries = {}
        self._paths = {}
        self._lock = threading.RLock()

    def _path(self, thread_id):
        if not THREAD_ID.fullmatch(str(thread_id or "")):
            return None
        cached = self._paths.get(thread_id)
        if cached and os.path.isfile(cached):
            return cached
        pattern = os.path.join(self.root, "**", f"*-{thread_id}.jsonl")
        paths = [os.path.realpath(path) for path in glob.glob(pattern, recursive=True)]
        paths = [path for path in paths
                 if os.path.commonpath((self.root, path)) == self.root
                 and os.path.basename(path).endswith(f"-{thread_id}.jsonl")]
        if not paths:
            return None
        path = max(paths, key=lambda item: os.path.getmtime(item))
        self._paths[thread_id] = path
        return path

    def _fresh(self, path, stat):
        return {"path": path, "inode": (stat.st_dev, stat.st_ino), "offset": 0,
                "remainder": b"", "discarding_oversized": False,
                "messages": deque(maxlen=self.max_messages),
                "active": False, "turn_id": None, "started_at": None,
                "completed_at": None, "last_activity_at": None,
                "revision": 0, "malformed_rows": 0, "oversized_rows": 0,
                "unknown_events": 0, "error": None}

    @staticmethod
    def _message(entry, role, text, timestamp, phase=None):
        text = str(text or "").strip()
        if not text:
            return
        item = {"role": role, "text": text}
        if timestamp:
            item["ts"] = timestamp
        if phase:
            item["phase"] = phase
        # Some Codex versions write the same visible message through adjacent
        # event shapes. Deduplicate only an exact adjacent duplicate.
        if entry["messages"] and all(entry["messages"][-1].get(key) == item.get(key)
                                     for key in ("role", "text", "phase")):
            return
        entry["messages"].append(item)

    def _event(self, entry, row):
        timestamp = row.get("timestamp")
        at = _epoch(timestamp)
        payload = row.get("payload") or {}
        typ = payload.get("type")
        if at is not None:
            entry["last_activity_at"] = max(entry.get("last_activity_at") or 0, at)
        if typ == "task_started":
            entry.update(active=True, turn_id=payload.get("turn_id"),
                         started_at=at, completed_at=None)
        elif typ in ("task_complete", "turn_aborted"):
            entry.update(active=False, turn_id=payload.get("turn_id") or entry.get("turn_id"),
                         completed_at=_epoch(payload.get("completed_at")) or at)
        elif typ == "user_message":
            self._message(entry, "user", payload.get("message"), timestamp)
        elif typ == "agent_message":
            self._message(entry, "assistant", payload.get("message"), timestamp,
                          payload.get("phase"))
        elif typ not in {"token_count", "context_compacted", "patch_apply_end",
                         "mcp_tool_call_end", "web_search_end", "item_completed",
                         "thread_settings_applied"}:
            entry["unknown_events"] += 1

    def _line(self, entry, raw):
        if len(raw) > MAX_ROW_BYTES:
            entry["oversized_rows"] += 1
            return
        # Visible messages and lifecycle are event_msg rows. session_meta is
        # intentionally ignored for now; its path/config payload is not needed.
        if b'"type":"event_msg"' not in raw:
            return
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            entry["malformed_rows"] += 1
            return
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            entry["malformed_rows"] += 1
            return
        self._event(entry, row)

    def observe(self, thread_id):
        """Fold new complete rows and return a bounded serializable snapshot."""
        path = self._path(thread_id)
        if not path:
            return None
        try:
            stat = os.stat(path)
            with self._lock:
                entry = self._entries.get(thread_id)
                replaced = (not entry or entry.get("path") != path or
                            entry.get("inode") != (stat.st_dev, stat.st_ino) or
                            stat.st_size < entry.get("offset", 0))
                if replaced:
                    entry = self._entries[thread_id] = self._fresh(path, stat)
                rows_seen = 0
                bytes_left = MAX_READ_BYTES_PER_OBSERVE
                deadline = time.monotonic() + MAX_READ_SECONDS_PER_OBSERVE
                with open(path, "rb") as handle:
                    handle.seek(entry["offset"])
                    while bytes_left > 0 and time.monotonic() < deadline:
                        chunk = handle.read(min(READ_CHUNK_BYTES, bytes_left))
                        if not chunk:
                            break
                        entry["offset"] += len(chunk)
                        bytes_left -= len(chunk)

                        # Once a row crosses MAX_ROW_BYTES, retain no more of it.
                        # Scan bounded chunks until its newline instead of joining
                        # an attacker-sized append into one bytes object first.
                        if entry["discarding_oversized"]:
                            newline = chunk.find(b"\n")
                            if newline < 0:
                                continue
                            entry["discarding_oversized"] = False
                            rows_seen += 1
                            chunk = chunk[newline + 1:]
                            if not chunk:
                                continue

                        data = entry["remainder"] + chunk
                        lines = data.split(b"\n")
                        entry["remainder"] = lines.pop() if lines else data
                        for raw in lines:
                            rows_seen += 1
                            if raw:
                                self._line(entry, raw)
                        if len(entry["remainder"]) > MAX_ROW_BYTES:
                            entry["oversized_rows"] += 1
                            entry["remainder"] = b""
                            entry["discarding_oversized"] = True
                if rows_seen:
                    entry["revision"] += rows_seen
                entry["error"] = None
                return self._snapshot(thread_id, entry)
        except (OSError, ValueError) as exc:
            with self._lock:
                entry = self._entries.get(thread_id)
                if entry:
                    entry["error"] = str(exc)
                    return self._snapshot(thread_id, entry)
            return {"thread_id": thread_id, "error": str(exc), "messages": []}

    def _snapshot(self, thread_id, entry):
        warnings = []
        if entry.get("malformed_rows"):
            warnings.append(f"{entry['malformed_rows']} malformed rollout rows ignored")
        if entry.get("oversized_rows"):
            warnings.append(f"{entry['oversized_rows']} oversized rollout rows ignored")
        return {"thread_id": thread_id, "active": bool(entry.get("active")),
                "turn_id": entry.get("turn_id"), "started_at": entry.get("started_at"),
                "completed_at": entry.get("completed_at"),
                "last_activity_at": entry.get("last_activity_at"),
                "messages": [dict(item) for item in entry["messages"]],
                "revision": f"rollout:{entry.get('inode')}:{entry.get('offset')}:{entry.get('revision')}",
                "warning": "; ".join(warnings) or None, "error": entry.get("error"),
                "confidence": "observed_local_rollout"}
