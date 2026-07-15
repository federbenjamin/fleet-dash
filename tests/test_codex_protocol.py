import json
import queue
import threading
import time
import unittest

from codex_adapter import CodexAppServer, CodexError


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
        client.thread_state["thread-one"] = {
            "status": "running", "turn_id": "turn-active"}

        client.steer_turn("thread-one", "Focus on the failing test")

        self.assertEqual(calls, [("turn/steer", {
            "threadId": "thread-one", "expectedTurnId": "turn-active",
            "input": [{"type": "text", "text": "Focus on the failing test"}],
        })])


if __name__ == "__main__":
    unittest.main()
