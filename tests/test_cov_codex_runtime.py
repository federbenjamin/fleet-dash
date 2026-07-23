"""Coverage tests for fleetdash.codex_runtime: executable resolution, socket
probing, shared/managed startup error paths, and migration helper branches."""
import os
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from fleetdash import codex_runtime as rt
from fleetdash.codex_runtime import (
    CodexError, CodexRuntimeMigration, codex_command, codex_control_socket,
    ensure_managed_codex_runtime, ensure_shared_codex_runtime,
    migrate_codex_runtime_metadata, _socket_accepting)


class CodexCommandTest(unittest.TestCase):
    def test_configured_runnable_and_unrunnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "codex")
            with open(good, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(good, 0o755)
            self.assertEqual(codex_command(good), os.path.realpath(good))
            bad = os.path.join(tmp, "not-exec")
            with open(bad, "w") as handle:
                handle.write("x")
            os.chmod(bad, 0o644)
            with self.assertRaisesRegex(CodexError, "not runnable"):
                codex_command(bad)

    def test_which_found(self):
        with mock.patch.object(rt.shutil, "which", return_value="/usr/bin/codex"):
            self.assertEqual(codex_command(), "/usr/bin/codex")

    def test_nvm_glob_fallback_and_not_found(self):
        with mock.patch.object(rt.shutil, "which", return_value=None):
            with mock.patch.object(rt.glob, "glob",
                                   return_value=["/n/v1/bin/codex", "/n/v2/bin/codex"]):
                self.assertEqual(codex_command(), "/n/v2/bin/codex")
            with mock.patch.object(rt.glob, "glob", return_value=[]):
                with self.assertRaisesRegex(CodexError, "not found"):
                    codex_command()


class SocketProbeTest(unittest.TestCase):
    def test_socket_accepting_true_and_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            try:
                self.assertTrue(_socket_accepting(path))
            finally:
                server.close()
            self.assertFalse(_socket_accepting(os.path.join(tmp, "missing.sock")))


class SharedRuntimeTest(unittest.TestCase):
    def test_unlinks_stale_socket_then_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.sock")
            open(path, "w").close()  # a stale socket file to be removed
            ready = {"value": False}
            started = []

            def factory(command, **kwargs):
                started.append(command)
                ready["value"] = True
                return SimpleNamespace(poll=lambda: None)

            ensure_shared_codex_runtime(
                "/opt/codex", path, process_factory=factory,
                probe=lambda p: ready["value"], sleeper=lambda s: None)
            self.assertFalse(os.path.exists(path) and not started)
            self.assertTrue(started)

    def test_process_exit_during_startup_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.sock")
            with self.assertRaisesRegex(CodexError, "exited during startup"):
                ensure_shared_codex_runtime(
                    "/opt/codex", path,
                    process_factory=lambda *a, **k: SimpleNamespace(poll=lambda: 3),
                    probe=lambda p: False, sleeper=lambda s: None,
                    clock=iter([0, 1]).__next__)

    def test_timeout_when_socket_never_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.sock")
            times = iter([0, 0, 100])
            with self.assertRaisesRegex(CodexError, "did not become ready"):
                ensure_shared_codex_runtime(
                    "/opt/codex", path,
                    process_factory=lambda *a, **k: SimpleNamespace(poll=lambda: None),
                    probe=lambda p: False, sleeper=lambda s: None,
                    clock=lambda: next(times))


class ManagedRuntimeTest(unittest.TestCase):
    def test_start_runner_exception(self):
        def runner(*a, **k):
            raise OSError("cannot exec")
        with self.assertRaisesRegex(CodexError, "failed to start"):
            ensure_managed_codex_runtime("/opt/codex", "/m.sock", runner=runner,
                                         probe=lambda p: False, sleeper=lambda s: None)

    def test_start_nonzero_returncode(self):
        def runner(*a, **k):
            return SimpleNamespace(returncode=2, stdout="", stderr="bad daemon")
        with self.assertRaisesRegex(CodexError, "bad daemon"):
            ensure_managed_codex_runtime("/opt/codex", "/m.sock", runner=runner,
                                         probe=lambda p: False, sleeper=lambda s: None)

    def test_socket_never_ready_after_start(self):
        def runner(*a, **k):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        times = iter([0, 0, 100])
        with self.assertRaisesRegex(CodexError, "did not become ready"):
            ensure_managed_codex_runtime(
                "/opt/codex", "/m.sock", runner=runner,
                probe=lambda p: False, sleeper=lambda s: None,
                clock=lambda: next(times))

    def test_enable_remote_control_failure(self):
        state = {"ready": False}

        def runner(command, **kwargs):
            if command[-1] == "start":
                state["ready"] = True
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="no remote control")
        with self.assertRaisesRegex(CodexError, "Remote Control could not be enabled"):
            ensure_managed_codex_runtime(
                "/opt/codex", "/m.sock", runner=runner,
                probe=lambda p: state["ready"], sleeper=lambda s: None)

    def test_skip_remote_control_when_disabled(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        ensure_managed_codex_runtime(
            "/opt/codex", "/m.sock", runner=runner, probe=lambda p: True,
            sleeper=lambda s: None, enable_remote_control=False)
        self.assertEqual(calls, [])


class AtomicStateAndMigrationTest(unittest.TestCase):
    def test_atomic_update_no_path_returns_empty(self):
        self.assertEqual(migrate_codex_runtime_metadata(None), {})

    def test_migration_draining_phase_leaves_metadata(self):
        # A non-committed phase writes migration metadata but converts nothing.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state = migrate_codex_runtime_metadata(path, phase="draining",
                                                   blockers=["turn active"],
                                                   clock=lambda: 5)
            self.assertEqual(state["runtime_migration"]["phase"], "draining")
            self.assertEqual(state["runtime_migration"]["blockers"], ["turn active"])
            self.assertEqual(state["runtime_migration"]["converted_threads"], 0)

    def test_control_socket_private_and_managed_env_override(self):
        with mock.patch.dict(os.environ,
                             {"FLEET_DASH_CODEX_SOCKET": "/custom/private.sock",
                              "FLEET_DASH_CODEX_MANAGED_SOCKET": "/custom/managed.sock"},
                             clear=False):
            self.assertEqual(codex_control_socket(managed=False), "/custom/private.sock")
            self.assertEqual(codex_control_socket(managed=True), "/custom/managed.sock")


class MigrationHelperTest(unittest.TestCase):
    def _migration(self, tmp, runner=None, probe=None):
        return CodexRuntimeMigration(
            "/opt/codex", os.path.join(tmp, "state.json"),
            os.path.realpath(os.path.join(tmp, "legacy.sock")),
            os.path.realpath(os.path.join(tmp, "managed.sock")),
            mock.Mock(), runner=runner or mock.Mock(),
            probe=probe or (lambda p: True))

    def test_mutation_blocked_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp)
            migration.phase = "switching"
            self.assertTrue(migration.mutation_blocked())
            migration.phase = "committed"
            self.assertFalse(migration.mutation_blocked())
            diag = migration.diagnostics()
            self.assertEqual(diag["mode"], "managed")

    def test_turn_active_variants(self):
        self.assertFalse(CodexRuntimeMigration._turn_active({}))
        self.assertFalse(CodexRuntimeMigration._turn_active("not-a-dict"))
        self.assertTrue(CodexRuntimeMigration._turn_active(
            {"turns": [{"status": "inProgress"}]}))
        self.assertTrue(CodexRuntimeMigration._turn_active(
            {"turns": [{"status": {"type": "running"}}]}))
        self.assertFalse(CodexRuntimeMigration._turn_active(
            {"turns": [{"status": "completed"}]}))

    def test_owned_returns_empty_when_state_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp)
            self.assertEqual(migration._owned(), [])

    def test_terminal_blockers_when_runner_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            def runner(*a, **k):
                raise OSError("ps unavailable")
            migration = self._migration(tmp, runner=runner)
            self.assertEqual(migration._terminal_blockers(),
                             ["legacy terminal check unavailable"])

    def test_maybe_migrate_no_op_when_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp)
            migration.phase = "committed"
            migration.maybe_migrate(SimpleNamespace())
            self.assertIsNone(migration._worker)

    def test_maybe_migrate_skips_while_worker_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp)
            migration._worker = SimpleNamespace(is_alive=lambda: True)
            migration.maybe_migrate(SimpleNamespace())
            self.assertTrue(migration._worker.is_alive())

    def test_listener_pid_lookup_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.realpath(os.path.join(tmp, "legacy.sock"))

            def lsof_fail(command, **kwargs):
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            migration = self._migration(tmp, runner=lsof_fail)
            migration.legacy_socket = legacy
            with self.assertRaisesRegex(CodexError, "could not identify"):
                migration._listener_pid()

            def ambiguous(command, **kwargs):
                return SimpleNamespace(returncode=0,
                                       stdout="p1\np2\n", stderr="")
            migration.runner = ambiguous
            with self.assertRaisesRegex(CodexError, "ambiguous"):
                migration._listener_pid()

    def test_retire_legacy_noop_when_socket_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp, probe=lambda p: False)
            self.assertIsNone(migration._retire_legacy())

    def test_shared_runtime_reuses_accepting_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.sock")
            factory = mock.Mock()
            ensure_shared_codex_runtime("/opt/codex", path, process_factory=factory,
                                        probe=lambda p: True)
            factory.assert_not_called()

    def test_migration_needed_tolerates_corrupt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as handle:
                handle.write("{broken")
            self.assertTrue(rt.codex_runtime_migration_needed(
                path, "/legacy.sock", probe=lambda p: True))

    def test_turn_active_nested_status_field(self):
        self.assertTrue(CodexRuntimeMigration._turn_active(
            {"turns": [{"status": {"status": "started"}}]}))

    def test_runtime_blockers_source_read_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            def runner(command, **kwargs):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            migration = self._migration(tmp, runner=runner)

            class Client:
                approvals = {}
                thread_state = {}

                def owns_active_turn(self, tid):
                    return False

                def read_thread(self, tid):
                    raise RuntimeError("read failed")

            blockers = migration._runtime_blockers(
                Client(), [("owned", {"unmaterialized": False})])
            self.assertIn("source read unavailable", blockers)

    def test_listener_pid_ps_missing_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.realpath(os.path.join(tmp, "legacy.sock"))

            def missing(command, **kwargs):
                if command[0] == "lsof":
                    return SimpleNamespace(returncode=0,
                                           stdout="p44\nccodex\nn" + legacy + "\n",
                                           stderr="")
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            migration = self._migration(tmp, runner=missing)
            migration.legacy_socket = legacy
            with self.assertRaisesRegex(CodexError, "disappeared"):
                migration._listener_pid()

            def malformed(command, **kwargs):
                if command[0] == "lsof":
                    return SimpleNamespace(returncode=0,
                                           stdout="p44\nccodex\nn" + legacy + "\n",
                                           stderr="")
                return SimpleNamespace(returncode=0,
                                       stdout="notanumber /opt/codex app-server\n",
                                       stderr="")
            migration.runner = malformed
            with self.assertRaisesRegex(CodexError, "malformed"):
                migration._listener_pid()

    def test_attempt_target_history_unavailable_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            import json
            with open(path, "w") as handle:
                json.dump({"threads": ["owned"], "thread_meta": {
                    "owned": {"runtime_owner": rt.LEGACY_RUNTIME_OWNER,
                              "unmaterialized": False}}}, handle)

            class Source:
                approvals = {}
                thread_state = {}

                def owns_active_turn(self, tid):
                    return False

                def read_thread(self, tid):
                    return {"id": tid, "turns": []}

            class Target:
                def start(self):
                    return None

                def list_threads(self):
                    return []

                def read_thread(self, tid):
                    raise RuntimeError("target read failed")

                def close(self):
                    self.closed = True

            legacy = os.path.realpath(os.path.join(tmp, "legacy.sock"))
            target = Target()

            def runner(command, **kwargs):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            migration = CodexRuntimeMigration(
                "/opt/codex", path, legacy,
                os.path.realpath(os.path.join(tmp, "managed.sock")),
                lambda: target, runner=runner, probe=lambda p: True)
            adapter = SimpleNamespace(
                _runtime_action_lock=threading.RLock(), client=Source(),
                runtime_owner=rt.LEGACY_RUNTIME_OWNER, _loaded_threads=set(),
                _loaded_generation=None, _last_refresh=0)
            migration._attempt(adapter)
            self.assertEqual(migration.phase, "blocked")
            self.assertIn("target history unavailable", migration.error)

    def test_maybe_migrate_runs_worker_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            migration = self._migration(tmp)
            started = threading.Event()

            def fake_attempt(adapter):
                started.set()
            migration._attempt = fake_attempt
            migration.maybe_migrate(SimpleNamespace())
            self.assertTrue(started.wait(2))
            migration._worker.join(2)


if __name__ == "__main__":
    unittest.main()
