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
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
EFFORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
MAX_ROW_BYTES = 2 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
MAX_READ_BYTES_PER_OBSERVE = 4 * 1024 * 1024
MAX_READ_SECONDS_PER_OBSERVE = 0.05
MAX_CHILDREN_PER_PARENT = 32


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
                "model": None, "effort": None, "token_usage": {},
                "revision": 0, "malformed_rows": 0, "oversized_rows": 0,
                "unknown_events": 0, "error": None}

    @staticmethod
    def _setting(value, pattern):
        value = str(value or "").strip()
        return value if pattern.fullmatch(value) else None

    def _turn_context(self, entry, payload):
        """Keep only the display settings emitted by the local rollout."""
        model = self._setting(payload.get("model"), MODEL_ID)
        effort = self._setting(payload.get("effort"), EFFORT)
        if model:
            entry["model"] = model
        if effort:
            entry["effort"] = effort

    @staticmethod
    def _token_usage(payload):
        info = payload.get("info")
        if not isinstance(info, dict):
            return None

        def normalize(value):
            if not isinstance(value, dict):
                return None
            out = {}
            for source, target in (("input_tokens", "inputTokens"),
                                   ("cached_input_tokens", "cachedInputTokens"),
                                   ("cache_write_input_tokens", "cacheWriteInputTokens"),
                                   ("output_tokens", "outputTokens"),
                                   ("reasoning_output_tokens", "reasoningOutputTokens"),
                                   ("total_tokens", "totalTokens")):
                try:
                    number = int(value.get(source))
                except (TypeError, ValueError, OverflowError):
                    continue
                if 0 <= number <= 10 ** 12:
                    out[target] = number
            return out or None

        usage = {"last": normalize(info.get("last_token_usage")),
                 "total": normalize(info.get("total_token_usage"))}
        try:
            window = int(info.get("model_context_window"))
        except (TypeError, ValueError, OverflowError):
            window = 0
        if 1 <= window <= 2_000_000:
            usage["modelContextWindow"] = window
        return {key: value for key, value in usage.items() if value}

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
        elif typ == "token_count":
            usage = self._token_usage(payload)
            if usage:
                entry["token_usage"] = usage
        elif typ not in {"token_count", "context_compacted", "patch_apply_end",
                         "mcp_tool_call_end", "web_search_end", "item_completed",
                         "thread_settings_applied"}:
            entry["unknown_events"] += 1

    def _line(self, entry, raw):
        if len(raw) > MAX_ROW_BYTES:
            entry["oversized_rows"] += 1
            return
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            entry["malformed_rows"] += 1
            return
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            entry["malformed_rows"] += 1
            return
        if row.get("type") == "event_msg":
            self._event(entry, row)
        elif row.get("type") == "turn_context":
            self._turn_context(entry, row["payload"])

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
                snapshot = self._snapshot(thread_id, entry)
                snapshot["agents"] = self._child_agents(thread_id, path)
                return snapshot
        except (OSError, ValueError) as exc:
            with self._lock:
                entry = self._entries.get(thread_id)
                if entry:
                    entry["error"] = str(exc)
                    return self._snapshot(thread_id, entry)
            return {"thread_id": thread_id, "error": str(exc), "messages": []}

    def _child_agents(self, parent_id, parent_path):
        """Discover sibling rollout files that declare this parent, bounded by day."""
        prefix = f'"parent_thread_id":"{parent_id}"'.encode()
        candidates = sorted(glob.glob(os.path.join(os.path.dirname(parent_path), "rollout-*.jsonl")),
                            key=lambda item: os.path.getmtime(item), reverse=True)
        agents = []
        for path in candidates[:MAX_CHILDREN_PER_PARENT]:
            if os.path.realpath(path) == os.path.realpath(parent_path):
                continue
            try:
                with open(path, "rb") as handle:
                    first = handle.read(16 * 1024)
                if prefix not in first:
                    continue
                match = re.search(br'"id":"([A-Za-z0-9-]{8,80})"', first)
                child_id = match.group(1).decode() if match else None
            except (OSError, UnicodeDecodeError):
                continue
            if not THREAD_ID.fullmatch(str(child_id or "")):
                continue
            child = self.observe(child_id)
            if not child:
                continue
            active = bool(child.get("active"))
            messages = child.get("messages") or []
            last = next((item for item in reversed(messages)
                         if item.get("role") == "assistant" and item.get("text")), None)
            agents.append({"agent_id": "agent-" + child_id, "native_session_id": child_id,
                           "session_id": "codex:" + parent_id, "agent_type": "codex",
                           "description": "Codex subagent", "depth": 0,
                           "model": child.get("model") or "", "family": "codex",
                           "effort": child.get("effort"),
                           "state": "running" if active else "done",
                           "total_tokens": None, "cost": None,
                           "cost_source": "unavailable", "tokens": {}, "spark": [],
                           "tok_per_s": None, "started": child.get("started_at"),
                           "last": child.get("last_activity_at"), "last_msg": last,
                           "convo_v": child.get("revision")})
        return agents

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
                "model": entry.get("model"), "effort": entry.get("effort"),
                "token_usage": dict(entry.get("token_usage") or {}),
                "messages": [dict(item) for item in entry["messages"]],
                "revision": f"rollout:{entry.get('inode')}:{entry.get('offset')}:{entry.get('revision')}",
                "warning": "; ".join(warnings) or None, "error": entry.get("error"),
                "confidence": "observed_local_rollout"}
