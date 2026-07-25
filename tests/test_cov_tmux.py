"""Coverage for fleetdash.engine_tmux (the tmux control transport) and the
`_terminal_write` / `_terminal_spawn` dispatcher in fleetdash.engine_transport.

Nothing here may touch the developer's own tmux server: every test either drives
a recorded `_tmux_run` or points `paths.TMUX_SOCKETS` at a temporary directory.
"""
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash import engine_tmux as tm
from fleetdash import paths as engine_paths
from fleetdash.engine_tmux import TmuxOps
from fleetdash.engine_transport import TransportOps


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


PANE = {"socket": "/sock", "pane_id": "%3", "session": "fleet", "attached": True}


class TmuxStub(TmuxOps, TransportOps):
    """Engine surface the transport actually needs, with recorded tmux calls."""

    def __init__(self, cfg=None, executable="/bin/tmux"):
        self.cfg = dict(cfg or {})
        self._tmux_executable = executable
        self._tmux_panes_cache = (0.0, {})
        self._tmux_lock = threading.Lock()
        self.calls = []
        self.results = []
        self.iterm_calls = []

    def _tmux_run(self, socket_path, args, timeout=None):
        self.calls.append((socket_path, list(args)))
        if not self.results:
            return result()
        return self.results.pop(0)

    def _iterm_write(self, target, steps, step_delay=None):
        self.iterm_calls.append((target, list(steps), step_delay))
        return {"ok": True, "transport": "applet"}


class CommandResolutionTests(unittest.TestCase):
    def test_cached_answer_is_reused(self):
        stub = TmuxStub(executable="/cached/tmux")
        self.assertEqual(stub._tmux_command(), "/cached/tmux")
        stub._tmux_executable = ""
        self.assertEqual(stub._tmux_command(), "")

    def test_configured_executable_must_be_runnable(self):
        with tempfile.TemporaryDirectory() as root:
            good = os.path.join(root, "tmux")
            with open(good, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(good, 0o755)
            stub = TmuxStub({"tmux_command": good}, executable=None)
            self.assertEqual(stub._tmux_command(), os.path.realpath(good))

            missing = TmuxStub({"tmux_command": os.path.join(root, "nope")},
                               executable=None)
            self.assertEqual(missing._tmux_command(), "")

    def test_path_lookup_then_absolute_fallbacks(self):
        stub = TmuxStub(executable=None)
        with mock.patch.object(tm.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(tm.os.path, "isfile", return_value=True), \
                mock.patch.object(tm.os, "access", return_value=True):
            self.assertEqual(stub._tmux_command(), "/usr/bin/tmux")

        absent = TmuxStub(executable=None)
        with mock.patch.object(tm.shutil, "which", return_value=None), \
                mock.patch.object(tm.os.path, "isfile", return_value=False):
            self.assertEqual(absent._tmux_command(), "")


class RunTests(unittest.TestCase):
    """`_tmux_run` itself, which the other suites replace."""

    class Runner(TmuxOps):
        def __init__(self, executable):
            self.cfg = {}
            self._tmux_executable = executable
            self._tmux_panes_cache = (0.0, {})
            self._tmux_lock = threading.Lock()

    def test_no_executable_returns_none(self):
        self.assertIsNone(self.Runner("")._tmux_run("/sock", ["list-panes"]))

    def test_completed_process_is_returned(self):
        runner = self.Runner("/bin/tmux")
        with mock.patch.object(tm.subprocess, "run",
                               return_value=result(stdout="out")) as run:
            self.assertEqual(runner._tmux_run("/sock", ["kill-server"]).stdout, "out")
        self.assertEqual(run.call_args[0][0],
                         ["/bin/tmux", "-S", "/sock", "kill-server"])

    def test_failure_is_none_not_an_exception(self):
        runner = self.Runner("/bin/tmux")
        with mock.patch.object(tm.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("tmux", 2)):
            self.assertIsNone(runner._tmux_run("/sock", ["list-panes"]))

    def test_detail_prefers_stderr_then_falls_back(self):
        self.assertEqual(TmuxOps._tmux_detail(None, "fallback"), "fallback")
        self.assertEqual(TmuxOps._tmux_detail(result(stderr=" boom "), "f"), "boom")
        self.assertEqual(TmuxOps._tmux_detail(result(stdout="out"), "f"), "out")
        self.assertEqual(TmuxOps._tmux_detail(result(stderr="   "), "f"), "f")


class SocketEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(engine_paths, "TMUX_SOCKETS", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stub = TmuxStub()

    def make_socket(self, name):
        path = os.path.join(self.tmp.name, name)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(path)
        return path

    def test_missing_directory_is_empty(self):
        with mock.patch.object(engine_paths, "TMUX_SOCKETS",
                               os.path.join(self.tmp.name, "nope")):
            self.assertEqual(self.stub._tmux_socket_paths(), [])

    def test_only_sockets_are_returned(self):
        sock = self.make_socket("default")
        with open(os.path.join(self.tmp.name, "notes.txt"), "w") as handle:
            handle.write("x")
        self.assertEqual(self.stub._tmux_socket_paths(), [sock])

    def test_enumeration_is_bounded(self):
        for index in range(self.stub.TMUX_SOCKET_LIMIT + 3):
            self.make_socket(f"s{index:02d}")
        self.assertEqual(len(self.stub._tmux_socket_paths()),
                         self.stub.TMUX_SOCKET_LIMIT)

    def test_unreadable_entry_is_skipped(self):
        self.make_socket("default")
        with mock.patch.object(tm.os, "lstat", side_effect=OSError):
            self.assertEqual(self.stub._tmux_socket_paths(), [])


class PaneDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.stub = TmuxStub()
        self.stub._tmux_socket_paths = lambda: ["/sock"]

    def rows(self, *lines):
        self.stub.results = [result(stdout="\n".join(lines) + "\n")]

    def test_valid_pane_is_mapped(self):
        self.rows("%2\t/dev/ttys009\tfleet\t1\t0")
        panes = self.stub._tmux_panes()
        self.assertEqual(panes["/dev/ttys009"],
                         {"socket": "/sock", "pane_id": "%2", "session": "fleet",
                          "attached": True})

    def test_dead_malformed_and_invalid_rows_are_dropped(self):
        self.rows("%2\t/dev/ttys001\tfleet\t0\t1",       # dead pane
                  "%3\t/dev/ttys002\tfleet\t0",          # wrong field count
                  "bad\t/dev/ttys003\tfleet\t0\t0",      # not a pane id
                  "%4\t/dev/pts/1\tfleet\t0\t0")         # not a macOS pty
        self.assertEqual(self.stub._tmux_panes(), {})

    def test_session_name_is_displayed_not_gated(self):
        self.rows("%2\t/dev/ttys009\tmy.repo work\x01\t0\t0")
        pane = self.stub._tmux_panes()["/dev/ttys009"]
        self.assertEqual(pane["session"], "my.repo work ")
        self.assertFalse(pane["attached"])

    def test_failed_or_oversized_listing_is_ignored(self):
        self.stub.results = [None]
        self.assertEqual(self.stub._tmux_panes(force=True), {})
        self.stub.results = [result(returncode=1, stderr="no server running")]
        self.assertEqual(self.stub._tmux_panes(force=True), {})
        self.stub.results = [result(stdout="x" * 1_000_001)]
        self.assertEqual(self.stub._tmux_panes(force=True), {})

    def test_cache_is_reused_until_forced(self):
        self.rows("%2\t/dev/ttys009\tfleet\t1\t0")
        self.assertIn("/dev/ttys009", self.stub._tmux_panes())
        self.stub.results = [result(stdout="")]
        self.assertIn("/dev/ttys009", self.stub._tmux_panes())      # cached
        self.assertEqual(self.stub._tmux_panes(force=True), {})     # refreshed

    def test_target_for_tty_validates_its_input(self):
        self.rows("%2\t/dev/ttys009\tfleet\t1\t0")
        self.assertIsNone(self.stub._tmux_target_for_tty(""))
        self.assertIsNone(self.stub._tmux_target_for_tty("/etc/passwd"))
        self.assertEqual(self.stub._tmux_target_for_tty("/dev/ttys009")["pane_id"], "%2")
        self.assertIsNone(self.stub._tmux_target_for_tty("/dev/ttys010"))


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.stub = TmuxStub()

    def sent(self):
        return [args[-1] for _, args in self.stub.calls if args[0] == "send-keys"]

    def test_text_and_newline_are_separate_literal_keys(self):
        out = self.stub._tmux_write(PANE, [("hello", True)], step_delay=0)
        self.assertEqual(out, {"ok": True, "transport": "tmux"})
        self.assertEqual(self.sent(), ["hello", "\r"])
        self.assertEqual(self.stub.calls[0][1][:5],
                         ["send-keys", "-t", "%3", "-l", "--"])

    def test_empty_text_with_newline_sends_only_the_return(self):
        self.stub._tmux_write(PANE, [("", True)], step_delay=0)
        self.assertEqual(self.sent(), ["\r"])

    def test_escape_sequences_pass_through_unchanged(self):
        self.stub._tmux_write(PANE, [("\x1b[B", False), ("\x1b", False)],
                              step_delay=0)
        self.assertEqual(self.sent(), ["\x1b[B", "\x1b"])

    def test_delay_fires_only_between_steps(self):
        with mock.patch.object(tm.time, "sleep") as sleep:
            self.stub._tmux_write(PANE, [("1", False), ("2", False), ("3", False)],
                                  step_delay=0.4)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(sleep.call_args[0][0], 0.4)

    def test_default_delay_matches_the_native_prompt_pacing(self):
        with mock.patch.object(tm.time, "sleep") as sleep:
            self.stub._tmux_write(PANE, [("1", False), ("2", False)])
        self.assertEqual(sleep.call_args[0][0], 0.4)

    def test_missing_tmux_proves_nothing_was_written(self):
        stub = TmuxStub(executable="")
        out = stub._tmux_write(PANE, [("hi", True)], step_delay=0)
        self.assertEqual(out["code"], "terminal_not_available")
        self.assertTrue(TransportOps._native_write_failed_before_delivery(out))

    def test_first_key_refused_is_a_proven_failure(self):
        self.stub.results = [result(returncode=1, stderr="can't find pane: %3")]
        out = self.stub._tmux_write(PANE, [("hi", False)], step_delay=0)
        self.assertEqual(out["code"], "terminal_not_available")
        self.assertIn("can't find pane", out["error"])

    def test_later_key_refused_is_uncertain(self):
        self.stub.results = [result(), result(returncode=1, stderr="pane gone")]
        out = self.stub._tmux_write(PANE, [("hi", True)], step_delay=0)
        self.assertEqual(out["code"], "delivery_uncertain")
        self.assertFalse(TransportOps._native_write_failed_before_delivery(out))

    def test_lost_result_is_always_uncertain(self):
        self.stub.results = [None]
        out = self.stub._tmux_write(PANE, [("hi", False)], step_delay=0)
        self.assertEqual(out["code"], "delivery_uncertain")
        self.assertFalse(TransportOps._native_write_failed_before_delivery(out))


class CaptureTests(unittest.TestCase):
    """`_tmux_capture`: read-only, bounded, and control-character scrubbed."""

    def setUp(self):
        self.stub = TmuxStub()

    def test_visible_screen_is_returned_as_lines(self):
        self.stub.results = [result(stdout="❯ 1. Red   \n  2. Green\n\n\n")]
        out = self.stub._tmux_capture(PANE)
        self.assertEqual(out, {"ok": True, "lines": ["❯ 1. Red", "  2. Green"],
                               "truncated": False})
        self.assertEqual(self.stub.calls[0][1],
                         ["capture-pane", "-p", "-t", "%3"])

    def test_no_keys_are_ever_sent(self):
        self.stub.results = [result(stdout="x\n")]
        self.stub._tmux_capture(PANE)
        self.assertNotIn("send-keys", [args[0] for _, args in self.stub.calls])

    def test_control_characters_are_scrubbed(self):
        self.stub.results = [result(stdout="a\x1b[31mb\x00c\x07d\te\n")]
        self.assertEqual(self.stub._tmux_capture(PANE)["lines"], ["a [31mb c d\te"])

    def test_rows_and_columns_are_bounded(self):
        self.stub.results = [result(stdout="\n".join(f"row{index}" for index in range(9)))]
        out = self.stub._tmux_capture(PANE, max_rows=4)
        self.assertEqual(out["lines"], ["row5", "row6", "row7", "row8"])
        self.assertTrue(out["truncated"])

        self.stub.results = [result(stdout="y" * 900)]
        self.assertEqual(len(self.stub._tmux_capture(PANE, max_columns=40)["lines"][0]), 40)

    def test_blank_screen_and_failures(self):
        self.stub.results = [result(stdout="\n\n\n")]
        self.assertEqual(self.stub._tmux_capture(PANE), {"ok": True, "lines": [],
                                                         "truncated": False})
        self.stub.results = [result(returncode=1, stderr="can't find pane")]
        self.assertEqual(self.stub._tmux_capture(PANE)["ok"], False)
        self.stub.results = [None]
        self.assertFalse(self.stub._tmux_capture(PANE)["ok"])
        self.assertFalse(TmuxStub(executable="")._tmux_capture(PANE)["ok"])


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.stub = TmuxStub({"terminal_app": "Ghostty"})

    def test_focus_selects_the_pane_and_raises_the_app(self):
        with mock.patch.object(tm.subprocess, "run", return_value=result()) as run:
            out = self.stub._tmux_write(PANE, [("__FOCUS__", False)])
        self.assertEqual(out, {"ok": True, "transport": "tmux", "focused": True})
        self.assertEqual([args[0] for _, args in self.stub.calls],
                         ["select-window", "select-pane"])
        self.assertEqual(run.call_args[0][0], ["open", "-a", "Ghostty"])

    def test_no_app_configured_raises_nothing(self):
        stub = TmuxStub({"terminal_app": ""})
        with mock.patch.object(tm.subprocess, "run") as run:
            self.assertTrue(stub._tmux_write(PANE, [("__FOCUS__", False)])["ok"])
        run.assert_not_called()

    def test_unraisable_app_is_a_warning_not_a_failure(self):
        with mock.patch.object(tm.subprocess, "run",
                               return_value=result(returncode=1, stderr="no app")):
            out = self.stub._tmux_write(PANE, [("__FOCUS__", False)])
        self.assertTrue(out["ok"])
        self.assertIn("could not be raised", out["warning"])

        with mock.patch.object(tm.subprocess, "run", side_effect=OSError("boom")):
            out = self.stub._tmux_write(PANE, [("__FOCUS__", False)])
        self.assertIn("could not be raised", out["warning"])

    def test_detached_session_says_so(self):
        pane = {**PANE, "attached": False}
        with mock.patch.object(tm.subprocess, "run", return_value=result()):
            out = self.stub._tmux_focus_pane(pane)
        self.assertIn("no terminal is attached", out["warning"])

    def test_select_failure_writes_nothing(self):
        self.stub.results = [result(returncode=1, stderr="can't find pane")]
        out = self.stub._tmux_focus_pane(PANE)
        self.assertEqual(out["code"], "terminal_not_available")

        self.stub.results = [None]
        self.assertEqual(self.stub._tmux_focus_pane(PANE)["code"],
                         "terminal_not_available")


class SpawnTests(unittest.TestCase):
    def setUp(self):
        self.stub = TmuxStub()
        self.stub._tmux_socket_paths = lambda: ["/sockA", "/sockB"]

    def test_window_joins_the_server_already_hosting_the_session(self):
        # the first socket has no such session; the second one does
        self.stub.results = [result(returncode=1, stderr="can't find session"),
                             result(), result()]
        out = self.stub._tmux_spawn("cd /tmp && claude", label="claude")
        self.assertEqual(out, {"ok": True, "transport": "tmux",
                               "tmux_session": "fleet", "tmux_window": "claude",
                               "tmux_attach": "tmux attach -t fleet"})
        self.assertEqual([socket for socket, _ in self.stub.calls],
                         ["/sockA", "/sockB", "/sockB"])
        self.assertEqual(self.stub.calls[1][1], ["has-session", "-t", "=fleet"])
        self.assertEqual(self.stub.calls[2][1][:6],
                         ["new-window", "-d", "-t", "=fleet", "-n", "claude"])
        # an interactive login shell, so the pane gets the environment a terminal
        # tab would give it, then a login shell that outlives the session
        self.assertEqual(
            self.stub.calls[2][1][-1],
            "${SHELL:-/bin/sh} -i -l -c 'cd /tmp && claude'; "
            "exec ${SHELL:-/bin/sh} -l")
        self.assertEqual(self.stub._tmux_panes_cache, (0.0, {}))

    def test_fleet_never_starts_the_server_itself(self):
        """A server Fleet started would hand Claude launchd's minimal PATH."""
        self.stub.results = [result(returncode=1), result(returncode=1)]
        out = self.stub._tmux_spawn("claude")
        self.assertEqual(out["code"], "terminal_not_available")
        self.assertIn("tmux new -s fleet", out["error"])
        self.assertNotIn("new-session", [args[0] for _, args in self.stub.calls])

    def test_configured_session_name_is_used(self):
        self.stub.cfg["tmux_session"] = "work"
        self.stub.results = [result(), result()]
        self.assertEqual(self.stub._tmux_spawn("claude", label="resume")["tmux_window"],
                         "resume")
        self.assertEqual(self.stub.calls[0][1], ["has-session", "-t", "=work"])

    def test_window_label_is_bounded(self):
        self.stub.results = [result(), result()]
        self.assertEqual(
            self.stub._tmux_spawn("claude", label="../../evil name")["tmux_window"],
            "evilname")

        self.stub.calls, self.stub.results = [], [result(), result()]
        self.assertEqual(self.stub._tmux_spawn("claude", label="!!")["tmux_window"],
                         "claude")

    def test_missing_tmux_is_a_proven_failure(self):
        stub = TmuxStub(executable="")
        out = stub._tmux_spawn("claude")
        self.assertEqual(out["code"], "terminal_not_available")
        self.assertIn("not installed", out["error"])

    def test_refused_window_versus_lost_result(self):
        self.stub.results = [result(), result(returncode=1, stderr="nope")]
        self.assertEqual(self.stub._tmux_spawn("claude")["code"],
                         "terminal_not_available")

        self.stub.calls, self.stub.results = [], [result(), None]
        out = self.stub._tmux_spawn("claude")
        self.assertEqual(out["code"], "delivery_uncertain")
        self.assertIn("tmux attach -t fleet", out["error"])

    def test_socket_probe_tolerates_a_dead_server(self):
        self.stub.results = [None, result()]
        self.assertTrue(self.stub._tmux_spawn("claude")["ok"])


class SettingsTests(unittest.TestCase):
    def test_terminal_app_name_is_bounded(self):
        self.assertEqual(TmuxStub({"terminal_app": "iTerm"})._terminal_app_name(),
                         "iTerm")
        self.assertEqual(TmuxStub({"terminal_app": ""})._terminal_app_name(), "")
        self.assertEqual(
            TmuxStub({"terminal_app": "Term; rm -rf /"})._terminal_app_name(), "")
        self.assertEqual(TmuxStub({"terminal_app": "x" * 80})._terminal_app_name(), "")

    def test_session_name_falls_back_to_fleet(self):
        self.assertEqual(TmuxStub({"tmux_session": "work"})._tmux_session_name(),
                         "work")
        self.assertEqual(TmuxStub({"tmux_session": "a:b"})._tmux_session_name(),
                         "fleet")
        self.assertEqual(TmuxStub({})._tmux_session_name(), "fleet")


class DispatcherTests(unittest.TestCase):
    """`_terminal_write` / `_terminal_spawn`: selection is server-derived."""

    def setUp(self):
        self.stub = TmuxStub()
        self.stub._tmux_socket_paths = lambda: ["/sock"]
        self.stub.results = [result(stdout="%2\t/dev/ttys009\tfleet\t1\t0\n")]

    def test_mode_is_validated(self):
        self.assertEqual(self.stub._terminal_transport_mode(), "auto")
        self.stub.cfg["terminal_transport"] = " TMUX "
        self.assertEqual(self.stub._terminal_transport_mode(), "tmux")
        self.stub.cfg["terminal_transport"] = "carrier pigeon"
        self.assertEqual(self.stub._terminal_transport_mode(), "auto")

    def test_auto_uses_tmux_for_a_tmux_pane(self):
        out = self.stub._terminal_write("/dev/ttys009", [("hi", True)], step_delay=0)
        self.assertEqual(out["transport"], "tmux")
        self.assertEqual(self.stub.iterm_calls, [])

    def test_auto_falls_back_to_the_applet_for_a_plain_tab(self):
        out = self.stub._terminal_write("/dev/ttys999", [("hi", True)], step_delay=0)
        self.assertEqual(out["transport"], "applet")
        self.assertEqual(self.stub.iterm_calls[0][0], "/dev/ttys999")

    def test_applet_mode_never_looks_for_a_pane(self):
        self.stub.cfg["terminal_transport"] = "applet"
        self.stub._tmux_target_for_tty = lambda *a, **k: self.fail("no lookup")
        self.assertEqual(
            self.stub._terminal_write("/dev/ttys009", [("hi", True)])["transport"],
            "applet")

    def test_tmux_mode_refuses_a_non_tmux_session(self):
        self.stub.cfg["terminal_transport"] = "tmux"
        out = self.stub._terminal_write("/dev/ttys999", [("hi", True)])
        self.assertEqual(out["code"], "terminal_not_available")
        self.assertTrue(TransportOps._native_write_failed_before_delivery(out))
        self.assertEqual(self.stub.iterm_calls, [])

    def test_spawn_prefers_tmux(self):
        with mock.patch.object(TmuxStub, "_tmux_spawn",
                               return_value={"ok": True, "transport": "tmux"}) as spawn:
            self.assertEqual(self.stub._terminal_spawn("claude", label="resume"),
                             {"ok": True, "transport": "tmux"})
        self.assertEqual(spawn.call_args.kwargs, {"label": "resume"})
        self.assertEqual(self.stub.iterm_calls, [])

    def test_spawn_falls_back_only_on_a_proven_failure(self):
        absent = TmuxStub(executable="")     # no tmux: nothing was created
        self.assertEqual(absent._terminal_spawn("claude")["transport"], "applet")
        self.assertEqual(absent.iterm_calls[0][0], "SPAWN")

        # an uncertain tmux result may already have opened a window; a second
        # attempt through the applet would create a duplicate session
        uncertain = {"ok": False, "code": "delivery_uncertain", "error": "lost"}
        with mock.patch.object(TmuxStub, "_tmux_spawn", return_value=uncertain):
            self.assertEqual(self.stub._terminal_spawn("claude"), uncertain)
        self.assertEqual(self.stub.iterm_calls, [])

    def test_tmux_mode_reports_the_missing_session(self):
        self.stub.cfg["terminal_transport"] = "tmux"
        missing = {"ok": False, "code": "terminal_not_available", "error": "no session"}
        with mock.patch.object(TmuxStub, "_tmux_spawn", return_value=missing):
            self.assertEqual(self.stub._terminal_spawn("claude"), missing)
        self.assertEqual(self.stub.iterm_calls, [])

    def test_applet_mode_spawns_through_the_applet(self):
        self.stub.cfg["terminal_transport"] = "applet"
        self.assertEqual(self.stub._terminal_spawn("claude")["transport"], "applet")
        self.assertEqual(self.stub.iterm_calls[0][1], [("claude", False)])


@unittest.skipUnless(shutil.which("tmux") or os.path.exists("/opt/homebrew/bin/tmux"),
                     "tmux is not installed")
class LiveTmuxTests(unittest.TestCase):
    """End-to-end against a real tmux server on a private socket directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sockets = os.path.join(self.tmp.name, "tmux-live")
        patcher = mock.patch.object(engine_paths, "TMUX_SOCKETS", self.sockets)
        patcher.start()
        self.addCleanup(patcher.stop)

        class LiveStub(TmuxOps, TransportOps):
            def __init__(inner):
                inner.cfg = {"tmux_session": "fdtest", "terminal_app": ""}
                inner._tmux_executable = None
                inner._tmux_panes_cache = (0.0, {})
                inner._tmux_lock = threading.Lock()

        self.stub = LiveStub()
        self.socket_path = os.path.join(self.sockets, "default")
        self.addCleanup(self.kill_server)

    def kill_server(self):
        if os.path.exists(self.socket_path):
            self.stub._tmux_run(self.socket_path, ["kill-server"])

    def start_server(self):
        """Stand in for the operator's own `tmux new -s fdtest`."""
        os.makedirs(self.sockets, mode=0o700, exist_ok=True)
        started = self.stub._tmux_run(
            self.socket_path, ["new-session", "-d", "-s", "fdtest"], timeout=15)
        self.assertIsNotNone(started)
        self.assertEqual(started.returncode, 0, started.stderr)

    def test_spawn_refuses_to_start_the_server_itself(self):
        out = self.stub._tmux_spawn("true", label="probe")
        self.assertEqual(out["code"], "terminal_not_available")
        self.assertIn("tmux new -s fdtest", out["error"])
        self.assertFalse(os.path.exists(self.socket_path))

    def test_spawn_then_discover_then_write(self):
        self.start_server()
        before = set(self.stub._tmux_panes(force=True))
        marker = os.path.join(self.tmp.name, "marker.txt")
        spawned = self.stub._tmux_spawn(f"cd {self.tmp.name}", label="probe")
        self.assertTrue(spawned["ok"], spawned)

        panes = self.stub._tmux_panes(force=True)
        new = set(panes) - before
        self.assertEqual(len(new), 1, panes)
        tty = new.pop()
        self.assertEqual(panes[tty]["session"], "fdtest")
        self.assertEqual(self.stub._tmux_target_for_tty(tty)["pane_id"],
                         panes[tty]["pane_id"])

        # a real shell is still running in the pane, so keys reach a real tty
        written = self.stub._terminal_write(
            tty, [(f"printf ok > {marker}", True)], step_delay=0)
        self.assertEqual(written, {"ok": True, "transport": "tmux"})
        for _ in range(60):
            if os.path.exists(marker):
                break
            tm.time.sleep(0.05)
        with open(marker) as handle:
            self.assertEqual(handle.read(), "ok")

    def test_unknown_pane_is_a_proven_failure(self):
        self.stub._tmux_spawn("true", label="probe")
        pane = {"socket": os.path.join(self.sockets, "default"),
                "pane_id": "%999", "session": "fdtest", "attached": False}
        out = self.stub._tmux_write(pane, [("x", False)], step_delay=0)
        self.assertEqual(out["code"], "terminal_not_available")
        self.assertIn("pane", out["error"])



class BatchedCaptureTests(unittest.TestCase):
    """One tmux invocation for many panes (invariant 78).

    Measured 2026-07-24 on tmux 3.7b with 49 panes: 241 ms as one client
    invocation per pane against 5.6 ms batched. The whole cost is forking the
    client, so batching is what makes a fleet-wide observation pass affordable.
    """

    def setUp(self):
        self.stub = TmuxStub()

    def _panes(self, count, socket_path="/tmp/s"):
        return [{"socket": socket_path, "pane_id": f"%{index}"} for index in range(count)]

    def _stdout(self, blocks):
        mark = TmuxStub.CAPTURE_MARK
        rows = []
        for index, lines in blocks:
            rows.append(f"{mark}{index}")
            rows.extend(lines)
        return "\n".join(rows)

    def _result(self, stdout):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def test_one_invocation_covers_every_pane(self):
        self.stub.results = [self._result(self._stdout(
            [(0, ["a"]), (1, ["b"]), (2, ["c"])]))]
        got = self.stub._tmux_capture_many(self._panes(3))
        self.assertEqual(len(self.stub.calls), 1)     # ONE subprocess, not three
        self.assertEqual(got["%0"]["lines"], ["a"])
        self.assertEqual(got["%2"]["lines"], ["c"])

    def test_panes_are_grouped_by_socket(self):
        panes = [{"socket": "/tmp/a", "pane_id": "%0"},
                 {"socket": "/tmp/b", "pane_id": "%1"}]
        self.stub.results = [self._result(self._stdout([(0, ["x"])])),
                             self._result(self._stdout([(0, ["y"])]))]
        got = self.stub._tmux_capture_many(panes)
        self.assertEqual(len(self.stub.calls), 2)     # one per socket, not per pane
        self.assertEqual(got["%0"]["lines"], ["x"])
        self.assertEqual(got["%1"]["lines"], ["y"])

    def test_a_pane_that_vanished_is_simply_absent(self):
        self.stub.results = [self._result(self._stdout([(0, ["only"])]))]
        got = self.stub._tmux_capture_many(self._panes(2))
        self.assertIn("%0", got)
        self.assertNotIn("%1", got)

    def test_control_characters_are_scrubbed_and_bounded(self):
        self.stub.results = [self._result(self._stdout([(0, ["a\x07b" + "z" * 500])]))]
        got = self.stub._tmux_capture_many(self._panes(1), max_columns=10)
        self.assertNotIn("\x07", got["%0"]["lines"][0])
        self.assertLessEqual(len(got["%0"]["lines"][0]), 10)

    def test_only_the_last_rows_are_kept(self):
        rows = [f"line{index}" for index in range(10)]
        self.stub.results = [self._result(self._stdout([(0, rows)]))]
        got = self.stub._tmux_capture_many(self._panes(1), max_rows=3)
        self.assertEqual(got["%0"]["lines"], ["line7", "line8", "line9"])
        self.assertTrue(got["%0"]["truncated"])

    def test_a_failed_socket_yields_nothing_for_its_panes(self):
        self.stub.results = [SimpleNamespace(returncode=1, stdout="", stderr="boom")]
        self.assertEqual(self.stub._tmux_capture_many(self._panes(2)), {})

    def test_no_tmux_binary_yields_nothing(self):
        self.stub._tmux_executable = ""
        self.assertEqual(self.stub._tmux_capture_many(self._panes(2)), {})

    def test_no_panes_makes_no_call(self):
        self.assertEqual(self.stub._tmux_capture_many([]), {})
        self.assertEqual(self.stub.calls, [])

    def test_a_malformed_marker_is_ignored(self):
        mark = TmuxStub.CAPTURE_MARK
        self.stub.results = [self._result(f"{mark}notanumber\nrow\n{mark}0\nkept")]
        got = self.stub._tmux_capture_many(self._panes(1))
        self.assertEqual(got["%0"]["lines"], ["kept"])

    def test_trailing_blank_rows_are_dropped(self):
        """Unused screen below the content is not content."""
        self.stub.results = [self._result(self._stdout([(0, ["text", "", "  ", ""])]))]
        got = self.stub._tmux_capture_many(self._panes(1))
        self.assertEqual(got["%0"]["lines"], ["text"])

    def test_an_index_beyond_the_group_is_discarded(self):
        self.stub.results = [self._result(self._stdout([(9, ["ghost"])]))]
        self.assertEqual(self.stub._tmux_capture_many(self._panes(1)), {})

if __name__ == "__main__":  # pragma: no cover - module entrypoint
    unittest.main()
