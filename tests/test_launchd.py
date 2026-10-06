"""fleetdash.launchd: the install identity and the plists it renders."""
import contextlib
import io
import os
import plistlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash import launchd
from fleetdash import paths as engine_paths

_ENV_KEYS = ("FLEET_DASH_LABEL_PREFIX", "FLEET_DASH_PROD_CHECKOUT",
             "FLEET_DASH_PROD_STATE", "FLEET_DASH_CODEX_SOCKET")


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        home = os.path.join(self.root, "home")
        claude = os.path.join(home, ".claude")
        env = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS}
        for patcher in (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(launchd.pwd, "getpwuid",
                                  return_value=SimpleNamespace(pw_name="tester")),
                mock.patch.object(engine_paths, "HOME", home),
                mock.patch.object(engine_paths, "PRODUCTION_CHECKOUT", os.path.join(claude, "prod")),
                mock.patch.object(engine_paths, "STAGING_CHECKOUT", os.path.join(claude, "src")),
                mock.patch.object(engine_paths, "PRODUCTION_BASE", os.path.join(claude, "prod-state")),
                mock.patch.object(engine_paths, "STAGING_BASE", os.path.join(claude, "staging-state")),
                mock.patch.object(engine_paths, "DEFAULT_CAPTURE_BASE", os.path.join(claude, "capture"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.claude = claude

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = launchd.main(list(argv))
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_render_writes_each_instance_plist_from_one_identity(self):
        c = self.claude
        prod_path = launchd.render("production", self.root)
        self.assertEqual(prod_path, os.path.join(self.root, "com.tester.fleet-dash.plist"))
        with open(prod_path, "rb") as handle:
            self.assertEqual(plistlib.load(handle), {
                "Label": "com.tester.fleet-dash",
                "ProgramArguments": ["/usr/bin/python3", f"{c}/prod/server.py"],
                "EnvironmentVariables": {
                    "FLEET_DASH_INSTANCE": "production",
                    "FLEET_DASH_STATE_DIR": f"{c}/prod-state",
                    "FLEET_DASH_CAPTURE_DIR": f"{c}/capture"},
                "RunAtLoad": True, "KeepAlive": True,
                "StandardOutPath": f"{c}/prod-state/fleet-dash.log",
                "StandardErrorPath": f"{c}/prod-state/fleet-dash.log",
                "ProcessType": "Background"})

        staging = launchd.plist("staging")
        self.assertEqual(staging["Label"], "com.tester.fleet-dash.staging")
        self.assertEqual(staging["ProgramArguments"][1], f"{c}/src/server.py")
        self.assertEqual(staging["EnvironmentVariables"], {
            "FLEET_DASH_INSTANCE": "staging",
            "FLEET_DASH_STATE_DIR": f"{c}/staging-state",
            "FLEET_DASH_CAPTURE_DIR": f"{c}/capture",
            "FLEET_DASH_STAGING_SOURCE": f"{c}/src",
            "FLEET_DASH_CODEX_SOCKET": f"{c}/staging-state/codex-app-server.sock",
            "FLEET_DASH_PORT": "8378"})
        self.assertEqual(staging["StandardErrorPath"], f"{c}/staging-state/fleet-dash.log")

        default_path = launchd.render("staging")
        self.assertEqual(default_path, os.path.join(
            engine_paths.HOME, "Library", "LaunchAgents", "com.tester.fleet-dash.staging.plist"))
        self.assertTrue(os.path.isfile(default_path))

        with mock.patch.dict(os.environ, {"FLEET_DASH_LABEL_PREFIX": "org.example",
                                          "FLEET_DASH_PROD_CHECKOUT": f"{self.root}/elsewhere",
                                          "FLEET_DASH_PROD_STATE": f"{self.root}/other-state"}):
            moved = launchd.plist("production")
        self.assertEqual(moved["Label"], "org.example.fleet-dash")
        self.assertEqual(moved["ProgramArguments"][1], f"{self.root}/elsewhere/server.py")
        self.assertEqual(moved["EnvironmentVariables"]["FLEET_DASH_STATE_DIR"], f"{self.root}/other-state")

    def test_cli_prints_values_and_refuses_bad_input(self):
        self.assertEqual(self.run_cli("label", "production"), (0, "com.tester.fleet-dash", ""))
        self.assertEqual(self.run_cli("bundle-id", "staging"),
                         (0, "com.tester.fleet-dash.staging.injector", ""))
        self.assertEqual(self.run_cli("checkout", "production"), (0, f"{self.claude}/prod", ""))
        self.assertEqual(self.run_cli("state", "staging"), (0, f"{self.claude}/staging-state", ""))
        out_dir = os.path.join(self.root, "agents")
        self.assertEqual(self.run_cli("render", "production", out_dir),
                         (0, os.path.join(out_dir, "com.tester.fleet-dash.plist"), ""))

        code, out, err = self.run_cli("label", "qa")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown instance: 'qa'", err)
        with mock.patch.dict(os.environ, {"FLEET_DASH_LABEL_PREFIX": "bad prefix"}):
            code, out, err = self.run_cli("label", "production")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("invalid launchd label prefix: 'bad prefix'", err)
        for argv in ((), ("label",), ("deploy", "production"), ("label", "production", out_dir)):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertTrue(err.startswith("usage:"), argv)


if __name__ == "__main__":
    unittest.main()
