import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

from claude_background import (ClaudeBackgroundError, ClaudeBackgroundTransport,
                               resolve_claude_command)


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
