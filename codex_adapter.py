#!/usr/bin/env python3
"""Supported Codex CLI integration through ``codex app-server``.

The adapter owns the JSON-RPC process and exposes provider-neutral session,
conversation, and action shapes to Fleet Dash. It deliberately never parses
Codex rollout files; their on-disk representation is not a public API.
"""
import json
import os
import subprocess
import threading
import time
import shutil
import glob
import hashlib
from collections import deque


class CodexError(RuntimeError):
    pass


def codex_command(configured=None):
    """Resolve Codex in interactive shells and launchd's minimal PATH."""
    if configured:
        path = os.path.realpath(os.path.expanduser(configured))
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise CodexError(f"configured Codex executable is not runnable: {path}")
    found = shutil.which("codex")
    if found:
        return found
    candidates = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/codex")),
                        reverse=True)
    if candidates:
        return candidates[0]
    raise CodexError("codex executable not found; set codex_command in config.json")


class CodexAppServer:
    """Small synchronous client over app-server's supported stdio JSONL transport."""

    def __init__(self, command=None, timeout=8, process_factory=None, clock=None):
        self.command = command or [codex_command(), "app-server"]
        self.timeout = timeout
        self.process_factory = process_factory or subprocess.Popen
        self.clock = clock or time.time
        self.proc = None
        self.lock = threading.RLock()
        self.pending = {}
        self.next_id = 1
        self.generation = 0
        self.notifications = deque(maxlen=1000)
        self.diagnostics = deque(maxlen=200)
        self.approvals = {}
        self.thread_state = {}
        self.account_rate_limits = {}
        self.reader = None
        self.last_error = None

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            env = os.environ.copy()
            # Keep the symlink's bin directory: npm installs `codex` beside the
            # matching `node`; resolving the symlink jumps into node_modules.
            command_dir = os.path.dirname(os.path.abspath(self.command[0]))
            env["PATH"] = command_dir + os.pathsep + env.get("PATH", "")
            self.generation += 1
            self.proc = self.process_factory(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, text=True, bufsize=1, env=env)
            self.reader = threading.Thread(target=self._read_loop, daemon=True)
            self.reader.start()
        self.request("initialize", {"clientInfo": {
            "name": "fleet_dash", "title": "Fleet Dash", "version": "0.2.0"},
            "capabilities": {"experimentalApi": True}})
        self.notify("initialized", {})

    def close(self):
        with self.lock:
            proc, self.proc = self.proc, None
            self.generation += 1
            waiters = list(self.pending.values())
            self.pending.clear()
        if proc and proc.poll() is None:
            proc.terminate()
        for waiter in waiters:
            waiter["error"] = {"message": "Codex app-server closed"}
            waiter["event"].set()

    def _send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            self.last_error = str(exc)
            raise CodexError(f"Codex app-server write failed: {exc}") from exc

    def request(self, method, params=None, timeout=None):
        if self.proc is None or self.proc.poll() is not None:
            self.start()
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            waiter = self.pending[rid] = {"event": threading.Event(),
                                          "generation": self.generation,
                                          "method": method}
            self._send({"method": method, "id": rid, "params": params or {}})
        if not waiter["event"].wait(timeout or self.timeout):
            with self.lock:
                self.pending.pop(rid, None)
            raise CodexError(f"Codex app-server timed out: {method}")
        if "error" in waiter:
            raise CodexError(waiter["error"].get("message", str(waiter["error"])))
        return waiter.get("result") or {}

    def notify(self, method, params=None):
        if self.proc is None or self.proc.poll() is not None:
            self.start()
        with self.lock:
            self._send({"method": method, "params": params or {}})

    def respond(self, rid, result):
        with self.lock:
            self._send({"id": rid, "result": result})

    def respond_error(self, rid, message, code=-32601):
        with self.lock:
            self._send({"id": rid, "error": {"code": code, "message": message}})

    def _diagnostic(self, kind, detail):
        with self.lock:
            self.diagnostics.append({"ts": self.clock(), "kind": kind,
                                     "detail": str(detail)[:1000]})

    def _read_loop(self):
        proc = self.proc
        reader_generation = self.generation
        try:
            for raw in proc.stdout:
                if os.environ.get("FLEET_DASH_CODEX_DEBUG"):
                    print(f"codex app-server: {raw.rstrip()}", file=__import__("sys").stderr,
                          flush=True)
                try:
                    msg = json.loads(raw)
                except Exception as exc:
                    self._diagnostic("malformed_json", f"{exc}: {raw.rstrip()[:500]}")
                    continue
                if not isinstance(msg, dict):
                    self._diagnostic("invalid_message", repr(msg))
                    continue
                rid = msg.get("id")
                if rid is not None and ("result" in msg or "error" in msg):
                    with self.lock:
                        waiter = self.pending.pop(rid, None)
                    if waiter:
                        waiter.update(msg)
                        waiter["event"].set()
                    else:
                        self._diagnostic("late_or_duplicate_response", rid)
                    continue
                if rid is not None and msg.get("method"):
                    self._server_request(msg)
                elif msg.get("method"):
                    self._notification(msg["method"], msg.get("params") or {})
        except Exception as exc:
            self.last_error = str(exc)
            self._diagnostic("reader_error", exc)
        finally:
            with self.lock:
                waiter_ids = [rid for rid, waiter in self.pending.items()
                              if waiter.get("generation") == reader_generation]
                waiters = [self.pending.pop(rid) for rid in waiter_ids]
                stale_ids = [key for key, request in self.approvals.items()
                             if request.get("generation") == reader_generation]
                stale_requests = [self.approvals.pop(key) for key in stale_ids]
                for request in stale_requests:
                    tid = request.get("thread_id")
                    if not tid:
                        continue
                    state = self.thread_state.setdefault(tid, {})
                    queue = state.get("pending", [])
                    state["pending"] = [key for key in queue if key not in stale_ids]
                    state["error"] = ("App Server restarted while waiting for a client "
                                      "response; the old request is no longer answerable")
                    state["revision"] = state.get("revision", 0) + 1
            for waiter in waiters:
                waiter["error"] = {"message": "Codex app-server stopped"}
                waiter["event"].set()
            for request in stale_requests:
                self._diagnostic("stale_pending_request", request.get("method"))

    def _server_request(self, msg):
        method, params = msg["method"], msg.get("params") or {}
        supported = ("item/tool/requestUserInput", "mcpServer/elicitation/request",
                     "item/commandExecution/requestApproval",
                     "item/fileChange/requestApproval", "item/permissions/requestApproval",
                     "applyPatchApproval", "execCommandApproval")
        if method in supported:
            thread_id = params.get("threadId")
            key = str(msg["id"])
            with self.lock:
                self.approvals[key] = {"request_id": msg["id"], "method": method,
                                       "thread_id": thread_id, "params": params,
                                       "seen_at": self.clock(), "state": "pending",
                                       "generation": self.generation}
                if thread_id:
                    state = self.thread_state.setdefault(thread_id, {})
                    queue = state.setdefault("pending", [])
                    if key not in queue:
                        queue.append(key)
                    state["revision"] = state.get("revision", 0) + 1
            return
        self._diagnostic("unsupported_server_request", method)
        self.respond_error(msg["id"], f"unsupported client request: {method}")

    def _notification(self, method, params):
        now = self.clock()
        with self.lock:
            self.notifications.append((now, method, params))
            if method == "account/rateLimits/updated":
                self.account_rate_limits = params.get("rateLimits") or {}
                return
            if method == "serverRequest/resolved":
                key = str(params.get("requestId"))
                request = self.approvals.pop(key, None)
                if request and request.get("thread_id"):
                    queue = self.thread_state.setdefault(
                        request["thread_id"], {}).get("pending", [])
                    if key in queue:
                        queue.remove(key)
                return
            thread = params.get("thread") or {}
            tid = params.get("threadId") or thread.get("id")
            if not tid:
                if method in ("error", "warning", "configWarning", "deprecationNotice"):
                    self._diagnostic(method, params.get("message") or params)
                return
            state = self.thread_state.setdefault(tid, {})
            state["updated_at"] = now
            state["revision"] = state.get("revision", 0) + 1
            if method == "turn/started":
                turn = params.get("turn") or {}
                state.update(status="running", turn_id=turn.get("id"), error=None)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status") or "completed"
                state.update(status="idle" if status in ("completed", "interrupted") else "error",
                             turn_id=None, completed_at=now,
                             turn_status=status, error=turn.get("error"))
            elif method == "thread/tokenUsage/updated":
                state["token_usage"] = params.get("tokenUsage") or params.get("usage") or {}
            elif method == "thread/status/changed":
                state["thread_status"] = params.get("status") or {}
            elif method == "thread/settings/updated":
                settings = params.get("threadSettings") or {}
                collaboration = settings.get("collaborationMode") or {}
                state.update(model=settings.get("model") or state.get("model"),
                             effort=settings.get("effort"),
                             collaboration_mode=collaboration.get("mode") or "default")
            elif method in ("item/started", "item/completed"):
                item = params.get("item") or {}
                if item.get("id"):
                    state.setdefault("items", {})[item["id"]] = item
            elif method == "item/agentMessage/delta":
                iid = params.get("itemId")
                if iid:
                    item = state.setdefault("items", {}).setdefault(
                        iid, {"id": iid, "type": "agentMessage", "text": ""})
                    item["text"] = item.get("text", "") + str(params.get("delta") or "")
            elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta",
                            "item/plan/delta"):
                state.setdefault("stream_events", []).append(
                    {"type": method, "text": params.get("delta") or "", "ts": now})
                del state["stream_events"][:-100]
            elif method in ("thread/compacted",):
                state["compacted_at"] = now
                state["compacting"] = None
            elif method in ("error", "warning", "guardianWarning", "configWarning"):
                state["last_notice"] = {"method": method, "params": params, "ts": now}
                if method == "error":
                    state["error"] = params.get("message") or str(params)

    def list_threads(self, limit=100):
        data, cursor = [], None
        while len(data) < limit:
            params = {"limit": min(100, limit - len(data)), "sortKey": "updated_at",
                      "sortDirection": "desc", "useStateDbOnly": False}
            if cursor:
                params["cursor"] = cursor
            result = self.request("thread/list", params)
            data.extend(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return data

    def loaded_thread_ids(self):
        return (self.request("thread/loaded/list", {}).get("data") or [])

    def list_models(self):
        return (self.request("model/list", {"limit": 100}).get("data") or [])

    def list_skills(self, cwd):
        return (self.request("skills/list", {"cwds": [cwd], "forceReload": False})
                .get("data") or [])

    def account_usage(self):
        return self.request("account/usage/read", {})

    def account_limits(self):
        result = self.request("account/rateLimits/read", {})
        with self.lock:
            self.account_rate_limits = result
        return result

    def read_thread(self, thread_id):
        return (self.request("thread/read", {"threadId": thread_id,
                                              "includeTurns": True}).get("thread") or {})

    def start_thread(self, cwd, model=None, effort=None):
        params = {"cwd": cwd}
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        result = self.request("thread/start", params)
        thread = dict(result.get("thread") or {})
        thread["model"] = result.get("model") or model or ""
        thread["effort"] = result.get("reasoningEffort") or effort
        tid = thread.get("id")
        if tid:
            with self.lock:
                self.thread_state.setdefault(tid, {}).update(
                    model=thread["model"], effort=thread["effort"])
        return thread

    def resume_thread(self, thread_id):
        result = self.request("thread/resume", {"threadId": thread_id})
        thread = dict(result.get("thread") or {})
        thread["model"] = result.get("model") or ""
        thread["effort"] = result.get("reasoningEffort")
        with self.lock:
            self.thread_state.setdefault(thread_id, {}).update(
                model=thread["model"], effort=thread["effort"])
        return thread

    def set_mode(self, thread_id, mode, model, effort=None):
        if mode not in ("plan", "default"):
            raise CodexError("unknown Codex collaboration mode")
        if not model:
            raise CodexError("Codex model is unavailable; refresh and try again")
        collaboration = {"mode": mode, "settings": {
            "model": model, "reasoning_effort": effort,
            "developer_instructions": None}}
        result = self.request("thread/settings/update", {
            "threadId": thread_id, "collaborationMode": collaboration})
        with self.lock:
            self.thread_state.setdefault(thread_id, {}).update(
                collaboration_mode=mode, model=model, effort=effort)
        return result or {"mode": mode}

    def start_turn(self, thread_id, text, mode=None, model=None, effort=None, inputs=None):
        params = {"threadId": thread_id,
                  "input": inputs or [{"type": "text", "text": text}]}
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        return self.request("turn/start", params)

    def steer_turn(self, thread_id, text):
        turn_id = self.thread_state.get(thread_id, {}).get("turn_id")
        if not turn_id:
            raise CodexError("Codex thread has no active turn to steer")
        return self.request("turn/steer", {"threadId": thread_id, "turnId": turn_id,
                                            "input": [{"type": "text", "text": text}]})

    def interrupt(self, thread_id):
        turn_id = self.thread_state.get(thread_id, {}).get("turn_id")
        if not turn_id:
            raise CodexError("Codex thread has no active turn")
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def archive(self, thread_id):
        return self.request("thread/archive", {"threadId": thread_id})

    def compact(self, thread_id):
        return self.request("thread/compact/start", {"threadId": thread_id})

    def review(self, thread_id, target=None):
        return self.request("review/start", {"threadId": thread_id,
                                               "target": target or {"type": "uncommittedChanges"}})

    def _resolve_request(self, request, result):
        request["state"] = "responding"
        try:
            self.respond(request["request_id"], result)
        except Exception:
            request["state"] = "pending"
            raise
        key = str(request["request_id"])
        with self.lock:
            self.approvals.pop(key, None)
            tid = request.get("thread_id")
            queue = self.thread_state.get(tid, {}).get("pending", []) if tid else []
            if key in queue:
                queue.remove(key)

    def decide(self, nonce, choice):
        with self.lock:
            approval = self.approvals.get(str(nonce))
        if not approval:
            raise CodexError("stale Codex approval")
        if approval.get("state", "pending") != "pending":
            raise CodexError("Codex approval is already being answered")
        if choice not in ("allow", "always", "deny", "cancel"):
            raise CodexError("unknown approval decision")
        method = approval["method"]
        if method == "item/permissions/requestApproval":
            requested = approval["params"].get("permissions") or {}
            granted = ({k: v for k, v in requested.items() if v is not None}
                       if choice != "deny" else {})
            if choice == "cancel":
                granted = {}
            result = {"permissions": granted,
                      "scope": "session" if choice == "always" else "turn"}
        elif method in ("applyPatchApproval", "execCommandApproval"):
            decision = {"allow": "approved", "always": "approved_for_session",
                        "deny": "denied", "cancel": "abort"}[choice]
            result = {"decision": decision}
        else:
            decision = {"allow": "accept", "always": "acceptForSession",
                        "deny": "decline", "cancel": "cancel"}[choice]
            result = {"decision": decision}
        self._resolve_request(approval, result)
        return {"ok": True}

    def answer_questions(self, nonce, submitted):
        with self.lock:
            request = self.approvals.get(str(nonce))
        if not request or request.get("method") != "item/tool/requestUserInput":
            raise CodexError("stale Codex question")
        if request.get("state", "pending") != "pending":
            raise CodexError("Codex question is already being answered")
        questions = request["params"].get("questions") or []
        if not isinstance(submitted, list) or len(submitted) > len(questions):
            raise CodexError("invalid Codex question response")
        result = {}
        for index, question in enumerate(questions):
            answer = submitted[index] if index < len(submitted) else {}
            options = question.get("options") or []
            values = []
            digits = answer.get("digits") or []
            if not isinstance(digits, list):
                raise CodexError("invalid option selection")
            for digit in digits:
                try:
                    option = options[int(digit) - 1]
                except (ValueError, TypeError, IndexError):
                    continue
                values.append(option.get("label", ""))
            other = str(answer.get("other") or "").strip()
            if other:
                values.append(other)
            if not values:
                raise CodexError("every question needs an answer")
            qid = question.get("id")
            if not qid:
                raise CodexError("question is missing an id")
            result[qid] = {"answers": values}
        self._resolve_request(request, {"answers": result})
        return {"ok": True}

    def answer_elicitation(self, nonce, action, content=None):
        with self.lock:
            request = self.approvals.get(str(nonce))
        if not request or request.get("method") != "mcpServer/elicitation/request":
            raise CodexError("stale Codex elicitation")
        if request.get("state", "pending") != "pending":
            raise CodexError("Codex elicitation is already being answered")
        if action not in ("accept", "decline", "cancel"):
            raise CodexError("unknown elicitation action")
        result = {"action": action}
        if action == "accept":
            if not isinstance(content, dict):
                raise CodexError("accepted elicitation needs structured content")
            result["content"] = content
        self._resolve_request(request, result)
        return {"ok": True}


class CodexAdapter:
    PROVIDER = "codex"

    def __init__(self, enabled=True, client=None, state_path=None, clock=None,
                 stall_seconds=180):
        self.enabled = enabled
        self.client = client or CodexAppServer()
        self.state_path = state_path
        self.clock = clock or time.time
        self.stall_seconds = stall_seconds
        self.error = None
        self.error_at = None
        self._resumed = False
        self._sessions = []
        self._refreshing = False
        self._last_refresh = 0
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self.models = []
        self._account = None
        self._account_error = None
        self._account_error_at = None
        self._last_account_refresh = 0
        self._skills = {}

    @staticmethod
    def key(native_id):
        return f"codex:{native_id}"

    @staticmethod
    def native(key):
        return key.split(":", 1)[1] if str(key).startswith("codex:") else key

    def sessions(self):
        if not self.enabled:
            return []
        with self._lock:
            if not self._refreshing and self.clock() - self._last_refresh >= 2:
                self._refreshing = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return [dict(s) for s in self._sessions]

    def _refresh(self):
        try:
            self._resume_managed()
            threads = self.client.list_threads()
            models = self.client.list_models() if hasattr(self.client, "list_models") else []
            error = None
        except Exception as exc:
            now = self.clock()
            with self._lock:
                self.error = str(exc)
                self.error_at = now
                for session in self._sessions:
                    session["stale"] = True
                    session["stale_reason"] = self.error
                    session["state"] = "stale"
                    session["capabilities"] = {
                        **session.get("capabilities", {}), "submit": False,
                        "interrupt": False, "takeover": False, "close": False}
                self._refreshing = False
                self._last_refresh = now
            return
        now = self.clock()
        self._refresh_account(now)
        persisted = self._state()
        modes = dict(persisted.get("modes") or {})
        managed = set(persisted.get("threads") or [])
        thread_meta = dict(persisted.get("thread_meta") or {})
        out = []
        listed = set()
        for thread in threads:
            tid = thread.get("id")
            if not tid or thread.get("parentThreadId"):
                continue
            listed.add(tid)
            is_managed = tid in managed
            detail_error = None
            if is_managed:
                try:
                    detail = self.client.read_thread(tid)
                    if detail:
                        thread = {**thread, **detail}
                except Exception as exc:
                    detail_error = str(exc)
            live = self.client.thread_state.get(tid, {})
            pending = self._pending(tid, live.get("pending"))
            updated = thread.get("updatedAt") or thread.get("createdAt")
            turn_lifecycle = _latest_turn_lifecycle(thread)
            live_completed = _epoch(live.get("completed_at"))
            completion_times = [value for value in
                                (turn_lifecycle.get("completed_at"), live_completed)
                                if value is not None]
            completed_epoch = max(completion_times) if completion_times else None
            activity_times = [value for value in
                              (_epoch(updated), _epoch(live.get("updated_at")),
                               turn_lifecycle.get("started_at"), completed_epoch)
                              if value is not None]
            # thread.updatedAt can remain stale when ChatGPT desktop owns the turn.
            # Prefer the newest lifecycle evidence instead of the first truthy field.
            updated_epoch = max(activity_times) if activity_times else now
            recorded = thread.get("status") or {}
            recorded_type = recorded.get("type") if isinstance(recorded, dict) else recorded
            flags = set(recorded.get("activeFlags") or []) if isinstance(recorded, dict) else set()
            native_running = live.get("status") == "running" or recorded_type == "active"
            turn_started = turn_lifecycle.get("started_at")
            observed_running = turn_lifecycle.get("active", False) and (
                completed_epoch is None or turn_started is None or turn_started > completed_epoch)
            running = native_running or observed_running
            quiet = max(0, now - updated_epoch)
            if not is_managed:
                state = "reopenable"
            elif detail_error or live.get("error") or recorded_type == "systemError":
                state = "error"
            elif pending or flags.intersection({"waitingOnApproval", "waitingOnUserInput"}):
                state = "needs_you"
            elif running and quiet > self.stall_seconds:
                state = "stalled"
            elif running:
                state = "running"
            elif completed_epoch is not None and 0 <= now - completed_epoch < 90:
                state = "turn_done"
            elif recorded_type == "notLoaded" and quiet > 86400:
                state = "dormant"
            else:
                state = "idle"
            cwd = thread.get("cwd") or ""
            usage = live.get("token_usage") or {}
            mode = live.get("collaboration_mode") or modes.get(tid) or "default"
            model = thread.get("model") or live.get("model") or ""
            effort = thread.get("effort") or live.get("effort")
            ctx_tokens = _usage_total(usage)
            ctx_window = _usage_window(usage)
            files = _files(thread, cwd)
            messages = _conversation(thread)
            agents = _agents(thread, tid)
            for agent in agents:
                try:
                    child = self.client.read_thread(agent["agent_id"])
                    turns = child.get("turns") or []
                    last_status = turns[-1].get("status") if turns else None
                    child_status = child.get("status") or {}
                    child_status = (child_status.get("type") if
                                    isinstance(child_status, dict) else child_status)
                    if last_status:
                        agent["state"] = ("done" if last_status == "completed" else
                                          "ended" if last_status in
                                          ("failed", "interrupted") else "running")
                    elif child_status == "active":
                        agent["state"] = "running"
                    elif child_status == "systemError":
                        agent["state"] = "ended"
                    elif turns and child_status in ("idle", "notLoaded"):
                        agent["state"] = "done"
                    agent["convo_v"] = sum(len(t.get("items") or []) for t in turns)
                    agent["model"] = child.get("model") or agent["model"]
                    child_usage = self.client.thread_state.get(agent["agent_id"], {}).get(
                        "token_usage") or {}
                    if child_usage:
                        agent["total_tokens"] = _usage_total(child_usage)
                        agent["tokens"] = _token_breakdown(child_usage)
                except Exception:
                    pass
            agents_running = sum(a["state"] in ("running", "stalled") for a in agents)
            revision = _revision(thread, live)
            if is_managed and not detail_error:
                self._cache_snapshot(tid, messages, files, revision, thread)
            owned_turn = bool(live.get("turn_id"))
            can_interrupt = (is_managed and owned_turn and
                             state in ("running", "stalled", "needs_you"))
            uncontrolled_active = (state in ("running", "stalled", "needs_you") and
                                   not can_interrupt)
            reg_status = ("running" if state in ("running", "stalled") else
                          "turn_done" if state == "turn_done" else
                          live.get("status") or recorded_type)
            out.append({
                "session_id": self.key(tid), "native_session_id": tid,
                "provider": "codex", "name": thread.get("name") or thread.get("title"),
                "title": thread.get("name") or thread.get("title"),
                "project": os.path.basename(cwd) or cwd or "Codex", "cwd": cwd,
                "branch": (thread.get("gitInfo") or {}).get("branch"),
                "model": model, "family": "codex", "effort": effort,
                "collaboration_mode": mode, "running": None,
                "last_msg": _last_message(messages, thread.get("preview")),
                "state": state, "reg_status": reg_status,
                "quiet_s": round(quiet),
                "ctx_tokens": ctx_tokens,
                "ctx_pct": round(100 * ctx_tokens / ctx_window, 1) if ctx_window else None,
                "cost": None, "cost_source": "unavailable", "bridge_url": None,
                "started_ms": _millis(thread.get("createdAt")), "pending": pending,
                "compacting": live.get("compacting"), "muted": False,
                "convo_v": revision, "files_n": len(files), "agents": agents,
                "agents_running": agents_running, "agents_total": len(agents),
                "agent_cost": None, "stale": False,
                "error": detail_error or live.get("error"),
                "capabilities": {"submit": is_managed and
                    state not in ("error", "stale") and not uncontrolled_active,
                    "interrupt": can_interrupt,
                    "takeover": not is_managed, "archive": is_managed,
                    "close": is_managed and not uncontrolled_active,
                    "compact": is_managed and state not in
                        ("running", "stalled", "needs_you"),
                    "review": is_managed and state not in
                        ("running", "stalled", "needs_you"),
                    "files": bool(files), "focus_terminal": False,
                    "focus_terminal_reason": "Codex App Server has no terminal-focus API",
                    "answer_structured": bool(pending and pending.get("kind") in
                                              ("question", "elicitation")),
                    "decide_approval": bool(pending), "spawn_agent": True,
                    "relay_agent": is_managed and not uncontrolled_active,
                    "relay_agent_direct": False,
                    "account_usage": bool(self._account), "exact_cost": False,
                    "measured_throughput": False},
            })
        # App Server assigns a thread ID before the first turn materializes a
        # rollout. Such a thread is intentionally absent from thread/list.
        for tid in managed - listed:
            meta = thread_meta.get(tid) or {}
            if meta.get("unmaterialized"):
                out.append(self._stub_session(tid, meta, modes.get(tid) or "default"))
        with self._lock:
            self._sessions = out
            self.models = [{"id": m.get("model") or m.get("id"),
                            "name": m.get("displayName") or m.get("model") or m.get("id"),
                            "efforts": [e.get("reasoningEffort") or e.get("effort") or e
                                        for e in (m.get("supportedReasoningEfforts") or [])]}
                           for m in models if m.get("model") or m.get("id")]
            self.error = error
            self.error_at = None
            self._refreshing = False
            self._last_refresh = self.clock()

    def _refresh_account(self, now):
        if now - self._last_account_refresh < 30:
            return
        self._last_account_refresh = now
        try:
            limits = self.client.account_limits()
            tokens = self.client.account_usage()
            account = _account_usage(limits, tokens)
        except Exception as exc:
            with self._lock:
                self._account_error = str(exc)
                self._account_error_at = now
            return
        with self._lock:
            self._account = {**account, "stale": False, "error": None}
            self._account_error = None
            self._account_error_at = None

    def account_usage(self):
        with self._lock:
            if not self._account:
                return ({"provider": "codex", "stale": True,
                         "error": self._account_error}
                        if self._account_error else None)
            out = dict(self._account)
            if self._account_error:
                out.update(stale=True, error=self._account_error,
                           error_at=self._account_error_at)
            return out

    def _managed(self):
        return list(self._state().get("threads") or [])

    def _modes(self):
        return dict(self._state().get("modes") or {})

    def _state(self):
        if not self.state_path:
            return {}
        with self._state_lock:
            try:
                with open(self.state_path) as handle:
                    return json.load(handle) or {}
            except (OSError, ValueError):
                return {}

    def _save_state(self, state):
        if not self.state_path:
            return
        with self._state_lock:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(state, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)

    def _remember(self, tid, mode=None, meta=None):
        if not self.state_path or not tid:
            return
        with self._state_lock:
            state = self._state()
            ids = list(state.get("threads") or [])
            if tid not in ids:
                ids.append(tid)
            modes = dict(state.get("modes") or {})
            if mode in ("plan", "default"):
                modes[tid] = mode
            kept = ids[-200:]
            state.update(threads=kept,
                         modes={key: value for key, value in modes.items() if key in kept})
            snapshots = dict(state.get("snapshots") or {})
            state["snapshots"] = {key: value for key, value in snapshots.items()
                                  if key in kept}
            thread_meta = dict(state.get("thread_meta") or {})
            if meta:
                thread_meta[tid] = dict(meta)
            state["thread_meta"] = {key: value for key, value in thread_meta.items()
                                    if key in kept}
            self._save_state(state)

    def _forget(self, tid):
        with self._state_lock:
            state = self._state()
            state["threads"] = [item for item in state.get("threads") or [] if item != tid]
            for key in ("modes", "snapshots", "thread_meta"):
                values = dict(state.get(key) or {})
                values.pop(tid, None)
                state[key] = values
            self._save_state(state)

    def _cache_snapshot(self, tid, messages, files, revision, thread):
        if not self.state_path:
            return
        with self._state_lock:
            state = self._state()
            snapshots = dict(state.get("snapshots") or {})
            old = snapshots.get(tid) or {}
            if old.get("revision") == revision:
                return
            snapshots[tid] = {"revision": revision, "messages": messages[-300:],
                              "files": files[-100:], "updated_at": self.clock(),
                              "info": {"session_id": self.key(tid),
                                       "cwd": thread.get("cwd") or "",
                                       "model": thread.get("model") or "",
                                       "title": thread.get("name") or thread.get("title")}}
            state["snapshots"] = snapshots
            thread_meta = dict(state.get("thread_meta") or {})
            if tid in thread_meta:
                thread_meta[tid] = {**thread_meta[tid], "unmaterialized": False}
                state["thread_meta"] = thread_meta
            self._save_state(state)

    def _resume_managed(self):
        if self._resumed or not hasattr(self.client, "request"):
            return
        self._resumed = True
        for tid in self._managed():
            try:
                if hasattr(self.client, "resume_thread"):
                    thread = self.client.resume_thread(tid)
                else:
                    self.client.request("thread/resume", {"threadId": tid})
                    thread = {}
                mode = self._modes().get(tid) or "default"
                if hasattr(self.client, "set_mode") and thread.get("model"):
                    self.client.set_mode(tid, mode, thread.get("model"),
                                         thread.get("effort"))
            except Exception:
                continue

    def start_thread(self, cwd, model=None, effort=None, mode="plan"):
        if mode not in ("plan", "default"):
            raise CodexError("unknown Codex collaboration mode")
        thread = self.client.start_thread(cwd, model, effort)
        tid = thread.get("id")
        resolved_model = thread.get("model") or model or ""
        resolved_effort = thread.get("effort") or effort or ("medium" if mode == "plan" else None)
        if tid and hasattr(self.client, "set_mode"):
            self.client.set_mode(tid, mode, resolved_model, resolved_effort)
        if tid:
            now = self.clock()
            meta = {"cwd": cwd, "model": resolved_model, "effort": resolved_effort,
                    "name": thread.get("name"), "created_at": now,
                    "unmaterialized": True}
            self._remember(tid, mode, meta)
            stub = self._stub_session(tid, meta, mode)
            with self._lock:
                self._sessions = [s for s in self._sessions
                                  if s.get("session_id") != stub["session_id"]] + [stub]
                self._last_refresh = 0
        return thread

    def _stub_session(self, tid, meta, mode):
        cwd = meta.get("cwd") or ""
        now = meta.get("created_at") or self.clock()
        return {"session_id": self.key(tid), "native_session_id": tid,
                    "provider": "codex", "name": meta.get("name"),
                    "title": meta.get("name"), "project": os.path.basename(cwd) or cwd,
                    "cwd": cwd, "branch": None, "model": meta.get("model") or "",
                    "family": "codex", "effort": meta.get("effort"),
                    "collaboration_mode": mode, "running": None,
                    "last_msg": None, "state": "idle",
                    "reg_status": "loaded", "quiet_s": 0, "ctx_tokens": 0,
                    "ctx_pct": None, "cost": None, "cost_source": "unavailable",
                    "bridge_url": None, "started_ms": round(now * 1000), "pending": None,
                    "compacting": None, "muted": False, "convo_v": f"{int(now * 1000)}:0",
                    "files_n": 0,
                    "agents": [], "agents_running": 0, "agents_total": 0,
                    "agent_cost": None, "capabilities": {"submit": True,
                    "interrupt": False, "close": True, "focus_terminal": False,
                    "focus_terminal_reason": "Codex App Server has no terminal-focus API",
                    "answer_structured": False, "takeover": False,
                    "archive": True, "compact": True, "review": True, "files": False,
                    "decide_approval": False, "spawn_agent": True, "relay_agent": False,
                    "relay_agent_direct": False, "account_usage": False,
                    "exact_cost": False, "measured_throughput": False}}

    def _pending(self, tid, nonce):
        if isinstance(nonce, list):
            nonce = nonce[0] if nonce else None
        approval = self.client.approvals.get(str(nonce)) if nonce else None
        if not approval or approval.get("thread_id") != tid:
            return None
        p = approval["params"]
        if approval["method"] == "item/tool/requestUserInput":
            questions = []
            for q in p.get("questions") or []:
                questions.append({"header": q.get("header", "Question"),
                                  "question": q.get("question", ""),
                                  "multiSelect": False,
                                  "allowOther": bool(q.get("isOther", True)),
                                  "secret": bool(q.get("isSecret", False)),
                                  "options": [{"label": o.get("label", ""),
                                               "description": o.get("description", "")}
                                              for o in (q.get("options") or [])]})
            return {"kind": "question", "nonce": str(nonce), "questions": questions,
                    "dismiss_action": "cancel_turn"}
        if approval["method"] == "mcpServer/elicitation/request":
            return _elicitation_pending(str(nonce), p)
        summary = p.get("reason") or p.get("command") or p.get("grantRoot") or "Codex request"
        method = approval["method"]
        kind = {"item/commandExecution/requestApproval": "command",
                "item/fileChange/requestApproval": "file_change",
                "item/permissions/requestApproval": "permissions",
                "applyPatchApproval": "file_change",
                "execCommandApproval": "command"}.get(method, "unknown")
        if p.get("cwd"):
            summary = f"{summary}\nworking directory: {p['cwd']}"
        return {"kind": "permission", "nonce": str(nonce), "tool": kind,
                "approval_kind": kind, "input_summary": str(summary)[:1500],
                "decisions": ["allow", "always", "deny", "cancel"]}

    def context(self, key):
        tid = self.native(key)
        try:
            thread = self.client.read_thread(tid)
            messages = _conversation(thread)
            files = _files(thread, thread.get("cwd") or "")
            revision = _revision(thread, self.client.thread_state.get(tid, {}))
            self._cache_snapshot(tid, messages, files, revision, thread)
            return {"ok": True, "messages": messages, "files": files,
                    "revision": revision}
        except Exception as exc:
            if "not materialized yet" in str(exc):
                return {"ok": True, "messages": [], "files": []}
            snapshot = (self._state().get("snapshots") or {}).get(tid)
            if snapshot:
                return {"ok": True, "messages": snapshot.get("messages") or [],
                        "files": snapshot.get("files") or [], "closed": True,
                        "stale": True, "error": str(exc), "info": snapshot.get("info") or {},
                        "revision": snapshot.get("revision")}
            return {"ok": False, "error": str(exc)}

    def agent_context(self, key, agent_id):
        out = self.context(self.key(agent_id))
        if out.get("ok"):
            usage = self.client.thread_state.get(agent_id, {}).get("token_usage") or {}
            out["info"] = {"agent_id": agent_id, "agent_type": "codex",
                           "description": "Codex subagent", "model": "",
                           "family": "codex", "tokens": _token_breakdown(usage),
                           "total_tokens": _usage_total(usage) if usage else None,
                           "cost": None, "cost_source": "unavailable"}
        return out

    def commands(self, key, cwd):
        tid = self.native(key)
        out = [
            {"name": "/compact", "desc": "Compact the current conversation",
             "scope": "app-server", "danger": True, "execution": "action",
             "action": "compact"},
            {"name": "/review", "desc": "Review uncommitted changes",
             "scope": "app-server", "danger": False, "execution": "action",
             "action": "review"},
        ]
        errors = []
        try:
            entries = self.client.list_skills(cwd)
        except Exception as exc:
            return {"ok": True, "commands": out, "warning": str(exc)}
        skills = {}
        for entry in entries:
            errors.extend(entry.get("errors") or [])
            for skill in entry.get("skills") or []:
                if not skill.get("enabled", True) or not skill.get("name") or not skill.get("path"):
                    continue
                name = "$" + skill["name"]
                skills[name] = {"name": skill["name"], "path": skill["path"]}
                out.append({"name": name,
                            "desc": (skill.get("description") or
                                     skill.get("shortDescription") or "Codex skill")[:120],
                            "scope": str(skill.get("scope") or "skill").lower(),
                            "danger": False, "execution": "skill"})
        self._skills[tid] = skills
        return {"ok": True, "commands": out,
                "warnings": [_safe_json(error)[:500] for error in errors]}

    def file_content(self, key, fpath):
        context = self.context(key)
        if not context.get("ok"):
            return None, None, context.get("error") or "session unavailable"
        allowed = {item.get("path") for item in context.get("files") or []}
        if fpath not in allowed:
            return None, None, "not a file this Codex thread changed or generated"
        try:
            if os.path.getsize(fpath) > 8_000_000:
                return None, None, "file too large to preview (>8MB)"
            with open(fpath, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            return None, None, f"unreadable: {exc}"
        ext = os.path.splitext(fpath)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp",
                 ".svg": "image/svg+xml"}.get(ext, "text/plain; charset=utf-8")
        return ctype, data, None

    def act(self, action):
        typ = action.get("type")
        tid = self.native(action.get("session_id"))
        try:
            with self._lock:
                known = next((s for s in self._sessions
                              if s.get("native_session_id") == tid), None)
            if known and known.get("capabilities", {}).get("takeover") \
               and typ not in ("takeover", "resume"):
                return {"ok": False,
                        "error": "external Codex thread is read-only until explicitly taken over"}
            required_capability = {"text": "submit", "mode": "submit",
                                   "interrupt": "interrupt", "close": "close",
                                   "compact": "compact", "review": "review",
                                   "relay": "relay_agent"}.get(typ)
            if known and required_capability and not known.get("capabilities", {}).get(
                    required_capability):
                if known.get("state") in ("running", "stalled", "needs_you"):
                    return {"ok": False, "error":
                            "Codex is active in another client; control it there until the turn ends"}
                return {"ok": False, "error": f"session does not support {typ}"}
            if typ == "text":
                text = str(action.get("text") or "").strip()
                if not text:
                    return {"ok": False, "error": "empty text"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                mode = session.get("collaboration_mode") or self._modes().get(tid) or "default"
                live = self.client.thread_state.get(tid, {})
                model = session.get("model") or live.get("model")
                effort = session.get("effort") or live.get("effort")
                if mode == "plan" and not model:
                    return {"ok": False, "error": "Codex model is unavailable; refresh and try again"}
                if live.get("status") == "running" and hasattr(self.client, "steer_turn"):
                    self.client.steer_turn(tid, text)
                else:
                    self.client.start_turn(tid, text, mode=mode,
                                           model=model, effort=effort)
            elif typ == "mode":
                mode = str(action.get("mode") or "")
                if mode not in ("plan", "default"):
                    return {"ok": False, "error": "mode must be plan or default"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                live = self.client.thread_state.get(tid, {})
                model = session.get("model") or live.get("model")
                effort = (session.get("effort") or live.get("effort") or
                          ("medium" if mode == "plan" else None))
                self.client.set_mode(tid, mode, model, effort)
                self._remember(tid, mode)
                with self._lock:
                    for current in self._sessions:
                        if current.get("native_session_id") == tid:
                            current["collaboration_mode"] = mode
                            current["effort"] = effort
                            # A mode is not a pending request. Expose this only when
                            # App Server actually asks a structured question.
                            current["capabilities"]["answer_structured"] = False
                return {"ok": True, "mode": mode}
            elif typ == "interrupt":
                self.client.interrupt(tid)
            elif typ in ("resume", "takeover"):
                thread = self.client.resume_thread(tid)
                self._remember(tid, self._modes().get(tid) or "default")
                self._last_refresh = 0
                return {"ok": True, "session_id": self.key(tid),
                        "model": thread.get("model")}
            elif typ in ("archive", "close"):
                if typ == "close" and known and known.get("state") in (
                        "running", "stalled", "needs_you"):
                    self.client.interrupt(tid)
                try:
                    self.client.archive(tid)
                except Exception as exc:
                    # thread/start assigns an ID before the first turn creates a
                    # rollout. There is nothing provider-side to archive yet.
                    if "no rollout found for thread id" not in str(exc).lower():
                        raise
                self._forget(tid)
                with self._lock:
                    self._sessions = [session for session in self._sessions
                                      if session.get("native_session_id") != tid]
            elif typ == "compact":
                self.client.compact(tid)
                self.client.thread_state.setdefault(tid, {})["compacting"] = 0
            elif typ == "review":
                self.client.review(tid)
            elif typ == "skill":
                name = str(action.get("name") or "")
                skill = self._skills.get(tid, {}).get(name)
                if not skill:
                    return {"ok": False,
                            "error": "unknown or disabled Codex skill; refresh commands"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                inputs = [{"type": "skill", "name": skill["name"], "path": skill["path"]}]
                args = str(action.get("args") or "").strip()
                if args:
                    inputs.append({"type": "text", "text": args})
                self.client.start_turn(
                    tid, "", mode=session.get("collaboration_mode") or "default",
                    model=session.get("model"), effort=session.get("effort"),
                    inputs=inputs)
            elif typ == "permission":
                return self.client.decide(action.get("nonce"), action.get("choice"))
            elif typ in ("multiq", "option"):
                answers = action.get("answers") if typ == "multiq" else [action]
                return self.client.answer_questions(action.get("nonce"), answers or [])
            elif typ == "elicitation":
                return self.client.answer_elicitation(
                    action.get("nonce"), action.get("choice"), action.get("content"))
            elif typ == "dismiss":
                nonce = str(action.get("nonce") or "")
                request = self.client.approvals.get(nonce)
                if not request:
                    return {"ok": False, "error": "stale Codex request"}
                if request.get("method") == "mcpServer/elicitation/request":
                    return self.client.answer_elicitation(nonce, "decline")
                if request.get("method") == "item/tool/requestUserInput":
                    self.client.interrupt(tid)
                    return {"ok": True, "dismissed_by": "turn_interrupt"}
                return self.client.decide(nonce, "cancel")
            elif typ == "relay":
                text = str(action.get("text") or "").strip()
                aid = str(action.get("agent_id") or "").strip()
                if not text or not aid:
                    return {"ok": False, "error": "agent and message are required"}
                relay = f"Relay this message to subagent {aid}: {text}"
                live = self.client.thread_state.get(tid, {})
                if live.get("status") == "running" and hasattr(self.client, "steer_turn"):
                    self.client.steer_turn(tid, relay)
                else:
                    with self._lock:
                        session = next((s for s in self._sessions
                                        if s.get("native_session_id") == tid), {})
                    self.client.start_turn(tid, relay,
                        mode=session.get("collaboration_mode") or "default",
                        model=session.get("model"), effort=session.get("effort"))
                return {"ok": True, "relayed_via": "parent"}
            else:
                return {"ok": False, "error": f"Codex does not support {typ} here"}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


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


def _latest_turn_lifecycle(thread):
    """Return lifecycle evidence that survives cross-App-Server thread reads.

    ChatGPT desktop turns can appear as ``interrupted`` to Fleet Dash's separate
    App Server while they are still running. The durable distinction is that an
    active turn has startedAt but no completedAt; a genuinely ended turn has a
    completion timestamp.
    """
    turns = thread.get("turns") or []
    if not turns:
        return {"active": False, "started_at": None, "completed_at": None}
    turn = turns[-1] or {}
    started_at = _epoch(turn.get("startedAt"))
    completed_at = _epoch(turn.get("completedAt"))
    status = str(turn.get("status") or "").replace("-", "").replace("_", "").lower()
    explicitly_active = status in {"active", "inprogress", "running", "started"}
    active = (turn.get("completedAt") is None and
              (explicitly_active or started_at is not None))
    return {"active": active, "started_at": started_at,
            "completed_at": completed_at, "status": turn.get("status")}


def _millis(value):
    ep = _epoch(value)
    return round(ep * 1000) if ep else None


def _usage_total(usage):
    latest = usage.get("last") or usage.get("total") or usage
    if latest.get("totalTokens") is not None:
        return int(latest.get("totalTokens") or 0)
    return sum(int(latest.get(k) or 0) for k in
               ("inputTokens", "outputTokens", "reasoningOutputTokens"))


def _usage_window(usage):
    try:
        return int(usage.get("modelContextWindow") or 0)
    except (TypeError, ValueError):
        return 0


def _token_breakdown(usage):
    latest = usage.get("last") or usage.get("total") or usage
    return {"in": latest.get("inputTokens"),
            "cache_read": latest.get("cachedInputTokens"),
            "out": latest.get("outputTokens"),
            "reasoning": latest.get("reasoningOutputTokens")}


def _account_usage(limits, tokens):
    """Normalize Codex account quota data for Fleet Dash's provider header."""
    snapshots = limits.get("rateLimitsByLimitId") or {}
    if not snapshots and limits.get("rateLimits"):
        snapshots = {"codex": limits["rateLimits"]}
    buckets = []
    plan_type = None
    for limit_id, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        plan_type = plan_type or snapshot.get("planType")
        name = snapshot.get("limitName")
        for slot in ("primary", "secondary"):
            window = snapshot.get(slot)
            if not isinstance(window, dict) or window.get("usedPercent") is None:
                continue
            minutes = window.get("windowDurationMins")
            if minutes == 300:
                window_name = "5-hour"
            elif minutes == 10080:
                window_name = "weekly"
            elif minutes:
                window_name = f"{round(minutes / 60)}-hour"
            else:
                window_name = slot
            label = f"{name} {window_name}" if name else window_name
            resets_at = window.get("resetsAt")
            reset_iso = None
            if resets_at:
                try:
                    reset_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(int(resets_at)))
                except (TypeError, ValueError, OverflowError):
                    pass
            buckets.append({"id": f"{limit_id}:{slot}", "label": label,
                            "used_pct": int(window.get("usedPercent") or 0),
                            "reset": reset_iso, "window_minutes": minutes})
    summary = tokens.get("summary") or {}
    return {"provider": "codex", "plan_type": plan_type,
            "buckets": buckets,
            "lifetime_tokens": summary.get("lifetimeTokens"),
            "daily_usage": tokens.get("dailyUsageBuckets") or [],
            "reset_credits": (limits.get("rateLimitResetCredits") or {}).get(
                "availableCount", 0)}


def _conversation(thread):
    messages = []
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            typ = item.get("type", "")
            if typ in ("userMessage", "agentMessage"):
                text = _item_text(item)
                messages.append({"role": "user" if typ == "userMessage" else "assistant",
                                 "text": text, "ts": item.get("createdAt")})
            elif typ == "plan":
                messages.append({"role": "event", "kind": "plan", "title": "Plan",
                                 "detail": str(item.get("text") or "")[:4000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "reasoning":
                detail = "\n".join(str(x) for x in
                                   ((item.get("summary") or []) + (item.get("content") or [])))
                messages.append({"role": "event", "kind": "reasoning",
                                 "title": "Reasoning", "detail": detail[:4000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ in ("commandExecution", "fileChange", "mcpToolCall",
                         "dynamicToolCall", "collabAgentToolCall"):
                raw_arg = (item.get("command") or item.get("changes") or
                           item.get("arguments") or item.get("prompt") or item.get("server") or "")
                if not isinstance(raw_arg, str):
                    raw_arg = json.dumps(raw_arg, separators=(",", ":"))
                name = item.get("tool") or typ
                messages.append({"role": "tool", "name": name, "arg": raw_arg[:500],
                                 "result": (item.get("aggregatedOutput") or item.get("result") or
                                            item.get("error") or item.get("status")),
                                 "ts": item.get("createdAt")})
            elif typ == "webSearch":
                messages.append({"role": "tool", "name": "webSearch",
                                 "arg": str(item.get("query") or "")[:500],
                                 "result": item.get("action"), "ts": item.get("createdAt")})
            elif typ == "imageView":
                messages.append({"role": "tool", "name": "imageView",
                                 "arg": str(item.get("path") or "")[:500],
                                 "ts": item.get("createdAt")})
            elif typ == "imageGeneration":
                messages.append({"role": "tool", "name": "imageGeneration",
                                 "arg": str(item.get("revisedPrompt") or "")[:500],
                                 "result": item.get("status"), "ts": item.get("createdAt")})
            elif typ == "sleep":
                messages.append({"role": "event", "kind": "sleep", "title": "Paused",
                                 "detail": f"{item.get('durationMs') or 0} ms",
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ in ("enteredReviewMode", "exitedReviewMode"):
                messages.append({"role": "event", "kind": "review",
                                 "title": ("Entered review mode" if typ.startswith("entered")
                                           else "Exited review mode"),
                                 "detail": str(item.get("review") or "")[:4000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "contextCompaction":
                messages.append({"role": "event", "kind": "compact",
                                 "title": "Conversation compacted", "detail": "",
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "subAgentActivity":
                messages.append({"role": "event", "kind": "agent",
                                 "title": f"Subagent {item.get('kind') or 'activity'}",
                                 "detail": str(item.get("agentPath") or
                                               item.get("agentThreadId") or "")[:1000],
                                 "level": "info", "ts": item.get("createdAt")})
            elif typ == "hookPrompt":
                messages.append({"role": "event", "kind": "hook",
                                 "title": "Hook prompt", "detail": "Provider hook input",
                                 "level": "info", "ts": item.get("createdAt")})
            else:
                messages.append({"role": "event", "kind": "unknown",
                                 "title": f"Unsupported Codex item: {typ or 'unknown'}",
                                 "detail": _safe_json(item)[:2000], "level": "warning",
                                 "ts": item.get("createdAt")})
    return messages


def _item_text(item):
    if item.get("text"):
        return str(item["text"])
    out = []
    for part in item.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            out.append(str(part["text"]))
        elif part.get("type") in ("image", "localImage"):
            out.append(f"[image: {part.get('path') or part.get('url') or 'attached'}]")
        elif part.get("type") in ("mention", "skill"):
            out.append(str(part.get("name") or part.get("path") or ""))
    return "\n".join(filter(None, out))


def _safe_json(value):
    try:
        return json.dumps(value, separators=(",", ":"), default=str)
    except Exception:
        return repr(value)


def _revision(thread, live):
    fingerprint = []
    for turn in thread.get("turns") or []:
        fingerprint.append((turn.get("id"), turn.get("status"),
                            [(item.get("id"), item.get("type"), item.get("status"),
                              len(_item_text(item))) for item in turn.get("items") or []]))
    digest = hashlib.sha256(_safe_json(fingerprint).encode()).hexdigest()[:12]
    updated_ms = _millis(thread.get("updatedAt")) or _millis(thread.get("createdAt")) or 0
    return f"{updated_ms}:{int(live.get('revision') or 0)}:{digest}"


def _last_message(messages, fallback=None):
    for message in reversed(messages):
        if message.get("role") in ("user", "assistant") and message.get("text"):
            text = " ".join(str(message["text"]).split())
            return {"role": message["role"], "text": text[:280]}
    if fallback:
        return {"role": "user", "text": " ".join(str(fallback).split())[:280]}
    return None


def _files(thread, cwd):
    root = os.path.realpath(cwd) if cwd else None
    found = {}

    def add(raw, caption, ts=None, change_kind=None):
        if not isinstance(raw, str) or not raw:
            return
        path = raw if os.path.isabs(raw) else os.path.join(cwd or "", raw)
        path = os.path.realpath(os.path.expanduser(path))
        if root and path != root and not path.startswith(root + os.sep):
            return
        ext = os.path.splitext(path)[1].lower()
        found[path] = {"name": os.path.basename(path), "path": path,
                       "kind": "image" if ext in
                       (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg") else "text",
                       "missing": not os.path.isfile(path), "caption": caption,
                       "ts": ts, "source": "codex", "change_kind": change_kind,
                       "delivered": False}

    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if item.get("type") == "fileChange":
                for change in item.get("changes") or []:
                    if isinstance(change, dict):
                        kind = change.get("kind") or "updated"
                        add(change.get("path"), f"Codex {kind} this file",
                            item.get("createdAt"), kind)
            elif item.get("type") == "imageGeneration":
                add(item.get("savedPath"), "Codex generated this image",
                    item.get("createdAt"), "generated")
    return list(reversed(list(found.values())))


def _elicitation_pending(nonce, params):
    schema = params.get("requestedSchema") or {}
    fields = []
    properties = schema.get("properties") or {} if isinstance(schema, dict) else {}
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    for name, raw in properties.items():
        field = raw if isinstance(raw, dict) else {}
        field_type = field.get("type")
        multi = field_type == "array"
        enum_source = field.get("items") if multi else field
        enum_source = enum_source if isinstance(enum_source, dict) else {}
        values = enum_source.get("enum") or []
        options = [{"value": value, "label": str(value), "description": ""}
                   for value in values]
        fields.append({"name": name, "label": field.get("title") or name,
                       "description": field.get("description") or "",
                       "type": "select" if options else
                               ("boolean" if field_type == "boolean" else "text"),
                       "multiSelect": multi, "options": options,
                       "required": name in required, "secret": False})
    return {"kind": "elicitation", "nonce": nonce,
            "server": params.get("serverName"), "message": params.get("message") or "",
            "mode": params.get("mode"), "url": params.get("url"), "fields": fields,
            "decisions": ["accept", "decline", "cancel"]}


def _agents(thread, parent_id):
    found = {}
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if item.get("type") == "subAgentActivity":
                aid = item.get("agentThreadId")
                if aid:
                    activity = item.get("kind")
                    state = ({"completed": "done", "interrupted": "ended",
                              "failed": "ended", "errored": "ended",
                              "shutdown": "ended"}.get(activity, "running"))
                    found[aid] = {"agent_id": aid, "session_id": f"codex:{parent_id}",
                                  "agent_type": (item.get("agentPath") or "codex").split("/")[-1],
                                  "description": item.get("agentPath") or "Codex subagent",
                                  "depth": 0, "model": "", "family": "codex", "effort": None,
                                  "state": state,
                                  "total_tokens": None, "cost": None,
                                  "cost_source": "unavailable", "tokens": {}, "spark": [],
                                  "tok_per_s": None, "started": None, "last": None,
                                  "last_msg": None, "convo_v": 0}
                continue
            if item.get("type") != "collabAgentToolCall":
                continue
            states = item.get("agentsStates") or {}
            for aid in item.get("receiverThreadIds") or []:
                raw = (states.get(aid) or {}).get("status")
                state = {"pendingInit": "running", "running": "running",
                         "completed": "done", "interrupted": "ended",
                         "errored": "ended", "shutdown": "ended"}.get(raw, "running")
                found[aid] = {"agent_id": aid, "session_id": f"codex:{parent_id}",
                              "agent_type": "codex", "description": item.get("prompt") or "",
                              "depth": 0, "model": item.get("model") or "",
                              "family": "codex", "effort": item.get("reasoningEffort"),
                              "state": state, "total_tokens": None, "cost": None,
                              "cost_source": "unavailable", "tokens": {}, "spark": [],
                              "tok_per_s": None,
                              "started": None, "last": None, "last_msg": None}
    return list(found.values())
