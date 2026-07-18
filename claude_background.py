"""Bounded control bridge for Claude Code background sessions.

Claude Code intentionally exposes background sessions through ``claude attach``.
Fleet drives that supported client in a private pseudo-terminal for the duration of
one action, then sends Claude's documented Ctrl+Z detach key.  Fleet never opens the
daemon roster, handles its authentication material, or speaks the private PTY socket
protocol.
"""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import threading
import time


JOB_ID_RE = re.compile(r"^[A-Fa-f0-9]{8}$")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class ClaudeBackgroundError(RuntimeError):
    """A bounded, user-safe background attachment failure."""


def resolve_claude_command(configured=None, home=None):
    """Resolve one executable without invoking a shell or trusting PATH alone."""
    value = str(configured or "").strip()
    if value:
        if not os.path.isabs(value):
            raise ClaudeBackgroundError("configured Claude command must be an absolute path")
        candidate = value
    else:
        root = home or os.path.expanduser("~")
        local = os.path.join(root, ".local", "bin", "claude")
        candidate = local if os.path.isfile(local) or os.path.islink(local) else \
            (shutil.which("claude") or "")
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise ClaudeBackgroundError("Claude Code executable is unavailable")
    return os.path.realpath(candidate)


class ClaudeBackgroundTransport:
    """Attach, write allowlisted terminal operations, and detach exactly once."""

    MAX_CAPTURE = 64 * 1024

    def __init__(self, executable=None, *, home=None, attach_timeout=8.0,
                 detach_timeout=.25, popen=subprocess.Popen, run=subprocess.run,
                 clock=time.monotonic, sleep=time.sleep):
        self.executable = resolve_claude_command(executable, home=home)
        self.attach_timeout = max(1.0, min(20.0, float(attach_timeout)))
        self.detach_timeout = max(.1, min(10.0, float(detach_timeout)))
        self._popen = popen
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._locks_guard = threading.Lock()
        self._locks = {}

    @staticmethod
    def job_id(value):
        value = str(value or "")
        if not JOB_ID_RE.fullmatch(value):
            raise ClaudeBackgroundError("Claude background session has no valid job ID")
        return value.lower()

    def _job_lock(self, job_id):
        with self._locks_guard:
            return self._locks.setdefault(job_id, threading.Lock())

    @staticmethod
    def _set_window(fd, rows=50, columns=200):
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", int(rows), int(columns), 0, 0))

    @staticmethod
    def _nonblocking(fd):
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @classmethod
    def _safe_output(cls, raw):
        text = raw.decode("utf-8", "replace")
        text = _ANSI_RE.sub("", text)
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[-300:] or "Claude background attachment closed"

    def _read_ready(self, master, process):
        deadline = self._clock() + self.attach_timeout
        captured = bytearray()
        while self._clock() < deadline:
            if process.poll() is not None:
                try:
                    chunk = os.read(master, self.MAX_CAPTURE)
                    captured.extend(chunk)
                except OSError:
                    pass
                # Once attach starts painting, captured bytes may include session
                # prose. Never reflect PTY output through Fleet's API.
                raise ClaudeBackgroundError(
                    "Claude background attachment closed before it was ready")
            ready, _, _ = select.select([master], [], [], .05)
            if not ready:
                continue
            try:
                chunk = os.read(master, 8192)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    raise ClaudeBackgroundError(
                        "Claude background attachment closed before it was ready")
                raise
            if not chunk:
                continue
            remaining = self.MAX_CAPTURE - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            # The official attach client has entered its alternate-screen TUI and
            # painted either a prompt or an active-turn frame.  Do not scrape any
            # session content; these terminal-control markers are readiness only.
            if b"\x1b[?1049h" in captured and (b"\x1b[?25h" in captured or
                                                 b"\xe2\x9d\xaf" in captured):
                return
        raise ClaudeBackgroundError("Claude background attachment timed out")

    @staticmethod
    def _operations(steps):
        operations = []
        for text, newline in steps:
            if text == "__FOCUS__":
                raise ClaudeBackgroundError("background session has no focused terminal")
            if text:
                data = str(text).encode("utf-8")
                if len(data) > 120_000:
                    raise ClaudeBackgroundError("background input is too large")
                operations.append(data)
            if newline:
                operations.append(b"\r")
        return operations

    def write(self, job_id, steps, *, step_delay=.05):
        """Deliver one Engine-composed action through the official attach client."""
        try:
            job_id = self.job_id(job_id)
            operations = self._operations(steps)
        except ClaudeBackgroundError as exc:
            return {"ok": False, "code": "background_connection_lost",
                    "error": str(exc)[:300]}
        delay = max(0.0, min(1.0, float(step_delay)))
        with self._job_lock(job_id):
            master = slave = None
            process = None
            try:
                master, slave = pty.openpty()
                self._set_window(slave)
                self._nonblocking(master)
                process = self._popen(
                    [self.executable, "attach", job_id], stdin=slave, stdout=slave,
                    stderr=slave, close_fds=True, start_new_session=True,
                    env={**os.environ, "TERM": "xterm-256color", "NO_COLOR": "1"})
                os.close(slave)
                slave = None
                self._read_ready(master, process)
                for index, data in enumerate(operations):
                    view = memoryview(data)
                    while view:
                        try:
                            written = os.write(master, view)
                            view = view[written:]
                        except BlockingIOError:
                            select.select([], [master], [], .05)
                    if index + 1 < len(operations) and delay:
                        self._sleep(delay)
                # Let the TUI consume the final key before its documented detach.
                if operations:
                    self._sleep(min(.1, max(.02, delay)))
                os.write(master, b"\x1a")
                try:
                    process.wait(timeout=self.detach_timeout)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                if process.returncode not in (0, -15):
                    raise ClaudeBackgroundError("Claude background attachment did not detach cleanly")
                return {"ok": True, "transport": "claude_attach"}
            except ClaudeBackgroundError as exc:
                return {"ok": False, "code": "background_connection_lost",
                        "error": str(exc)[:300]}
            except Exception:
                return {"ok": False, "code": "background_connection_lost",
                        "error": "Claude background connection failed"}
            finally:
                if slave is not None:
                    try:
                        os.close(slave)
                    except OSError:
                        pass
                if master is not None:
                    try:
                        os.close(master)
                    except OSError:
                        pass
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                    except Exception:
                        pass

    def stop(self, job_id):
        try:
            job_id = self.job_id(job_id)
        except ClaudeBackgroundError as exc:
            return {"ok": False, "code": "background_connection_lost",
                    "error": str(exc)[:300]}
        with self._job_lock(job_id):
            try:
                result = self._run([self.executable, "stop", job_id],
                                   capture_output=True, text=True, timeout=10,
                                   env={**os.environ, "NO_COLOR": "1"})
            except Exception:
                return {"ok": False, "code": "background_connection_lost",
                        "error": "Claude background stop failed"}
            if result.returncode:
                detail = (result.stderr or result.stdout or
                          "Claude background stop failed")
                return {"ok": False, "code": "background_connection_lost",
                        "error": self._safe_output(detail.encode())}
            return {"ok": True, "transport": "claude_stop"}

    def attach_command(self, job_id, cwd):
        """Return argv components for an explicit user-visible desktop attach."""
        return self.executable, self.job_id(job_id), os.path.realpath(cwd)
