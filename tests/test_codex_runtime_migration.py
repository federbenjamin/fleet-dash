import json
import os
import signal
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash.codex_adapter import (
    CodexError,
    CodexRuntimeMigration,
    LEGACY_RUNTIME_OWNER,
    MANAGED_RUNTIME_OWNER,
    codex_control_socket,
    codex_runtime_migration_needed,
    ensure_managed_codex_runtime,
    migrate_codex_runtime_metadata,
)
from fleetdash.codex_launcher import (
    BEGIN,
    END,
    find_real_codex,
    install_launcher,
    launcher_paths,
    route_arguments,
)


class RuntimeClient:
    def __init__(self, threads=None):
        self.threads = threads or {}
        self.thread_state = {}
        self.approvals = {}
        self.generation = 1
        self.closed = False
        self.started = False

    def start(self):
        self.started = True

    def list_threads(self):
        return [{"id": tid} for tid in self.threads]

    def read_thread(self, tid):
        value = self.threads[tid]
        if isinstance(value, Exception):
            raise value
        return json.loads(json.dumps(value))

    def owns_active_turn(self, tid):
        return bool(self.thread_state.get(tid, {}).get("turn_id"))

    def close(self):
        self.closed = True


class RuntimeMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "codex_threads.json")
        self.legacy_socket = os.path.realpath(os.path.join(self.tmp.name, "legacy.sock"))
        self.managed_socket = os.path.realpath(os.path.join(self.tmp.name, "managed.sock"))
        self.thread = {"id": "owned", "cwd": "/work", "name": "same",
                       "turns": [{"id": "turn-1", "status": "completed",
                                  "items": [{"type": "userMessage", "text": "hello"},
                                            {"type": "agentMessage", "text": "done"}]}]}
        self._write_state()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_state(self, owner=LEGACY_RUNTIME_OWNER, migration=None):
        with open(self.state_path, "w") as handle:
            json.dump({"threads": ["owned", "external"], "modes": {"owned": "plan"},
                       "snapshots": {"owned": {"revision": "r1"}},
                       "thread_meta": {
                           "owned": {"runtime_owner": owner, "cwd": "/work"},
                           "external": {"runtime_owner": "external"}},
                       "runtime_migration": migration} if migration else
                      {"threads": ["owned", "external"], "modes": {"owned": "plan"},
                       "snapshots": {"owned": {"revision": "r1"}},
                       "thread_meta": {
                           "owned": {"runtime_owner": owner, "cwd": "/work"},
                           "external": {"runtime_owner": "external"}}}, handle)

    @staticmethod
    def _read(path):
        with open(path) as handle:
            return json.load(handle)

    def _runner(self, command, **kwargs):
        if command[:2] == ["ps", "-axo"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[0] == "lsof":
            return SimpleNamespace(returncode=0,
                                   stdout="p44\nccodex\nn" + self.legacy_socket + "\n",
                                   stderr="")
        if command[:3] == ["ps", "-p", "44"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(f"{os.getuid()} /opt/codex app-server --listen "
                        f"unix://{self.legacy_socket}\n"), stderr="")
        raise AssertionError(command)

    def _adapter(self, source):
        return SimpleNamespace(
            _runtime_action_lock=threading.RLock(), client=source,
            runtime_owner=LEGACY_RUNTIME_OWNER, _loaded_threads={"owned"},
            _loaded_generation=1, _last_refresh=10)

    def test_metadata_commit_converts_only_legacy_owned_and_preserves_payloads(self):
        before = self._read(self.state_path)
        state = migrate_codex_runtime_metadata(self.state_path, clock=lambda: 20)
        self.assertEqual(state["thread_meta"]["owned"]["runtime_owner"],
                         MANAGED_RUNTIME_OWNER)
        self.assertEqual(state["thread_meta"]["external"]["runtime_owner"], "external")
        self.assertEqual(state["modes"], before["modes"])
        self.assertEqual(state["snapshots"], before["snapshots"])
        self.assertEqual(state["runtime_migration"]["phase"], "committed")
        self.assertTrue(os.path.isfile(self.state_path + ".pre-managed-daemon.bak"))
        self.assertEqual(self._read(self.state_path + ".pre-managed-daemon.bak"), before)

    def test_corrupt_state_fails_closed_without_overwrite(self):
        with open(self.state_path, "w") as handle:
            handle.write("{broken")
        with self.assertRaisesRegex(CodexError, "corrupt"):
            migrate_codex_runtime_metadata(self.state_path)
        with open(self.state_path) as handle:
            self.assertEqual(handle.read(), "{broken")

    def test_migration_needed_requires_both_uncommitted_state_and_live_socket(self):
        self.assertTrue(codex_runtime_migration_needed(
            self.state_path, self.legacy_socket, probe=lambda _path: True))
        migrate_codex_runtime_metadata(self.state_path)
        self.assertFalse(codex_runtime_migration_needed(
            self.state_path, self.legacy_socket, probe=lambda _path: True))

    def test_active_turn_drains_without_starting_target_or_stopping_source(self):
        source = RuntimeClient({"owned": self.thread})
        source.thread_state["owned"] = {"status": "running", "turn_id": "turn-live"}
        target_factory = mock.Mock()
        killed = []
        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket, self.managed_socket,
            target_factory, runner=self._runner, killer=lambda *args: killed.append(args),
            probe=lambda _path: True)
        migration._attempt(self._adapter(source))
        self.assertEqual(migration.phase, "draining")
        self.assertIn("turn active", migration.blockers)
        target_factory.assert_not_called()
        self.assertEqual(killed, [])
        self.assertEqual(self._read(self.state_path)["thread_meta"]["owned"][
            "runtime_owner"], LEGACY_RUNTIME_OWNER)

    def test_pending_request_compaction_bootstrap_and_terminal_are_blockers(self):
        cases = (
            (lambda client: client.approvals.update({"1": {
                "thread_id": "owned", "state": "pending"}}), "pending provider request"),
            (lambda client: client.thread_state.update({"owned": {"compacting": 0}}),
             "compaction active"),
        )
        for configure, expected in cases:
            with self.subTest(expected=expected):
                self._write_state()
                source = RuntimeClient({"owned": self.thread})
                configure(source)
                migration = CodexRuntimeMigration(
                    "/opt/codex", self.state_path, self.legacy_socket,
                    self.managed_socket, mock.Mock(), runner=self._runner,
                    probe=lambda _path: True)
                migration._attempt(self._adapter(source))
                self.assertIn(expected, migration.blockers)

        self._write_state()
        state = self._read(self.state_path)
        state["thread_meta"]["owned"]["unmaterialized"] = True
        with open(self.state_path, "w") as handle:
            json.dump(state, handle)
        source = RuntimeClient({"owned": self.thread})
        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket, self.managed_socket,
            mock.Mock(), runner=self._runner, probe=lambda _path: True)
        migration._attempt(self._adapter(source))
        self.assertIn("bootstrap incomplete", migration.blockers)

        self._write_state()
        attached_runner = mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout=("91 codex resume --remote unix://" +
                                  self.legacy_socket + " owned\n"), stderr=""))
        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket, self.managed_socket,
            mock.Mock(), runner=attached_runner, probe=lambda _path: True)
        migration._attempt(self._adapter(source))
        self.assertIn("legacy terminal attached", migration.blockers)

    def test_terminal_inspection_failure_blocks_migration_closed(self):
        source = RuntimeClient({"owned": self.thread})

        def failed_runner(command, **kwargs):
            if command[:2] == ["ps", "-axo"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="denied")
            raise AssertionError(command)

        target_factory = mock.Mock()
        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket,
            self.managed_socket, target_factory, runner=failed_runner,
            probe=lambda _path: True)
        migration._attempt(self._adapter(source))
        self.assertEqual(migration.phase, "draining")
        self.assertIn("legacy terminal check unavailable", migration.blockers)
        target_factory.assert_not_called()

    def test_target_start_failure_keeps_source_and_legacy_metadata(self):
        source = RuntimeClient({"owned": self.thread})
        target = RuntimeClient({"owned": self.thread})

        def fail_start():
            raise RuntimeError("target unavailable")

        target.start = fail_start
        adapter = self._adapter(source)
        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket,
            self.managed_socket, lambda: target, runner=self._runner,
            probe=lambda _path: True)
        migration._attempt(adapter)
        self.assertEqual(migration.phase, "blocked")
        self.assertIs(adapter.client, source)
        self.assertTrue(target.closed)
        self.assertEqual(self._read(self.state_path)["thread_meta"]["owned"][
            "runtime_owner"], LEGACY_RUNTIME_OWNER)

    def test_commit_failure_after_retirement_keeps_verified_target_live(self):
        source = RuntimeClient({"owned": self.thread})
        target = RuntimeClient({"owned": self.thread})
        live = {"value": True}
        adapter = self._adapter(source)

        def kill(_pid, _sig):
            live["value"] = False

        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket,
            self.managed_socket, lambda: target, runner=self._runner,
            killer=kill, probe=lambda _path: live["value"])
        original_checkpoint = migration._checkpoint

        def fail_commit(phase, *args, **kwargs):
            if phase == "committed":
                raise OSError("disk full")
            return original_checkpoint(phase, *args, **kwargs)

        migration._checkpoint = fail_commit
        migration._attempt(adapter)
        self.assertEqual(migration.phase, "blocked")
        self.assertIs(adapter.client, target)
        self.assertFalse(target.closed)
        self.assertTrue(source.closed)
        self.assertEqual(self._read(self.state_path)["thread_meta"]["owned"][
            "runtime_owner"], LEGACY_RUNTIME_OWNER)

    def test_history_mismatch_blocks_before_source_retirement(self):
        source = RuntimeClient({"owned": self.thread})
        changed = json.loads(json.dumps(self.thread))
        changed["turns"][0]["items"][1]["text"] = "different"
        target = RuntimeClient({"owned": changed})
        killed = []
        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket, self.managed_socket,
            lambda: target, runner=self._runner,
            killer=lambda *args: killed.append(args), probe=lambda _path: True)
        adapter = self._adapter(source)
        migration._attempt(adapter)
        self.assertEqual(migration.phase, "blocked")
        self.assertIn("fingerprint mismatch", migration.error)
        self.assertIs(adapter.client, source)
        self.assertTrue(target.closed)
        self.assertEqual(killed, [])

    def test_history_fingerprint_treats_missing_and_null_fields_as_equivalent(self):
        source = json.loads(json.dumps(self.thread))
        target = json.loads(json.dumps(self.thread))
        target["canAcceptDirectInput"] = None
        target["turns"][0]["items"][0]["results"] = None
        self.assertEqual(CodexRuntimeMigration._fingerprint(source),
                         CodexRuntimeMigration._fingerprint(target))

    def test_successful_cutover_verifies_then_sigterms_exact_listener_and_commits(self):
        source = RuntimeClient({"owned": self.thread})
        target = RuntimeClient({"owned": self.thread})
        live = {"value": True}
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            live["value"] = False

        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket, self.managed_socket,
            lambda: target, runner=self._runner, killer=kill,
            probe=lambda _path: live["value"])
        adapter = self._adapter(source)
        migration._attempt(adapter)
        self.assertEqual(migration.phase, "committed")
        self.assertEqual(killed, [(44, signal.SIGTERM)])
        self.assertIs(adapter.client, target)
        self.assertTrue(source.closed)
        self.assertEqual(adapter.runtime_owner, MANAGED_RUNTIME_OWNER)
        state = self._read(self.state_path)
        self.assertEqual(state["runtime_migration"]["phase"], "committed")
        self.assertEqual(state["thread_meta"]["owned"]["runtime_owner"],
                         MANAGED_RUNTIME_OWNER)

    def test_wrong_uid_or_argv_never_receives_signal(self):
        source = RuntimeClient({"owned": self.thread})
        target = RuntimeClient({"owned": self.thread})

        def runner(command, **kwargs):
            result = self._runner(command, **kwargs)
            if command[:3] == ["ps", "-p", "44"]:
                result.stdout = (f"{os.getuid() + 1} /opt/codex app-server --listen "
                                 f"unix://{self.legacy_socket}\n")
            return result

        killer = mock.Mock()
        migration = CodexRuntimeMigration(
            "/opt/codex", self.state_path, self.legacy_socket, self.managed_socket,
            lambda: target, runner=runner, killer=killer, probe=lambda _path: True)
        migration._attempt(self._adapter(source))
        self.assertEqual(migration.phase, "blocked")
        killer.assert_not_called()

    def test_official_npm_shim_accepts_only_its_exact_platform_binary(self):
        with tempfile.TemporaryDirectory() as root:
            package_root = os.path.join(root, "node_modules", "@openai", "codex")
            wrapper = os.path.join(package_root, "bin", "codex.js")
            vendor = os.path.join(
                package_root, "node_modules", "@openai", "codex-darwin-arm64",
                "vendor", "aarch64-apple-darwin", "bin", "codex")
            os.makedirs(os.path.dirname(wrapper))
            os.makedirs(os.path.dirname(vendor))
            for path in (wrapper, vendor):
                with open(path, "w") as handle:
                    handle.write("#!/bin/sh\n")
                os.chmod(path, 0o755)

            def runner(command, **kwargs):
                if command[0] == "lsof":
                    return SimpleNamespace(
                        returncode=0,
                        stdout="p44\nccodex\nn" + self.legacy_socket + "\n",
                        stderr="")
                if command[:3] == ["ps", "-p", "44"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(f"{os.getuid()} {vendor} app-server --listen "
                                f"unix://{self.legacy_socket}\n"), stderr="")
                raise AssertionError(command)

            with mock.patch("fleetdash.codex_adapter.platform.system", return_value="Darwin"), \
                    mock.patch("fleetdash.codex_adapter.platform.machine", return_value="arm64"):
                migration = CodexRuntimeMigration(
                    wrapper, self.state_path, self.legacy_socket,
                    self.managed_socket, mock.Mock(), runner=runner,
                    probe=lambda _path: True)
            self.assertEqual(migration._listener_pid(), 44)

            unrelated = os.path.join(root, "unrelated", "codex")
            os.makedirs(os.path.dirname(unrelated))
            with open(unrelated, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(unrelated, 0o755)

            def unrelated_runner(command, **kwargs):
                result = runner(command, **kwargs)
                if command[:3] == ["ps", "-p", "44"]:
                    result.stdout = (f"{os.getuid()} {unrelated} app-server --listen "
                                     f"unix://{self.legacy_socket}\n")
                return result

            migration.runner = unrelated_runner
            with self.assertRaisesRegex(CodexError, "not the exact legacy listener"):
                migration._listener_pid()


class ManagedDaemonAndLauncherTest(unittest.TestCase):
    def test_socket_selection_keeps_staging_private(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"CODEX_HOME": os.path.join(home, "codex")},
                                 clear=False):
                self.assertEqual(codex_control_socket(managed=True),
                                 os.path.join(home, "codex", "app-server-control",
                                              "app-server-control.sock"))
                self.assertEqual(codex_control_socket(managed=False, state_dir=home),
                                 os.path.join(home, "codex-app-server.sock"))

    def test_managed_daemon_capability_start_remote_enable_and_reuse(self):
        calls = []
        environments = []
        ready = {"value": False}

        def runner(command, **kwargs):
            calls.append(command)
            environments.append(kwargs.get("env"))
            if command[-1:] == ["start"]:
                ready["value"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ensure_managed_codex_runtime(
            "/opt/codex", "/managed.sock", runner=runner,
            probe=lambda _path: ready["value"], sleeper=lambda _seconds: None)
        self.assertEqual(calls[0][-1], "start")
        self.assertEqual(calls[1][-1], "enable-remote-control")
        self.assertEqual(environments[0], environments[1])
        self.assertEqual(environments[1]["PATH"].split(os.pathsep)[0], "/opt")
        calls.clear()
        environments.clear()
        ensure_managed_codex_runtime(
            "/opt/codex", "/managed.sock", runner=runner,
            probe=lambda _path: True, sleeper=lambda _seconds: None)
        self.assertEqual(calls, [["/opt/codex", "app-server", "daemon",
                                  "enable-remote-control"]])
        self.assertEqual(environments[0]["PATH"].split(os.pathsep)[0], "/opt")

    def test_launcher_argument_matrix(self):
        endpoint = "unix:///managed.sock"
        routed = (
            ([], ["--remote", endpoint]),
            (["fix parser"], ["--remote", endpoint, "fix parser"]),
            (["resume", "abc"], ["--remote", endpoint, "resume", "abc"]),
            (["fork", "--last"], ["--remote", endpoint, "fork", "--last"]),
            (["archive", "abc"], ["--remote", endpoint, "archive", "abc"]),
            (["-m", "gpt", "hello"], ["--remote", endpoint, "-m", "gpt", "hello"]),
        )
        for args, expected in routed:
            with self.subTest(args=args):
                self.assertEqual(route_arguments(args, "/managed.sock"), (expected, True))
        passthrough = (
            ["exec", "echo hi"], ["review"], ["doctor"], ["app-server", "daemon", "start"],
            ["remote-control", "pair"], ["--version"],
            ["resume", "--remote", "unix:///other.sock", "abc"],
        )
        for args in passthrough:
            with self.subTest(args=args):
                self.assertEqual(route_arguments(args, "/managed.sock"), (args, False))

    def test_launcher_install_is_idempotent_backs_up_zshrc_and_tracks_nvm_path(self):
        with tempfile.TemporaryDirectory() as home:
            source = os.path.abspath(__import__("fleetdash.codex_launcher", fromlist=["__file__"]).__file__)
            zshrc = os.path.join(home, ".zshrc")
            with open(zshrc, "w") as handle:
                handle.write("export EXISTING=1\n")
            os.chmod(zshrc, 0o644)
            first = install_launcher(source=source, home=home)
            second = install_launcher(source=source, home=home)
            self.assertEqual(first["state"], "ready")
            self.assertEqual(second["state"], "ready")
            with open(zshrc) as handle:
                shell = handle.read()
            self.assertEqual(shell.count(BEGIN), 1)
            self.assertEqual(shell.count(END), 1)
            self.assertEqual(os.stat(zshrc).st_mode & 0o777, 0o644)
            with open(zshrc + ".fleet-dash-pre-codex-launcher") as handle:
                self.assertEqual(handle.read(), "export EXISTING=1\n")
            paths = launcher_paths(home)
            nvm = os.path.join(home, ".nvm", "versions", "node", "v99", "bin")
            os.makedirs(nvm)
            real = os.path.join(nvm, "codex")
            with open(real, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(real, 0o755)
            self.assertEqual(find_real_codex(
                path=paths["bin_dir"] + os.pathsep + nvm,
                own_path=paths["launcher"]), os.path.realpath(real))

    def test_launcher_refuses_partial_shell_marker_without_rewriting(self):
        with tempfile.TemporaryDirectory() as home:
            zshrc = os.path.join(home, ".zshrc")
            original = BEGIN + "\nexport PATH=/broken\n"
            with open(zshrc, "w") as handle:
                handle.write(original)
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                install_launcher(source=os.path.abspath(
                    __import__("fleetdash.codex_launcher", fromlist=["__file__"]).__file__), home=home)
            with open(zshrc) as handle:
                self.assertEqual(handle.read(), original)


if __name__ == "__main__":
    unittest.main()
