#!/usr/bin/env python3
"""Supported Codex CLI integration through ``codex app-server``.

The adapter owns the JSON-RPC process and exposes provider-neutral session,
conversation, and action shapes to Fleet Dash. It deliberately never parses
Codex rollout files; their on-disk representation is not a public API.
"""
import json
import os
import base64
import struct
import subprocess
import threading
import time
import shutil
import glob
import hashlib
import socket
from collections import deque
from repo_center import observed_test_outcome


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


def codex_control_socket():
    """Return Fleet's custom path for Codex's supported Unix transport."""
    configured = os.environ.get("FLEET_DASH_CODEX_SOCKET")
    return os.path.abspath(os.path.expanduser(
        configured or os.path.join("~", ".claude", "fleet-dash",
                                   "codex-app-server.sock")))


_shared_runtime_lock = threading.Lock()


def _socket_accepting(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(.25)
    try:
        client.connect(path)
        return True
    except OSError:
        return False
    finally:
        client.close()


def ensure_shared_codex_runtime(executable=None, socket_path=None, timeout=8,
                                process_factory=None, sleeper=None, clock=None,
                                probe=None):
    """Start one detached, multi-client App Server on the supported Unix transport.

    `codex app-server daemon` is only available to the standalone installer. Fleet
    supports npm-managed Codex by owning the same Unix listener directly. The
    detached process survives a Fleet web-daemon restart; subsequent starts reuse
    the accepting socket instead of creating another runtime.
    """
    executable = executable or codex_command()
    socket_path = socket_path or codex_control_socket()
    process_factory = process_factory or subprocess.Popen
    sleeper = sleeper or time.sleep
    clock = clock or time.monotonic
    probe = probe or _socket_accepting
    with _shared_runtime_lock:
        if probe(socket_path):
            return
        os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
        if os.path.lexists(socket_path):
            os.unlink(socket_path)
        command = [executable, "app-server", "--listen", "unix://" + socket_path]
        env = os.environ.copy()
        command_dir = os.path.dirname(os.path.abspath(executable))
        env["PATH"] = command_dir + os.pathsep + env.get("PATH", "")
        process = process_factory(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=None, text=True, start_new_session=True, env=env)
        deadline = clock() + timeout
        while clock() < deadline:
            if probe(socket_path):
                return
            code = process.poll()
            if code is not None:
                raise CodexError(f"Codex shared App Server exited during startup ({code})")
            sleeper(.05)
        raise CodexError("Codex shared App Server socket did not become ready")


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

    def account_info(self):
        return self.request("account/read", {"refreshToken": False})

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
        return self.request("turn/steer", {"threadId": thread_id,
                                            "expectedTurnId": turn_id,
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
                 stall_seconds=180, external_observer=None):
        self.enabled = enabled
        self.client = client or CodexAppServer()
        self.state_path = state_path
        self.clock = clock or time.time
        self.stall_seconds = stall_seconds
        self.error = None
        self.error_at = None
        self._resumed_generation = None
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
        self.external_observer = external_observer
        self._tracked_external = set()

    @staticmethod
    def key(native_id):
        return f"codex:{native_id}"

    @staticmethod
    def native(key):
        return key.split(":", 1)[1] if str(key).startswith("codex:") else key

    def track_external(self, keys):
        """Choose external threads whose local lifecycle should be observed.

        Observation never changes ownership or capabilities. Fleet tracks only
        explicitly pinned threads so archive discovery remains cheap.
        """
        values = {self.native(key) for key in (keys or []) if str(key).startswith("codex:")}
        with self._lock:
            self._tracked_external = values

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
            loaded = set()
            if hasattr(self.client, "loaded_thread_ids"):
                for entry in self.client.loaded_thread_ids():
                    loaded.add(entry.get("id") if isinstance(entry, dict) else entry)
                loaded.discard(None)
            models = self.client.list_models() if hasattr(self.client, "list_models") else []
            error = None
        except Exception as exc:
            now = self.clock()
            with self._lock:
                self.error = str(exc)
                self.error_at = now
                for session in self._sessions:
                    if session.get("state") != "stale":
                        session["stale_previous_state"] = session.get("state")
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
        thread_meta = dict(persisted.get("thread_meta") or {})
        managed = {tid for tid in (persisted.get("threads") or [])
                   if (thread_meta.get(tid) or {}).get("runtime_owner") == "fleet_shared"}
        with self._lock:
            tracked_external = set(self._tracked_external)
        out = []
        listed = set()
        for thread in threads:
            tid = thread.get("id")
            if not tid or thread.get("parentThreadId"):
                continue
            listed.add(tid)
            source = thread.get("source") or "unknown"
            # App Server also writes `source: vscode` for Fleet's own rich-client
            # threads. Ownership comes from the runtime marker we persisted when
            # creating/adopting the thread, never from this presentation label.
            is_managed = tid in managed
            desktop_owned = source == "vscode" and not is_managed
            # A CLI connected with `codex --remote unix://...` is another client
            # of Fleet's canonical runtime. Adopt it automatically so both
            # surfaces steer the same live turn instead of resuming a copy.
            if tid in loaded and not desktop_owned and not is_managed:
                self._remember(tid, modes.get(tid) or "default", {
                    "runtime_owner": "fleet_shared", "origin": source,
                    "cwd": thread.get("cwd") or "", "model": thread.get("model") or "",
                    "effort": thread.get("effort"), "name": thread.get("name"),
                    "created_at": _epoch(thread.get("createdAt")) or now,
                    "unmaterialized": False})
                managed.add(tid)
                is_managed = True
            observation = None
            if not is_managed and tid in tracked_external and self.external_observer:
                try:
                    observation = self.external_observer.observe(tid)
                except Exception as exc:
                    observation = {"error": str(exc), "messages": []}
            detail_error = None
            if is_managed:
                try:
                    detail = self.client.read_thread(tid)
                    if detail:
                        thread = {**thread, **detail}
                except Exception as exc:
                    detail_error = str(exc)
            live = self.client.thread_state.setdefault(tid, {})
            pending = self._pending(tid, live.get("pending"))
            updated = thread.get("updatedAt") or thread.get("createdAt")
            turn_lifecycle = _latest_turn_lifecycle(thread)
            live_completed = _epoch(live.get("completed_at"))
            completion_times = [value for value in
                                (turn_lifecycle.get("completed_at"), live_completed,
                                 (observation or {}).get("completed_at"))
                                if value is not None]
            completed_epoch = max(completion_times) if completion_times else None
            activity_times = [value for value in
                              (_epoch(updated), _epoch(live.get("updated_at")),
                               turn_lifecycle.get("started_at"), completed_epoch,
                               (observation or {}).get("last_activity_at"),
                               (observation or {}).get("started_at"))
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
            running = native_running or observed_running or bool(
                (observation or {}).get("active"))
            quiet = max(0, now - updated_epoch)
            if detail_error or live.get("error") or recorded_type == "systemError":
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
            if (observation or {}).get("messages"):
                messages = observation["messages"]
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
            if observation and observation.get("revision"):
                revision = observation["revision"]
            if is_managed and not detail_error:
                self._cache_snapshot(tid, messages, files, revision, thread)
            canonical_turn_id = live.get("turn_id")
            if is_managed and not canonical_turn_id and observed_running:
                canonical_turn_id = turn_lifecycle.get("turn_id")
                if canonical_turn_id:
                    live["turn_id"] = canonical_turn_id
                    live["status"] = "running"
            owned_turn = bool(canonical_turn_id)
            can_interrupt = (is_managed and owned_turn and
                             state in ("running", "stalled", "needs_you"))
            uncontrolled_active = (state in ("running", "stalled", "needs_you") and
                                   not can_interrupt)
            materialized = not bool((thread_meta.get(tid) or {}).get("unmaterialized"))
            can_attach = (is_managed and materialized and
                          state not in ("running", "stalled", "needs_you"))
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
                "_latest_prose": _latest_prose(messages),
                "repo_outcome": observed_test_outcome(
                    messages, session_id=self.key(tid), provider="codex"),
                "state": state, "reg_status": reg_status,
                "headless": not is_managed, "read_only": not is_managed,
                "read_only_reason": ("ChatGPT Desktop and VS Code use a different App Server; "
                                     "this transcript is view only" if desktop_owned else
                                     "This thread is not loaded in Fleet's shared App Server; "
                                     "its transcript is view only"),
                "codex_source": source,
                "observed_external": bool(observation),
                "observation_confidence": (observation or {}).get("confidence"),
                "observation_warning": ((observation or {}).get("warning") or
                                        (observation or {}).get("error")),
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
                    "takeover": False, "archive": is_managed,
                    "close": is_managed and not uncontrolled_active,
                    "compact": is_managed and state not in
                        ("running", "stalled", "needs_you"),
                    "review": is_managed and state not in
                        ("running", "stalled", "needs_you"),
                    "files": bool(files), "focus_terminal": can_attach,
                    "focus_terminal_mode": "attach" if can_attach else None,
                    "focus_terminal_label": ("attach" if can_attach else
                                             "starting" if is_managed and not materialized else
                                             "view only" if not is_managed else
                                             "turn active"),
                    "focus_terminal_reason": ("Open a Codex TUI attached to Fleet's shared "
                                              "App Server" if can_attach else
                                              "Fleet is creating the saved Codex session needed "
                                              "by the terminal" if is_managed and not materialized else
                                              "Wait for the current Codex turn to finish before "
                                              "attaching" if is_managed else
                                              "External Codex runtime is view only"),
                    "answer_structured": bool(pending and pending.get("kind") in
                                              ("question", "elicitation")),
                    "decide_approval": bool(pending), "spawn_agent": True,
                    "relay_agent": is_managed and not uncontrolled_active,
                    "relay_agent_direct": False,
                    "account_usage": bool(self._account), "exact_cost": False,
                    "measured_throughput": False},
            })
        # App Server assigns a thread ID before the first turn materializes a
        # rollout. Keep that short-lived shell only while the canonical runtime
        # still reports it loaded. Without either runtime state or a rollout, the
        # ID can never be resumed or used and must not become an Available ghost.
        for tid in managed - listed:
            meta = thread_meta.get(tid) or {}
            if meta.get("unmaterialized"):
                if tid in loaded:
                    out.append(self._stub_session(tid, meta,
                                                  modes.get(tid) or "default"))
                else:
                    self._forget(tid)
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
            info = self.client.account_info()
            account = _account_usage(limits, tokens, info)
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
        state = self._state()
        meta = state.get("thread_meta") or {}
        return [tid for tid in (state.get("threads") or [])
                if (meta.get(tid) or {}).get("runtime_owner") == "fleet_shared"]

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
            thread_meta[tid] = {**(thread_meta.get(tid) or {}), **(meta or {}),
                                "runtime_owner": "fleet_shared"}
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
        if not hasattr(self.client, "request"):
            return
        generation = getattr(self.client, "generation", 0)
        if self._resumed_generation == generation:
            return
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
        self._resumed_generation = getattr(self.client, "generation", generation)

    def start_thread(self, cwd, model=None, effort=None, mode="plan",
                     initial_text=None):
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
            if initial_text:
                try:
                    self.client.start_turn(tid, initial_text, mode=mode,
                                           model=resolved_model,
                                           effort=resolved_effort)
                except Exception as exc:
                    # Do not leave a broken empty card behind when the bootstrap
                    # turn fails. An empty App Server thread may not have a rollout
                    # yet, so archive is best-effort and local ownership is always
                    # removed.
                    try:
                        self.client.archive(tid)
                    except Exception:
                        pass
                    self._forget(tid)
                    with self._lock:
                        self._sessions = [s for s in self._sessions
                                          if s.get("session_id") != self.key(tid)]
                    raise CodexError(
                        f"failed to start initial Codex turn: {exc}") from exc
                self._remember(tid, mode, {"unmaterialized": False})
                with self._lock:
                    for current in self._sessions:
                        if current.get("native_session_id") == tid:
                            current.update(state="running", reg_status="running",
                                           last_msg={"role": "user",
                                                     "text": initial_text})
                            current["capabilities"].update(
                                focus_terminal=False,
                                focus_terminal_mode=None,
                                focus_terminal_label="turn active",
                                focus_terminal_reason=(
                                    "Wait for the current Codex turn to finish before attaching"))
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
                    "files_n": 0, "headless": False, "read_only": False,
                    "read_only_reason": None, "codex_source": "appServer",
                    "agents": [], "agents_running": 0, "agents_total": 0,
                    "agent_cost": None, "capabilities": {"submit": True,
                    "interrupt": False, "close": True, "focus_terminal": False,
                    "focus_terminal_mode": None, "focus_terminal_label": "starting",
                    "focus_terminal_reason": ("Fleet is creating the saved Codex session needed "
                                              "by the terminal"),
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
        with self._lock:
            known = next((item for item in self._sessions
                          if item.get("native_session_id") == tid), None)
        if known and known.get("read_only") and self.external_observer:
            observed = self.external_observer.observe(tid)
            if observed and observed.get("messages"):
                return {"ok": True, "messages": observed["messages"], "files": [],
                        "revision": observed.get("revision"), "read_only": True,
                        "observation_confidence": observed.get("confidence"),
                        "warning": observed.get("warning") or observed.get("error")}
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
            if known and known.get("read_only"):
                return {"ok": False,
                        "error": known.get("read_only_reason") or
                                 "external Codex thread is view only"}
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
        return {"active": False, "started_at": None, "completed_at": None,
                "turn_id": None}
    turn = turns[-1] or {}
    started_at = _epoch(turn.get("startedAt"))
    completed_at = _epoch(turn.get("completedAt"))
    status = str(turn.get("status") or "").replace("-", "").replace("_", "").lower()
    explicitly_active = status in {"active", "inprogress", "running", "started"}
    active = (turn.get("completedAt") is None and
              (explicitly_active or started_at is not None))
    return {"active": active, "started_at": started_at,
            "completed_at": completed_at, "status": turn.get("status"),
            "turn_id": turn.get("id")}


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


def _account_usage(limits, tokens, info=None):
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
    current = ((info or {}).get("account") or {})
    plan_type = current.get("planType") or plan_type
    summary = tokens.get("summary") or {}
    return {"provider": "codex", "plan_type": plan_type,
            "email": current.get("email"),
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
                status = item.get("status")
                exit_code = item.get("exitCode")
                failed = None
                if isinstance(exit_code, int):
                    failed = exit_code != 0
                elif str(status or "").lower() in ("failed", "error", "interrupted",
                                                    "cancelled", "canceled"):
                    failed = True
                elif str(status or "").lower() in ("completed", "succeeded", "success"):
                    failed = False
                messages.append({"role": "tool", "name": name, "arg": raw_arg[:500],
                                 "command": raw_arg[:2000] if typ == "commandExecution" else None,
                                 "result": (item.get("aggregatedOutput") or item.get("result") or
                                            item.get("error") or item.get("status")),
                                 "status": status, "exit_code": exit_code, "failed": failed,
                                 "completed_at": item.get("completedAt"),
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
            text = str(message["text"]).strip()
            return {"role": message["role"],
                    "text": text[:499] + "…" if len(text) > 500 else text}
    if fallback:
        text = str(fallback).strip()
        return {"role": "user",
                "text": text[:499] + "…" if len(text) > 500 else text}
    return None


def _latest_prose(messages):
    for message in reversed(messages):
        if message.get("role") in ("user", "assistant") and message.get("text"):
            return {"role": message["role"], "text": str(message["text"]).strip()}
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
