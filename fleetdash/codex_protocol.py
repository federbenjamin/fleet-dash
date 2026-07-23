#!/usr/bin/env python3
"""Codex App Server protocol client: the Unix-transport WebSocket process shim,
the synchronous JSON-RPC ``CodexAppServer`` client, and the error-payload
normalization helpers those clients own.

Depends only on ``codex_runtime`` (CodexError, codex_command); it never imports
``codex_adapter``.
"""
import os
import json
import time
import base64
import struct
import socket
import hashlib
import threading
import subprocess
from collections import deque

from .codex_runtime import CodexError, codex_command


class _WebSocketInput:
    def __init__(self, process):
        self.process = process

    def write(self, raw):
        for line in raw.splitlines():
            if line:
                self.process.send_text(line)
        return len(raw)

    def flush(self):
        return None


class _WebSocketOutput:
    def __init__(self, process):
        self.process = process

    def __iter__(self):
        return self

    def __next__(self):
        message = self.process.receive_text()
        if message is None:
            raise StopIteration
        return message + "\n"


class UnixWebSocketProcess:
    """Process-shaped WebSocket client for App Server's supported Unix transport."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, socket_path, timeout=8, connected_socket=None):
        self.socket_path = socket_path
        self.returncode = None
        self._send_lock = threading.Lock()
        self._buffer = bytearray()
        self.sock = connected_socket or socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            if connected_socket is None:
                self.sock.connect(socket_path)
            self._handshake()
            self.sock.settimeout(None)
        except Exception:
            self.sock.close()
            self.returncode = 1
            raise
        self.stdin = _WebSocketInput(self)
        self.stdout = _WebSocketOutput(self)

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CodexError("Codex shared App Server closed during WebSocket handshake")
            self._buffer.extend(chunk)
            if len(self._buffer) > 64_000:
                raise CodexError("Codex shared App Server sent an oversized handshake")
        end = self._buffer.index(b"\r\n\r\n") + 4
        raw = bytes(self._buffer[:end])
        del self._buffer[:end]
        lines = raw.decode("latin-1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise CodexError(f"Codex WebSocket upgrade failed: {lines[0] if lines else 'empty'}")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + self.GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise CodexError("Codex WebSocket upgrade returned an invalid accept key")

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is not None:
            return
        self.returncode = -15
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def _read_exact(self, size):
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self._buffer)))
            if not chunk:
                self.returncode = 0
                self.sock.close()
                return None
            self._buffer.extend(chunk)
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def _send_frame(self, opcode, payload=b""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._send_lock:
            try:
                self.sock.sendall(bytes(header) + masked)
            except OSError:
                self.returncode = 1
                raise

    def send_text(self, text):
        self._send_frame(0x1, text)

    def receive_text(self):
        fragments = bytearray()
        text_message = False
        while self.returncode is None:
            head = self._read_exact(2)
            if head is None:
                return None
            fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
            masked, length = bool(head[1] & 0x80), head[1] & 0x7F
            if length == 126:
                raw = self._read_exact(2)
                if raw is None:
                    return None
                length = struct.unpack("!H", raw)[0]
            elif length == 127:
                raw = self._read_exact(8)
                if raw is None:
                    return None
                length = struct.unpack("!Q", raw)[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length)
            if payload is None:
                return None
            if mask:
                payload = bytes(value ^ mask[index % 4]
                                for index, value in enumerate(payload))
            if opcode == 0x8:
                self.returncode = 0
                self.sock.close()
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                text_message = True
            elif opcode == 0x0 and text_message:
                fragments.extend(payload)
            else:
                continue
            if fin:
                try:
                    return fragments.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CodexError(f"Codex WebSocket sent invalid UTF-8: {exc}") from exc
        return None


class CodexAppServer:
    """Synchronous JSON-RPC client over a process-shaped App Server transport."""

    def __init__(self, command=None, timeout=8, process_factory=None, clock=None,
                 startup_command=None, startup_factory=None, startup=None):
        self.command = command or [codex_command(), "app-server"]
        self.startup_command = startup_command
        self.startup = startup
        self.timeout = timeout
        self.process_factory = process_factory or subprocess.Popen
        self.startup_factory = startup_factory or subprocess.run
        self.clock = clock or time.time
        self.proc = None
        self.lock = threading.RLock()
        self.ready = threading.Condition(self.lock)
        self.connection_state = "stopped"
        self._start_token = None
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
        deadline = time.monotonic() + self.timeout
        with self.ready:
            while self.connection_state in ("starting", "initializing"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexError("Codex app-server initialization timed out")
                self.ready.wait(remaining)
            if (self.connection_state == "ready" and self.proc and
                    self.proc.poll() is None):
                return
            start_token = object()
            self._start_token = start_token
            self.connection_state = "starting"
        proc = None
        try:
            env = os.environ.copy()
            # Keep the symlink's bin directory: npm installs `codex` beside the
            # matching `node`; resolving the symlink jumps into node_modules.
            command_dir = os.path.dirname(os.path.abspath(self.command[0]))
            env["PATH"] = command_dir + os.pathsep + env.get("PATH", "")
            if self.startup:
                try:
                    self.startup()
                except Exception as exc:
                    self.last_error = str(exc)
                    if isinstance(exc, CodexError):
                        raise
                    raise CodexError(f"Codex shared App Server failed to start: {exc}") from exc
            elif self.startup_command:
                try:
                    started = self.startup_factory(
                        self.startup_command, capture_output=True, text=True,
                        timeout=self.timeout, env=env)
                except Exception as exc:
                    self.last_error = str(exc)
                    raise CodexError(f"Codex shared App Server failed to start: {exc}") from exc
                if started.returncode:
                    detail = (started.stderr or started.stdout or
                              f"exit {started.returncode}").strip()
                    self.last_error = detail
                    raise CodexError(f"Codex shared App Server failed to start: {detail}")
            proc = self.process_factory(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, text=True, bufsize=1, env=env)
            with self.ready:
                if (self.connection_state != "starting" or
                        self._start_token is not start_token):
                    if proc.poll() is None:
                        proc.terminate()
                    raise CodexError("Codex app-server startup was cancelled")
                self.generation += 1
                self.proc = proc
                self.connection_state = "initializing"
                self.reader = threading.Thread(target=self._read_loop, daemon=True)
                self.reader.start()
                self.ready.notify_all()
            self._request_connected("initialize", {"clientInfo": {
                "name": "fleet_dash", "title": "Fleet Dash", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True}})
            self._notify_connected("initialized", {})
        except Exception as exc:
            self.last_error = str(exc)
            with self.ready:
                owns_start = self._start_token is start_token
            if owns_start:
                self.close()
            elif proc and proc is not self.proc and proc.poll() is None:
                proc.terminate()
            if isinstance(exc, CodexError):
                raise
            raise CodexError(f"Codex app-server failed to initialize: {exc}") from exc
        with self.ready:
            if self._start_token is not start_token or self.proc is not proc:
                raise CodexError("Codex app-server startup was cancelled")
            if not self.proc or self.proc.poll() is not None:
                self.connection_state = "stopped"
                self._start_token = None
                self.ready.notify_all()
                raise CodexError("Codex app-server stopped during initialization")
            self.connection_state = "ready"
            self._start_token = None
            self.ready.notify_all()

    def close(self):
        with self.ready:
            closing_generation = self.generation
            proc, self.proc = self.proc, None
            self.generation += 1
            self.connection_state = "stopped"
            self._start_token = None
            self._invalidate_turn_control_locked(closing_generation)
            waiters = list(self.pending.values())
            self.pending.clear()
            self.ready.notify_all()
        if proc and proc.poll() is None:
            proc.terminate()
        for waiter in waiters:
            waiter["error"] = {"message": "Codex app-server closed"}
            waiter["event"].set()

    def _invalidate_turn_control_locked(self, generation):
        """Drop turn authority issued by one dead client connection.

        The transcript and a subsequent thread/read may still prove that work is
        active.  They do not prove that this WebSocket still owns the live turn.
        """
        for state in self.thread_state.values():
            if state.get("turn_generation") != generation:
                continue
            state["turn_id"] = None
            state["turn_generation"] = None
            state["control_lost_at"] = self.clock()
            state["revision"] = state.get("revision", 0) + 1

    def owns_active_turn(self, thread_id):
        with self.lock:
            state = self.thread_state.get(thread_id, {})
            return bool(
                state.get("turn_id") and
                state.get("turn_generation") == self.generation and
                self.proc is not None and self.proc.poll() is None)

    def _send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            self.last_error = str(exc)
            raise CodexError(f"Codex app-server write failed: {exc}") from exc

    def request(self, method, params=None, timeout=None):
        self.start()
        return self._request_connected(method, params, timeout)

    def _request_connected(self, method, params=None, timeout=None):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                raise CodexError("Codex app-server is not connected")
            rid = self.next_id
            self.next_id += 1
            waiter = self.pending[rid] = {"event": threading.Event(),
                                          "generation": self.generation,
                                          "method": method}
            self._send({"method": method, "id": rid, "params": params or {}})
        if not waiter["event"].wait(timeout or self.timeout):
            with self.lock:
                self.pending.pop(rid, None)
            self.last_error = f"timed out: {method}"
            self._diagnostic("request_timeout", method)
            # Session lifecycle timeouts mean this local WebSocket is no longer
            # trustworthy. Detach only Fleet's client transport so the next
            # request reconnects to the still-running shared App Server.
            if str(method).startswith(("thread/", "turn/")):
                self.close()
            raise CodexError(f"Codex app-server timed out: {method}")
        if "error" in waiter:
            raise CodexError(waiter["error"].get("message", str(waiter["error"])))
        self.last_error = None
        return waiter.get("result") or {}

    def notify(self, method, params=None):
        self.start()
        self._notify_connected(method, params)

    def _notify_connected(self, method, params=None):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                raise CodexError("Codex app-server is not connected")
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
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            with self.lock:
                self._invalidate_turn_control_locked(reader_generation)
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
                state.update(status="running", turn_id=turn.get("id"),
                             turn_generation=self.generation,
                             control_lost_at=None, no_active_turn_at=None,
                             error=None)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status") or "completed"
                turn_error = _error_text(turn.get("error"))
                failed_status = status not in ("completed", "interrupted")
                state.update(status=("blocked" if failed_status and
                                     _is_limit_error(turn_error) else
                                     "error" if failed_status else "idle"),
                             turn_id=None, turn_generation=None, completed_at=now,
                             no_active_turn_at=now, turn_status=status,
                             error=turn_error or None)
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
                # Item notifications carry the canonical turn id even when a
                # client misses ``turn/started`` during reconnect or compaction.
                # Recover authority only from this live transport event; a
                # later thread/read can describe another App Server's turn and
                # is deliberately not sufficient.
                turn_id = params.get("turnId")
                # A reconnect may first observe the completed compaction item,
                # so that completion is also sufficient live authority. Other
                # completed items do not resurrect a turn on their own.
                recovers_turn = (method == "item/started" or
                                 item.get("type") == "contextCompaction")
                if recovers_turn and turn_id:
                    state.update(status="running", turn_id=turn_id,
                                 turn_generation=self.generation,
                                 control_lost_at=None, no_active_turn_at=None,
                                 error=None)
                if item.get("type") == "contextCompaction":
                    if method == "item/started":
                        state["compacting"] = 0
                    else:
                        state["compacted_at"] = now
                        state["compacting"] = None
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
                # Current schemas include the active turn id on this legacy
                # notification. Compaction can replace the turn identity, so
                # retaining the pre-compact id strands steering until the turn
                # ends. Keep the turn running; only turn/completed ends it.
                turn_id = params.get("turnId")
                state.update(compacted_at=now, compacting=None)
                if turn_id:
                    state.update(status="running", turn_id=turn_id,
                                 turn_generation=self.generation,
                                 control_lost_at=None, no_active_turn_at=None,
                                 error=None)
            elif method in ("error", "warning", "guardianWarning", "configWarning"):
                state["last_notice"] = {"method": method, "params": params, "ts": now}
                if method == "error":
                    state["error"] = _error_text(params.get("message") or params)
                    if _is_limit_error(state["error"]):
                        state["status"] = "blocked"
                        state["turn_id"] = None

    def list_threads(self, limit=1000):
        """Page the archive far enough to cover Fleet's persisted ownership set."""
        limit = max(1, min(1000, int(limit or 1000)))
        data, cursor, seen_cursors = [], None, set()
        while len(data) < limit:
            params = {"limit": min(100, limit - len(data)), "sortKey": "updated_at",
                      "sortDirection": "desc", "useStateDbOnly": False}
            if cursor:
                params["cursor"] = cursor
            result = self.request("thread/list", params)
            data.extend(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return data[:limit]

    def loaded_thread_ids(self):
        return (self.request("thread/loaded/list", {}).get("data") or [])

    def list_models(self):
        return (self.request("model/list", {"limit": 100}).get("data") or [])

    def list_skills(self, cwd):
        return (self.request("skills/list", {"cwds": [cwd], "forceReload": False})
                .get("data") or [])

    def account_usage(self):
        return self.request("account/usage/read", {})

    def account_info(self):
        return self.request("account/read", {"refreshToken": False})

    def account_limits(self):
        result = self.request("account/rateLimits/read", {})
        with self.lock:
            self.account_rate_limits = result
        return result

    def read_thread(self, thread_id):
        return (self.request("thread/read", {"threadId": thread_id,
                                              "includeTurns": True},
                             timeout=min(self.timeout, 4)).get("thread") or {})

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
        result = self.request("turn/start", params)
        turn = result.get("turn") or result
        turn_id = turn.get("id") or result.get("turnId")
        if turn_id:
            with self.lock:
                self.thread_state.setdefault(thread_id, {}).update(
                    status="running", turn_id=turn_id,
                    turn_generation=self.generation, control_lost_at=None,
                    no_active_turn_at=None)
        return result

    def steer_turn(self, thread_id, text, inputs=None):
        with self.lock:
            turn_id = self.thread_state.get(thread_id, {}).get("turn_id")
        if not turn_id or not self.owns_active_turn(thread_id):
            raise CodexError(
                "Codex control connection was lost; the message can be queued safely",
                code="provider_control_unavailable", queueable=True)
        try:
            return self.request("turn/steer", {"threadId": thread_id,
                                                "expectedTurnId": turn_id,
                                                "input": inputs or [{"type": "text", "text": text}]})
        except CodexError as exc:
            message = str(exc).lower()
            no_active = "no active turn to steer" in message
            changed_turn = ("expected active turn id" in message or
                            "active turn id mismatch" in message)
            if not no_active and not changed_turn:
                raise
            now = self.clock()
            with self.lock:
                state = self.thread_state.setdefault(thread_id, {})
                state.update(turn_id=None, turn_generation=None,
                             revision=state.get("revision", 0) + 1)
                if no_active:
                    state.update(status="idle", completed_at=now,
                                 no_active_turn_at=now, control_lost_at=None)
                else:
                    state.update(status="running", control_lost_at=now,
                                 no_active_turn_at=None)
            if no_active:
                raise CodexError("Codex turn ended before the message was accepted",
                                 code="turn_ended") from exc
            raise CodexError(
                "Codex active turn changed; the message can be queued safely",
                code="provider_control_unavailable", queueable=True) from exc

    def interrupt(self, thread_id):
        with self.lock:
            turn_id = self.thread_state.get(thread_id, {}).get("turn_id")
        if not turn_id or not self.owns_active_turn(thread_id):
            raise CodexError("Codex control connection was lost",
                             code="provider_control_unavailable")
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


def _safe_json(value):
    try:
        return json.dumps(value, separators=(",", ":"), default=str)
    except Exception:
        return repr(value)


def _error_text(value, limit=1000):
    """Project App Server's string-or-object errors to bounded UI text."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        preferred = next((value.get(key) for key in
                          ("message", "detail", "error", "reason")
                          if value.get(key) not in (None, "")), None)
        text = _error_text(preferred, limit) if preferred is not None else _safe_json(value)
    else:
        text = _safe_json(value)
    text = str(text).replace("\x00", " ").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _is_limit_error(value):
    text = _error_text(value).lower()
    return any(marker in text for marker in (
        "rate limit", "usage limit", "quota exceeded", "limit reached",
        "too many requests", "insufficient quota", "context window exceeded",
        "maximum context length", "maximum token limit"))
