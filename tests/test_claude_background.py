import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash.claude_background import (ClaudeBackgroundError, ClaudeBackgroundTransport,
                               resolve_claude_command)


class _FakeAttachProcess:
    def __init__(self, wait_returncode=0):
        self.returncode = None
        self.wait_returncode = wait_returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.wait_returncode
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class ClaudeBackgroundTransportTest(unittest.TestCase):
    def setUp(self):
        self.transport = ClaudeBackgroundTransport(sys.executable)

    def test_command_resolution_requires_absolute_executable(self):
        with self.assertRaises(ClaudeBackgroundError):
            resolve_claude_command("claude")
        with self.assertRaises(ClaudeBackgroundError):
            resolve_claude_command("/definitely/missing/claude")
        self.assertEqual(resolve_claude_command(sys.executable),
                         os.path.realpath(sys.executable))

    def test_job_id_and_terminal_operations_are_bounded(self):
        self.assertEqual(self.transport.job_id("A1B2C3D4"), "a1b2c3d4")
        for value in ("", "../../x", "a1b2", "a1b2c3d4;rm"):
            with self.assertRaises(ClaudeBackgroundError):
                self.transport.job_id(value)
        self.assertEqual(self.transport._operations([
            ("hello", True), ("1", False), ("", True)]),
            [b"hello", b"\r", b"1", b"\r"])
        with self.assertRaises(ClaudeBackgroundError):
            self.transport._operations([("__FOCUS__", False)])
        with self.assertRaises(ClaudeBackgroundError):
            self.transport._operations([("x" * 120_001, False)])

    def test_output_is_ansi_stripped_bounded_and_control_clean(self):
        raw = b"\x1b[31mfailed\x1b[0m\x00 secret-safe " + b"x" * 600
        value = self.transport._safe_output(raw)
        self.assertNotIn("\x1b", value)
        self.assertNotIn("\x00", value)
        self.assertLessEqual(len(value), 300)

    def test_invalid_job_never_starts_process_or_shell(self):
        process = []
        transport = ClaudeBackgroundTransport(
            sys.executable, popen=lambda *args, **kwargs: process.append(args))
        result = transport.write("../../bad", [("hello", True)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "background_connection_lost")
        self.assertEqual(process, [])

    def test_readiness_failure_before_first_write_is_connection_lost(self):
        process = _FakeAttachProcess()
        transport = ClaudeBackgroundTransport(
            sys.executable, popen=lambda *args, **kwargs: process)
        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready",
                                  side_effect=ClaudeBackgroundError("not ready")), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write") as write:
            result = transport.write("A1B2C3D4", [("hello", False)])

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "background_connection_lost")
        self.assertNotIn("delivery uncertain", result["error"])
        write.assert_not_called()

    def test_failure_after_first_partial_write_is_delivery_uncertain(self):
        process = _FakeAttachProcess()
        transport = ClaudeBackgroundTransport(
            sys.executable, popen=lambda *args, **kwargs: process)
        writes = []

        def write(_fd, data):
            writes.append(bytes(data))
            if len(writes) == 1:
                return 1
            raise OSError("fixture write failure")

        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready"), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write", side_effect=write):
            result = transport.write("A1B2C3D4", [("hello", False)], step_delay=0)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "delivery_uncertain")
        self.assertIn("delivery uncertain", result["error"])
        self.assertEqual(writes[:2], [b"hello", b"ello"])

    def test_unclean_detach_after_write_is_delivery_uncertain(self):
        process = _FakeAttachProcess(wait_returncode=7)
        transport = ClaudeBackgroundTransport(
            sys.executable, popen=lambda *args, **kwargs: process, sleep=lambda _delay: None)
        writes = []

        def write(_fd, data):
            writes.append(bytes(data))
            return len(data)

        with mock.patch("fleetdash.claude_background.pty.openpty", return_value=(101, 102)), \
                mock.patch.object(transport, "_set_window"), \
                mock.patch.object(transport, "_nonblocking"), \
                mock.patch.object(transport, "_read_ready"), \
                mock.patch("fleetdash.claude_background.os.close"), \
                mock.patch("fleetdash.claude_background.os.write", side_effect=write):
            result = transport.write("A1B2C3D4", [("hello", False)], step_delay=0)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "delivery_uncertain")
        self.assertIn("did not detach cleanly", result["error"])
        self.assertEqual(writes, [b"hello", b"\x1a"])

    def test_stop_uses_fixed_argv_and_returns_bounded_error(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="stopped", stderr="")

        transport = ClaudeBackgroundTransport(sys.executable, run=run)
        result = transport.stop("A1B2C3D4")
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], [os.path.realpath(sys.executable),
                                      "stop", "a1b2c3d4"])
        self.assertNotIn("shell", calls[0][1])

    def test_visible_attach_command_returns_only_validated_components(self):
        with tempfile.TemporaryDirectory() as cwd:
            executable, job, path = self.transport.attach_command("A1B2C3D4", cwd)
        self.assertEqual(executable, os.path.realpath(sys.executable))
        self.assertEqual(job, "a1b2c3d4")
        self.assertTrue(os.path.isabs(path))


if __name__ == "__main__":
    unittest.main()
