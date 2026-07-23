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
import re
import fcntl
import signal
import shlex
import platform
from concurrent.futures import ThreadPoolExecutor, wait
from collections import deque
from repo_center import observed_test_outcome


EXTERNAL_OBSERVATION_SECONDS = 24 * 60 * 60
MAX_RECENT_EXTERNAL_OBSERVATIONS = 32


class CodexError(RuntimeError):
    def __init__(self, message, *, code="codex_error", queueable=False):
        super().__init__(message)
        self.code = code
        self.queueable = bool(queueable)


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


def _codex_executable_identities(executable):
    """Resolve the exact native binary an official npm Codex shim launches."""
    resolved = os.path.realpath(executable)
    identities = {resolved}
    system = platform.system().lower()
    machine = platform.machine().lower()
    target = {
        ("darwin", "arm64"): ("codex-darwin-arm64", "aarch64-apple-darwin"),
        ("darwin", "aarch64"): ("codex-darwin-arm64", "aarch64-apple-darwin"),
        ("darwin", "x86_64"): ("codex-darwin-x64", "x86_64-apple-darwin"),
    }.get((system, machine))
    package_root = os.path.dirname(os.path.dirname(resolved))
    if (target and os.path.basename(resolved) == "codex.js" and
            os.path.basename(os.path.dirname(resolved)) == "bin" and
            os.path.basename(package_root) == "codex" and
            os.path.basename(os.path.dirname(package_root)) == "@openai"):
        package, triple = target
        candidates = (
            os.path.join(package_root, "node_modules", "@openai", package,
                         "vendor", triple, "bin", "codex"),
            os.path.join(package_root, "vendor", triple, "bin", "codex"),
        )
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                identities.add(os.path.realpath(candidate))
    return identities


def _local_model_catalog(path, max_bytes=4 * 1024 * 1024):
    """Read Codex's bounded, credential-free local model catalog."""
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise CodexError("Codex model cache is unexpectedly large")
    payload = json.loads(raw)
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise CodexError("Codex model cache has no model catalog")
    by_id = {}
    order = []
    for model in entries[:100]:
        if not isinstance(model, dict):
            continue
        visibility = str(model.get("visibility") or "").strip().lower()
        if (model.get("hidden") is True or model.get("visible") is False or
                visibility in ("hidden", "hide", "none")):
            continue
        model_id = model.get("slug") or model.get("model") or model.get("id")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if (not model_id or len(model_id) > 128 or
                re.search(r"[\x00-\x1f\x7f]", model_id) or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model_id)):
            continue
        efforts = []
        raw_efforts = (model.get("supported_reasoning_levels") or
                       model.get("supportedReasoningEfforts") or [])
        for effort in raw_efforts[:20] if isinstance(raw_efforts, list) else []:
            value = ((effort.get("effort") or effort.get("reasoningEffort"))
                     if isinstance(effort, dict) else effort)
            if not isinstance(value, str):
                continue
            value = value.strip()
            if (value and len(value) <= 32 and
                    not re.search(r"[\x00-\x1f\x7f]", value) and
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", value) and
                    value not in efforts):
                efforts.append(value)
        display = model.get("display_name") or model.get("displayName") or model_id
        if not isinstance(display, str):
            continue
        display = display.strip()
        if (not display or len(display) > 160 or
                re.search(r"[\x00-\x1f\x7f]", display)):
            continue
        if model_id not in by_id:
            order.append(model_id)
            by_id[model_id] = {"id": model_id, "name": display, "efforts": []}
        for value in efforts:
            if value not in by_id[model_id]["efforts"]:
                by_id[model_id]["efforts"].append(value)
    out = [by_id[model_id] for model_id in order]
    if not out:
        raise CodexError("Codex model cache contains no usable models")
    return out


LEGACY_RUNTIME_OWNER = "fleet_shared"
MANAGED_RUNTIME_OWNER = "managed_daemon"
RUNTIME_MIGRATION_SCHEMA = 1


def codex_control_socket(managed=True, state_dir=None):
    """Return the canonical production socket or Fleet's isolated legacy socket.

    Production uses Codex's documented default control socket so Fleet, the TUI,
    and Remote Control are clients of one runtime. Staging and migration probes
    keep an explicit private socket and can never join production accidentally.
    """
    if managed:
        configured = os.environ.get("FLEET_DASH_CODEX_MANAGED_SOCKET")
        codex_home = os.path.abspath(os.path.expanduser(
            os.environ.get("CODEX_HOME") or os.path.join("~", ".codex")))
        default = os.path.join(codex_home, "app-server-control",
                               "app-server-control.sock")
    else:
        configured = os.environ.get("FLEET_DASH_CODEX_SOCKET")
        default = os.path.join(
            os.path.abspath(os.path.expanduser(
                state_dir or os.path.join("~", ".claude", "fleet-dash"))),
            "codex-app-server.sock")
    return os.path.abspath(os.path.expanduser(configured or default))


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
    socket_path = socket_path or codex_control_socket(managed=False)
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


def ensure_managed_codex_runtime(executable=None, socket_path=None, timeout=12,
                                 runner=None, sleeper=None, clock=None, probe=None,
                                 enable_remote_control=True):
    """Start Codex's managed daemon and wait for its default control socket.

    The command is idempotent. Current npm builds expose this command but the
    manager itself requires the official standalone payload; its start result is
    the authoritative prerequisite gate.
    """
    executable = executable or codex_command()
    socket_path = socket_path or codex_control_socket(managed=True)
    runner = runner or subprocess.run
    sleeper = sleeper or time.sleep
    clock = clock or time.monotonic
    probe = probe or _socket_accepting
    with _shared_runtime_lock:
        env = os.environ.copy()
        command_dir = os.path.dirname(os.path.abspath(executable))
        env["PATH"] = command_dir + os.pathsep + env.get("PATH", "")
        if not probe(socket_path):
            try:
                started = runner(
                    [executable, "app-server", "daemon", "start"],
                    capture_output=True, text=True, timeout=timeout, env=env)
            except Exception as exc:
                raise CodexError(f"Codex managed daemon failed to start: {exc}") from exc
            if started.returncode:
                detail = (started.stderr or started.stdout or
                          f"exit {started.returncode}").strip()
                raise CodexError("Codex managed daemon failed to start: " + detail)
            deadline = clock() + timeout
            while clock() < deadline:
                if probe(socket_path):
                    break
                sleeper(.05)
            else:
                raise CodexError("Codex managed daemon socket did not become ready")
        if enable_remote_control:
            enabled = runner(
                [executable, "app-server", "daemon", "enable-remote-control"],
                capture_output=True, text=True, timeout=timeout, env=env)
            if enabled.returncode:
                detail = (enabled.stderr or enabled.stdout or
                          f"exit {enabled.returncode}").strip()
                raise CodexError("Codex daemon started, but Remote Control could not be enabled: " +
                                 detail)


def _atomic_codex_state_update(path, transform):
    """Serialize migration writes across production/staging and fsync the result."""
    if not path:
        return {}
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    lock_path = path + ".migration.lock"
    with open(lock_path, "a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            try:
                with open(path) as handle:
                    state = json.load(handle) or {}
            except FileNotFoundError:
                state = {}
            except ValueError as exc:
                raise CodexError("Codex runtime state is corrupt; migration did not modify it") from exc
            backup = path + ".pre-managed-daemon.bak"
            if os.path.isfile(path) and not os.path.exists(backup):
                shutil.copyfile(path, backup)
                os.chmod(backup, 0o600)
                with open(backup, "rb") as handle:
                    os.fsync(handle.fileno())
            updated = transform(dict(state))
            tmp = path + ".migration.tmp"
            with open(tmp, "w") as handle:
                json.dump(updated, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            directory = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return updated
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def migrate_codex_runtime_metadata(path, phase="committed", error=None,
                                   blockers=None, clock=None):
    """Checkpoint migration status and convert only proven Fleet-owned threads."""
    now = (clock or time.time)()

    def transform(state):
        migration = dict(state.get("runtime_migration") or {})
        migration.update({
            "schema": RUNTIME_MIGRATION_SCHEMA,
            "source": "fleet_private",
            "target": MANAGED_RUNTIME_OWNER,
            "phase": phase,
            "updated_at": now,
            "blockers": sorted(set(str(value)[:120] for value in (blockers or [])))[:50],
            "error": str(error)[:500] if error else None,
        })
        migration.setdefault("started_at", now)
        thread_meta = dict(state.get("thread_meta") or {})
        converted = 0
        if phase == "committed":
            for tid, raw in list(thread_meta.items()):
                meta = dict(raw or {})
                if meta.get("runtime_owner") == LEGACY_RUNTIME_OWNER:
                    meta.update(runtime_owner=MANAGED_RUNTIME_OWNER,
                                control_runtime=MANAGED_RUNTIME_OWNER)
                    thread_meta[tid] = meta
                    converted += 1
            migration["committed_at"] = now
        migration["converted_threads"] = converted
        state["thread_meta"] = thread_meta
        state["runtime_migration"] = migration
        return state

    return _atomic_codex_state_update(path, transform)


def codex_runtime_migration_needed(state_path, legacy_socket, probe=None):
    """Return true only while an uncommitted private listener still exists."""
    probe = probe or _socket_accepting
    try:
        with open(state_path) as handle:
            state = json.load(handle) or {}
    except (OSError, ValueError):
        state = {}
    migration = state.get("runtime_migration") or {}
    return migration.get("phase") != "committed" and probe(legacy_socket)


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


class CodexRuntimeMigration:
    """Drain Fleet's private listener and atomically rebind to the managed daemon."""

    def __init__(self, executable, state_path, legacy_socket, managed_socket,
                 target_factory, clock=None, runner=None, killer=None, probe=None):
        self.executable = os.path.realpath(executable)
        self.executable_identities = _codex_executable_identities(executable)
        self.state_path = state_path
        self.legacy_socket = os.path.realpath(legacy_socket)
        self.managed_socket = os.path.realpath(managed_socket)
        self.target_factory = target_factory
        self.clock = clock or time.time
        self.runner = runner or subprocess.run
        self.killer = killer or os.kill
        self.probe = probe or _socket_accepting
        self._lock = threading.Lock()
        self._worker = None
        self.phase = "draining"
        self.error = None
        self.blockers = []
        self.converted_threads = 0

    def mutation_blocked(self):
        return self.phase in ("preflight", "switching", "source_stopped")

    def diagnostics(self):
        with self._lock:
            return {"mode": "managed" if self.phase == "committed" else "migrating",
                    "phase": self.phase, "blockers": list(self.blockers),
                    "error": self.error, "converted_threads": self.converted_threads}

    def maybe_migrate(self, adapter):
        """Schedule one bounded attempt; never make Fleet's poll thread wait."""
        with self._lock:
            if self.phase == "committed" or (self._worker and self._worker.is_alive()):
                return
            self._worker = threading.Thread(
                target=self._attempt, args=(adapter,), daemon=True,
                name="fleet-codex-runtime-migration")
            self._worker.start()

    @staticmethod
    def _turn_active(thread):
        turns = thread.get("turns") or [] if isinstance(thread, dict) else []
        if not turns:
            return False
        status = turns[-1].get("status") if isinstance(turns[-1], dict) else None
        if isinstance(status, dict):
            status = status.get("type") or status.get("status")
        return str(status or "").lower() in {
            "inprogress", "in_progress", "running", "started", "pending"}

    @staticmethod
    def _fingerprint(thread):
        if not isinstance(thread, dict):
            return None

        def canonical(value):
            if isinstance(value, dict):
                return {key: canonical(item) for key, item in value.items()
                        if item is not None}
            if isinstance(value, list):
                return [canonical(item) for item in value]
            return value

        stable = {key: thread.get(key) for key in
                  ("id", "cwd", "name", "title", "archived", "parentThreadId", "gitInfo")
                  if key in thread}
        stable["turns"] = thread.get("turns") or []
        raw = json.dumps(canonical(stable), sort_keys=True,
                         separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _owned(self):
        try:
            with open(self.state_path) as handle:
                state = json.load(handle) or {}
        except (OSError, ValueError):
            state = {}
        meta = state.get("thread_meta") or {}
        return [(tid, meta.get(tid) or {}) for tid in (state.get("threads") or [])
                if (meta.get(tid) or {}).get("runtime_owner") == LEGACY_RUNTIME_OWNER]

    def _terminal_blockers(self):
        try:
            result = self.runner(["ps", "-axo", "pid=,command="],
                                 capture_output=True, text=True, timeout=4)
        except Exception:
            return ["legacy terminal check unavailable"]
        if result.returncode:
            return ["legacy terminal check unavailable"]
        endpoint = "unix://" + self.legacy_socket
        blockers = []
        for line in result.stdout.splitlines():
            if endpoint in line and "--remote" in line and "codex" in line:
                blockers.append("legacy terminal attached")
                break
        return blockers

    def _runtime_blockers(self, client, owned):
        blockers = self._terminal_blockers()
        owned_ids = {tid for tid, _ in owned}
        requests = list(getattr(client, "approvals", {}).values())
        if any((item or {}).get("thread_id") in owned_ids and
               (item or {}).get("state", "pending") == "pending" for item in requests):
            blockers.append("pending provider request")
        states = getattr(client, "thread_state", {})
        for tid, meta in owned:
            live = states.get(tid) or {}
            if live.get("compacting") is not None:
                blockers.append("compaction active")
                continue
            if (live.get("status") == "running" or live.get("turn_id") or
                    getattr(client, "owns_active_turn", lambda _tid: False)(tid)):
                blockers.append("turn active")
                continue
            if meta.get("unmaterialized"):
                blockers.append("bootstrap incomplete")
                continue
            try:
                if self._turn_active(client.read_thread(tid)):
                    blockers.append("turn active")
            except Exception:
                blockers.append("source read unavailable")
        return sorted(set(blockers))

    def _listener_pid(self):
        result = self.runner(
            ["lsof", "-nP", "-U", "-a", "-c", "codex", "-Fpcn", self.legacy_socket],
            capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise CodexError("could not identify the legacy Codex listener")
        pids = {int(line[1:]) for line in result.stdout.splitlines()
                if line.startswith("p") and line[1:].isdigit()}
        if len(pids) != 1:
            raise CodexError("legacy Codex listener identity is ambiguous")
        pid = next(iter(pids))
        inspected = self.runner(["ps", "-p", str(pid), "-o", "uid=,command="],
                                capture_output=True, text=True, timeout=5)
        if inspected.returncode or not inspected.stdout.strip():
            raise CodexError("legacy Codex listener disappeared during identity check")
        uid_text, _, command = inspected.stdout.strip().partition(" ")
        try:
            uid = int(uid_text)
            argv = shlex.split(command.strip())
        except (ValueError, OSError) as exc:
            raise CodexError("legacy Codex listener identity is malformed") from exc
        expected = ["app-server", "--listen", "unix://" + self.legacy_socket]
        if uid != os.getuid() or len(argv) < 4 or argv[-3:] != expected or \
                os.path.realpath(argv[0]) not in self.executable_identities:
            raise CodexError("refusing to stop a process that is not the exact legacy listener")
        return pid

    def _retire_legacy(self):
        if not self.probe(self.legacy_socket):
            return
        pid = self._listener_pid()
        self.killer(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self.probe(self.legacy_socket):
                return
            time.sleep(.05)
        raise CodexError("legacy Codex listener did not stop after SIGTERM")

    def _checkpoint(self, phase, error=None, blockers=None):
        state = migrate_codex_runtime_metadata(
            self.state_path, phase=phase, error=error, blockers=blockers,
            clock=self.clock)
        migration = state.get("runtime_migration") or {}
        with self._lock:
            self.phase = phase
            self.error = str(error)[:500] if error else None
            self.blockers = list(blockers or [])
            self.converted_threads = int(migration.get("converted_threads") or 0)

    def _attempt(self, adapter):
        target = None
        try:
            with adapter._runtime_action_lock:
                owned = self._owned()
                blockers = self._runtime_blockers(adapter.client, owned)
                if blockers:
                    self._checkpoint("draining", blockers=blockers)
                    return
                self._checkpoint("preflight")
                source_threads = {}
                for tid, meta in owned:
                    if not meta.get("unmaterialized"):
                        source_threads[tid] = adapter.client.read_thread(tid)
                target = self.target_factory()
                target.start()
                target.list_threads()
                mismatches = []
                for tid, source in source_threads.items():
                    try:
                        candidate = target.read_thread(tid)
                    except Exception:
                        mismatches.append("target history unavailable")
                        continue
                    if self._fingerprint(source) != self._fingerprint(candidate):
                        mismatches.append("history fingerprint mismatch")
                if mismatches:
                    raise CodexError("; ".join(mismatches[:10]))
                self._checkpoint("switching")
                self._retire_legacy()
                self._checkpoint("source_stopped")
                # Commit durable authority before rebinding the in-memory client.
                # A crash on either side of this line has one clear restart owner:
                # legacy before commit, managed after commit.
                self._checkpoint("committed")
                old = adapter.client
                adapter.client = target
                adapter.runtime_owner = MANAGED_RUNTIME_OWNER
                adapter._loaded_threads = set()
                adapter._loaded_generation = None
                adapter._last_refresh = 0
                target = None
                old.close()
        except Exception as exc:
            source_gone = False
            try:
                source_gone = not self.probe(self.legacy_socket)
            except Exception:
                pass
            if target is not None and source_gone:
                # The source has crossed the point of no return. Keep the
                # already-verified target live and retry only the durable commit;
                # never revive or reconnect the retired private runtime.
                old = adapter.client
                adapter.client = target
                adapter._loaded_threads = set()
                adapter._loaded_generation = None
                adapter._last_refresh = 0
                target = None
                try:
                    old.close()
                except Exception:
                    pass
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
            try:
                self._checkpoint("blocked", error=exc)
            except Exception:
                with self._lock:
                    self.phase = "blocked"
                    self.error = str(exc)[:500]


class CodexAdapter:
    PROVIDER = "codex"

    def __init__(self, enabled=True, client=None, state_path=None, clock=None,
                 stall_seconds=180, external_observer=None, dormant_seconds=7200,
                 models_cache_path=None, refresh_budget_seconds=1.5,
                 refresh_workers=4, runtime_owner=LEGACY_RUNTIME_OWNER,
                 runtime_migration=None):
        self.enabled = enabled
        self.client = client or CodexAppServer()
        self.state_path = state_path
        self.clock = clock or time.time
        self.stall_seconds = stall_seconds
        self.dormant_seconds = max(0, float(dormant_seconds))
        self.error = None
        self.error_at = None
        self._sessions = []
        self._refreshing = False
        self._last_refresh = 0
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._mutation_locks_guard = threading.Lock()
        self._mutation_locks = {}
        # Refresh commits and accepted provider mutations both replace the live
        # projection. This barrier plus settings revisions prevents a refresh
        # built from pre-save data from winning after a successful save.
        self._projection_commit_lock = threading.RLock()
        self.models = []
        self._model_error = None
        self._model_error_at = None
        self._models_cache_path = os.path.abspath(os.path.expanduser(
            models_cache_path or "~/.codex/models_cache.json"))
        self._models_cache_signature = None
        self._loaded_threads = set()
        self._loaded_generation = None
        self._loaded_error = None
        self._loaded_error_at = None
        self._account = None
        self._account_error = None
        self._account_error_at = None
        self._last_account_refresh = 0
        self._account_refreshing = False
        self._skills = {}
        self._detail_errors = {}
        self._detail_retry_after = {}
        self._refresh_budget_seconds = max(.05, float(refresh_budget_seconds))
        self._detail_executor = ThreadPoolExecutor(
            max_workers=max(1, min(8, int(refresh_workers))),
            thread_name_prefix="fleet-codex-detail")
        self._refresh_diagnostics = deque(maxlen=50)
        self.external_observer = external_observer
        self._tracked_external = set()
        self.runtime_owner = runtime_owner
        self.runtime_migration = runtime_migration
        self._runtime_action_lock = threading.RLock()

    def _owns_metadata(self, meta):
        return (meta or {}).get("runtime_owner") == self.runtime_owner

    def _mutation_lock(self, thread_id):
        with self._mutation_locks_guard:
            return self._mutation_locks.setdefault(thread_id, threading.RLock())

    @staticmethod
    def key(native_id):
        return f"codex:{native_id}"

    @staticmethod
    def native(key):
        return key.split(":", 1)[1] if str(key).startswith("codex:") else key

    def _owns_active_turn(self, thread_id):
        checker = getattr(self.client, "owns_active_turn", None)
        if checker:
            return bool(checker(thread_id))
        # Test/alternate clients predate connection generations. Production's
        # CodexAppServer always uses the strict generation check above.
        return bool(self.client.thread_state.get(thread_id, {}).get("turn_id"))

    def _settings_runtime_blocker(self, thread_id):
        """Return why native settings cannot change at this exact instant."""
        client_lock = getattr(self.client, "lock", None)
        if client_lock:
            with client_lock:
                live = dict(self.client.thread_state.get(thread_id, {}) or {})
                requests = [dict(item) for item in self.client.approvals.values()
                            if isinstance(item, dict)]
        else:
            live = dict(self.client.thread_state.get(thread_id, {}) or {})
            requests = [dict(item) for item in self.client.approvals.values()
                        if isinstance(item, dict)]
        pending = any(request.get("thread_id") == thread_id and
                      request.get("state", "pending") == "pending"
                      for request in requests)
        if live.get("compacting") is not None:
            return "Codex is compacting; wait for compaction to finish", live
        if pending:
            return "Codex is waiting on a request; answer it before changing settings", live
        if (self._owns_active_turn(thread_id) or live.get("status") == "running" or
                live.get("turn_id")):
            return "Codex model and effort can change only while the turn is idle", live
        return None, live

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
        if self.runtime_migration:
            self.runtime_migration.maybe_migrate(self)
        with self._lock:
            if not self._refreshing and self.clock() - self._last_refresh >= 2:
                self._refreshing = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return [dict(s) for s in self._sessions]

    def _stale_session(self, session, reason):
        stale = {**session, "capabilities": dict(session.get("capabilities") or {})}
        previous_state = stale.get("state")
        if previous_state != "stale":
            stale["stale_previous_state"] = previous_state
        can_queue = not stale.get("read_only")
        stale.update(state="stale", stale=True, stale_reason=str(reason)[:1000],
                     control_state="reconnecting" if can_queue else "view_only",
                     queue_accepting=can_queue)
        capabilities = stale["capabilities"]
        for name in ("submit", "interrupt", "archive", "close", "compact", "review",
                     "answer_structured", "decide_approval", "relay_agent",
                     "change_model_effort"):
            capabilities[name] = False
        capabilities["queue_submit"] = can_queue
        return stale

    def _mark_refresh_failure(self, exc):
        now = self.clock()
        reason = str(exc)[:1000]
        with self._lock:
            self.error = reason
            self.error_at = now
            self._sessions = [self._stale_session(session, reason)
                              for session in self._sessions]
            self._refresh_diagnostics.append({"ts": now, "error": reason})

    @staticmethod
    def _thread_shape_error(thread):
        """Reject malformed provider rows before normalization can poison a pass."""
        if not isinstance(thread, dict):
            return "thread row is not an object"
        if not isinstance(thread.get("id"), str) or not thread.get("id"):
            return "thread row has no string id"
        if thread.get("gitInfo") is not None and not isinstance(thread.get("gitInfo"), dict):
            return "thread gitInfo is not an object"
        status = thread.get("status")
        if status is not None and not isinstance(status, (dict, str)):
            return "thread status has an invalid shape"
        if isinstance(status, dict):
            flags = status.get("activeFlags")
            if flags is not None and (not isinstance(flags, list) or
                                      any(not isinstance(flag, str) for flag in flags)):
                return "thread active flags have an invalid shape"
        turns = thread.get("turns")
        if turns is not None and not isinstance(turns, list):
            return "thread turns are not a list"
        for turn in turns or []:
            if not isinstance(turn, dict):
                return "thread contains a malformed turn"
            items = turn.get("items")
            if items is not None and not isinstance(items, list):
                return "thread items are not a list"
            for item in items or []:
                if not isinstance(item, dict):
                    return "thread contains a malformed item"
                if item.get("type") == "collabAgentToolCall":
                    states = item.get("agentsStates")
                    receivers = item.get("receiverThreadIds")
                    if states is not None and not isinstance(states, dict):
                        return "subagent states are not an object"
                    if receivers is not None and (not isinstance(receivers, list) or
                                                  any(not isinstance(aid, str)
                                                      for aid in receivers)):
                        return "subagent receiver ids are invalid"
                if item.get("type") == "subAgentActivity":
                    aid = item.get("agentThreadId")
                    path = item.get("agentPath")
                    if aid is not None and not isinstance(aid, str):
                        return "subagent id is invalid"
                    if path is not None and not isinstance(path, str):
                        return "subagent path is invalid"
        return None

    def _read_threads_bounded(self, thread_ids, deadline):
        ids = list(dict.fromkeys(str(tid) for tid in thread_ids if tid))
        if not ids:
            return {}, {}, set()
        if deadline <= time.monotonic():
            return {}, {}, set(ids)
        futures = {self._detail_executor.submit(self.client.read_thread, tid): tid
                   for tid in ids}
        remaining = max(0, deadline - time.monotonic())
        done, pending = wait(futures, timeout=remaining)
        results, errors = {}, {}
        for future in done:
            tid = futures[future]
            try:
                detail = future.result()
                shaped = ({"id": tid, **detail} if isinstance(detail, dict) else detail)
                shape_error = self._thread_shape_error(shaped)
                if shape_error:
                    errors[tid] = shape_error
                else:
                    results[tid] = detail
            except Exception as exc:
                errors[tid] = str(exc)[:1000]
        deferred = {futures[future] for future in pending}
        for future in pending:
            future.cancel()
        return results, errors, deferred

    def _enrich_agents_bounded(self, sessions, deadline):
        targets = {}
        for session in sessions:
            if session.get("stale"):
                continue
            for agent in session.get("agents") or []:
                # Child thread ids are immutable. Once canonical child detail
                # proves a terminal state, the parent projection's historical
                # started/interacted rows can never make that child live again.
                if agent.get("state") in ("done", "ended"):
                    continue
                aid = agent.get("agent_id")
                if aid:
                    targets.setdefault(str(aid), []).append(agent)
        details, _errors, _deferred = self._read_threads_bounded(targets, deadline)
        for aid, child in details.items():
            turns = child.get("turns") or []
            last_status = turns[-1].get("status") if turns else None
            child_status = child.get("status") or {}
            child_status = (child_status.get("type") if
                            isinstance(child_status, dict) else child_status)
            for agent in targets.get(aid, []):
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
                child_usage = self.client.thread_state.get(aid, {}).get(
                    "token_usage") or {}
                if child_usage:
                    agent["total_tokens"] = _usage_total(child_usage)
                    agent["tokens"] = _token_breakdown(child_usage)
        for session in sessions:
            agents = session.get("agents") or []
            session["agents_running"] = sum(
                agent.get("state") in ("running", "stalled") for agent in agents)

    def _refresh(self):
        try:
            self._refresh_once()
        except Exception as exc:
            self._mark_refresh_failure(exc)
        finally:
            with self._lock:
                self._refreshing = False
                self._last_refresh = self.clock()

    def _refresh_once(self):
        try:
            threads = self.client.list_threads()
        except Exception as exc:
            self._mark_refresh_failure(exc)
            return
        now = self.clock()
        loaded = set()
        if hasattr(self.client, "loaded_thread_ids"):
            try:
                for entry in self.client.loaded_thread_ids():
                    loaded.add(entry.get("id") if isinstance(entry, dict) else entry)
                loaded.discard(None)
                with self._lock:
                    self._loaded_threads = set(loaded)
                    self._loaded_generation = getattr(self.client, "generation", None)
                    self._loaded_error = None
                    self._loaded_error_at = None
            except Exception as exc:
                with self._lock:
                    self._loaded_error = str(exc)
                    self._loaded_error_at = now
                # Loaded-state discovery is advisory. Never detach owned sessions
                # or resume every remembered thread because this one call failed.
                loaded = set()
        self._refresh_local_models(now)
        self._schedule_account_refresh(now)
        persisted = self._state()
        modes = dict(persisted.get("modes") or {})
        thread_meta = dict(persisted.get("thread_meta") or {})
        managed = {tid for tid in (persisted.get("threads") or [])
                   if self._owns_metadata(thread_meta.get(tid) or {})}
        with self._lock:
            tracked_external = set(self._tracked_external)
            previous_by_tid = {
                item.get("native_session_id"): {
                    **item, "capabilities": dict(item.get("capabilities") or {})}
                for item in self._sessions if item.get("native_session_id")}
        out, listed, clean_threads = [], set(), []
        for candidate in threads:
            shape_error = self._thread_shape_error(candidate)
            if shape_error:
                tid = candidate.get("id") if isinstance(candidate, dict) else None
                if isinstance(tid, str) and tid:
                    listed.add(tid)
                    previous = previous_by_tid.get(tid)
                    if not previous and tid in managed:
                        previous = self._stub_session(
                            tid, thread_meta.get(tid) or {}, modes.get(tid) or "default")
                    if previous:
                        out.append(self._stale_session(previous, shape_error))
                self._refresh_diagnostics.append(
                    {"ts": now, "thread_id": tid, "error": shape_error})
                continue
            if candidate.get("parentThreadId"):
                continue
            clean_threads.append(candidate)
            listed.add(candidate["id"])

        detail_deadline = time.monotonic() + self._refresh_budget_seconds
        detail_candidates = []
        for thread in clean_threads:
            tid = thread["id"]
            adoptable = tid in loaded
            if (tid in managed or adoptable) and now >= self._detail_retry_after.get(tid, 0):
                detail_candidates.append(tid)
        missing_materialized = {
            tid for tid in managed - listed
            if not (thread_meta.get(tid) or {}).get("unmaterialized")}
        detail_candidates.extend(tid for tid in missing_materialized
                                 if now >= self._detail_retry_after.get(tid, 0))
        detail_results, detail_failures, detail_deferred = self._read_threads_bounded(
            detail_candidates, detail_deadline)
        for tid, error in detail_failures.items():
            self._detail_errors[tid] = error
            self._detail_retry_after[tid] = now + 30
        for tid in detail_results:
            self._detail_errors.pop(tid, None)
            self._detail_retry_after.pop(tid, None)
        for tid in missing_materialized:
            detail = detail_results.get(tid)
            if detail:
                clean_threads.append({"id": tid, **detail})

        # App Server exposes external threads as notLoaded and omits their live
        # turns. Observe only the newest bounded slice of their local rollouts so
        # active CLI/Desktop work can remain in Now without scanning the archive.
        recent_external = []
        for thread in clean_threads:
            tid = thread.get("id")
            if not tid or tid in managed:
                continue
            updated = _epoch(thread.get("updatedAt") or thread.get("createdAt"))
            if updated is None or now - updated > EXTERNAL_OBSERVATION_SECONDS:
                continue
            recent_external.append((updated, tid))
        recent_external = {
            tid for _, tid in sorted(recent_external, reverse=True)[
                :MAX_RECENT_EXTERNAL_OBSERVATIONS]
        }

        for thread in clean_threads:
            tid = thread.get("id")
            if tid in listed and any(item.get("native_session_id") == tid for item in out):
                continue
            listed.add(tid)
            source = thread.get("source") or "unknown"
            # App Server also writes `source: vscode` for Fleet's own rich-client
            # threads and remote CLI clients. The exact managed daemon's loaded
            # list is runtime evidence; this presentation label is not.
            is_managed = tid in managed
            # A CLI connected with `codex --remote unix://...` is another client
            # of Fleet's canonical runtime. Adopt it automatically so both
            # surfaces steer the same live turn instead of resuming a copy.
            if tid in loaded and not is_managed:
                self._remember(tid, modes.get(tid) or "default", {
                    "runtime_owner": self.runtime_owner, "origin": source,
                    "cwd": thread.get("cwd") or "", "model": thread.get("model") or "",
                    "effort": thread.get("effort"), "name": thread.get("name"),
                    "created_at": _epoch(thread.get("createdAt")) or now,
                    "unmaterialized": False})
                managed.add(tid)
                is_managed = True
            observation = None
            if (not is_managed and self.external_observer and
                    (tid in tracked_external or tid in recent_external)):
                try:
                    observation = self.external_observer.observe(tid)
                except Exception as exc:
                    observation = {"error": str(exc), "messages": []}
            detail_error = None
            if is_managed:
                retry_at = self._detail_retry_after.get(tid, 0)
                if now < retry_at:
                    detail_error = self._detail_errors.get(tid)
                elif tid in detail_results:
                    thread = {**thread, **detail_results[tid]}
                elif tid in detail_deferred:
                    detail_error = "detail refresh deferred by the bounded refresh budget"
                else:
                    detail_error = detail_failures.get(tid)
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
            turn_started = turn_lifecycle.get("started_at")
            no_active_turn_at = _epoch(live.get("no_active_turn_at"))
            provider_proved_idle = bool(no_active_turn_at and
                                        (turn_started is None or
                                         turn_started <= no_active_turn_at))
            native_running = ((live.get("status") == "running" or
                               recorded_type == "active") and
                              not provider_proved_idle)
            observed_running = turn_lifecycle.get("active", False) and (
                completed_epoch is None or turn_started is None or turn_started > completed_epoch)
            running = native_running or observed_running or bool(
                (observation or {}).get("active"))
            quiet = max(0, now - updated_epoch)
            turn_error = _error_text(turn_lifecycle.get("error"))
            provider_error = _error_text(live.get("error") or thread.get("error") or
                                         (recorded.get("error") if
                                          isinstance(recorded, dict) else None) or
                                         turn_error)
            blocked = (live.get("status") == "blocked" or
                       _is_limit_error(provider_error))
            if blocked:
                state = "blocked"
            elif provider_error or recorded_type == "systemError":
                state = "error"
            elif pending or flags.intersection({"waitingOnApproval", "waitingOnUserInput"}):
                state = "needs_you"
            elif running and quiet > self.stall_seconds:
                state = "stalled"
            elif running:
                state = "running"
            elif completed_epoch is not None and 0 <= now - completed_epoch < 90:
                state = "turn_done"
            elif recorded_type == "notLoaded" and quiet > self.dormant_seconds:
                state = "dormant"
            else:
                state = "idle"
            cwd = thread.get("cwd") or ""
            usage = live.get("token_usage") or {}
            mode = live.get("collaboration_mode") or modes.get(tid) or "default"
            persisted_meta = thread_meta.get(tid) or {}
            # thread/start and thread/resume report the selected model/effort,
            # but thread/list and thread/read do not. Keep Fleet's saved values
            # as the durable restart/compaction fallback for owned threads.
            # For owned live threads, App Server's state is newer than a
            # thread/read projection built before a settings update completed.
            model = ((live.get("model") or thread.get("model")) if is_managed else
                     (thread.get("model") or live.get("model"))) or \
                    persisted_meta.get("model") or ""
            if is_managed:
                effort = (live.get("effort") if "effort" in live else
                          thread.get("effort") if "effort" in thread else
                          persisted_meta.get("effort"))
            else:
                effort = (thread.get("effort") if "effort" in thread else
                          live.get("effort") if "effort" in live else
                          persisted_meta.get("effort"))
            settings_revision = int(persisted_meta.get("settings_revision") or 0)
            if is_managed:
                discovered_meta = {}
                discovered_model = live.get("model") or thread.get("model")
                effort_present = "effort" in live or "effort" in thread
                discovered_effort = (live.get("effort") if "effort" in live else
                                     thread.get("effort") if "effort" in thread else None)
                if discovered_model and discovered_model != persisted_meta.get("model"):
                    discovered_meta["model"] = discovered_model
                if effort_present and discovered_effort != persisted_meta.get("effort"):
                    discovered_meta["effort"] = discovered_effort
                if discovered_meta:
                    discovered_meta["settings_revision"] = settings_revision + 1
                    with self._projection_commit_lock:
                        try:
                            if self._remember(tid, mode, discovered_meta,
                                              expected_settings_revision=settings_revision):
                                settings_revision += 1
                        except Exception as exc:
                            # Runtime state remains canonical even when the local
                            # durability file is temporarily unwritable.
                            self._refresh_diagnostics.append({"ts": now, "thread_id": tid,
                                "error": "settings metadata persistence failed: " + str(exc)[:500]})
            ctx_tokens = _usage_total(usage)
            ctx_window = _usage_window(usage)
            files = _files(thread, cwd)
            messages = _conversation(thread)
            if (observation or {}).get("messages"):
                messages = observation["messages"]
            agents = _agents(thread, tid)
            previous_agents = {
                item.get("agent_id"): item
                for item in ((previous_by_tid.get(tid) or {}).get("agents") or [])
                if item.get("agent_id")}
            for agent in agents:
                previous_agent = previous_agents.get(agent.get("agent_id"))
                if (previous_agent or {}).get("state") not in ("done", "ended"):
                    continue
                # Parent thread/read does not emit a completion activity for
                # every Codex child. Preserve terminal child detail across a
                # later partial refresh instead of resurrecting it from the
                # parent's old started/interacted event.
                agent["state"] = previous_agent["state"]
                for key in ("convo_v", "model", "total_tokens", "tokens",
                            "started", "last", "last_msg"):
                    if previous_agent.get(key) is not None:
                        agent[key] = previous_agent[key]
            agents_running = sum(a["state"] in ("running", "stalled") for a in agents)
            revision = _revision(thread, live)
            if observation and observation.get("revision"):
                revision = observation["revision"]
            if is_managed and not detail_error:
                self._cache_snapshot(tid, messages, files, revision, thread)
            owned_turn = is_managed and self._owns_active_turn(tid)
            can_interrupt = (is_managed and owned_turn and
                             state in ("running", "stalled", "needs_you"))
            uncontrolled_active = (state in ("running", "stalled", "needs_you") and
                                   not can_interrupt)
            control_state = ("connected_active" if owned_turn else
                             "reconnecting" if is_managed and uncontrolled_active else
                             "connected_idle" if is_managed else "view_only")
            materialized = not bool((thread_meta.get(tid) or {}).get("unmaterialized"))
            settings_supported = bool(is_managed and materialized and self.models)
            compacting = live.get("compacting") is not None
            can_change_settings = bool(settings_supported and state not in
                ("running", "stalled", "needs_you", "blocked", "error", "stale") and
                not compacting)
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
                "settings_revision": settings_revision,
                "collaboration_mode": mode, "running": None,
                "last_msg": (_last_message(messages, thread.get("preview")) or
                             (previous_by_tid.get(tid) or {}).get("last_msg")),
                "_latest_prose": _latest_prose(messages),
                "repo_outcome": observed_test_outcome(
                    messages, session_id=self.key(tid), provider="codex"),
                "state": state, "reg_status": reg_status,
                "headless": not is_managed, "read_only": not is_managed,
                "read_only_reason": (
                    None if is_managed else
                    "This thread is not loaded in Fleet's managed App Server; "
                    "its transcript is view only"),
                "codex_source": self.runtime_owner if is_managed else "external",
                "codex_provider_source": source,
                "observed_external": bool(observation),
                "observation_confidence": (observation or {}).get("confidence"),
                "observation_warning": ((observation or {}).get("warning") or
                                        (observation or {}).get("error")),
                "control_state": control_state,
                "queue_accepting": bool(is_managed and uncontrolled_active and
                                        state not in ("blocked", "error", "stale")),
                "quiet_s": round(quiet),
                "ctx_tokens": ctx_tokens,
                "ctx_window": ctx_window,
                "ctx_pct": round(100 * ctx_tokens / ctx_window, 1) if ctx_window else None,
                "total_tokens": _usage_cumulative(usage),
                "cost": None, "cost_source": "unavailable", "bridge_url": None,
                "started_ms": _millis(thread.get("createdAt")), "pending": pending,
                "compacting": live.get("compacting"), "muted": False,
                "convo_v": revision, "files_n": len(files), "agents": agents,
                "agents_running": agents_running, "agents_total": len(agents),
                "agent_cost": None, "stale": False,
                "error": provider_error or None, "refresh_warning": detail_error,
                "capabilities": {"submit": is_managed and
                    state not in ("blocked", "error", "stale") and
                    not uncontrolled_active and not compacting,
                    "queue_submit": bool(is_managed and (uncontrolled_active or compacting) and
                                         state not in ("blocked", "error", "stale")),
                    "interrupt": can_interrupt,
                    "takeover": False, "archive": is_managed,
                    "close": is_managed and not uncontrolled_active,
                    "compact": is_managed and state not in
                        ("running", "stalled", "needs_you") and not compacting,
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
                    "relay_agent": is_managed and not uncontrolled_active and not compacting,
                    "relay_agent_direct": False,
                    "model_effort_settings": settings_supported,
                    "change_model_effort": can_change_settings,
                    "change_model_effort_reason": (
                        "Available after compaction finishes" if settings_supported and compacting else
                        "Available when the Codex turn is idle" if settings_supported and
                        not can_change_settings else
                        "Codex's local model catalog is unavailable" if is_managed and
                        materialized and not self.models else
                        "External Codex runtime is view only" if not is_managed else
                        "Fleet is creating this session" if not materialized else ""),
                    "account_usage": bool(self._account), "exact_cost": False,
                    "measured_throughput": False},
            })
        # App Server assigns a thread ID before the first turn materializes a
        # rollout. Keep that short-lived shell only while the canonical runtime
        # still reports it loaded. Without either runtime state or a rollout, the
        # ID can never be resumed or used and must not become an Available ghost.
        for tid in managed - listed:
            meta = thread_meta.get(tid) or {}
            live = self.client.thread_state.get(tid, {})
            previous = previous_by_tid.get(tid)
            runtime_present = (tid in loaded or live.get("status") == "running" or
                               bool(live.get("turn_id")))
            missing_error = (self._detail_errors.get(tid) or
                             ("detail refresh deferred by the bounded refresh budget"
                              if tid in detail_deferred else None))
            if not meta.get("unmaterialized") and missing_error and not runtime_present:
                fallback = previous or self._stub_session(
                    tid, meta, modes.get(tid) or "default")
                out.append(self._stale_session(fallback, missing_error))
                continue
            created_at = float(meta.get("created_at") or 0)
            recently_created = bool(created_at and 0 <= now - created_at < 30)
            # thread/start and turn/start can become usable before thread/list
            # exposes the new row. Preserve the locally owned card through that
            # propagation window instead of making it disappear from Fleet.
            if runtime_present or (previous and recently_created):
                fallback = previous or self._stub_session(
                    tid, meta, modes.get(tid) or "default")
                pending = self._pending(tid, live.get("pending"))
                completed_at = _epoch(live.get("completed_at"))
                owned_turn = self._owns_active_turn(tid)
                active = live.get("status") == "running" or owned_turn
                if pending:
                    state = "needs_you"
                elif active:
                    state = "running"
                elif completed_at is not None and 0 <= now - completed_at < 90:
                    state = "turn_done"
                else:
                    state = fallback.get("state") or "idle"
                fallback.update(state=state, pending=pending, stale=False,
                                error=live.get("error"),
                                reg_status=("running" if state == "running" else
                                            "turn_done" if state == "turn_done" else
                                            live.get("status") or
                                            fallback.get("reg_status")))
                capabilities = fallback["capabilities"]
                uncontrolled_active = state in ("running", "needs_you") and not owned_turn
                compacting = live.get("compacting") is not None
                settings_supported = bool(capabilities.get("model_effort_settings"))
                capabilities.update(
                    submit=not uncontrolled_active and not compacting,
                    queue_submit=uncontrolled_active or compacting,
                    interrupt=owned_turn and state in ("running", "needs_you"),
                    close=not uncontrolled_active,
                    compact=not uncontrolled_active and not compacting,
                    change_model_effort=(settings_supported and not uncontrolled_active and
                                         state not in ("running", "needs_you") and not compacting),
                    change_model_effort_reason=(
                        "Available after compaction finishes" if compacting else
                        "Available when the Codex turn is idle" if settings_supported and
                        state in ("running", "needs_you") else ""),
                    focus_terminal=False,
                    focus_terminal_mode=None,
                    focus_terminal_label=("turn active" if state in
                                          ("running", "needs_you") else "starting"),
                    focus_terminal_reason=(
                        "Wait for the current Codex turn to finish before attaching"
                        if state in ("running", "needs_you") else
                        "Fleet is syncing this Codex session with the shared App Server"))
                fallback["control_state"] = ("connected_active" if owned_turn else
                                             "reconnecting" if uncontrolled_active else
                                             "connected_idle")
                fallback["queue_accepting"] = uncontrolled_active
                out.append(fallback)
                continue
            if meta.get("unmaterialized"):
                if tid in loaded:
                    out.append(self._stub_session(tid, meta,
                                                  modes.get(tid) or "default"))
                else:
                    self._forget(tid)
        # Parent detail and child lifecycle reads are separate bounded phases.
        # Reusing the parent deadline meant it was normally expired before a
        # single child could be checked, leaving completed agents active forever.
        agent_deadline = time.monotonic() + self._refresh_budget_seconds
        self._enrich_agents_bounded(out, agent_deadline)
        with self._projection_commit_lock:
            latest = self._state()
            latest_meta = latest.get("thread_meta") or {}
            latest_modes = latest.get("modes") or {}
            for session in out:
                tid = session.get("native_session_id")
                meta = latest_meta.get(tid) or {}
                if not self._owns_metadata(meta):
                    continue
                latest_revision = int(meta.get("settings_revision") or 0)
                projected_revision = int(session.get("settings_revision") or 0)
                if latest_revision > projected_revision:
                    if meta.get("model"):
                        session["model"] = meta["model"]
                    if "effort" in meta:
                        session["effort"] = meta.get("effort")
                    session["settings_revision"] = latest_revision
                if latest_modes.get(tid) in ("plan", "default"):
                    session["collaboration_mode"] = latest_modes[tid]
            with self._lock:
                self._sessions = out
                self.error = None
                self.error_at = None
                self._refreshing = False
                self._last_refresh = self.clock()

    def _refresh_local_models(self, now):
        """Refresh model options from Codex's own cache, never its control socket."""
        signature = None
        try:
            stat = os.stat(self._models_cache_path)
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._models_cache_signature:
                return
            models = _local_model_catalog(self._models_cache_path)
        except Exception as exc:
            with self._lock:
                # A changed cache that no longer parses must fail closed. Keeping
                # the prior catalog would advertise settings the provider no
                # longer says are valid.
                if signature != self._models_cache_signature:
                    self.models = []
                    self._models_cache_signature = signature
                self._model_error = str(exc)
                self._model_error_at = now
            return
        with self._lock:
            self.models = models
            self._models_cache_signature = signature
            self._model_error = None
            self._model_error_at = None

    def _schedule_account_refresh(self, now):
        with self._lock:
            if self._account_refreshing or now - self._last_account_refresh < 30:
                return
            self._account_refreshing = True

        def refresh():
            try:
                self._refresh_account(now)
            finally:
                with self._lock:
                    self._account_refreshing = False
        threading.Thread(target=refresh, daemon=True,
                         name="fleet-codex-usage").start()

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

    def diagnostics(self):
        with self._lock:
            protocol = list(getattr(self.client, "diagnostics", []))
            counts = {}
            for item in protocol:
                kind = str(item.get("kind") or "unknown")
                counts[kind] = counts.get(kind, 0) + 1
            out = {
                "provider_error": self.error,
                "provider_error_at": self.error_at,
                "model_error": self._model_error,
                "model_error_at": self._model_error_at,
                "model_catalog_size": len(self.models),
                "loaded_error": self._loaded_error,
                "loaded_error_at": self._loaded_error_at,
                "account_error": self._account_error,
                "account_error_at": self._account_error_at,
                "transport_error": getattr(self.client, "last_error", None),
                "transport_generation": getattr(self.client, "generation", None),
                "loaded_threads": len(self._loaded_threads),
                "visible_sessions": len(self._sessions),
                "refresh_errors": list(self._refresh_diagnostics),
                "protocol_events": counts,
            }
            if self.runtime_migration:
                out["runtime"] = self.runtime_migration.diagnostics()
            else:
                out["runtime"] = {"mode": ("managed" if self.runtime_owner ==
                                             MANAGED_RUNTIME_OWNER else "private"),
                                  "phase": "committed" if self.runtime_owner ==
                                           MANAGED_RUNTIME_OWNER else "isolated",
                                  "blockers": [], "error": None}
            return out

    def runtime_status(self):
        if self.runtime_migration:
            return self.runtime_migration.diagnostics()
        return {"mode": ("managed" if self.runtime_owner == MANAGED_RUNTIME_OWNER
                          else "private"),
                "phase": ("committed" if self.runtime_owner == MANAGED_RUNTIME_OWNER
                           else "isolated"),
                "blockers": [], "error": None}

    def _managed(self):
        state = self._state()
        meta = state.get("thread_meta") or {}
        return [tid for tid in (state.get("threads") or [])
                if self._owns_metadata(meta.get(tid) or {})]

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

    def _remember(self, tid, mode=None, meta=None, expected_settings_revision=None):
        if not self.state_path or not tid:
            return False
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
            current_meta = dict(thread_meta.get(tid) or {})
            if expected_settings_revision is not None and int(
                    current_meta.get("settings_revision") or 0) != int(
                        expected_settings_revision):
                return False
            thread_meta[tid] = {**current_meta, **(meta or {}),
                                "runtime_owner": self.runtime_owner,
                                "control_runtime": self.runtime_owner}
            state["thread_meta"] = {key: value for key, value in thread_meta.items()
                                    if key in kept}
            self._save_state(state)
            return True

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
                              "files": files[-100:], "agents": _agents(thread, tid)[-100:],
                              "updated_at": self.clock(),
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

    def _ensure_loaded(self, tid):
        """Load one owned thread on demand without disturbing active runtimes."""
        if not hasattr(self.client, "resume_thread"):
            return {}
        generation = getattr(self.client, "generation", None)
        with self._lock:
            known_loaded = (self._loaded_generation == generation and
                            tid in self._loaded_threads)
        if known_loaded:
            return {}
        # A reconnected Fleet WebSocket does not mean the shared runtime died.
        # Ask the runtime before resuming: thread/resume can abort an active turn.
        if hasattr(self.client, "loaded_thread_ids"):
            try:
                loaded = {entry.get("id") if isinstance(entry, dict) else entry
                          for entry in self.client.loaded_thread_ids()}
                loaded.discard(None)
                with self._lock:
                    self._loaded_threads = loaded
                    self._loaded_generation = getattr(self.client, "generation", None)
                    self._loaded_error = None
                    self._loaded_error_at = None
                if tid in loaded:
                    return {}
            except Exception as exc:
                with self._lock:
                    self._loaded_error = str(exc)
                    self._loaded_error_at = self.clock()
                # Do not guess that a thread is unloaded. Let the exact action
                # return a useful error instead of risking an active-turn abort.
                return {}
        thread = self.client.resume_thread(tid)
        with self._lock:
            self._loaded_threads.add(tid)
            self._loaded_generation = getattr(self.client, "generation", None)
        return thread or {}

    def resume_capability(self, key):
        """Return whether a saved thread is explicitly owned by Fleet's runtime."""
        tid = self.native(key)
        if not self.enabled:
            return False, "Codex App Server is unavailable"
        state = self._state()
        meta = (state.get("thread_meta") or {}).get(tid) or {}
        if tid not in (state.get("threads") or []) or \
                not self._owns_metadata(meta):
            return False, "external Codex thread is view only"
        if meta.get("unmaterialized"):
            return False, "the Codex thread never created a saved conversation"
        return True, None

    def resume_owned_thread(self, key):
        """Load one exact Fleet-owned thread without adopting an external thread."""
        allowed, reason = self.resume_capability(key)
        if not allowed:
            return {"ok": False, "error": reason}
        tid = self.native(key)
        try:
            self._ensure_loaded(tid)
            with self._lock:
                self._last_refresh = 0
            return {"ok": True, "session_id": self.key(tid), "resumed": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_thread(self, cwd, model=None, effort=None, mode="plan",
                     initial_text=None, _runtime_locked=False):
        if not _runtime_locked:
            with self._runtime_action_lock:
                if self.runtime_migration and self.runtime_migration.mutation_blocked():
                    raise CodexError("Codex runtime migration is switching control; retry shortly",
                                     code="provider_control_unavailable", queueable=True)
                return self.start_thread(cwd, model, effort, mode, initial_text,
                                         _runtime_locked=True)
        if mode not in ("plan", "default"):
            raise CodexError("unknown Codex collaboration mode")
        thread = self.client.start_thread(cwd, model, effort)
        tid = thread.get("id")
        resolved_model = thread.get("model") or model or ""
        resolved_effort = thread.get("effort") or effort or ("medium" if mode == "plan" else None)
        if tid and hasattr(self.client, "set_mode"):
            self.client.set_mode(tid, mode, resolved_model, resolved_effort)
        if tid:
            with self._lock:
                self._loaded_threads.add(tid)
                self._loaded_generation = getattr(self.client, "generation", None)
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
                    "settings_revision": int(meta.get("settings_revision") or 0),
                    "collaboration_mode": mode, "running": None,
                    "last_msg": None, "state": "idle",
                    "reg_status": "loaded", "quiet_s": 0, "ctx_tokens": 0,
                    "ctx_window": None,
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
                    "relay_agent_direct": False, "model_effort_settings": False,
                    "change_model_effort": False,
                    "change_model_effort_reason": "Fleet is creating this session",
                    "account_usage": False,
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
                    "agents": _agents(thread, tid),
                    "revision": revision}
        except Exception as exc:
            if "not materialized yet" in str(exc):
                return {"ok": True, "messages": [], "files": [], "agents": []}
            snapshot = (self._state().get("snapshots") or {}).get(tid)
            if snapshot:
                return {"ok": True, "messages": snapshot.get("messages") or [],
                        "files": snapshot.get("files") or [],
                        "agents": snapshot.get("agents") or [], "closed": True,
                        "stale": True, "error": str(exc), "info": snapshot.get("info") or {},
                        "revision": snapshot.get("revision")}
            return {"ok": False, "error": str(exc)}

    def agent_context(self, key, agent_id):
        parent_id = self.native(key)
        agent_id = str(agent_id or "")
        with self._lock:
            parent = next((item for item in self._sessions
                           if item.get("native_session_id") == parent_id), None)
            member = next((item for item in (parent or {}).get("agents") or []
                           if str(item.get("agent_id") or "") == agent_id), None)
        if not parent:
            historical = self.context(key)
            member = next((item for item in (historical.get("agents") or [])
                           if str(item.get("agent_id") or "") == agent_id), None)
        if not member:
            return {"ok": False, "error": "no such subagent"}
        out = self.context(self.key(agent_id))
        if out.get("ok"):
            usage = self.client.thread_state.get(agent_id, {}).get("token_usage") or {}
            ctx_tokens = _usage_total(usage) if usage else None
            ctx_window = _usage_window(usage) if usage else None
            out["info"] = {"agent_id": agent_id, "agent_type": "codex",
                           "description": "Codex subagent", "model": "",
                           "family": "codex", "tokens": _token_breakdown(usage),
                           "total_tokens": _usage_cumulative(usage) if usage else None,
                           "ctx_tokens": ctx_tokens, "ctx_window": ctx_window,
                           "ctx_pct": (round(100 * ctx_tokens / ctx_window, 1)
                                       if ctx_window else None),
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
                 ".svg": "image/svg+xml", ".pdf": "application/pdf",
                 ".json": "application/json; charset=utf-8"}.get(
                     ext, "text/plain; charset=utf-8")
        # Keep HTML inert at the authenticated endpoint. The browser client
        # renders it only inside its sandboxed static-preview iframe.
        return ctype, data, None

    def act(self, action, _mutation_locked=False, _runtime_locked=False):
        if not _runtime_locked:
            with self._runtime_action_lock:
                if self.runtime_migration and self.runtime_migration.mutation_blocked():
                    return {"ok": False,
                            "error": "Codex runtime migration is switching control; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                return self.act(action, _mutation_locked=_mutation_locked,
                                _runtime_locked=True)
        typ = action.get("type")
        tid = self.native(action.get("session_id"))
        try:
            with self._lock:
                known = next((s for s in self._sessions
                              if s.get("native_session_id") == tid), None)
            meta = (self._state().get("thread_meta") or {}).get(tid) or {}
            if not known:
                return {"ok": False, "error": "unknown Codex session"}
            if known.get("read_only") or not self._owns_metadata(meta):
                return {"ok": False,
                        "error": known.get("read_only_reason") or
                                 "external Codex thread is view only"}
            if known.get("stale") or known.get("state") == "stale":
                if typ in ("text", "image_text") and known.get(
                        "capabilities", {}).get("queue_submit"):
                    return {"ok": False,
                            "error": "Codex control is reconnecting; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                return {"ok": False, "error": "Codex provider state is stale; refresh and retry",
                        "code": "provider_control_unavailable"}
            if (not _mutation_locked and typ in
                    ("text", "image_text", "mode", "session_settings", "compact",
                     "skill", "relay", "review")):
                with self._mutation_lock(tid):
                    # Re-enter so ownership, staleness, capabilities, live turn
                    # state, and settings are all re-read inside the lock.
                    return self.act(action, _mutation_locked=True)
            required_capability = {"text": "submit", "image_text": "submit", "mode": "submit",
                                   "session_settings": "change_model_effort",
                                   "interrupt": "interrupt", "close": "close",
                                   "archive": "archive", "skill": "submit",
                                   "compact": "compact", "review": "review",
                                   "permission": "decide_approval",
                                   "multiq": "answer_structured",
                                   "option": "answer_structured",
                                   "elicitation": "answer_structured",
                                   "dismiss": "decide_approval",
                                   "relay": "relay_agent"}.get(typ)
            if required_capability and not known.get("capabilities", {}).get(
                    required_capability):
                if (required_capability == "submit" and typ in ("text", "image_text") and
                        known.get("capabilities", {}).get("queue_submit")):
                    return {"ok": False,
                            "error": "Codex control is reconnecting; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                if known.get("state") in ("running", "stalled", "needs_you"):
                    return {"ok": False, "error":
                            "Codex is active in another client; control it there until the turn ends"}
                return {"ok": False, "error": f"session does not support {typ}"}
            if typ in ("permission", "multiq", "option", "elicitation", "dismiss"):
                request = self.client.approvals.get(str(action.get("nonce") or ""))
                if not request or request.get("thread_id") != tid:
                    return {"ok": False, "error": "stale Codex request"}
            if typ == "session_settings":
                # The catalog is a server-owned projection of Codex's local
                # credential-free model cache. Client text never reaches the
                # settings protocol unless both values are in that catalog.
                if "expected_model" not in action or "expected_effort" not in action:
                    return {"ok": False,
                            "error": "expected model and effort are required",
                            "code": "stale_settings"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), None)
                    catalog = {str(item.get("id") or ""): dict(item)
                               for item in self.models if item.get("id")}
                if not session or not session.get("capabilities", {}).get(
                        "change_model_effort"):
                    return {"ok": False,
                            "error": "Codex model and effort can change only while the turn is idle"}
                model = str(action.get("model") or "").strip()
                if model not in catalog:
                    return {"ok": False, "error": "unknown Codex model"}
                efforts = [str(value) for value in
                           (catalog[model].get("efforts") or []) if value]
                effort = str(action.get("effort") or "").strip()
                if efforts and effort not in efforts:
                    return {"ok": False,
                            "error": "unsupported effort level for this Codex model"}
                if not efforts and effort:
                    return {"ok": False,
                            "error": "this Codex model does not advertise effort controls"}
                self._ensure_loaded(tid)
                blocker, live = self._settings_runtime_blocker(tid)
                if blocker:
                    return {"ok": False, "error": blocker}
                # Live App Server state is canonical when it reports settings;
                # the fleet projection is only a fallback for omitted fields.
                current_model = str(live.get("model") or session.get("model") or "")
                current_effort = str((live.get("effort") if "effort" in live else
                                      session.get("effort")) or "")
                if (str(action.get("expected_model") or "") != current_model or
                        str(action.get("expected_effort") or "") != current_effort):
                    return {"ok": False,
                            "error": "settings changed in another view — refresh and try again",
                            "code": "stale_settings"}
                # An attached Codex TUI can change collaboration mode without a
                # Fleet refresh. Preserve that live mode while updating model /
                # effort so a settings save cannot silently revert the TUI.
                mode = (live.get("collaboration_mode") or
                        session.get("collaboration_mode") or
                        self._modes().get(tid) or "default")
                self.client.set_mode(tid, mode, model, effort or None)
                # Provider acceptance precedes metadata. A persistence failure
                # must not lie that the provider rejected or roll the UI back.
                warning = None
                durable = False
                with self._projection_commit_lock:
                    latest_meta = ((self._state().get("thread_meta") or {}).get(tid) or {})
                    next_revision = int(latest_meta.get("settings_revision") or 0) + 1
                    try:
                        durable = self._remember(tid, mode, {
                            "model": model, "effort": effort or None,
                            "settings_revision": next_revision},
                            expected_settings_revision=next_revision - 1)
                    except Exception as exc:
                        warning = ("Applied in Codex, but Fleet could not persist the setting: " +
                                   str(exc)[:300])
                    if not durable and warning is None:
                        warning = ("Applied in Codex, but Fleet could not durably save the setting; "
                                   "the live runtime remains updated")
                    with self._lock:
                        for current in self._sessions:
                            if current.get("native_session_id") == tid:
                                current["model"] = model
                                current["effort"] = effort or None
                                current["settings_revision"] = next_revision
                result = {"ok": True, "model": model, "effort": effort or None,
                          "durable": durable}
                if warning:
                    result["warning"] = warning
                return result
            elif typ in ("text", "image_text"):
                text = str(action.get("text") or "").strip()
                image_paths = list(action.get("image_paths") or []) if typ == "image_text" else []
                if not text and not image_paths:
                    return {"ok": False, "error": "empty text"}
                if image_paths and (len(image_paths) > 4 or any(
                        not isinstance(path, str) or not os.path.isabs(path) for path in image_paths)):
                    return {"ok": False, "error": "invalid image inputs"}
                text = text or ("Please inspect the attached image." if len(image_paths) == 1 else
                                "Please inspect the attached images.")
                inputs = (([{"type": "text", "text": text}] +
                           [{"type": "localImage", "path": path} for path in image_paths])
                          if image_paths else None)
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                blocker, live = self._settings_runtime_blocker(tid)
                owned_turn = self._owns_active_turn(tid)
                if blocker and (live.get("compacting") is not None or
                                "waiting on a request" in blocker or not owned_turn):
                    return {"ok": False,
                            "error": blocker + "; message can be queued",
                            "code": "provider_control_unavailable", "queueable": True}
                mode = session.get("collaboration_mode") or self._modes().get(tid) or "default"
                model = live.get("model") or session.get("model")
                effort = (live.get("effort") if "effort" in live else
                          session.get("effort"))
                if mode == "plan" and not model:
                    return {"ok": False, "error": "Codex model is unavailable; refresh and try again"}
                start_after_ended = False
                if owned_turn and hasattr(self.client, "steer_turn"):
                    try:
                        if inputs:
                            self.client.steer_turn(tid, text, inputs=inputs)
                        else:
                            self.client.steer_turn(tid, text)
                        return {"ok": True}
                    except CodexError as exc:
                        # The provider definitively rejected this steer before
                        # accepting input. Starting the same payload is safe;
                        # mismatched/ambiguous active turns remain queueable.
                        if exc.code != "turn_ended":
                            raise
                        start_after_ended = True
                if start_after_ended or not self._owns_active_turn(tid):
                    self._ensure_loaded(tid)
                    blocker, live = self._settings_runtime_blocker(tid)
                    if blocker and (not start_after_ended or
                                    live.get("compacting") is not None or
                                    "waiting on a request" in blocker):
                        return {"ok": False,
                                "error": blocker + "; message can be queued",
                                "code": "provider_control_unavailable", "queueable": True}
                    kwargs = {"mode": mode, "model": model, "effort": effort}
                    if inputs:
                        kwargs["inputs"] = inputs
                    self.client.start_turn(tid, text, **kwargs)
            elif typ == "mode":
                mode = str(action.get("mode") or "")
                if mode not in ("plan", "default"):
                    return {"ok": False, "error": "mode must be plan or default"}
                with self._lock:
                    session = next((s for s in self._sessions
                                    if s.get("native_session_id") == tid), {})
                self._ensure_loaded(tid)
                blocker, live = self._settings_runtime_blocker(tid)
                if blocker:
                    return {"ok": False, "error": blocker}
                model = live.get("model") or session.get("model")
                # An explicit live null is canonical (for example a TUI mode
                # that disables reasoning effort). Only fall back when App
                # Server omitted the field entirely.
                effort = (live.get("effort") if "effort" in live else
                          (session.get("effort") or
                           ("medium" if mode == "plan" else None)))
                self.client.set_mode(tid, mode, model, effort)
                warning = None
                durable = False
                with self._projection_commit_lock:
                    latest_meta = ((self._state().get("thread_meta") or {}).get(tid) or {})
                    next_revision = int(latest_meta.get("settings_revision") or 0) + 1
                    try:
                        durable = self._remember(tid, mode, {
                            "model": model, "effort": effort,
                            "settings_revision": next_revision},
                            expected_settings_revision=next_revision - 1)
                    except Exception as exc:
                        warning = ("Applied in Codex, but Fleet could not persist the mode: " +
                                   str(exc)[:300])
                    if not durable and warning is None:
                        warning = ("Applied in Codex, but Fleet could not durably save the mode; "
                                   "the live runtime remains updated")
                    with self._lock:
                        for current in self._sessions:
                            if current.get("native_session_id") == tid:
                                current["collaboration_mode"] = mode
                                current["model"] = model
                                current["effort"] = effort
                                current["settings_revision"] = next_revision
                                # A mode is not a pending request. Expose this only when
                                # App Server actually asks a structured question.
                                current["capabilities"]["answer_structured"] = False
                result = {"ok": True, "mode": mode, "durable": durable}
                if warning:
                    result["warning"] = warning
                return result
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
                self._ensure_loaded(tid)
                blocker, _live = self._settings_runtime_blocker(tid)
                if blocker:
                    return {"ok": False, "error": blocker}
                self.client.compact(tid)
                self.client.thread_state.setdefault(tid, {})["compacting"] = 0
            elif typ == "review":
                self._ensure_loaded(tid)
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
                self._ensure_loaded(tid)
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
                start_after_ended = False
                if self._owns_active_turn(tid) and hasattr(self.client, "steer_turn"):
                    try:
                        self.client.steer_turn(tid, relay)
                        return {"ok": True, "relayed_via": "parent"}
                    except CodexError as exc:
                        if exc.code != "turn_ended":
                            raise
                        start_after_ended = True
                if start_after_ended or not self._owns_active_turn(tid):
                    with self._lock:
                        session = next((s for s in self._sessions
                                        if s.get("native_session_id") == tid), {})
                    self._ensure_loaded(tid)
                    self.client.start_turn(tid, relay,
                        mode=session.get("collaboration_mode") or "default",
                        model=session.get("model"), effort=session.get("effort"))
                return {"ok": True, "relayed_via": "parent"}
            else:
                return {"ok": False, "error": f"Codex does not support {typ} here"}
            return {"ok": True}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            if isinstance(exc, CodexError):
                result.update(code=exc.code, queueable=exc.queueable)
            return result


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
            "turn_id": turn.get("id"), "error": turn.get("error")}


def _millis(value):
    ep = _epoch(value)
    return round(ep * 1000) if ep else None


def _usage_total(usage):
    latest = usage.get("last") or usage.get("total") or usage
    if latest.get("totalTokens") is not None:
        return int(latest.get("totalTokens") or 0)
    return sum(int(latest.get(k) or 0) for k in
               ("inputTokens", "outputTokens", "reasoningOutputTokens"))


def _usage_cumulative(usage):
    """Return only provider-reported cumulative usage; never relabel a turn as lifetime."""
    total = usage.get("total") if isinstance(usage, dict) else None
    if not isinstance(total, dict):
        return None
    if total.get("totalTokens") is not None:
        return int(total.get("totalTokens") or 0)
    fields = ("inputTokens", "outputTokens", "reasoningOutputTokens")
    if not any(total.get(key) is not None for key in fields):
        return None
    return sum(int(total.get(key) or 0) for key in fields)


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
            "account_id": current.get("id") or current.get("email") or "active",
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
                    "text": text[:799] + "…" if len(text) > 800 else text}
    if fallback:
        text = str(fallback).strip()
        return {"role": "user",
                "text": text[:799] + "…" if len(text) > 800 else text}
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
        # Dict insertion order is the delivery chronology used below. Repeated
        # changes must become the newest file, not keep their first-seen slot.
        found.pop(path, None)
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

    def remember(aid, agent):
        previous = found.get(aid)
        if (previous and previous.get("state") in ("done", "ended") and
                agent.get("state") not in ("done", "ended")):
            agent["state"] = previous["state"]
        found[aid] = agent

    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            if item.get("type") == "subAgentActivity":
                aid = item.get("agentThreadId")
                if aid:
                    activity = item.get("kind")
                    state = ({"completed": "done", "interrupted": "ended",
                              "failed": "ended", "errored": "ended",
                              "shutdown": "ended"}.get(activity, "running"))
                    remember(aid, {
                        "agent_id": aid, "session_id": f"codex:{parent_id}",
                        "agent_type": (item.get("agentPath") or "codex").split("/")[-1],
                        "description": item.get("agentPath") or "Codex subagent",
                        "depth": 0, "model": "", "family": "codex", "effort": None,
                        "state": state, "total_tokens": None, "cost": None,
                        "cost_source": "unavailable", "tokens": {}, "spark": [],
                        "tok_per_s": None, "started": None, "last": None,
                        "last_msg": None, "convo_v": 0})
                continue
            if item.get("type") != "collabAgentToolCall":
                continue
            states = item.get("agentsStates") or {}
            for aid in item.get("receiverThreadIds") or []:
                raw = (states.get(aid) or {}).get("status")
                state = {"pendingInit": "running", "running": "running",
                         "completed": "done", "interrupted": "ended",
                         "errored": "ended", "shutdown": "ended"}.get(raw, "running")
                remember(aid, {
                    "agent_id": aid, "session_id": f"codex:{parent_id}",
                    "agent_type": "codex", "description": item.get("prompt") or "",
                    "depth": 0, "model": item.get("model") or "",
                    "family": "codex", "effort": item.get("reasoningEffort"),
                    "state": state, "total_tokens": None, "cost": None,
                    "cost_source": "unavailable", "tokens": {}, "spark": [],
                    "tok_per_s": None,
                    "started": None, "last": None, "last_msg": None})
    return list(found.values())
