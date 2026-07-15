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

    def __init__(self, command=None, timeout=8):
        self.command = command or [codex_command(), "app-server"]
        self.timeout = timeout
        self.proc = None
        self.lock = threading.RLock()
        self.pending = {}
        self.next_id = 1
        self.notifications = []
        self.approvals = {}
        self.thread_state = {}
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
            self.proc = subprocess.Popen(
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
        if proc and proc.poll() is None:
            proc.terminate()

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
            waiter = self.pending[rid] = {"event": threading.Event()}
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

    def _read_loop(self):
        proc = self.proc
        try:
            for raw in proc.stdout:
                if os.environ.get("FLEET_DASH_CODEX_DEBUG"):
                    print(f"codex app-server: {raw.rstrip()}", file=__import__("sys").stderr,
                          flush=True)
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                rid = msg.get("id")
                if rid is not None and ("result" in msg or "error" in msg):
                    with self.lock:
                        waiter = self.pending.pop(rid, None)
                    if waiter:
                        waiter.update(msg)
                        waiter["event"].set()
                    continue
                if rid is not None and msg.get("method"):
                    self._server_request(msg)
                elif msg.get("method"):
                    self._notification(msg["method"], msg.get("params") or {})
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            with self.lock:
                waiters = list(self.pending.values())
                self.pending.clear()
            for waiter in waiters:
                waiter["error"] = {"message": "Codex app-server stopped"}
                waiter["event"].set()

    def _server_request(self, msg):
        method, params = msg["method"], msg.get("params") or {}
        if method == "item/tool/requestUserInput":
            thread_id = params.get("threadId")
            key = str(msg["id"])
            with self.lock:
                self.approvals[key] = {"request_id": msg["id"], "method": method,
                                       "thread_id": thread_id, "params": params,
                                       "seen_at": time.time()}
                if thread_id:
                    self.thread_state.setdefault(thread_id, {})["pending"] = key
            return
        if method in ("item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval", "item/permissions/requestApproval"):
            thread_id = params.get("threadId")
            key = str(msg["id"])
            with self.lock:
                self.approvals[key] = {"request_id": msg["id"], "method": method,
                                       "thread_id": thread_id, "params": params,
                                       "seen_at": time.time()}
                if thread_id:
                    self.thread_state.setdefault(thread_id, {})["pending"] = key
            return
        # Fleet Dash does not advertise optional elicitation/attestation capabilities.
        self.respond(msg["id"], {"error": "unsupported client request"})

    def _notification(self, method, params):
        now = time.time()
        with self.lock:
            self.notifications.append((now, method, params))
            del self.notifications[:-1000]
            thread = params.get("thread") or {}
            tid = params.get("threadId") or thread.get("id")
            if not tid:
                return
            state = self.thread_state.setdefault(tid, {})
            state["updated_at"] = now
            if method == "turn/started":
                turn = params.get("turn") or {}
                state.update(status="running", turn_id=turn.get("id"))
            elif method == "turn/completed":
                state.update(status="idle", turn_id=None, pending=None)
            elif method == "thread/tokenUsage/updated":
                state["token_usage"] = params.get("tokenUsage") or params.get("usage") or {}

    def list_threads(self, limit=100):
        result = self.request("thread/list", {"limit": limit, "sortKey": "updated_at",
                                               "sortDirection": "desc",
                                               "useStateDbOnly": True})
        return result.get("data") or []

    def loaded_thread_ids(self):
        return (self.request("thread/loaded/list", {}).get("data") or [])

    def list_models(self):
        return (self.request("model/list", {"limit": 100}).get("data") or [])

    def read_thread(self, thread_id):
        return (self.request("thread/read", {"threadId": thread_id,
                                              "includeTurns": True}).get("thread") or {})

    def start_thread(self, cwd, model=None, effort=None):
        params = {"cwd": cwd}
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        return (self.request("thread/start", params).get("thread") or {})

    def start_turn(self, thread_id, text):
        return self.request("turn/start", {"threadId": thread_id,
                                            "input": [{"type": "text", "text": text}]})

    def interrupt(self, thread_id):
        turn_id = self.thread_state.get(thread_id, {}).get("turn_id")
        if not turn_id:
            raise CodexError("Codex thread has no active turn")
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def decide(self, nonce, choice):
        with self.lock:
            approval = self.approvals.pop(str(nonce), None)
        if not approval:
            raise CodexError("stale Codex approval")
        if choice not in ("allow", "always", "deny"):
            raise CodexError("unknown approval decision")
        if approval["method"] == "item/permissions/requestApproval":
            requested = approval["params"].get("permissions") or {}
            granted = ({k: v for k, v in requested.items() if v is not None}
                       if choice != "deny" else {})
            self.respond(approval["request_id"], {
                "permissions": granted, "scope": "session" if choice == "always" else "turn"})
        else:
            decision = {"allow": "accept", "always": "acceptForSession",
                        "deny": "decline"}[choice]
            self.respond(approval["request_id"], {"decision": decision})
        return {"ok": True}

    def answer_questions(self, nonce, submitted):
        with self.lock:
            request = self.approvals.pop(str(nonce), None)
        if not request or request.get("method") != "item/tool/requestUserInput":
            raise CodexError("stale Codex question")
        questions = request["params"].get("questions") or []
        result = {}
        for index, question in enumerate(questions):
            answer = submitted[index] if index < len(submitted) else {}
            options = question.get("options") or []
            values = []
            for digit in answer.get("digits") or []:
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
            result[question["id"]] = {"answers": values}
        self.respond(request["request_id"], {"answers": result})
        return {"ok": True}


class CodexAdapter:
    PROVIDER = "codex"

    def __init__(self, enabled=True, client=None, state_path=None):
        self.enabled = enabled
        self.client = client or CodexAppServer()
        self.state_path = state_path
        self.error = None
        self._resumed = False
        self._sessions = []
        self._refreshing = False
        self._last_refresh = 0
        self._lock = threading.Lock()
        self.models = []

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
            if not self._refreshing and time.time() - self._last_refresh >= 2:
                self._refreshing = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return [dict(s) for s in self._sessions]

    def _refresh(self):
        try:
            self._resume_managed()
            threads = self.client.list_threads()
            loaded = set(self.client.loaded_thread_ids()) if hasattr(self.client, "loaded_thread_ids") else None
            if loaded is not None:
                threads = [t for t in threads if t.get("id") in loaded]
            models = self.client.list_models() if hasattr(self.client, "list_models") else []
            error = None
        except Exception as exc:
            with self._lock:
                self.error = str(exc)
                self._refreshing = False
                self._last_refresh = time.time()
            return
        now = time.time()
        out = []
        for thread in threads:
            tid = thread.get("id")
            if not tid:
                continue
            try:
                detail = self.client.read_thread(tid)
                if detail:
                    thread = {**thread, **detail}
            except Exception:
                pass
            live = self.client.thread_state.get(tid, {})
            pending = self._pending(tid, live.get("pending"))
            updated = thread.get("updatedAt") or thread.get("createdAt")
            updated_epoch = _epoch(updated) or live.get("updated_at") or now
            recorded = thread.get("status") or {}
            recorded_type = recorded.get("type") if isinstance(recorded, dict) else recorded
            running = live.get("status") == "running" or recorded_type == "active"
            state = "needs_you" if pending else ("running" if running else "idle")
            cwd = thread.get("cwd") or ""
            usage = live.get("token_usage") or {}
            agents = _agents(thread, tid)
            for agent in agents:
                try:
                    child = self.client.read_thread(agent["agent_id"])
                    turns = child.get("turns") or []
                    last_status = turns[-1].get("status") if turns else None
                    agent["state"] = ("done" if last_status == "completed" else
                                      "ended" if last_status in ("failed", "interrupted") else
                                      "running")
                    agent["convo_v"] = sum(len(t.get("items") or []) for t in turns)
                    agent["model"] = child.get("model") or agent["model"]
                except Exception:
                    pass
            agents_running = sum(a["state"] in ("running", "stalled") for a in agents)
            out.append({
                "session_id": self.key(tid), "native_session_id": tid,
                "provider": "codex", "name": thread.get("name") or thread.get("title"),
                "title": thread.get("name") or thread.get("title"),
                "project": os.path.basename(cwd) or cwd or "Codex", "cwd": cwd,
                "branch": (thread.get("gitInfo") or {}).get("branch"),
                "model": thread.get("model") or "", "family": "codex",
                "effort": thread.get("effort"), "running": None,
                "last_msg": ({"role": "user", "text": str(thread.get("preview"))[:280]}
                             if thread.get("preview") else None),
                "state": state, "reg_status": live.get("status") or recorded_type,
                "quiet_s": round(max(0, now - updated_epoch)),
                "ctx_tokens": _usage_total(usage), "ctx_pct": None,
                "cost": 0.0, "cost_source": "unavailable", "bridge_url": None,
                "started_ms": _millis(thread.get("createdAt")), "pending": pending,
                "compacting": None, "muted": False, "convo_v": int(updated_epoch),
                "files_n": 0, "agents": agents, "agents_running": agents_running,
                "agents_total": len(agents), "agent_cost": 0.0,
                "capabilities": {"submit": True, "interrupt": state == "running",
                    "focus_terminal": False, "answer_structured": False,
                    "decide_approval": bool(pending), "spawn_agent": True,
                    "relay_agent": False, "account_usage": False, "exact_cost": False},
            })
        with self._lock:
            self._sessions = out
            self.models = [{"id": m.get("model") or m.get("id"),
                            "name": m.get("displayName") or m.get("model") or m.get("id"),
                            "efforts": [e.get("reasoningEffort") or e.get("effort") or e
                                        for e in (m.get("supportedReasoningEfforts") or [])]}
                           for m in models if m.get("model") or m.get("id")]
            self.error = error
            self._refreshing = False
            self._last_refresh = time.time()

    def _managed(self):
        if not self.state_path:
            return []
        try:
            with open(self.state_path) as handle:
                return list((json.load(handle) or {}).get("threads") or [])
        except (OSError, ValueError):
            return []

    def _remember(self, tid):
        if not self.state_path or not tid:
            return
        ids = self._managed()
        if tid not in ids:
            ids.append(tid)
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as handle:
            json.dump({"threads": ids[-200:]}, handle, indent=2)

    def _resume_managed(self):
        if self._resumed or not hasattr(self.client, "request"):
            return
        self._resumed = True
        for tid in self._managed():
            try:
                self.client.request("thread/resume", {"threadId": tid})
            except Exception:
                continue

    def start_thread(self, cwd, model=None, effort=None):
        thread = self.client.start_thread(cwd, model, effort)
        tid = thread.get("id")
        self._remember(tid)
        if tid:
            now = time.time()
            stub = {"session_id": self.key(tid), "native_session_id": tid,
                    "provider": "codex", "name": thread.get("name"),
                    "title": thread.get("name"), "project": os.path.basename(cwd) or cwd,
                    "cwd": cwd, "branch": None, "model": model or "", "family": "codex",
                    "effort": effort, "running": None, "last_msg": None, "state": "idle",
                    "reg_status": "loaded", "quiet_s": 0, "ctx_tokens": 0,
                    "ctx_pct": None, "cost": 0.0, "cost_source": "unavailable",
                    "bridge_url": None, "started_ms": round(now * 1000), "pending": None,
                    "compacting": None, "muted": False, "convo_v": int(now), "files_n": 0,
                    "agents": [], "agents_running": 0, "agents_total": 0,
                    "agent_cost": 0.0, "capabilities": {"submit": True,
                    "interrupt": False, "focus_terminal": False, "answer_structured": False,
                    "decide_approval": False, "spawn_agent": True, "relay_agent": False,
                    "account_usage": False, "exact_cost": False}}
            with self._lock:
                self._sessions = [s for s in self._sessions
                                  if s.get("session_id") != stub["session_id"]] + [stub]
                self._last_refresh = 0
        return thread

    def _pending(self, tid, nonce):
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
                                  "options": [{"label": o.get("label", ""),
                                               "description": o.get("description", "")}
                                              for o in (q.get("options") or [])]})
            return {"kind": "question", "nonce": str(nonce), "questions": questions}
        summary = p.get("reason") or p.get("command") or p.get("grantRoot") or "Codex request"
        return {"kind": "permission", "nonce": str(nonce),
                "tool": approval["method"].split("/")[1],
                "input_summary": str(summary)[:1500]}

    def context(self, key):
        try:
            thread = self.client.read_thread(self.native(key))
            return {"ok": True, "messages": _conversation(thread), "files": []}
        except Exception as exc:
            if "not materialized yet" in str(exc):
                return {"ok": True, "messages": [], "files": []}
            return {"ok": False, "error": str(exc)}

    def agent_context(self, key, agent_id):
        out = self.context(self.key(agent_id))
        if out.get("ok"):
            out["info"] = {"agent_id": agent_id, "agent_type": "codex",
                           "description": "Codex subagent", "model": "",
                           "family": "codex", "tokens": {}, "total_tokens": 0,
                           "cost": 0.0}
        return out

    def act(self, action):
        typ = action.get("type")
        tid = self.native(action.get("session_id"))
        try:
            if typ == "text":
                text = str(action.get("text") or "").strip()
                if not text:
                    return {"ok": False, "error": "empty text"}
                self.client.start_turn(tid, text)
            elif typ == "interrupt":
                self.client.interrupt(tid)
            elif typ == "permission":
                return self.client.decide(action.get("nonce"), action.get("choice"))
            elif typ in ("multiq", "option"):
                answers = action.get("answers") if typ == "multiq" else [action]
                return self.client.answer_questions(action.get("nonce"), answers or [])
            elif typ == "relay":
                return {"ok": False, "error": "Codex App Server does not allow direct "
                        "input to v2 subagents; message the parent thread instead"}
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


def _millis(value):
    ep = _epoch(value)
    return round(ep * 1000) if ep else None


def _usage_total(usage):
    latest = usage.get("last") or usage.get("total") or usage
    return sum(int(latest.get(k) or 0) for k in
               ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens"))


def _conversation(thread):
    messages = []
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            typ = item.get("type", "")
            if typ in ("userMessage", "agentMessage"):
                text = item.get("text") or ""
                if not text and isinstance(item.get("content"), list):
                    text = "\n".join(x.get("text", "") for x in item["content"]
                                     if isinstance(x, dict) and x.get("text"))
                messages.append({"role": "user" if typ == "userMessage" else "assistant",
                                 "text": text, "ts": item.get("createdAt")})
            elif typ in ("commandExecution", "fileChange", "mcpToolCall", "collabAgentToolCall"):
                raw_arg = item.get("command") or item.get("changes") or item.get("server") or ""
                if not isinstance(raw_arg, str):
                    raw_arg = json.dumps(raw_arg, separators=(",", ":"))
                messages.append({"role": "tool", "name": typ, "arg": raw_arg[:500],
                                 "result": item.get("aggregatedOutput") or item.get("status"),
                                 "ts": item.get("createdAt")})
    return messages


def _agents(thread, parent_id):
    found = {}
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if item.get("type") == "subAgentActivity":
                aid = item.get("agentThreadId")
                if aid:
                    found[aid] = {"agent_id": aid, "session_id": f"codex:{parent_id}",
                                  "agent_type": (item.get("agentPath") or "codex").split("/")[-1],
                                  "description": item.get("agentPath") or "Codex subagent",
                                  "depth": 0, "model": "", "family": "codex", "effort": None,
                                  "state": "ended" if item.get("kind") == "interrupted" else "running",
                                  "total_tokens": 0, "cost": 0.0, "tokens": {}, "spark": [],
                                  "tok_per_s": 0, "started": None, "last": None,
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
                              "state": state, "total_tokens": 0, "cost": 0.0,
                              "tokens": {}, "spark": [], "tok_per_s": 0,
                              "started": None, "last": None, "last_msg": None}
    return list(found.values())
