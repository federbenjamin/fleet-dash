import json
import os
import base64
import hashlib
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from codex_adapter import (CodexAppServer, CodexError, UnixWebSocketProcess,
                           ensure_shared_codex_runtime)


class QueueOutput:
    def __init__(self):
        self.items = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        item = self.items.get()
        if item is None:
            raise StopIteration
        return item


class CapturingInput:
    def __init__(self, process):
        self.process = process

    def write(self, raw):
        for line in raw.splitlines():
            if line:
                self.process.receive(json.loads(line))
        return len(raw)

    def flush(self):
        return None


class ScriptedProcess:
    def __init__(self, handler):
        self.handler = handler
        self.stdout = QueueOutput()
        self.stdin = CapturingInput(self)
        self.returncode = None
        self.received = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.crash(-15)

    def crash(self, code=1):
        if self.returncode is None:
            self.returncode = code
            self.stdout.items.put(None)

    def emit(self, message):
        raw = message if isinstance(message, str) else json.dumps(message)
        self.stdout.items.put(raw + "\n")

    def receive(self, message):
        self.received.append(message)
        if message.get("method") == "initialize" and message.get("id") is not None:
            self.emit({"id": message["id"], "result": {}})
        elif self.handler:
            self.handler(self, message)


class ProcessFactory:
    def __init__(self, handler=None):
        self.handler = handler
        self.processes = []

    def __call__(self, *args, **kwargs):
        process = ScriptedProcess(self.handler)
        self.processes.append(process)
        return process


class CodexProtocolTest(unittest.TestCase):
    def client(self, handler=None, timeout=0.2):
        factory = ProcessFactory(handler)
        client = CodexAppServer(command=["/bin/false", "app-server"], timeout=timeout,
                                process_factory=factory)
        return client, factory

    def test_concurrent_out_of_order_responses_match_request_ids(self):
        waiting = []

        def handler(process, message):
            if message.get("id") is None:
                return
            waiting.append(message)
            if len(waiting) == 2:
                for request in reversed(waiting):
                    process.emit({"id": request["id"],
                                  "result": {"method": request["method"]}})

        client, _ = self.client(handler)
        got = {}
        threads = [threading.Thread(target=lambda name=name: got.setdefault(
            name, client.request(name))) for name in ("alpha", "beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
        self.assertEqual(got["alpha"], {"method": "alpha"})
        self.assertEqual(got["beta"], {"method": "beta"})
        client.close()

    def test_concurrent_first_requests_wait_for_one_complete_initialization(self):
        init_seen = threading.Event()

        class GatedInitializeProcess(ScriptedProcess):
            def __init__(self):
                super().__init__(None)
                self.initialize_id = None

            def receive(self, message):
                self.received.append(message)
                if message.get("method") == "initialize":
                    self.initialize_id = message["id"]
                    init_seen.set()
                elif message.get("id") is not None:
                    self.emit({"id": message["id"],
                               "result": {"method": message["method"]}})

            def finish_initialize(self):
                self.emit({"id": self.initialize_id, "result": {}})

        process = GatedInitializeProcess()
        client = CodexAppServer(command=["/bin/false", "app-server"], timeout=.5,
                                process_factory=lambda *args, **kwargs: process)
        got, errors = {}, []

        def request(name):
            try:
                got[name] = client.request(name)
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=request, args=("alpha",))
        second = threading.Thread(target=request, args=("beta",))
        first.start()
        self.assertTrue(init_seen.wait(.2))
        second.start()
        time.sleep(.03)
        self.assertEqual([item["method"] for item in process.received], ["initialize"])
        process.finish_initialize()
        first.join(1)
        second.join(1)
        self.assertEqual(errors, [])
        self.assertEqual(got, {"alpha": {"method": "alpha"},
                               "beta": {"method": "beta"}})
        self.assertEqual(len([item for item in process.received
                              if item.get("method") == "initialize"]), 1)
        client.close()

    def test_failed_initialize_closes_transport_and_next_request_retries_cleanly(self):
        processes = []

        class FailingInitializeProcess(ScriptedProcess):
            def receive(self, message):
                self.received.append(message)
                if message.get("method") == "initialize":
                    self.emit({"id": message["id"],
                               "error": {"message": "initialize denied"}})

        def factory(*args, **kwargs):
            if not processes:
                process = FailingInitializeProcess(None)
            else:
                process = ScriptedProcess(lambda current, message: (
                    current.emit({"id": message["id"],
                                  "result": {"method": message["method"]}})
                    if message.get("id") is not None else None))
            processes.append(process)
            return process

        client = CodexAppServer(command=["/bin/false", "app-server"], timeout=.2,
                                process_factory=factory)
        with self.assertRaisesRegex(CodexError, "initialize denied"):
            client.request("first")
        self.assertEqual(processes[0].returncode, -15)
        self.assertIsNone(client.proc)
        self.assertEqual(client.connection_state, "stopped")
        self.assertEqual(client.request("second"), {"method": "second"})
        self.assertEqual(len(processes), 2)
        client.close()

    def test_thread_list_pages_past_one_hundred_rows(self):
        rows = [{"id": f"thread-{index}"} for index in range(150)]
        calls = []

        def handler(process, message):
            if message.get("method") != "thread/list":
                return
            calls.append(message["params"])
            start = int(message["params"].get("cursor") or 0)
            end = min(len(rows), start + int(message["params"]["limit"]))
            process.emit({"id": message["id"], "result": {
                "data": rows[start:end],
                "nextCursor": str(end) if end < len(rows) else None}})

        client, _ = self.client(handler)
        listed = client.list_threads()
        self.assertEqual(len(listed), 150)
        self.assertEqual(listed[-1]["id"], "thread-149")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("cursor", calls[0])
        self.assertEqual(calls[1]["cursor"], "100")
        client.close()

    def test_startup_prerequisite_runs_before_transport(self):
        events = []

        def startup(command, **kwargs):
            events.append(("startup", command))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        factory = ProcessFactory()
        client = CodexAppServer(
            command=["/bin/false", "transport"], timeout=.2,
            process_factory=lambda *args, **kwargs: (
                events.append(("transport", args[0])) or factory(*args, **kwargs)),
            startup_command=["/bin/false", "prepare"],
            startup_factory=startup)
        client.start()
        self.assertEqual([kind for kind, _ in events], ["startup", "transport"])
        client.close()

    def test_startup_failure_does_not_launch_transport(self):
        factory = ProcessFactory()
        client = CodexAppServer(
            command=["/bin/false", "transport"], timeout=.2,
            process_factory=factory,
            startup_command=["/bin/false", "prepare"],
            startup_factory=lambda *args, **kwargs: SimpleNamespace(
                returncode=1, stdout="", stderr="permission denied"))
        with self.assertRaisesRegex(CodexError, "permission denied"):
            client.start()
        self.assertEqual(factory.processes, [])

    def test_structured_usage_limit_blocks_only_its_thread(self):
        client, _ = self.client()
        client.thread_state["healthy"] = {"status": "idle", "error": None}
        client._notification("turn/completed", {"threadId": "limited", "turn": {
            "id": "turn-limited", "status": "failed",
            "error": {"code": "usage_limit", "message": "Usage limit reached"}}})
        self.assertEqual(client.thread_state["limited"]["status"], "blocked")
        self.assertEqual(client.thread_state["limited"]["error"],
                         "Usage limit reached")
        self.assertEqual(client.thread_state["healthy"],
                         {"status": "idle", "error": None})

    def test_compaction_events_recover_exact_turn_authority(self):
        client, _ = self.client()
        client.start()
        generation = client.generation
        client.thread_state["legacy"] = {
            "status": "running", "turn_id": "turn-before",
            "turn_generation": generation}

        client._notification("thread/compacted", {
            "threadId": "legacy", "turnId": "turn-after"})

        legacy = client.thread_state["legacy"]
        self.assertEqual(legacy["status"], "running")
        self.assertEqual(legacy["turn_id"], "turn-after")
        self.assertEqual(legacy["turn_generation"], generation)
        self.assertTrue(client.owns_active_turn("legacy"))
        self.assertIsNone(legacy["compacting"])

        client._notification("item/started", {
            "threadId": "modern", "turnId": "turn-modern",
            "startedAtMs": 1, "item": {
                "id": "compact-1", "type": "contextCompaction"}})
        modern = client.thread_state["modern"]
        self.assertEqual(modern["turn_id"], "turn-modern")
        self.assertEqual(modern["turn_generation"], generation)
        self.assertEqual(modern["compacting"], 0)
        self.assertTrue(client.owns_active_turn("modern"))

        client._notification("item/completed", {
            "threadId": "modern", "turnId": "turn-modern",
            "completedAtMs": 2, "item": {
                "id": "compact-1", "type": "contextCompaction"}})
        self.assertIsNone(modern["compacting"])
        self.assertEqual(modern["status"], "running")
        self.assertEqual(modern["turn_id"], "turn-modern")

        client._notification("item/completed", {
            "threadId": "completed-only", "turnId": "turn-completed-only",
            "completedAtMs": 3, "item": {
                "id": "compact-2", "type": "contextCompaction"}})
        completed_only = client.thread_state["completed-only"]
        self.assertEqual(completed_only["status"], "running")
        self.assertEqual(completed_only["turn_id"], "turn-completed-only")
        self.assertEqual(completed_only["turn_generation"], generation)
        self.assertTrue(client.owns_active_turn("completed-only"))
        client.close()

    def test_npm_shared_runtime_detaches_one_unix_listener_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = os.path.join(tmp, "control.sock")
            started = []

            def process_factory(command, **kwargs):
                started.append((command, kwargs))
                return SimpleNamespace(poll=lambda: None)

            probe = lambda path: bool(started)
            ensure_shared_codex_runtime(
                "/opt/codex", socket_path, process_factory=process_factory,
                probe=probe, sleeper=lambda seconds: None)
            command, kwargs = started[0]
            self.assertEqual(command, ["/opt/codex", "app-server", "--listen",
                                      "unix://" + socket_path])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stdin"], __import__("subprocess").DEVNULL)
            self.assertEqual(kwargs["stdout"], __import__("subprocess").DEVNULL)

            ensure_shared_codex_runtime(
                "/opt/codex", socket_path,
                process_factory=lambda *args, **kwargs: self.fail("duplicate listener"),
                probe=probe)
            self.assertEqual(len(started), 1)

    def test_unix_websocket_transport_handshake_masks_and_round_trips_jsonrpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app-server.sock")
            client_socket, server_socket = socket.socketpair()
            received = []

            def exact(conn, size):
                data = b""
                while len(data) < size:
                    data += conn.recv(size - len(data))
                return data

            def read_frame(conn):
                head = exact(conn, 2)
                length = head[1] & 0x7f
                if length == 126:
                    length = struct.unpack("!H", exact(conn, 2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", exact(conn, 8))[0]
                self.assertTrue(head[1] & 0x80, "client WebSocket frames must be masked")
                mask = exact(conn, 4)
                payload = exact(conn, length)
                return bytes(value ^ mask[index % 4]
                             for index, value in enumerate(payload)).decode()

            def send_frame(conn, message):
                payload = json.dumps(message).encode()
                if len(payload) < 126:
                    header = bytes([0x81, len(payload)])
                else:
                    header = bytes([0x81, 126]) + struct.pack("!H", len(payload))
                conn.sendall(header + payload)

            def serve():
                conn = server_socket
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                key_line = next(line for line in request.decode().split("\r\n")
                                if line.lower().startswith("sec-websocket-key:"))
                key = key_line.split(":", 1)[1].strip()
                accept = base64.b64encode(hashlib.sha1(
                    (key + UnixWebSocketProcess.GUID).encode()).digest()).decode()
                conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\n"
                              f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                while True:
                    try:
                        message = json.loads(read_frame(conn))
                    except (OSError, EOFError, json.JSONDecodeError):
                        break
                    received.append(message)
                    if message.get("id") is not None:
                        send_frame(conn, {"id": message["id"], "result": {
                            "method": message["method"]}})
                    if message.get("method") == "echo":
                        break
                conn.close()

            server = threading.Thread(target=serve, daemon=True)
            server.start()
            client = CodexAppServer(
                command=["unix", path], timeout=.5,
                process_factory=lambda *args, **kwargs: UnixWebSocketProcess(
                    path, .5, connected_socket=client_socket))
            self.assertEqual(client.request("echo"), {"method": "echo"})
            client.close()
            server.join(1)
            self.assertEqual([item.get("method") for item in received[:3]],
                             ["initialize", "initialized", "echo"])

    def test_timeout_clears_waiter_and_late_response_is_diagnostic(self):
        client, factory = self.client(timeout=0.03)
        with self.assertRaisesRegex(CodexError, "timed out"):
            client.request("never")
        request = next(item for item in factory.processes[0].received
                       if item.get("method") == "never")
        factory.processes[0].emit({"id": request["id"], "result": {"late": True}})
        time.sleep(0.02)
        self.assertIn("late_or_duplicate_response",
                      [item["kind"] for item in client.diagnostics])
        client.close()

    def test_thread_timeout_recycles_only_the_client_transport(self):
        def handler(process, message):
            if message.get("method") == "recover":
                process.emit({"id": message["id"], "result": {"ok": True}})

        client, factory = self.client(handler, timeout=0.03)
        with self.assertRaisesRegex(CodexError, "timed out: thread/list"):
            client.request("thread/list")
        self.assertIsNone(client.proc)
        self.assertEqual(client.request("recover"), {"ok": True})
        self.assertIsNone(client.last_error)
        self.assertEqual(len(factory.processes), 2)
        client.close()

    def test_malformed_json_is_visible_and_reader_continues(self):
        client, factory = self.client()
        client.start()
        process = factory.processes[0]
        process.emit("{not-json")
        process.emit({"method": "thread/status/changed", "params": {
            "threadId": "thr", "status": {"type": "idle"}}})
        time.sleep(0.02)
        self.assertEqual(client.thread_state["thr"]["thread_status"], {"type": "idle"})
        self.assertIn("malformed_json", [item["kind"] for item in client.diagnostics])
        client.close()

    def test_process_exit_fails_request_and_next_request_restarts(self):
        calls = {"n": 0}

        def handler(process, message):
            if message.get("method") == "crash":
                process.crash()
            elif message.get("method") == "recover":
                calls["n"] += 1
                process.emit({"id": message["id"], "result": {"ok": True}})

        client, factory = self.client(handler)
        with self.assertRaisesRegex(CodexError, "stopped"):
            client.request("crash")
        self.assertEqual(client.request("recover"), {"ok": True})
        self.assertEqual(len(factory.processes), 2)
        client.close()

    def test_process_exit_invalidates_only_old_generation_server_requests(self):
        client, factory = self.client()
        client.start()
        process = factory.processes[0]
        process.emit({"id": 41, "method": "item/commandExecution/requestApproval",
                      "params": {"threadId": "thr", "command": "touch /etc/x"}})
        time.sleep(0.02)
        self.assertIn("41", client.approvals)
        process.crash()
        time.sleep(0.02)
        self.assertNotIn("41", client.approvals)
        self.assertEqual(client.thread_state["thr"]["pending"], [])
        self.assertIn("no longer answerable", client.thread_state["thr"]["error"])
        self.assertIn("stale_pending_request",
                      [item["kind"] for item in client.diagnostics])

    def test_process_exit_revokes_only_that_connections_turn_authority(self):
        client, factory = self.client()
        client.start()
        process = factory.processes[0]
        generation = client.generation
        client.thread_state["owned"] = {
            "status": "running", "turn_id": "turn-owned",
            "turn_generation": generation}
        client.thread_state["newer"] = {
            "status": "running", "turn_id": "turn-newer",
            "turn_generation": generation + 1}

        process.crash()
        time.sleep(0.02)

        self.assertIsNone(client.thread_state["owned"]["turn_id"])
        self.assertIsNone(client.thread_state["owned"]["turn_generation"])
        self.assertEqual(client.thread_state["newer"]["turn_id"], "turn-newer")

    def test_server_requests_queue_and_resolve_independently(self):
        client, _ = self.client()
        client.respond = lambda rid, result: None
        for rid, method in ((1, "item/commandExecution/requestApproval"),
                            (2, "item/fileChange/requestApproval")):
            client._server_request({"id": rid, "method": method,
                                    "params": {"threadId": "thr"}})
        self.assertEqual(client.thread_state["thr"]["pending"], ["1", "2"])
        client.decide("1", "allow")
        self.assertEqual(client.thread_state["thr"]["pending"], ["2"])
        with self.assertRaisesRegex(CodexError, "stale"):
            client.decide("1", "allow")

    def test_invalid_question_does_not_lose_pending_request(self):
        client, _ = self.client()
        client.respond = lambda rid, result: None
        client._server_request({"id": 9, "method": "item/tool/requestUserInput",
                                "params": {"threadId": "thr", "questions": [{
                                    "id": "scope", "options": [{"label": "Full"}]}]}})
        with self.assertRaisesRegex(CodexError, "every question"):
            client.answer_questions("9", [{"digits": [8]}])
        self.assertIn("9", client.approvals)
        client.answer_questions("9", [{"digits": [1]}])
        self.assertNotIn("9", client.approvals)

    def test_all_new_and_legacy_approval_decisions(self):
        expected = {
            "allow": ("accept", "approved"),
            "always": ("acceptForSession", "approved_for_session"),
            "deny": ("decline", "denied"),
            "cancel": ("cancel", "abort"),
        }
        for choice, (modern, legacy) in expected.items():
            for method, decision in (("item/commandExecution/requestApproval", modern),
                                     ("item/fileChange/requestApproval", modern),
                                     ("execCommandApproval", legacy),
                                     ("applyPatchApproval", legacy)):
                with self.subTest(choice=choice, method=method):
                    client, _ = self.client()
                    responses = []
                    client.respond = lambda rid, result: responses.append(result)
                    client.approvals["1"] = {"request_id": 1, "method": method,
                        "params": {}, "state": "pending"}
                    client.decide("1", choice)
                    self.assertEqual(responses, [{"decision": decision}])

    def test_unsupported_server_request_gets_jsonrpc_error(self):
        client, _ = self.client()
        errors = []
        client.respond_error = lambda rid, message, code=-32601: errors.append(
            (rid, message, code))
        client._server_request({"id": 4, "method": "future/request", "params": {}})
        self.assertEqual(errors[0][0], 4)
        self.assertIn("future/request", errors[0][1])

    def test_mode_uses_verified_experimental_thread_setting_not_turn_field(self):
        client, _ = self.client()
        calls = []
        client.request = lambda method, params=None, timeout=None: calls.append(
            (method, params)) or {}
        client.set_mode("018f0000-0000-7000-8000-000000000000", "plan",
                        "gpt-5.4", "high")
        client.start_turn("018f0000-0000-7000-8000-000000000000", "hello",
                          mode="plan", model="gpt-5.4", effort="high")
        self.assertEqual(calls[0][0], "thread/settings/update")
        self.assertEqual(calls[0][1]["collaborationMode"]["mode"], "plan")
        self.assertNotIn("collaborationMode", calls[1][1])
        self.assertEqual(calls[1][1]["model"], "gpt-5.4")

    def test_steer_uses_active_turn_id_as_protocol_precondition(self):
        client, _ = self.client()
        calls = []
        client.request = lambda method, params=None, timeout=None: calls.append(
            (method, params)) or {"turnId": "turn-active"}
        client.proc = SimpleNamespace(poll=lambda: None)
        client.thread_state["thread-one"] = {
            "status": "running", "turn_id": "turn-active",
            "turn_generation": client.generation}

        client.steer_turn("thread-one", "Focus on the failing test")

        self.assertEqual(calls, [("turn/steer", {
            "threadId": "thread-one", "expectedTurnId": "turn-active",
            "input": [{"type": "text", "text": "Focus on the failing test"}],
        })])

    def test_no_active_turn_rejection_revokes_stale_authority_for_safe_restart(self):
        client, _ = self.client()
        client.proc = SimpleNamespace(poll=lambda: None)
        client.thread_state["thread-one"] = {
            "status": "running", "turn_id": "turn-stale",
            "turn_generation": client.generation}
        client.request = lambda method, params=None, timeout=None: (_ for _ in ()).throw(
            CodexError("no active turn to steer"))

        with self.assertRaises(CodexError) as caught:
            client.steer_turn("thread-one", "Start after the stale turn")

        self.assertEqual(caught.exception.code, "turn_ended")
        self.assertFalse(caught.exception.queueable)
        state = client.thread_state["thread-one"]
        self.assertEqual(state["status"], "idle")
        self.assertIsNone(state["turn_id"])
        self.assertIsNone(state["turn_generation"])
        self.assertIsNotNone(state["no_active_turn_at"])

    def test_changed_active_turn_rejection_is_queueable_not_restartable(self):
        client, _ = self.client()
        client.proc = SimpleNamespace(poll=lambda: None)
        client.thread_state["thread-one"] = {
            "status": "running", "turn_id": "turn-stale",
            "turn_generation": client.generation}
        client.request = lambda method, params=None, timeout=None: (_ for _ in ()).throw(
            CodexError("expected active turn id `turn-stale` but found `turn-new`"))

        with self.assertRaises(CodexError) as caught:
            client.steer_turn("thread-one", "Queue behind the new turn")

        self.assertEqual(caught.exception.code, "provider_control_unavailable")
        self.assertTrue(caught.exception.queueable)
        state = client.thread_state["thread-one"]
        self.assertEqual(state["status"], "running")
        self.assertIsNone(state["turn_id"])
        self.assertIsNone(state["turn_generation"])
        self.assertIsNotNone(state["control_lost_at"])

    def test_image_inputs_use_local_image_for_new_and_active_turns(self):
        client, _ = self.client()
        calls = []
        client.request = lambda method, params=None, timeout=None: calls.append(
            (method, params)) or {"turnId": "turn-active"}
        inputs = [{"type": "text", "text": "Inspect it"},
                  {"type": "localImage", "path": "/private/fleet/photo.jpg"}]
        client.start_turn("thread-one", "Inspect it", inputs=inputs)
        client.proc = SimpleNamespace(poll=lambda: None)
        client.thread_state["thread-one"] = {
            "status": "running", "turn_id": "turn-active",
            "turn_generation": client.generation}
        client.steer_turn("thread-one", "Inspect it", inputs=inputs)

        self.assertEqual(calls[0], ("turn/start", {
            "threadId": "thread-one", "input": inputs}))
        self.assertEqual(calls[1], ("turn/steer", {
            "threadId": "thread-one", "expectedTurnId": "turn-active",
            "input": inputs}))


if __name__ == "__main__":
    unittest.main()
