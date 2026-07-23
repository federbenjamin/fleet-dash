"""Coverage tests for fleetdash.claude_background: command resolution, PTY
readiness, the write() operation loop, detach handling, and stop()."""
import errno
import os
import pty
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash import claude_background as cb
from fleetdash.claude_background import (ClaudeBackgroundError,
                                         ClaudeBackgroundTransport,
                                         resolve_claude_command)


class _Proc:
    def __init__(self, poll_value=None, wait_values=None):
        self.returncode = None
        self._poll_value = poll_value
        self._wait_values = list(wait_values or [])
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._poll_value

    def wait(self, timeout=None):
        if self._wait_values:
            item = self._wait_values.pop(0)
            if isinstance(item, Exception):
                raise item
            self.returncode = item
            self._poll_value = item
            return item
        self.returncode = 0
        self._poll_value = 0
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class ResolveCommandTest(unittest.TestCase):
    def test_local_bin_path_is_preferred(self):
        with tempfile.TemporaryDirectory() as home:
            local = os.path.join(home, ".local", "bin", "claude")
            os.makedirs(os.path.dirname(local))
            with open(local, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(local, 0o755)
            self.assertEqual(resolve_claude_command("", home=home),
                             os.path.realpath(local))

    def test_falls_back_to_which(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(cb.shutil, "which", return_value=sys.executable):
                self.assertEqual(resolve_claude_command("", home=home),
                                 os.path.realpath(sys.executable))


class ReadReadyTest(unittest.TestCase):
    def setUp(self):
        self.transport = ClaudeBackgroundTransport(sys.executable)

    def test_ready_markers_detected(self):
        master, slave = pty.openpty()
        try:
            os.write(slave, b"\x1b[?1049h\x1b[?25h ready")
            self.transport._read_ready(master, _Proc())
        finally:
            os.close(master)
            os.close(slave)

    def test_process_exit_before_ready(self):
        master, slave = pty.openpty()
        self.transport._nonblocking(master)
        try:
            with self.assertRaisesRegex(ClaudeBackgroundError, "closed before"):
                self.transport._read_ready(master, _Proc(poll_value=0))
        finally:
            os.close(master)
            os.close(slave)

    def test_eio_is_connection_closed(self):
        with mock.patch.object(cb.select, "select", return_value=([7], [], [])), \
                mock.patch.object(cb.os, "read",
                                  side_effect=OSError(errno.EIO, "input/output")):
            with self.assertRaisesRegex(ClaudeBackgroundError, "closed before"):
                self.transport._read_ready(7, _Proc())

    def test_timeout(self):
        transport = ClaudeBackgroundTransport(sys.executable,
                                              clock=iter([0, 100]).__next__)
        master, slave = pty.openpty()
        self.transport._nonblocking(master)
        try:
            with self.assertRaisesRegex(ClaudeBackgroundError, "timed out"):
                transport._read_ready(master, _Proc())
        finally:
            os.close(master)
            os.close(slave)

    def test_blockingio_empty_and_ready(self):
        markers = b"\x1b[?1049h\x1b[?25h"
        with mock.patch.object(cb.select, "select", return_value=([7], [], [])), \
                mock.patch.object(cb.os, "read",
                                  side_effect=[BlockingIOError(), b"", markers]):
            self.transport._read_ready(7, _Proc())

    def test_non_eio_oserror_reraises(self):
        with mock.patch.object(cb.select, "select", return_value=([7], [], [])), \
                mock.patch.object(cb.os, "read",
                                  side_effect=OSError(errno.ENOTTY, "not a tty")):
            with self.assertRaises(OSError):
                self.transport._read_ready(7, _Proc())

    def test_set_window_on_real_pty(self):
        master, slave = pty.openpty()
        try:
            self.transport._set_window(slave)
        finally:
            os.close(master)
            os.close(slave)


class WriteFlowTest(unittest.TestCase):
    def test_clean_write_and_detach_success(self):
        process = _Proc(wait_values=[0])
        transport = ClaudeBackgroundTransport(sys.executable,
                                              popen=lambda *a, **k: process,
                                              sleep=lambda d: None)
        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready"), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write",
                           side_effect=lambda fd, data: len(data)):
            result = transport.write("A1B2C3D4", [("hello", True)], step_delay=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["transport"], "claude_attach")

    def test_blockingio_retries_then_delays_between_steps(self):
        process = _Proc(wait_values=[0])
        transport = ClaudeBackgroundTransport(sys.executable,
                                              popen=lambda *a, **k: process,
                                              sleep=lambda d: None)
        calls = {"n": 0}

        def os_write(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise BlockingIOError()  # first attempt not writable yet
            return len(data)

        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready"), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.select.select",
                           return_value=([], [101], [])), \
                mock.patch("fleetdash.claude_background.os.write", side_effect=os_write):
            result = transport.write("A1B2C3D4", [("a", False), ("b", False)],
                                     step_delay=0.01)
        self.assertTrue(result["ok"])

    def test_detach_timeout_terminates(self):
        process = _Proc(wait_values=[subprocess.TimeoutExpired("attach", 1), -15])
        transport = ClaudeBackgroundTransport(sys.executable,
                                              popen=lambda *a, **k: process,
                                              sleep=lambda d: None)
        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready"), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write",
                           side_effect=lambda fd, data: len(data)):
            result = transport.write("A1B2C3D4", [("hi", False)], step_delay=0)
        self.assertTrue(process.terminated)
        self.assertTrue(result["ok"])

    def test_finally_terminates_lingering_process(self):
        # _read_ready raises after nothing written, but the process is still alive
        # at the finally block, so it must be terminated there.
        process = _Proc(poll_value=None, wait_values=[0])
        transport = ClaudeBackgroundTransport(sys.executable,
                                              popen=lambda *a, **k: process)
        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready",
                                  side_effect=ClaudeBackgroundError("not ready")), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write"):
            result = transport.write("A1B2C3D4", [("hi", False)])
        self.assertFalse(result["ok"])
        self.assertTrue(process.terminated)

    def test_detach_second_timeout_kills(self):
        process = _Proc(wait_values=[
            subprocess.TimeoutExpired("attach", 1),
            subprocess.TimeoutExpired("attach", 1), 0])
        transport = ClaudeBackgroundTransport(sys.executable,
                                              popen=lambda *a, **k: process,
                                              sleep=lambda d: None)
        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready"), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write",
                           side_effect=lambda fd, data: len(data)):
            transport.write("A1B2C3D4", [("hi", False)], step_delay=0)
        self.assertTrue(process.killed)

    def test_popen_failure_closes_pty_and_swallows_close_errors(self):
        def popen(*a, **k):
            raise RuntimeError("spawn failed")
        transport = ClaudeBackgroundTransport(sys.executable, popen=popen)
        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch("fleetdash.claude_background.os.close",
                           side_effect=OSError("already closed")):
            result = transport.write("A1B2C3D4", [("hi", False)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "background_connection_lost")

    def test_finally_kill_when_terminate_times_out(self):
        process = _Proc(poll_value=None, wait_values=[
            subprocess.TimeoutExpired("attach", 1), 0])
        transport = ClaudeBackgroundTransport(sys.executable,
                                              popen=lambda *a, **k: process)
        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready",
                                  side_effect=ClaudeBackgroundError("not ready")), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write"):
            transport.write("A1B2C3D4", [("hi", False)])
        self.assertTrue(process.killed)


class StopFailureTest(unittest.TestCase):
    def test_invalid_job_id(self):
        transport = ClaudeBackgroundTransport(sys.executable)
        result = transport.stop("../bad")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "background_connection_lost")

    def test_run_exception(self):
        def run(*a, **k):
            raise OSError("cannot exec")
        transport = ClaudeBackgroundTransport(sys.executable, run=run)
        result = transport.stop("A1B2C3D4")
        self.assertFalse(result["ok"])
        self.assertIn("stop failed", result["error"])

    def test_nonzero_returncode_is_bounded(self):
        def run(*a, **k):
            return SimpleNamespace(returncode=1, stdout="", stderr="\x1b[31mno such job\x00")
        transport = ClaudeBackgroundTransport(sys.executable, run=run)
        result = transport.stop("A1B2C3D4")
        self.assertFalse(result["ok"])
        self.assertNotIn("\x1b", result["error"])


if __name__ == "__main__":
    unittest.main()
