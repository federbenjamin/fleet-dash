"""fleetdash.launchd: the install identity and the plists it renders."""
import contextlib
import io
import os
import pathlib
import plistlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash import launchd
from fleetdash import paths as engine_paths

_ENV_KEYS = ("FLEET_DASH_LABEL_PREFIX", "FLEET_DASH_PROD_CHECKOUT",
             "FLEET_DASH_PROD_STATE", "FLEET_DASH_STAGING_CHECKOUT")


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
        os.makedirs(os.path.join(claude, "src"))
        pathlib.Path(claude, "src", "server.py").touch()

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

    def test_render_ignores_the_callers_runtime_environment(self):
        hostile = {engine_paths.ENV_CODEX_SOCKET: "/prod/codex.sock",
                   engine_paths.ENV_PORT: "8377", engine_paths.ENV_INSTANCE: "production",
                   engine_paths.ENV_STATE_DIR: "/prod/state",
                   engine_paths.ENV_CAPTURE_DIR: "/prod/capture",
                   engine_paths.ENV_STAGING_SOURCE: "/prod/src"}
        clean_staging = launchd.plist("staging")
        with mock.patch.dict(os.environ, hostile):
            staging = launchd.plist("staging")
            production = launchd.plist("production")
        self.assertEqual(staging, clean_staging)
        self.assertEqual(staging["EnvironmentVariables"][engine_paths.ENV_CODEX_SOCKET],
                         f"{self.claude}/staging-state/codex-app-server.sock")
        self.assertEqual(production["EnvironmentVariables"][engine_paths.ENV_STATE_DIR],
                         f"{self.claude}/prod-state")
        self.assertEqual(production["EnvironmentVariables"][engine_paths.ENV_CAPTURE_DIR],
                         f"{self.claude}/capture")

    def test_plist_env_names_are_the_names_the_readers_import(self):
        names = {engine_paths.ENV_INSTANCE, engine_paths.ENV_STATE_DIR,
                 engine_paths.ENV_CAPTURE_DIR, engine_paths.ENV_STAGING_SOURCE,
                 engine_paths.ENV_CODEX_SOCKET, engine_paths.ENV_PORT}
        self.assertEqual(set(launchd.plist("staging")["EnvironmentVariables"]), names)
        for module in pathlib.Path(launchd.__file__).parent.glob("*.py"):
            if module.name == "paths.py":
                continue
            text = module.read_text()
            for name in names:
                self.assertNotIn(f'"{name}"', text, f"{module.name} holds {name} as a literal")

    def test_staging_checkout_is_the_clone_unless_overridden(self):
        clone = os.path.join(self.root, "code", "fleet-dash")
        with mock.patch.object(engine_paths, "STAGING_CHECKOUT", os.path.join(self.root, "absent")), \
                mock.patch.object(engine_paths, "REPO_ROOT", clone):
            self.assertEqual(launchd.checkout("staging"), clone)
            staging = launchd.plist("staging")
            self.assertEqual(staging["ProgramArguments"][1], f"{clone}/server.py")
            self.assertEqual(staging["EnvironmentVariables"][engine_paths.ENV_STAGING_SOURCE], clone)
            with mock.patch.dict(os.environ, {"FLEET_DASH_STAGING_CHECKOUT": f"{self.root}/pinned"}):
                self.assertEqual(launchd.checkout("staging"), f"{self.root}/pinned")
        with mock.patch.object(engine_paths, "REPO_ROOT", clone):
            self.assertEqual(launchd.checkout("staging"), f"{self.claude}/src")

    def test_failed_render_leaves_the_installed_plist_intact(self):
        target = launchd.render("production", self.root)
        before = pathlib.Path(target).read_bytes()

        def partial_dump(content, handle, **kwargs):
            handle.write(b"<?xml partial")
            raise OSError(28, "No space left on device")

        def interrupted(content, handle, **kwargs):
            handle.write(b"<?xml partial")
            raise KeyboardInterrupt

        with mock.patch.object(launchd.plistlib, "dump", partial_dump):
            code, out, err = self.run_cli("render", "production", self.root)
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("fleetdash.launchd: "), err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertEqual(pathlib.Path(target).read_bytes(), before)
        with mock.patch.object(launchd.plistlib, "dump", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                launchd.render("production", self.root)
        self.assertEqual(pathlib.Path(target).read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.root)), ["com.tester.fleet-dash.plist", "home"])
        self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o644")


class BuildInjectorTests(unittest.TestCase):
    def test_script_stamps_the_modules_state_dir_and_bundle_id(self):
        repo = pathlib.Path(launchd.__file__).parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp, "bin")
            bin_dir.mkdir()
            log = pathlib.Path(tmp, "calls.txt")
            stub = f'#!/bin/sh\necho "$(basename "$0") $*" >> "{log}"\n'
            (bin_dir / "plutil").write_text(stub)
            (bin_dir / "codesign").write_text(stub)
            (bin_dir / "osacompile").write_text(stub + f'cp "$3" "{tmp}/compiled.applescript"\n')
            for stub_file in bin_dir.iterdir():
                stub_file.chmod(0o755)
            state = f'{tmp}/state "dir" & \\ x'
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": tmp,
                   "FLEET_DASH_PROD_STATE": state, "FLEET_DASH_LABEL_PREFIX": "org.example"}
            for mode in ("production", "staging"):
                result = subprocess.run(
                    ["sh", str(repo / "scripts" / "build-injector.sh"), mode, f"{tmp}/Probe.app"],
                    cwd=tmp, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                compiled = pathlib.Path(tmp, "compiled.applescript").read_text()
                self.assertNotIn("@STATE_DIR@", compiled)
                self.assertNotIn("fleet-dash-prod-state", compiled)
                if mode == "production":
                    escaped = state.replace("\\", "\\\\").replace('"', '\\"')
                    self.assertIn(f'set base to "{escaped}/"', compiled)
                else:
                    self.assertIn("/.claude/fleet-dash-staging-state/", compiled)
            self.assertIn("CFBundleIdentifier -string org.example.fleet-dash.staging.injector",
                          log.read_text())


if __name__ == "__main__":
    unittest.main()
