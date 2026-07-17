"""Isolated Web Push runtime: private keys, supervised Node helper, durable queue."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit

from briefing import OperationsError


MAX_JOB_BYTES = 16_384
MAX_RESULT_BYTES = 4_096
SECRET_LIMIT = 8_192
KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class WebPushError(RuntimeError):
    """A bounded runtime/configuration failure safe to project without secrets."""


def _safe_env(node_path):
    env = {"PATH": os.pathsep.join((os.path.dirname(node_path), "/usr/bin", "/bin")),
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    for key in ("HOME", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def resolve_node(configured=None):
    """Resolve a fixed Node >=18 executable without shell or startup-file evaluation."""
    candidates = []
    if configured:
        path = os.path.abspath(os.path.expanduser(str(configured)))
        if path != str(configured) and not str(configured).startswith("~"):
            raise WebPushError("configured Web Push Node path must be absolute")
        candidates.append(path)
    else:
        found = shutil.which("node")
        if found:
            candidates.append(os.path.abspath(found))
        candidates.extend(reversed(sorted(glob.glob(
            os.path.expanduser("~/.nvm/versions/node/*/bin/node")))))
    seen = set()
    for path in candidates:
        if path in seen or not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        seen.add(path)
        try:
            result = subprocess.run(
                [path, "--version"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=3, check=False, env=_safe_env(path))
            match = re.fullmatch(rb"v(\d+)\.\d+\.\d+\s*", result.stdout[:80])
            if result.returncode == 0 and match and int(match.group(1)) >= 18:
                return path
        except (OSError, subprocess.SubprocessError):
            continue
    raise WebPushError("Node 18 or newer is unavailable for Web Push")


class PushSecretStore:
    """Load/create one VAPID keypair; corruption never causes silent regeneration."""

    def __init__(self, path, generator):
        self.path = os.path.abspath(path)
        self.generator = generator

    @staticmethod
    def _validated(value):
        if not isinstance(value, dict) or value.get("version") != 1:
            raise WebPushError("Web Push secret file has an unsupported format")
        public = value.get("vapid_public_key")
        private = value.get("vapid_private_key")
        action = value.get("action_secret")
        if (not isinstance(public, str) or not KEY_RE.fullmatch(public) or
                not 80 <= len(public) <= 100 or
                not isinstance(private, str) or not KEY_RE.fullmatch(private) or
                not 40 <= len(private) <= 60 or
                not isinstance(action, str) or not KEY_RE.fullmatch(action) or
                len(action) < 40):
            raise WebPushError("Web Push secret file is invalid")
        return {"version": 1, "vapid_public_key": public,
                "vapid_private_key": private, "action_secret": action}

    def _read(self):
        info = os.lstat(self.path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WebPushError("Web Push secret path must be a regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise WebPushError("Web Push secret file has the wrong owner")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise WebPushError("Web Push secret file must use mode 0600")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise WebPushError("Web Push secret file changed while opening")
            raw = os.read(fd, SECRET_LIMIT + 1)
        finally:
            os.close(fd)
        if len(raw) > SECRET_LIMIT:
            raise WebPushError("Web Push secret file is too large")
        try:
            return self._validated(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, TypeError):
            raise WebPushError("Web Push secret file is invalid")

    def _write(self, value):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        temporary = self.path + ".tmp-" + secrets.token_hex(8)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            # Link publishes the fully fsynced inode only if the destination is still
            # absent. A second daemon racing startup reads the winner instead of
            # replacing its keypair and invalidating subscriptions.
            os.link(temporary, self.path, follow_symlinks=False)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load_or_create(self):
        try:
            return self._read()
        except FileNotFoundError:
            generated = self.generator()
            value = self._validated({"version": 1,
                "vapid_public_key": generated.get("public_key"),
                "vapid_private_key": generated.get("private_key"),
                "action_secret": secrets.token_urlsafe(32)})
            try:
                self._write(value)
            except FileExistsError:
                return self._read()
            return value


def generate_vapid_keys(node_path, worker_path):
    """One bounded fixed-argv key generation call used only when no secret file exists."""
    process = subprocess.Popen(
        [node_path, worker_path, "--generate"], cwd=os.path.dirname(worker_path),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=_safe_env(node_path))
    try:
        stdout, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise WebPushError("Web Push key generation timed out")
    if process.returncode != 0 or len(stdout) > MAX_RESULT_BYTES:
        raise WebPushError("Web Push key generation failed")
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        raise WebPushError("Web Push key generation returned invalid data")
    if not isinstance(result, dict):
        raise WebPushError("Web Push key generation returned invalid data")
    return result


class WebPushHelper:
    """One supervised, serialized JSONL channel to the fixed Node helper."""

    def __init__(self, node_path, worker_path, *, vapid, subject, allowed_origins=None,
                 process_factory=subprocess.Popen, clock=time.monotonic):
        self.node_path = node_path
        self.worker_path = os.path.abspath(worker_path)
        self.vapid = vapid
        self.subject = subject
        self.allowed_origins = list(allowed_origins or [])
        self.process_factory = process_factory
        self.clock = clock
        self.lock = threading.RLock()
        self.process = None
        self.responses = queue.Queue(maxsize=8)
        self.ready = False
        self.restarts = 0
        self.next_start_at = 0.0
        self.last_error = None

    def _reader(self, stream):
        while True:
            line = stream.readline(MAX_RESULT_BYTES + 2)
            if not line:
                self.responses.put({"_eof": True})
                return
            if len(line) > MAX_RESULT_BYTES or not line.endswith(b"\n"):
                self.responses.put({"_protocol": True})
                return
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError):
                self.responses.put({"_protocol": True})
                return
            self.responses.put(value if isinstance(value, dict) else {"_protocol": True})

    def _start_locked(self):
        if self.process and self.process.poll() is None and self.ready:
            return
        if self.process and self.process.poll() is not None:
            self._failed_locked()
            raise WebPushError("Web Push helper is restarting")
        if self.clock() < self.next_start_at:
            raise WebPushError("Web Push helper is restarting")
        self._stop_locked()
        self.responses = queue.Queue(maxsize=8)
        try:
            self.process = self.process_factory(
                [self.node_path, self.worker_path], cwd=os.path.dirname(self.worker_path),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=_safe_env(self.node_path), bufsize=0)
            threading.Thread(target=self._reader, args=(self.process.stdout,),
                             daemon=True).start()
            response = self._exchange_locked({"type": "init", "request_id": "init",
                "vapid": {"public_key": self.vapid["vapid_public_key"],
                          "private_key": self.vapid["vapid_private_key"]},
                "subject": self.subject, "allowed_origins": self.allowed_origins}, 5)
            if response.get("type") != "ready":
                raise WebPushError("Web Push helper initialization failed")
            self.ready = True
            self.last_error = None
        except Exception as exc:
            self._failed_locked()
            if isinstance(exc, WebPushError):
                raise
            raise WebPushError("Web Push helper could not start")

    def _exchange_locked(self, message, timeout):
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_JOB_BYTES:
            raise WebPushError("Web Push helper job is too large")
        if not self.process or self.process.poll() is not None:
            raise WebPushError("Web Push helper is unavailable")
        try:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
            response = self.responses.get(timeout=timeout)
        except (BrokenPipeError, OSError, queue.Empty):
            raise WebPushError("Web Push helper did not respond")
        if response.get("_eof") or response.get("_protocol"):
            raise WebPushError("Web Push helper protocol failed")
        if response.get("request_id") != message.get("request_id"):
            raise WebPushError("Web Push helper response did not match its job")
        return response

    def _failed_locked(self):
        if (self.process is None and not self.ready and
                self.clock() < self.next_start_at):
            return
        self.last_error = "helper unavailable"
        self.ready = False
        self.restarts += 1
        self.next_start_at = self.clock() + min(30, 2 ** min(self.restarts, 4))
        self._stop_locked()

    def _stop_locked(self):
        process, self.process = self.process, None
        self.ready = False
        if not process:
            return
        try:
            process.terminate()
            process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=1)
            except Exception:
                pass
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def ensure_started(self):
        with self.lock:
            self._start_locked()

    def send(self, job, timeout=15):
        with self.lock:
            try:
                self._start_locked()
                response = self._exchange_locked(job, timeout)
                if response.get("type") != "result":
                    raise WebPushError("Web Push helper returned an invalid result")
                return response
            except Exception:
                self._failed_locked()
                raise

    def stop(self):
        with self.lock:
            self._stop_locked()

    def status(self):
        with self.lock:
            return {"ready": bool(self.ready and self.process and self.process.poll() is None),
                    "restarts": self.restarts,
                    "state": "ready" if self.ready else
                             ("restarting" if self.clock() < self.next_start_at else "unavailable")}


class WebPushService:
    """Background bridge from durable SQLite leases to the isolated Node sender."""

    def __init__(self, operations, base_dir, config, *, helper_factory=WebPushHelper,
                 node_resolver=resolve_node, clock=time.time):
        self.operations = operations
        self.base_dir = os.path.abspath(base_dir)
        self.config = config
        self.helper_factory = helper_factory
        self.node_resolver = node_resolver
        self.clock = clock
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        self.helper = None
        self.public_key = None
        self.runtime_state = "starting"
        self.runtime_error = None

    @property
    def worker_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_push_worker.js")

    def _subject(self):
        value = str(self.config.get("web_push_subject") or "").strip()
        if not value:
            dashboard = str(self.config.get("dashboard_url") or "").strip()
            value = dashboard if dashboard.startswith("https://") else \
                "mailto:fleet-dash@localhost.invalid"
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname:
            return value
        if parsed.scheme == "mailto" and parsed.path:
            return value
        raise WebPushError("Web Push subject must be an HTTPS URL or mailto address")

    def _bootstrap(self):
        node = self.node_resolver(self.config.get("web_push_node_command") or None)
        store = PushSecretStore(
            os.path.join(self.base_dir, "push-secrets.json"),
            lambda: generate_vapid_keys(node, self.worker_path))
        vapid = store.load_or_create()
        with self.lock:
            self.public_key = vapid["vapid_public_key"]
        helper = self.helper_factory(
            node, self.worker_path, vapid=vapid, subject=self._subject(),
            allowed_origins=self.config.get("web_push_allowed_origins") or [])
        helper.ensure_started()
        with self.lock:
            old, self.helper = self.helper, helper
            self.runtime_state = "ready"
            self.runtime_error = None
        if old and old is not helper:
            old.stop()

    @staticmethod
    def _payload(claim):
        event = claim["event"]
        if event.get("push_test"):
            title, body = "Fleet notification test", "Web Push delivery is working."
        elif event.get("kind") in ("question", "approval", "form", "reply"):
            title, body = "Fleet needs you", "A coding session needs your response."
        elif event.get("kind") == "stall":
            title, body = "Fleet needs attention", "A coding session may be stalled."
        else:
            title, body = "Fleet needs attention", "A provider or delivery needs review."
        payload = {"version": 1, "event_id": claim["event_id"],
                   "title": title, "body": body,
                   "url": "/#notifications/" + claim["event_id"],
                   "unread": int(event.get("unread") or 0)}
        encoded = json.dumps(payload, separators=(",", ":"))
        forbidden = ("prompt", "command", "file_path", "branch", "cost", "account",
                     str(event.get("session_id") or ""))
        if len(encoded.encode()) > 2048 or any(item and item in encoded for item in forbidden):
            raise WebPushError("Web Push payload privacy validation failed")
        return encoded

    def _run(self):
        while not self.stop_event.is_set():
            with self.lock:
                helper = self.helper
            if helper is None:
                try:
                    self._bootstrap()
                except Exception:
                    with self.lock:
                        self.runtime_state = "unavailable"
                        self.runtime_error = "Web Push runtime is unavailable"
                    self.wake_event.wait(30)
                    self.wake_event.clear()
                    continue
                with self.lock:
                    helper = self.helper
            try:
                claim = self.operations.notification_claim_delivery(
                    self.config.get("web_push_allowed_origins") or [])
            except Exception:
                self.wake_event.wait(1)
                self.wake_event.clear()
                continue
            if not claim:
                self.wake_event.wait(.25)
                self.wake_event.clear()
                continue
            try:
                result = helper.send({"type": "send", "request_id": claim["id"],
                    "subscription": claim["subscription"], "payload": self._payload(claim),
                    "ttl": 300, "urgency": "high",
                    "topic": hashlib.sha256(claim["event_id"].encode()).hexdigest()[:24]})
                with self.lock:
                    self.runtime_state = "ready"
                    self.runtime_error = None
            except Exception:
                result = {"ok": False, "retryable": True, "code": "helper_unavailable"}
                with self.lock:
                    self.runtime_state = "degraded"
                    self.runtime_error = "Web Push helper is restarting"
            try:
                self.operations.notification_finish_delivery(claim["id"], result)
            except (OperationsError, OSError):
                pass

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="web-push", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        with self.lock:
            helper = self.helper
        if helper:
            helper.stop()

    def enqueue_test(self, device_id):
        delivery = self.operations.notification_create_test_delivery(device_id)
        self.wake_event.set()
        return delivery

    def status(self):
        with self.lock:
            helper_status = self.helper.status() if self.helper else {
                "ready": False, "restarts": 0, "state": self.runtime_state}
            public_key = self.public_key
            state = self.runtime_state
        return {"configured": bool(public_key), "public_key": public_key,
                "delivery": "ready" if helper_status["ready"] else state,
                "helper": helper_status,
                "queue": self.operations.notification_delivery_diagnostics()}
