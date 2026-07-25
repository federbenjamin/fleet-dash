"""Coverage tests for fleetdash.codex_protocol: WebSocket framing, JSON-RPC
client method wrappers, notification folding, and error normalization."""
import json
import os
import socket
import struct
import threading
import time
import unittest
from types import SimpleNamespace

from fleetdash.codex_protocol import (CodexAppServer, UnixWebSocketProcess,
                                      _error_text, _is_limit_error, _safe_json)
from fleetdash.codex_runtime import CodexError


class FakeStdin:
    def __init__(self, on_write=None):
        self.lines = []
        self._on_write = on_write

    def write(self, data):
        if self._on_write:
            self._on_write(data)
        self.lines.append(data)
        return len(data)

    def flush(self):
        return None


class FakeProc:
    def __init__(self, on_write=None, returncode=None):
        self.stdin = FakeStdin(on_write)
        self.returncode = returncode

    def poll(self):
        return self.returncode


def bare_client():
    """A client with no transport; direct method calls only."""
    return CodexAppServer(command=["/bin/false", "app-server"], timeout=0.2)


# ---- WebSocket transport helpers -------------------------------------------

def server_frame(opcode, payload=b"", fin=True):
    if isinstance(payload, str):
        payload = payload.encode()
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        header = bytes([b0, n])
    elif n < 65536:
        header = bytes([b0, 126]) + struct.pack("!H", n)
    else:
        header = bytes([b0, 127]) + struct.pack("!Q", n)
    return header + payload


def handshake_reply(request):
    import base64
    import hashlib
    key_line = next(line for line in request.decode("latin-1").split("\r\n")
                    if line.lower().startswith("sec-websocket-key:"))
    key = key_line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1(
        (key + UnixWebSocketProcess.GUID).encode()).digest()).decode()
    return ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()


def connected_process():
    """Return (proc, server_sock) with the handshake completed."""
    client_sock, server_sock = socket.socketpair()

    def serve():
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = server_sock.recv(4096)
            if not chunk:
                return
            request += chunk
        server_sock.sendall(handshake_reply(bytes(request)))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    proc = UnixWebSocketProcess("/unused.sock", 1, connected_socket=client_sock)
    thread.join(1)
    return proc, server_sock


class WebSocketTransportTest(unittest.TestCase):
    def _handshake_failure(self, responder):
        client_sock, server_sock = socket.socketpair()

        def serve():
            try:
                request = bytearray()
                while b"\r\n\r\n" not in request and len(request) < 4096:
                    chunk = server_sock.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                responder(server_sock, bytes(request))
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with self.assertRaises(CodexError):
                UnixWebSocketProcess("/unused.sock", 1, connected_socket=client_sock)
        finally:
            thread.join(1)
            server_sock.close()

    def test_connect_failure_closes_and_raises(self):
        with self.assertRaises(OSError):
            UnixWebSocketProcess("/nonexistent/fleet-codex.sock", 0.2)

    def test_handshake_closed_immediately(self):
        self._handshake_failure(lambda sock, req: sock.close())

    def test_handshake_non_101(self):
        self._handshake_failure(
            lambda sock, req: sock.sendall(b"HTTP/1.1 400 Bad\r\n\r\n"))

    def test_handshake_bad_accept_key(self):
        self._handshake_failure(lambda sock, req: sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Sec-WebSocket-Accept: wrong==\r\n\r\n"))

    def test_handshake_oversized(self):
        self._handshake_failure(lambda sock, req: sock.sendall(b"x" * 65000))

    def test_receive_text_length_126_and_127(self):
        proc, server = connected_process()
        try:
            server.sendall(server_frame(0x1, "a" * 200))
            self.assertEqual(proc.receive_text(), "a" * 200)
            # A 127-length frame exceeds the socketpair buffer; send concurrently.
            big = threading.Thread(
                target=lambda: server.sendall(server_frame(0x1, "b" * 70000)),
                daemon=True)
            big.start()
            self.assertEqual(proc.receive_text(), "b" * 70000)
            big.join(1)
        finally:
            proc.terminate()
            server.close()

    def test_receive_text_skips_ping_pong_and_unknown_opcode(self):
        proc, server = connected_process()
        try:
            server.sendall(server_frame(0x9, b"ping"))     # ping -> pong sent
            server.sendall(server_frame(0xA, b"pong"))     # pong -> ignored
            server.sendall(server_frame(0x3, b"weird"))    # unknown -> ignored
            server.sendall(server_frame(0x1, "final"))
            self.assertEqual(proc.receive_text(), "final")
        finally:
            proc.terminate()
            server.close()

    def test_receive_text_continuation_fragments(self):
        proc, server = connected_process()
        try:
            server.sendall(server_frame(0x1, "part-", fin=False))
            server.sendall(server_frame(0x0, "two", fin=True))
            self.assertEqual(proc.receive_text(), "part-two")
        finally:
            proc.terminate()
            server.close()

    def test_receive_text_close_frame_returns_none(self):
        proc, server = connected_process()
        try:
            server.sendall(server_frame(0x8, b""))
            self.assertIsNone(proc.receive_text())
            self.assertEqual(proc.returncode, 0)
        finally:
            server.close()

    def test_receive_text_invalid_utf8_raises(self):
        proc, server = connected_process()
        try:
            server.sendall(server_frame(0x1, b"\xff\xfe"))
            with self.assertRaises(CodexError):
                proc.receive_text()
        finally:
            proc.terminate()
            server.close()

    def test_partial_length_prefix_returns_none(self):
        proc, server = connected_process()
        try:
            server.sendall(bytes([0x81, 126]))  # promises 2 length bytes, none follow
            server.close()
            self.assertIsNone(proc.receive_text())
        finally:
            pass

    def test_send_text_frames_small_and_large(self):
        proc, server = connected_process()
        drained = []

        def drain():
            try:
                while True:
                    chunk = server.recv(65536)
                    if not chunk:
                        break
                    drained.append(chunk)
            except OSError:
                pass

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            proc.send_text("y" * 130)      # 126 length branch
            proc.send_text("z" * 70000)    # 127 length branch
            time.sleep(0.05)
        finally:
            proc.terminate()
            server.close()
            reader.join(1)
        self.assertTrue(drained)

    def test_send_frame_oserror_marks_returncode(self):
        proc, server = connected_process()
        server.close()
        proc.sock.close()
        with self.assertRaises(OSError):
            proc.send_text("boom")
        self.assertEqual(proc.returncode, 1)

    def test_terminate_is_idempotent(self):
        proc, server = connected_process()
        proc.terminate()
        self.assertEqual(proc.returncode, -15)
        proc.terminate()  # second call returns early
        server.close()

    def test_terminate_tolerates_shutdown_oserror(self):
        proc, server = connected_process()
        proc.sock.close()          # shutdown() will now raise OSError
        proc.terminate()
        self.assertEqual(proc.returncode, -15)
        server.close()

    def test_websocket_input_output_wrappers(self):
        proc, server = connected_process()
        frames = []

        def read_one():
            frames.append(_read_masked(server))

        reader = threading.Thread(target=read_one, daemon=True)
        reader.start()
        try:
            proc.stdin.write("line-one\nline-two\n")  # only non-empty lines sent
            self.assertIsNone(proc.stdin.flush())
            reader.join(1)
            self.assertEqual(frames[0], "line-one")
            # _WebSocketOutput.__next__ returns the next server text message
            server.sendall(server_frame(0x1, "hello"))
            self.assertEqual(next(proc.stdout), "hello\n")
        finally:
            proc.terminate()
            server.close()


def _read_masked(conn):
    def exact(n):
        data = b""
        while len(data) < n:
            more = conn.recv(n - len(data))
            if not more:
                break
            data += more
        return data
    head = exact(2)
    length = head[1] & 0x7f
    mask = exact(4)
    payload = exact(length)
    return bytes(v ^ mask[i % 4] for i, v in enumerate(payload)).decode()


# ---- CodexAppServer method wrappers & notifications ------------------------

class ClientMethodTest(unittest.TestCase):
    def _stubbed(self):
        client = bare_client()
        calls = []
        client.request = lambda method, params=None, timeout=None: (
            calls.append((method, params, timeout)) or self._responses.get(method, {}))
        return client, calls

    def setUp(self):
        self._responses = {}

    def test_simple_request_wrappers(self):
        self._responses = {
            "thread/loaded/list": {"data": [{"id": "a"}]},
            "model/list": {"data": [{"model": "gpt"}]},
            "skills/list": {"data": [{"skills": []}]},
            "account/usage/read": {"summary": {}},
            "account/read": {"account": {}},
            "account/rateLimits/read": {"rateLimits": {}},
            "thread/read": {"thread": {"id": "t"}},
        }
        client, calls = self._stubbed()
        self.assertEqual(client.loaded_thread_ids(), [{"id": "a"}])
        self.assertEqual(client.list_models(), [{"model": "gpt"}])
        self.assertEqual(client.list_skills("/cwd"), [{"skills": []}])
        self.assertEqual(client.account_usage(), {"summary": {}})
        self.assertEqual(client.account_info(), {"account": {}})
        self.assertEqual(client.account_limits(), {"rateLimits": {}})
        self.assertEqual(client.account_rate_limits, {"rateLimits": {}})
        self.assertEqual(client.read_thread("t"), {"id": "t"})
        methods = [c[0] for c in calls]
        self.assertIn("thread/read", methods)

    def test_start_thread_records_model_effort_state(self):
        client = bare_client()
        client.request = lambda method, params=None, timeout=None: {
            "thread": {"id": "t1"}, "model": "gpt-5.4", "reasoningEffort": "high"}
        thread = client.start_thread("/cwd", model="gpt-5.4", effort="high")
        self.assertEqual(thread["model"], "gpt-5.4")
        self.assertEqual(thread["effort"], "high")
        self.assertEqual(client.thread_state["t1"]["model"], "gpt-5.4")

    def test_start_thread_without_id(self):
        client = bare_client()
        client.request = lambda method, params=None, timeout=None: {"thread": {}}
        thread = client.start_thread("/cwd")
        self.assertEqual(thread["model"], "")

    def test_resume_thread_records_state(self):
        client = bare_client()
        client.request = lambda method, params=None, timeout=None: {
            "thread": {"id": "t"}, "model": "gpt", "reasoningEffort": "low"}
        thread = client.resume_thread("t")
        self.assertEqual(thread["model"], "gpt")
        self.assertEqual(client.thread_state["t"]["effort"], "low")

    def test_set_mode_validates_and_records(self):
        client = bare_client()
        calls = []
        client.request = lambda method, params=None, timeout=None: calls.append(
            (method, params)) or {}
        client.set_mode("t", "plan", "gpt", "high")
        self.assertEqual(client.thread_state["t"]["collaboration_mode"], "plan")
        with self.assertRaises(CodexError):
            client.set_mode("t", "weird", "gpt", "high")
        with self.assertRaises(CodexError):
            client.set_mode("t", "plan", None, "high")

    def test_interrupt_requires_owned_turn(self):
        client = bare_client()
        client.proc = SimpleNamespace(poll=lambda: None)
        with self.assertRaises(CodexError):
            client.interrupt("t")
        client.thread_state["t"] = {"turn_id": "turn-1", "turn_generation": client.generation}
        calls = []
        client.request = lambda method, params=None, timeout=None: calls.append(
            (method, params)) or {}
        client.interrupt("t")
        self.assertEqual(calls[0][0], "turn/interrupt")

    def test_steer_turn_no_active_turn_raises(self):
        client = bare_client()
        with self.assertRaises(CodexError) as caught:
            client.steer_turn("t", "hi")
        self.assertTrue(caught.exception.queueable)

    def test_steer_turn_reraises_unrelated_error(self):
        client = bare_client()
        client.proc = SimpleNamespace(poll=lambda: None)
        client.thread_state["t"] = {"turn_id": "turn-1",
                                    "turn_generation": client.generation}
        client.request = lambda *a, **k: (_ for _ in ()).throw(CodexError("boom"))
        with self.assertRaisesRegex(CodexError, "boom"):
            client.steer_turn("t", "hi")

    def test_archive_compact_review_wrappers(self):
        client = bare_client()
        calls = []
        client.request = lambda method, params=None, timeout=None: calls.append(
            (method, params)) or {}
        client.archive("t")
        client.compact("t")
        client.review("t")
        client.review("t", target={"type": "custom"})
        self.assertEqual([c[0] for c in calls],
                         ["thread/archive", "thread/compact/start",
                          "review/start", "review/start"])
        self.assertEqual(calls[2][1]["target"], {"type": "uncommittedChanges"})
        self.assertEqual(calls[3][1]["target"], {"type": "custom"})

    def test_notifications_update_thread_state(self):
        client = bare_client()
        client._notification("account/rateLimits/updated", {"rateLimits": {"x": 1}})
        self.assertEqual(client.account_rate_limits, {"x": 1})

        client._notification("turn/started",
                             {"threadId": "t", "turn": {"id": "turn-a"}})
        self.assertEqual(client.thread_state["t"]["status"], "running")

        client._notification("thread/tokenUsage/updated",
                             {"threadId": "t", "tokenUsage": {"total": 5}})
        self.assertEqual(client.thread_state["t"]["token_usage"], {"total": 5})

        client._notification("thread/status/changed",
                             {"threadId": "t", "status": {"type": "idle"}})
        self.assertEqual(client.thread_state["t"]["thread_status"], {"type": "idle"})

        client._notification("thread/settings/updated", {"threadId": "t",
            "threadSettings": {"model": "gpt", "effort": "high",
                               "collaborationMode": {"mode": "plan"}}})
        self.assertEqual(client.thread_state["t"]["collaboration_mode"], "plan")

        client._notification("item/agentMessage/delta",
                             {"threadId": "t", "itemId": "i1", "delta": "hel"})
        client._notification("item/agentMessage/delta",
                             {"threadId": "t", "itemId": "i1", "delta": "lo"})
        self.assertEqual(client.thread_state["t"]["items"]["i1"]["text"], "hello")

        for _ in range(120):
            client._notification("item/reasoning/textDelta",
                                 {"threadId": "t", "delta": "x"})
        self.assertLessEqual(len(client.thread_state["t"]["stream_events"]), 100)

    def test_turn_completed_error_and_limit(self):
        client = bare_client()
        client._notification("turn/completed", {"threadId": "t", "turn": {
            "status": "failed", "error": {"message": "boom"}}})
        self.assertEqual(client.thread_state["t"]["status"], "error")

        client._notification("turn/completed", {"threadId": "u", "turn": {
            "status": "completed"}})
        self.assertEqual(client.thread_state["u"]["status"], "idle")

    def test_error_notification_without_thread_is_diagnostic(self):
        client = bare_client()
        client._notification("error", {"message": "global boom"})
        self.assertIn("error", [d["kind"] for d in client.diagnostics])

    def test_error_notification_with_thread_sets_blocked(self):
        client = bare_client()
        client._notification("error", {"threadId": "t",
                                       "message": "usage limit reached"})
        self.assertEqual(client.thread_state["t"]["status"], "blocked")

    def test_serverrequest_resolved_removes_pending(self):
        client = bare_client()
        client.approvals["9"] = {"thread_id": "t", "method": "x"}
        client.thread_state["t"] = {"pending": ["9"]}
        client._notification("serverRequest/resolved", {"requestId": "9"})
        self.assertNotIn("9", client.approvals)
        self.assertEqual(client.thread_state["t"]["pending"], [])


class DirectTransportTest(unittest.TestCase):
    def test_send_write_failure_wraps(self):
        client = bare_client()
        client.proc = FakeProc(on_write=lambda data: (_ for _ in ()).throw(
            OSError("pipe")))
        with self.assertRaisesRegex(CodexError, "write failed"):
            client._send({"method": "x"})

    def test_request_connected_when_not_connected(self):
        client = bare_client()
        client.proc = None
        with self.assertRaisesRegex(CodexError, "not connected"):
            client._request_connected("thread/list")

    def test_notify_connected_when_not_connected(self):
        client = bare_client()
        client.proc = None
        with self.assertRaisesRegex(CodexError, "not connected"):
            client._notify_connected("initialized")

    def test_respond_and_respond_error(self):
        client = bare_client()
        writes = []
        client.proc = FakeProc(on_write=lambda data: writes.append(data))
        client.respond(1, {"ok": True})
        client.respond_error(2, "nope")
        self.assertEqual(len(writes), 2)
        self.assertIn("nope", writes[1])

    def test_close_fails_pending_waiters(self):
        client = bare_client()
        event = threading.Event()
        client.pending[1] = {"event": event, "generation": client.generation}
        client.close()
        self.assertTrue(event.is_set())

    def test_reader_survives_non_dict_message(self):
        client = bare_client()
        client.diagnostics.clear()

        class Stdout:
            def __init__(self):
                self._items = iter([json.dumps([1, 2]) + "\n"])

            def __iter__(self):
                return self._items

        client.proc = SimpleNamespace(stdout=Stdout(), poll=lambda: 0,
                                      terminate=lambda: None)
        client._read_loop()
        self.assertIn("invalid_message", [d["kind"] for d in client.diagnostics])

    def test_reader_error_cleans_up_pending_and_survives_terminate_failure(self):
        client = bare_client()
        client.diagnostics.clear()
        generation = client.generation

        class RaisingStdout:
            def __iter__(self):
                raise RuntimeError("reader boom")

        def terminate():
            raise OSError("terminate failed")  # exercises the guarded terminate

        client.proc = SimpleNamespace(stdout=RaisingStdout(), poll=lambda: None,
                                      terminate=terminate)
        waiter = {"event": threading.Event(), "generation": generation}
        client.pending[7] = waiter
        client.approvals["9"] = {"request_id": 9, "method": "execCommandApproval",
                                 "thread_id": "t", "generation": generation}
        # A stale request without a thread id skips per-thread cleanup (continue).
        client.approvals["10"] = {"request_id": 10, "method": "execCommandApproval",
                                  "thread_id": None, "generation": generation}
        client.thread_state["t"] = {"pending": ["9"], "revision": 0}
        client._read_loop()
        kinds = [d["kind"] for d in client.diagnostics]
        self.assertIn("reader_error", kinds)
        self.assertIn("stale_pending_request", kinds)
        self.assertTrue(waiter["event"].is_set())
        self.assertNotIn("9", client.approvals)
        self.assertIn("no longer answerable", client.thread_state["t"]["error"])

    def test_start_times_out_when_stuck_starting(self):
        client = CodexAppServer(command=["/bin/false", "app-server"], timeout=0.05)
        client.connection_state = "starting"
        with self.assertRaisesRegex(CodexError, "timed out"):
            client.start()

    def test_debug_env_does_not_break_reader(self):
        os.environ["FLEET_DASH_CODEX_DEBUG"] = "1"
        try:
            client = bare_client()

            class Stdout:
                def __iter__(self):
                    return iter([json.dumps({"method": "thread/status/changed",
                        "params": {"threadId": "t", "status": {"type": "idle"}}}) + "\n"])

            client.proc = SimpleNamespace(stdout=Stdout(), poll=lambda: 0,
                                          terminate=lambda: None)
            client._read_loop()
            self.assertEqual(client.thread_state["t"]["thread_status"], {"type": "idle"})
        finally:
            del os.environ["FLEET_DASH_CODEX_DEBUG"]


class StartupTest(unittest.TestCase):
    def _ok_process(self, alive=2):
        class Proc:
            def __init__(self):
                self.stdin = FakeStdin()
                self.returncode = None
                self._gate = threading.Event()
                self.stdout = self._gen()

            def _gen(self):
                yield json.dumps({"id": 1, "result": {}}) + "\n"
                self._gate.wait(alive)  # stay alive through initialization

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15
                self._gate.set()

        return Proc()

    def test_startup_callable_runs_then_transport(self):
        # Generous timeout on purpose: this asserts ORDER (startup callable before
        # transport), not speed, and a 1s budget intermittently expired when the
        # suite ran under coverage instrumentation.
        events = []
        client = CodexAppServer(
            command=["/bin/false", "app-server"], timeout=10,
            process_factory=lambda *a, **k: self._ok_process(alive=30),
            startup=lambda: events.append("startup"))
        client.start()
        self.assertEqual(events, ["startup"])
        client.close()

    def test_startup_callable_codexerror_propagates(self):
        def startup():
            raise CodexError("prereq failed")
        client = CodexAppServer(
            command=["/bin/false", "app-server"], timeout=0.5,
            process_factory=lambda *a, **k: self._ok_process(), startup=startup)
        with self.assertRaisesRegex(CodexError, "prereq failed"):
            client.start()

    def test_startup_callable_generic_error_wrapped(self):
        def startup():
            raise ValueError("weird")
        client = CodexAppServer(
            command=["/bin/false", "app-server"], timeout=0.5,
            process_factory=lambda *a, **k: self._ok_process(), startup=startup)
        with self.assertRaisesRegex(CodexError, "failed to start"):
            client.start()

    def test_startup_command_exception_wrapped(self):
        def runner(*a, **k):
            raise OSError("cannot exec")
        client = CodexAppServer(
            command=["/bin/false", "app-server"], timeout=0.5,
            process_factory=lambda *a, **k: self._ok_process(),
            startup_command=["/bin/false"], startup_factory=runner)
        with self.assertRaisesRegex(CodexError, "failed to start"):
            client.start()


class MaskedAndPartialFrameTest(unittest.TestCase):
    def test_masked_server_frame_is_unmasked(self):
        proc, server = connected_process()
        try:
            payload = b"masked-hi"
            mask = b"\x01\x02\x03\x04"
            masked = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
            frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked
            server.sendall(frame)
            self.assertEqual(proc.receive_text(), "masked-hi")
        finally:
            proc.terminate()
            server.close()

    def test_partial_127_length_returns_none(self):
        proc, server = connected_process()
        server.sendall(bytes([0x81, 127]))
        server.close()
        self.assertIsNone(proc.receive_text())

    def test_partial_payload_returns_none(self):
        proc, server = connected_process()
        server.sendall(bytes([0x81, 5]))  # promises 5 payload bytes
        server.close()
        self.assertIsNone(proc.receive_text())


class NotifyAndApprovalTest(unittest.TestCase):
    def test_notify_starts_then_notifies(self):
        client = bare_client()
        client.start = lambda: None
        writes = []
        client.proc = FakeProc(on_write=lambda data: writes.append(data))
        client.notify("initialized", {})
        self.assertTrue(writes)

    def test_resolve_request_respond_failure_resets_pending(self):
        client = bare_client()
        client.approvals["1"] = {"request_id": 1,
                                 "method": "execCommandApproval",
                                 "params": {}, "state": "pending"}

        def boom(rid, result):
            raise OSError("write failed")
        client.respond = boom
        with self.assertRaises(OSError):
            client.decide("1", "allow")
        self.assertEqual(client.approvals["1"]["state"], "pending")

    def test_decide_permissions_grant_and_cancel(self):
        for choice, expect_perm in (("allow", True), ("always", True),
                                    ("deny", False), ("cancel", False)):
            with self.subTest(choice=choice):
                client = bare_client()
                results = []
                client.respond = lambda rid, result: results.append(result)
                client.approvals["1"] = {"request_id": 1,
                    "method": "item/permissions/requestApproval",
                    "params": {"permissions": {"read": True, "write": None}},
                    "state": "pending"}
                client.decide("1", choice)
                granted = results[0]["permissions"]
                self.assertEqual(bool(granted), expect_perm)
        # confirm scope for 'always'
        client = bare_client()
        results = []
        client.respond = lambda rid, result: results.append(result)
        client.approvals["1"] = {"request_id": 1,
            "method": "item/permissions/requestApproval",
            "params": {"permissions": {"read": True}}, "state": "pending"}
        client.decide("1", "always")
        self.assertEqual(results[0]["scope"], "session")

    def test_decide_already_answered_and_unknown_choice(self):
        client = bare_client()
        client.respond = lambda rid, result: None
        client.approvals["1"] = {"request_id": 1, "method": "execCommandApproval",
                                 "params": {}, "state": "responding"}
        with self.assertRaisesRegex(CodexError, "already being answered"):
            client.decide("1", "allow")
        client.approvals["2"] = {"request_id": 2, "method": "execCommandApproval",
                                 "params": {}, "state": "pending"}
        with self.assertRaisesRegex(CodexError, "unknown approval decision"):
            client.decide("2", "maybe")

    def test_answer_questions_error_paths(self):
        client = bare_client()
        client.respond = lambda rid, result: None
        # stale / wrong method
        with self.assertRaisesRegex(CodexError, "stale Codex question"):
            client.answer_questions("x", [{"digits": [1]}])
        client.approvals["1"] = {"request_id": 1, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "q", "options": [{"label": "One"}]}]},
            "state": "responding"}
        with self.assertRaisesRegex(CodexError, "already being answered"):
            client.answer_questions("1", [{"digits": [1]}])
        client.approvals["2"] = {"request_id": 2, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"id": "q", "options": [{"label": "One"}]}]},
            "state": "pending"}
        with self.assertRaisesRegex(CodexError, "invalid Codex question response"):
            client.answer_questions("2", [{}, {}])   # too many answers
        with self.assertRaisesRegex(CodexError, "invalid option selection"):
            client.answer_questions("2", [{"digits": "notlist"}])
        # 'other' free text appended
        client.answer_questions("2", [{"digits": [], "other": "custom"}])

    def test_answer_questions_missing_qid(self):
        client = bare_client()
        client.respond = lambda rid, result: None
        client.approvals["3"] = {"request_id": 3, "method": "item/tool/requestUserInput",
            "params": {"questions": [{"options": [{"label": "One"}]}]},
            "state": "pending"}
        with self.assertRaisesRegex(CodexError, "missing an id"):
            client.answer_questions("3", [{"digits": [1]}])

    def test_answer_elicitation_paths(self):
        client = bare_client()
        responses = []
        client.respond = lambda rid, result: responses.append(result)
        with self.assertRaisesRegex(CodexError, "stale Codex elicitation"):
            client.answer_elicitation("x", "accept")
        client.approvals["1"] = {"request_id": 1,
            "method": "mcpServer/elicitation/request", "params": {},
            "state": "responding"}
        with self.assertRaisesRegex(CodexError, "already being answered"):
            client.answer_elicitation("1", "accept")
        client.approvals["2"] = {"request_id": 2,
            "method": "mcpServer/elicitation/request", "params": {}, "state": "pending"}
        with self.assertRaisesRegex(CodexError, "unknown elicitation action"):
            client.answer_elicitation("2", "shrug")
        with self.assertRaisesRegex(CodexError, "structured content"):
            client.answer_elicitation("2", "accept", content="notdict")
        client.answer_elicitation("2", "accept", content={"env": "prod"})
        self.assertEqual(responses[-1], {"action": "accept", "content": {"env": "prod"}})
        client.approvals["3"] = {"request_id": 3,
            "method": "mcpServer/elicitation/request", "params": {}, "state": "pending"}
        client.answer_elicitation("3", "decline")
        self.assertEqual(responses[-1], {"action": "decline"})


class ErrorHelperTest(unittest.TestCase):
    def test_safe_json_handles_circular(self):
        circular = {}
        circular["self"] = circular
        self.assertIsInstance(_safe_json(circular), str)

    def test_error_text_variants(self):
        self.assertEqual(_error_text(None), "")
        self.assertEqual(_error_text("hi"), "hi")
        self.assertEqual(_error_text({"message": "boom"}), "boom")
        self.assertEqual(_error_text({"detail": "d"}), "d")
        self.assertEqual(_error_text(123), "123")
        self.assertTrue(_error_text("x" * 2000).endswith("…"))
        self.assertIsInstance(_error_text({"weird": object()}), str)

    def test_is_limit_error(self):
        self.assertTrue(_is_limit_error("Usage limit reached"))
        self.assertTrue(_is_limit_error({"message": "quota exceeded"}))
        self.assertFalse(_is_limit_error("all good"))


if __name__ == "__main__":
    unittest.main()
