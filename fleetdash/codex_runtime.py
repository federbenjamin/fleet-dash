#!/usr/bin/env python3
"""Codex runtime lifecycle: executable resolution, shared/managed App Server
startup, the private-to-managed metadata migration, and the base CodexError.

Split out of ``codex_adapter`` as the lowest layer of the Codex integration:
neither the protocol client nor the adapter may be imported from here.
"""
import os
import json
import glob
import time
import fcntl
import shlex
import signal
import shutil
import socket
import hashlib
import platform
import threading
import subprocess


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
