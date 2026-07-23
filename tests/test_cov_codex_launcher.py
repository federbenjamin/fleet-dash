"""Coverage tests for fleetdash.codex_launcher: argument routing, real-codex
discovery, launcher install/status, and the main() entrypoint."""
import os
import tempfile
import unittest
from unittest import mock

from fleetdash import codex_launcher as launcher
from fleetdash.codex_launcher import (
    find_real_codex, install_launcher, launcher_status, main, managed_socket,
    route_arguments)


class ManagedSocketTest(unittest.TestCase):
    def test_uses_codex_home_env(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/custom/codex"}, clear=False):
            self.assertEqual(managed_socket(),
                             "/custom/codex/app-server-control/app-server-control.sock")

    def test_uses_home_argument(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            path = managed_socket(home="/tmp/home")
            self.assertTrue(path.endswith(
                ".codex/app-server-control/app-server-control.sock"))


class RouteArgumentsTest(unittest.TestCase):
    def test_value_option_and_equals_and_short_flags(self):
        endpoint = "unix:///m.sock"
        # -c consumes a value, then an interactive prompt triggers remote mode.
        self.assertEqual(route_arguments(["-c", "k=v", "hello"], "/m.sock"),
                         (["--remote", endpoint, "-c", "k=v", "hello"], True))
        # --config=... equals form is a single token.
        self.assertEqual(route_arguments(["--config=x", "hello"], "/m.sock"),
                         (["--remote", endpoint, "--config=x", "hello"], True))
        # An unknown short flag is skipped, prompt still routes.
        self.assertEqual(route_arguments(["-x", "hello"], "/m.sock"),
                         (["--remote", endpoint, "-x", "hello"], True))

    def test_double_dash_stops_command_scan(self):
        endpoint = "unix:///m.sock"
        self.assertEqual(route_arguments(["--", "resume"], "/m.sock"),
                         (["--remote", endpoint, "--", "resume"], True))


class FindRealCodexTest(unittest.TestCase):
    def test_explicit_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "codex")
            with open(real, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(real, 0o755)
            own = os.path.join(tmp, "launcher.py")
            with mock.patch.dict(os.environ, {"FLEET_DASH_CODEX_REAL": real},
                                 clear=False):
                self.assertEqual(find_real_codex(path="", own_path=own),
                                 os.path.realpath(real))

    def test_skips_empty_path_entries_and_raises_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = os.path.join(tmp, "launcher.py")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "could not find"):
                    find_real_codex(path=os.pathsep + os.pathsep, own_path=own)


class InstallAndStatusTest(unittest.TestCase):
    def _source(self):
        return os.path.abspath(launcher.__file__)

    def test_install_when_no_zshrc(self):
        with tempfile.TemporaryDirectory() as home:
            status = install_launcher(source=self._source(), home=home)
            self.assertEqual(status["state"], "ready")
            with open(os.path.join(home, ".zshrc")) as handle:
                self.assertIn(launcher.BEGIN, handle.read())

    def test_status_when_nothing_installed(self):
        with tempfile.TemporaryDirectory() as home:
            status = launcher_status(home=home)
            self.assertFalse(status["installed"])
            self.assertFalse(status["shell_configured"])
            self.assertEqual(status["state"], "repair_needed")


class MainTest(unittest.TestCase):
    def test_status_command(self):
        with mock.patch.object(launcher, "launcher_status",
                               return_value={"state": "ready"}):
            self.assertEqual(main(["--fleet-launcher-status"]), 0)

    def test_install_command(self):
        with mock.patch.object(launcher, "install_launcher",
                               return_value={"state": "ready"}):
            self.assertEqual(main(["--fleet-launcher-install"]), 0)

    def test_execv_path(self):
        calls = []
        with mock.patch.object(launcher, "find_real_codex", return_value="/opt/codex"), \
                mock.patch.object(launcher.os, "execv",
                                  side_effect=lambda exe, argv: calls.append((exe, argv))):
            main(["resume", "abc"])
        self.assertEqual(calls[0][0], "/opt/codex")

    def test_execv_failure_returns_127(self):
        with mock.patch.object(launcher, "find_real_codex",
                               side_effect=RuntimeError("no codex")):
            self.assertEqual(main(["resume"]), 127)


if __name__ == "__main__":
    unittest.main()
