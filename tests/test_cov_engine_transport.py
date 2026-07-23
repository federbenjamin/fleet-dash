"""Coverage tests for fleetdash.engine_transport.TransportOps: process/tty
resolution, Codex terminal route discovery, and background/close/reopen paths."""
import os
import signal
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from fleetdash import engine_transport as et
from fleetdash.engine_transport import TransportOps
from fleetdash.claude_background import ClaudeBackgroundError


class Stub(TransportOps):
    def __init__(self):
        self._claude_command_cache = {}
        self._tty_cache = {}
        self.cfg = {}
        self.is_staging = False
        self._codex_terminal_routes_cache = (0, {})
        self._claude_background = None
        self._claude_background_error = None
        self._iterm_calls = []
        self._iterm_result = {"ok": True}
        self._live = []
        self._ledger_row = None

    def _iterm_write(self, target, steps, step_delay=0.05):
        self._iterm_calls.append((target, steps))
        return dict(self._iterm_result)

    def live_sessions(self):
        return self._live

    def ledger_reader(self):
        row = self._ledger_row

        class DB:
            def execute(self, *a):
                return SimpleNamespace(fetchone=lambda: row)

            def close(self):
                pass
        return DB()

    def _safe_claude_transcript(self, sid, path):
        return bool(path)

    def _safe_reopen_cwd(self, cwd):
        return cwd if cwd and os.path.isdir(cwd) else None


def run_result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="")


class ProcessCommandTest(unittest.TestCase):
    def test_bad_pid_and_low_pid(self):
        stub = Stub()
        self.assertEqual(stub._claude_process_command({"pid": "notanint"}), "")
        self.assertEqual(stub._claude_process_command({"pid": 1}), "")

    def test_cache_and_subprocess_and_exception(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("claude --foo")):
            self.assertEqual(stub._claude_process_command({"pid": 100}), "claude --foo")
        # cached (no second subprocess call)
        with mock.patch.object(et.subprocess, "run",
                               side_effect=AssertionError("should be cached")):
            self.assertEqual(stub._claude_process_command({"pid": 100}), "claude --foo")
        with mock.patch.object(et.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(stub._claude_process_command({"pid": 200}), "")


class PermissionModesTest(unittest.TestCase):
    def test_auto_model_detection(self):
        self.assertTrue(TransportOps._claude_auto_model("claude-sonnet-5"))
        self.assertTrue(TransportOps._claude_auto_model("opus-4.7"))
        self.assertFalse(TransportOps._claude_auto_model("gpt-4"))

    def test_bypass_and_auto_modes(self):
        stub = Stub()
        tail = SimpleNamespace(permission_mode="default", model="claude-sonnet-5")
        with mock.patch.object(stub, "_claude_process_command",
                               return_value="claude --permission-mode bypassPermissions"):
            modes = stub._claude_permission_modes({"pid": 5}, tail)
        self.assertIn("bypassPermissions", modes)
        self.assertIn("auto", modes)

    def test_shlex_failure_falls_back_to_split(self):
        stub = Stub()
        tail = SimpleNamespace(permission_mode="bypassPermissions", model="gpt")
        with mock.patch.object(stub, "_claude_process_command",
                               return_value="claude 'unterminated"):
            modes = stub._claude_permission_modes({"pid": 5}, tail)
        self.assertIn("bypassPermissions", modes)


class TtyForPidTest(unittest.TestCase):
    def test_bad_and_low_pid(self):
        stub = Stub()
        self.assertEqual(stub._tty_for_pid("bad"), "")
        self.assertEqual(stub._tty_for_pid(0), "")

    def test_ps_tty_direct(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("ttys003")):
            self.assertEqual(stub._tty_for_pid(50), "ttys003")
        # cached
        self.assertEqual(stub._tty_for_pid(50), "ttys003")

    def test_lsof_fallback(self):
        stub = Stub()
        calls = {"n": 0}

        def run(command, **kwargs):
            calls["n"] += 1
            if command[0] == "ps":
                return run_result("??")     # no controlling tty
            return run_result("p60\nn/dev/ttys009\n")
        with mock.patch.object(et.subprocess, "run", side_effect=run):
            self.assertEqual(stub._tty_for_pid(60), "ttys009")

    def test_lsof_exception_returns_empty(self):
        stub = Stub()

        def run(command, **kwargs):
            if command[0] == "ps":
                return run_result("??")
            raise OSError("lsof missing")
        with mock.patch.object(et.subprocess, "run", side_effect=run):
            self.assertEqual(stub._tty_for_pid(70), "")

    def test_ps_exception_then_lsof_success(self):
        stub = Stub()

        def run(command, **kwargs):
            if command[0] == "ps":
                raise OSError("ps gone")
            return run_result("p80\nn/dev/ttys011\n")
        with mock.patch.object(et.subprocess, "run", side_effect=run):
            self.assertEqual(stub._tty_for_pid(80), "ttys011")


class CodexTerminalRoutesTest(unittest.TestCase):
    def _run_ps(self, line):
        def run(command, **kwargs):
            return run_result(line)
        return run

    def test_valid_route_and_filtered_lines(self):
        stub = Stub()
        tid = str(uuid.uuid4())
        socket_path = os.path.realpath("/tmp/fleet-codex.sock")
        lines = "\n".join([
            f"  111 ttys001 codex resume --remote unix://{socket_path} {tid}",
            "HEADER without a leading pid",                          # regex no match
            "  222 ?? codex resume --remote unix:///x other",       # bad tty
            "  333 ttys002 codex 'unterminated",                     # shlex fail
            "  777 ttys006 codex --version",                         # no resume verb
            "  444 ttys003 vim resume --remote unix:///x abc",       # no codex
            f"  555 ttys004 codex resume --remote unix:///wrong {tid}",  # socket mismatch
            "  666 ttys005 codex resume --remote unix://" + socket_path + " not-a-uuid",
        ])
        with mock.patch("fleetdash.codex_runtime.codex_control_socket",
                        return_value=socket_path), \
                mock.patch.object(et.subprocess, "run",
                                  side_effect=self._run_ps(lines)):
            routes = stub._codex_terminal_routes(force=True)
        self.assertIn(tid, routes)
        self.assertEqual(routes[tid]["tty"], "/dev/ttys001")

    def test_equals_remote_form(self):
        stub = Stub()
        tid = str(uuid.uuid4())
        socket_path = os.path.realpath("/tmp/fleet-codex2.sock")
        line = f"  111 ttys001 codex resume --remote=unix://{socket_path} {tid}"
        with mock.patch("fleetdash.codex_runtime.codex_control_socket",
                        return_value=socket_path), \
                mock.patch.object(et.subprocess, "run",
                                  side_effect=self._run_ps(line)):
            routes = stub._codex_terminal_routes(force=True)
        self.assertIn(tid, routes)

    def test_exception_uses_cache_fallback(self):
        stub = Stub()
        with mock.patch("fleetdash.codex_runtime.codex_control_socket",
                        return_value="/tmp/x.sock"), \
                mock.patch.object(et.subprocess, "run", side_effect=OSError("ps gone")):
            routes = stub._codex_terminal_routes(force=True)
        self.assertEqual(routes, {})

    def test_cache_hit_returns_without_probe(self):
        stub = Stub()
        import time
        stub._codex_terminal_routes_cache = (time.monotonic(), {"abc": {"tty": "/dev/ttys0"}})
        routes = stub._codex_terminal_routes()
        self.assertEqual(routes, {"abc": {"tty": "/dev/ttys0"}})

    def test_ps_nonzero_returncode_falls_back(self):
        stub = Stub()
        with mock.patch("fleetdash.codex_runtime.codex_control_socket",
                        return_value="/tmp/x.sock"), \
                mock.patch.object(et.subprocess, "run",
                                  return_value=run_result("", returncode=1)):
            self.assertEqual(stub._codex_terminal_routes(force=True), {})

    def test_codex_terminal_route_validates_uuid(self):
        stub = Stub()
        self.assertIsNone(stub._codex_terminal_route("not-a-uuid"))
        import time
        tid = str(uuid.uuid4())
        stub._codex_terminal_routes_cache = (time.monotonic(),
                                             {tid: {"tty": "/dev/ttys001", "pid": 1}})
        self.assertEqual(stub._codex_terminal_route(tid)["tty"], "/dev/ttys001")

    def test_apply_routes_updates_capabilities(self):
        sessions = [{"native_session_id": "t1", "state": "running",
                     "capabilities": {}},
                    {"native_session_id": "t2", "state": "idle", "capabilities": {}}]
        routes = {"t1": {"tty": "/dev/ttys001", "pid": 1}}
        TransportOps._apply_codex_terminal_routes(sessions, routes)
        self.assertTrue(sessions[0]["capabilities"]["focus_terminal"])
        self.assertEqual(sessions[0]["control_state"], "terminal_active")
        self.assertNotIn("focus_terminal", sessions[1]["capabilities"])


class BackgroundHelpersTest(unittest.TestCase):
    def test_static_helpers(self):
        self.assertTrue(TransportOps._is_background_claude({"kind": "bg"}))
        self.assertFalse(TransportOps._is_background_claude({"kind": "fg"}))
        self.assertTrue(TransportOps._native_write_failed_before_delivery(
            {"ok": False, "code": "background_connection_lost"}))
        self.assertFalse(TransportOps._native_write_failed_before_delivery(
            {"ok": True}))
        self.assertEqual(TransportOps._background_job_id({"jobId": "abc"}), "abc")
        self.assertEqual(TransportOps._background_job_id({"id": "def"}), "def")

    def test_transport_construction_success_and_cache(self):
        stub = Stub()
        with mock.patch.object(et, "ClaudeBackgroundTransport",
                               return_value="TRANSPORT"):
            self.assertEqual(stub._background_claude_transport(), "TRANSPORT")
        # cached
        self.assertEqual(stub._background_claude_transport(), "TRANSPORT")

    def test_transport_construction_failure(self):
        stub = Stub()
        with mock.patch.object(et, "ClaudeBackgroundTransport",
                               side_effect=ClaudeBackgroundError("no claude")):
            self.assertIsNone(stub._background_claude_transport())
        self.assertEqual(stub._claude_background_error, "no claude")

    def test_write_background_when_unavailable(self):
        stub = Stub()
        with mock.patch.object(stub, "_background_claude_transport", return_value=None):
            result = stub._write_background_claude({"jobId": "x"}, [], 0.05)
        self.assertFalse(result["ok"])

    def test_write_background_delegates(self):
        stub = Stub()
        transport = SimpleNamespace(write=lambda job, steps, step_delay: {"ok": True})
        with mock.patch.object(stub, "_background_claude_transport",
                               return_value=transport):
            self.assertTrue(stub._write_background_claude(
                {"jobId": "x"}, [("hi", True)], 0.05)["ok"])

    def test_focus_background_paths(self):
        stub = Stub()
        with mock.patch.object(stub, "_background_claude_transport", return_value=None):
            self.assertFalse(stub._focus_background_claude({"jobId": "x"})["ok"])

        transport = SimpleNamespace(attach_command=lambda job, cwd: (_ for _ in ()).throw(
            ClaudeBackgroundError("bad job")))
        with mock.patch.object(stub, "_background_claude_transport",
                               return_value=transport):
            self.assertFalse(stub._focus_background_claude({"jobId": "x"})["ok"])

        with tempfile.TemporaryDirectory() as cwd:
            transport = SimpleNamespace(
                attach_command=lambda job, c: ("/bin/claude", "abcd1234", cwd))
            with mock.patch.object(stub, "_background_claude_transport",
                                   return_value=transport):
                result = stub._focus_background_claude(
                    {"jobId": "abcd1234", "cwd": cwd, "sessionId": "s1"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["transport"], "claude_attach")

    def test_focus_background_missing_cwd(self):
        stub = Stub()
        transport = SimpleNamespace(
            attach_command=lambda job, c: ("/bin/claude", "abcd1234", "/no/such/dir"))
        with mock.patch.object(stub, "_background_claude_transport",
                               return_value=transport):
            result = stub._focus_background_claude({"jobId": "abcd1234"})
        self.assertFalse(result["ok"])


class CloseAndReopenTest(unittest.TestCase):
    def test_close_invalid_pid(self):
        stub = Stub()
        self.assertFalse(stub._close_claude_session({"pid": "notanint"})["ok"])
        self.assertFalse(stub._close_claude_session({"pid": 0})["ok"])
        self.assertFalse(stub._close_claude_session({"pid": os.getpid()})["ok"])

    def test_close_background_transport_unavailable(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("claude attach")), \
                mock.patch.object(stub, "_background_claude_transport",
                                  return_value=None):
            result = stub._close_claude_session({"pid": 999, "kind": "bg", "jobId": "x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "background_connection_lost")

    def test_close_interrupt_failure_warns(self):
        stub = Stub()
        stub._iterm_result = {"ok": False, "error": "iterm busy"}
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("claude --foo")), \
                mock.patch.object(stub, "_tty_for_pid", return_value="ttys001"), \
                mock.patch.object(et.os, "kill"):
            result = stub._close_claude_session({"pid": 999, "status": "shell"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["interrupted"])
        self.assertIn("iterm busy", result["warning"])

    def test_close_process_already_gone(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("claude")), \
                mock.patch.object(et.os, "kill",
                                  side_effect=ProcessLookupError("gone")):
            result = stub._close_claude_session({"pid": 999, "status": "idle"})
        self.assertTrue(result["ok"])

    def test_close_process_lookup_failure(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run", side_effect=OSError("ps gone")):
            self.assertIn("lookup failed", stub._close_claude_session({"pid": 999})["error"])

    def test_close_no_command_or_wrong_process(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run", return_value=run_result("")):
            self.assertIn("no longer running",
                          stub._close_claude_session({"pid": 999})["error"])
        with mock.patch.object(et.subprocess, "run", return_value=run_result("vim foo")):
            self.assertIn("non-Claude",
                          stub._close_claude_session({"pid": 999})["error"])

    def test_close_background_claude(self):
        stub = Stub()
        transport = SimpleNamespace(stop=lambda job: {"ok": True, "transport": "claude_stop"})
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("claude attach")), \
                mock.patch.object(stub, "_background_claude_transport",
                                  return_value=transport):
            result = stub._close_claude_session({"pid": 999, "kind": "bg", "jobId": "x"})
        self.assertTrue(result["ok"])

    def test_close_foreground_interrupts_then_kills(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("claude --foo")), \
                mock.patch.object(stub, "_tty_for_pid", return_value="ttys001"), \
                mock.patch.object(et.time, "sleep"), \
                mock.patch.object(et.os, "kill") as kill:
            result = stub._close_claude_session(
                {"pid": 999, "status": "busy"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["interrupted"])
        kill.assert_called_once_with(999, signal.SIGTERM)

    def test_close_kill_permission_error(self):
        stub = Stub()
        with mock.patch.object(et.subprocess, "run",
                               return_value=run_result("claude")), \
                mock.patch.object(et.os, "kill",
                                  side_effect=PermissionError("denied")):
            result = stub._close_claude_session({"pid": 999, "status": "idle"})
        self.assertFalse(result["ok"])

    def test_reopen_already_live(self):
        stub = Stub()
        stub._live = [{"sessionId": "s1"}]
        self.assertFalse(stub.reopen_claude_session("s1")["ok"])

    def test_reopen_not_closed_claude(self):
        stub = Stub()
        stub._ledger_row = ("/work", "codex", "/p.jsonl", 123)
        self.assertIn("not a closed Claude", stub.reopen_claude_session("s1")["error"])

    def test_reopen_lookup_error(self):
        stub = Stub()

        def broken_reader():
            raise OSError("db locked")
        stub.ledger_reader = broken_reader
        self.assertIn("lookup failed", stub.reopen_claude_session("s1")["error"])

    def test_reopen_success(self):
        stub = Stub()
        with tempfile.TemporaryDirectory() as cwd:
            stub._ledger_row = (cwd, "claude", "/p.jsonl", 123)
            result = stub.reopen_claude_session("11111111-1111-1111-1111-111111111111")
        self.assertTrue(result["ok"])
        self.assertTrue(result["reopened"])

    def test_reopen_bad_transcript_and_cwd(self):
        stub = Stub()
        stub._ledger_row = ("/work", "claude", "", 123)  # empty transcript path
        self.assertIn("unavailable", stub.reopen_claude_session("s1")["error"])
        stub._ledger_row = ("/no/such/dir", "claude", "/p.jsonl", 123)
        self.assertIn("working directory", stub.reopen_claude_session("s1")["error"])


if __name__ == "__main__":
    unittest.main()
