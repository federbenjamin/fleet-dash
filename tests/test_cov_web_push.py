"""Line-coverage tests for fleetdash.web_push.

These exercise the error/edge/backoff paths of the Web Push supervisor that the
behavioural suite in tests/test_web_push.py does not reach. The security model is
preserved: no real DNS/TLS, no real Node helper against a real endpoint, no real
~/.claude. The Node helper is driven with a scripted fake process (FakeProc) and
temp dirs stand in for state directories.
"""
import base64
import json
import os
import queue
import stat
import tempfile
import threading
import types
import unittest
from unittest import mock

from fleetdash import web_push
from fleetdash.briefing import OperationsError
from fleetdash.web_push import (ActionCapabilityCodec, PushSecretStore, WebPushError,
                                WebPushHelper, WebPushService, generate_vapid_keys, resolve_node)


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def generated_keys():
    return {"public_key": encoded(b"\x04" + b"v" * 64),
            "private_key": encoded(b"p" * 32)}


# --- scripted fake Node helper process -------------------------------------

class FakeStream:
    """A stdout stub whose readline() drains a preloaded queue then reports EOF."""

    def __init__(self, lines):
        self.q = queue.Queue()
        for line in lines:
            self.q.put(line)
        self.q.put(None)  # EOF sentinel

    def readline(self, _size=-1):
        item = self.q.get()
        return b"" if item is None else item

    def close(self):
        pass


class FakeStdin:
    def __init__(self):
        self.data = []

    def write(self, chunk):
        self.data.append(chunk)
        return len(chunk)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProc:
    """Minimal Popen stand-in feeding scripted JSONL lines to the reader thread."""

    def __init__(self, stdout_lines):
        self.stdin = FakeStdin()
        self.stdout = FakeStream(stdout_lines)
        self.stderr = None
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def helper_with(lines, **kwargs):
    factory = lambda *_a, **_k: FakeProc(list(lines))
    return WebPushHelper("/usr/bin/node", "/tmp/worker.js",
                         vapid={"vapid_public_key": "pub", "vapid_private_key": "priv"},
                         subject="mailto:x@localhost.invalid",
                         process_factory=factory, **kwargs)


READY = b'{"type":"ready","request_id":"init"}\n'


# --- ActionCapabilityCodec --------------------------------------------------

class CodecTests(unittest.TestCase):
    def _codec(self):
        return ActionCapabilityCodec(encoded(b"s" * 32))

    def test_init_rejects_non_ascii_secret(self):  # lines 42-43
        with self.assertRaisesRegex(WebPushError, "secret is invalid"):
            ActionCapabilityCodec("é")

    def test_init_rejects_short_secret(self):  # line 45
        with self.assertRaisesRegex(WebPushError, "secret is invalid"):
            ActionCapabilityCodec(encoded(b"\x00\x00\x00"))

    def test_decode_rejects_bad_charset(self):  # line 55
        with self.assertRaises(WebPushError):
            ActionCapabilityCodec._decode("has.dot")

    def test_decode_rejects_undecodable_base64(self):  # lines 58-59
        with self.assertRaises(WebPushError):
            ActionCapabilityCodec._decode("A")

    def test_mint_rejects_unknown_action(self):  # line 63
        with self.assertRaises(WebPushError):
            self._codec().mint("e", "d", "delete")

    def test_verify_rejects_malformed_token(self):  # line 72
        with self.assertRaises(WebPushError):
            self._codec().verify("no-dot-here")

    def test_verify_rejects_valid_signature_over_non_json(self):  # lines 80-81
        import hmac
        import hashlib
        codec = self._codec()
        raw = b"not json"
        sig = hmac.new(codec.secret, raw, hashlib.sha256).digest()
        token = codec._encode(raw) + "." + codec._encode(sig)
        with self.assertRaises(WebPushError):
            codec.verify(token)

    def test_verify_rejects_valid_signature_over_wrong_shape(self):  # line 88
        import hmac
        import hashlib
        codec = self._codec()
        raw = json.dumps({"v": 1}).encode()
        sig = hmac.new(codec.secret, raw, hashlib.sha256).digest()
        token = codec._encode(raw) + "." + codec._encode(sig)
        with self.assertRaises(WebPushError):
            codec.verify(token)


# --- resolve_node -----------------------------------------------------------

class ResolveNodeTests(unittest.TestCase):
    def test_configured_relative_path_is_rejected(self):  # lines 110-112
        with self.assertRaisesRegex(WebPushError, "must be absolute"):
            resolve_node("relative/node")

    def test_configured_missing_absolute_path_is_skipped(self):  # lines 113, 123, 135
        with self.assertRaisesRegex(WebPushError, "unavailable"):
            resolve_node("/definitely/not/here/node")

    def test_subprocess_error_is_swallowed_then_unavailable(self):  # lines 133-135
        with mock.patch.object(web_push.subprocess, "run", side_effect=OSError("boom")):
            with self.assertRaisesRegex(WebPushError, "unavailable"):
                resolve_node()


# --- PushSecretStore --------------------------------------------------------

class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "push-secrets.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _valid(self):
        return {"version": 1, "vapid_public_key": generated_keys()["public_key"],
                "vapid_private_key": generated_keys()["private_key"],
                "action_secret": encoded(b"a" * 32)}

    def test_validated_rejects_unsupported_format(self):  # line 148
        with self.assertRaisesRegex(WebPushError, "unsupported format"):
            PushSecretStore._validated({"version": 2})

    def test_validated_rejects_malformed_keys(self):  # line 158
        bad = dict(self._valid(), vapid_public_key="short")
        with self.assertRaisesRegex(WebPushError, "is invalid"):
            PushSecretStore._validated(bad)

    def _write_valid(self):
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump(self._valid(), stream)
        os.chmod(self.path, 0o600)

    def test_read_rejects_wrong_owner(self):  # line 167
        self._write_valid()
        store = PushSecretStore(self.path, generated_keys)
        real_uid = os.getuid()
        with mock.patch.object(web_push.os, "getuid", lambda: real_uid + 9999):
            with self.assertRaisesRegex(WebPushError, "wrong owner"):
                store._read()

    def test_read_detects_inode_swap_while_opening(self):  # line 175
        self._write_valid()
        store = PushSecretStore(self.path, generated_keys)
        bogus = types.SimpleNamespace(st_dev=-1, st_ino=-1)
        with mock.patch.object(web_push.os, "fstat", lambda _fd: bogus):
            with self.assertRaisesRegex(WebPushError, "changed while opening"):
                store._read()

    def test_read_rejects_oversized_file(self):  # line 180
        with open(self.path, "wb") as stream:
            stream.write(b"{" + b"0" * 9000)
        os.chmod(self.path, 0o600)
        store = PushSecretStore(self.path, generated_keys)
        with self.assertRaisesRegex(WebPushError, "too large"):
            store._read()

    def test_write_tolerates_missing_temp_on_unlink(self):  # lines 212-213
        store = PushSecretStore(self.path, generated_keys)
        with mock.patch.object(web_push.os, "unlink", side_effect=FileNotFoundError):
            store._write(store._validated(self._valid()))
        self.assertTrue(os.path.exists(self.path))

    def test_load_or_create_rereads_on_racing_writer(self):  # lines 226-227
        store = PushSecretStore(self.path, generated_keys)
        winner = self._valid()

        def racing_write(_value):
            with open(self.path, "w", encoding="utf-8") as stream:
                json.dump(winner, stream)
            os.chmod(self.path, 0o600)
            raise FileExistsError

        with mock.patch.object(store, "_write", side_effect=racing_write):
            result = store.load_or_create()
        self.assertEqual(result["action_secret"], winner["action_secret"])


# --- generate_vapid_keys ----------------------------------------------------

class GenerateKeysTests(unittest.TestCase):
    def _run_with(self, proc):
        with mock.patch.object(web_push.subprocess, "Popen", return_value=proc):
            return generate_vapid_keys("/usr/bin/node", "/tmp/worker.js")

    def test_timeout_kills_and_raises(self):  # lines 239-242
        class TimeoutProc:
            returncode = 0
            def __init__(self):
                self.calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise web_push.subprocess.TimeoutExpired("node", timeout)
                return (b"", b"")
            def kill(self):
                pass
        with self.assertRaisesRegex(WebPushError, "timed out"):
            self._run_with(TimeoutProc())

    def test_nonzero_return_raises(self):  # line 244
        proc = types.SimpleNamespace(returncode=1,
                                     communicate=lambda timeout=None: (b"", b""))
        with self.assertRaisesRegex(WebPushError, "generation failed"):
            self._run_with(proc)

    def test_invalid_json_raises(self):  # lines 247-248
        proc = types.SimpleNamespace(returncode=0,
                                     communicate=lambda timeout=None: (b"not json", b""))
        with self.assertRaisesRegex(WebPushError, "invalid data"):
            self._run_with(proc)

    def test_non_dict_result_raises(self):  # line 250
        proc = types.SimpleNamespace(returncode=0,
                                     communicate=lambda timeout=None: (b"[1,2]", b""))
        with self.assertRaisesRegex(WebPushError, "invalid data"):
            self._run_with(proc)


# --- WebPushHelper ----------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_reader_flags_missing_newline_as_protocol(self):  # lines 280-282, 334
        helper = helper_with([b"abc"])  # no trailing newline
        with self.assertRaisesRegex(WebPushError, "protocol failed"):
            helper.ensure_started()
        helper.stop()

    def test_reader_flags_bad_json_as_protocol(self):  # lines 285-287
        helper = helper_with([b"garbage\n"])
        with self.assertRaisesRegex(WebPushError, "protocol failed"):
            helper.ensure_started()
        helper.stop()

    def test_backoff_window_blocks_start(self):  # lines 296-297
        helper = helper_with([READY], clock=lambda: 0.0)
        helper.next_start_at = 100.0
        with self.assertRaisesRegex(WebPushError, "restarting"):
            helper.ensure_started()

    def test_init_not_ready_fails_closed(self):  # lines 312, 315-318
        helper = helper_with([b'{"type":"nope","request_id":"init"}\n'])
        with self.assertRaisesRegex(WebPushError, "initialization failed"):
            helper.ensure_started()
        helper.stop()

    def test_generic_start_exception_is_wrapped(self):  # lines 315-316, 319
        def boom(*_a, **_k):
            raise RuntimeError("factory down")
        helper = WebPushHelper("/usr/bin/node", "/tmp/worker.js",
                               vapid={"vapid_public_key": "pub", "vapid_private_key": "priv"},
                               subject="mailto:x@localhost.invalid", process_factory=boom)
        with self.assertRaisesRegex(WebPushError, "could not start"):
            helper.ensure_started()

    def test_mismatched_request_id_fails(self):  # line 336
        helper = helper_with([b'{"type":"ready","request_id":"other"}\n'])
        with self.assertRaisesRegex(WebPushError, "did not match"):
            helper.ensure_started()
        helper.stop()

    def test_exchange_rejects_oversized_job(self):  # line 324
        helper = helper_with([READY])
        with self.assertRaisesRegex(WebPushError, "job is too large"):
            helper._exchange_locked({"request_id": "x", "p": "A" * 20000}, 1)

    def test_exchange_without_process_is_unavailable(self):  # line 326
        helper = helper_with([READY])
        with self.assertRaisesRegex(WebPushError, "unavailable"):
            helper._exchange_locked({"request_id": "x"}, 1)

    def test_exchange_empty_queue_times_out(self):  # lines 331-332
        helper = helper_with([READY])
        helper.process = types.SimpleNamespace(poll=lambda: None, stdin=FakeStdin())
        helper.responses = queue.Queue()
        with self.assertRaisesRegex(WebPushError, "did not respond"):
            helper._exchange_locked({"request_id": "x"}, 0)

    def test_stop_locked_survives_terminate_kill_and_close_errors(self):  # lines 357-369
        class BadStream:
            def close(self):
                raise OSError("nope")

        class BadProc:
            def __init__(self):
                self.stdin = BadStream()
                self.stdout = BadStream()
                self.stderr = BadStream()
            def terminate(self):
                raise RuntimeError("terminate")
            def kill(self):
                return None
            def wait(self, timeout=None):
                raise RuntimeError("wait")

        helper = helper_with([READY])
        helper.process = BadProc()
        helper._stop_locked()  # must not raise
        self.assertIsNone(helper.process)

    def test_send_rejects_non_result_response(self):  # line 381
        helper = helper_with([READY, b'{"request_id":"job1","type":"nope"}\n'])
        with self.assertRaisesRegex(WebPushError, "invalid result"):
            helper.send({"type": "send", "request_id": "job1"})
        helper.stop()


# --- WebPushService ---------------------------------------------------------

class ServiceUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _service(self, operations=None, config=None, **kwargs):
        return WebPushService(operations, self.tmp.name, config or {}, **kwargs)

    def test_runtime_failure_then_success_transitions(self):  # lines 425-436, 442
        service = self._service()
        service._runtime_failure("degraded", "claim_failed", "queue claim failed")
        self.assertEqual(service.runtime_state, "degraded")
        self.assertEqual(service.runtime_error, "claim_failed")
        self.assertEqual(service.runtime_failure_counts["claim_failed"], 1)
        # repeat with no change exercises the unchanged branch
        service._runtime_failure("degraded", "claim_failed", "queue claim failed")
        self.assertEqual(service.runtime_failure_counts["claim_failed"], 2)
        service._runtime_success()
        self.assertEqual(service.runtime_state, "ready")
        self.assertIsNone(service.runtime_error)

    def test_bootstrap_stops_previous_helper(self):  # line 487
        PushSecretStore(os.path.join(self.tmp.name, "push-secrets.json"),
                        generated_keys).load_or_create()
        stopped = []

        class SimpleHelper:
            def __init__(self, *_a, **_k):
                pass
            def ensure_started(self):
                pass
            def stop(self):
                stopped.append(True)

        service = self._service(helper_factory=SimpleHelper,
                                node_resolver=lambda _c: "/usr/bin/false")
        previous = SimpleHelper()
        service.helper = previous
        service._bootstrap()
        self.assertEqual(stopped, [True])
        self.assertIsNot(service.helper, previous)

    def test_payload_stall_and_generic_kinds(self):  # lines 495-498
        service = self._service()
        service.action_codec = None
        stall = json.loads(service._payload(
            {"event_id": "e", "device_id": "d", "event": {"kind": "stall"}}))
        self.assertEqual(stall["title"], "Fleet needs attention")
        self.assertEqual(stall["body"], "A coding session may be stalled.")
        other = json.loads(service._payload(
            {"event_id": "e", "device_id": "d", "event": {"kind": "failure"}}))
        self.assertEqual(other["body"], "A provider or delivery needs review.")

    def test_payload_privacy_validation_rejects_oversized(self):  # line 521
        service = self._service()
        service.action_codec = None
        with self.assertRaisesRegex(WebPushError, "privacy validation failed"):
            service._payload({"event_id": "A" * 3000, "device_id": "d",
                              "event": {"kind": "question"}})

    def test_capability_action_requires_codec(self):  # lines 525-528
        service = self._service()
        service.action_codec = None
        with self.assertRaisesRegex(WebPushError, "unavailable"):
            service.capability_action("token")

    def test_capability_action_verifies_and_consumes(self):  # lines 529-530
        applied = {"until": 123}

        class Ops:
            def notification_consume_capability(self, claims, mute_callback=None):
                self.claims = claims
                return applied

        ops = Ops()
        service = self._service(operations=ops)
        service.action_codec = ActionCapabilityCodec(encoded(b"s" * 32))
        token = service.action_codec.mint("evt", "phone", "snooze")
        self.assertIs(service.capability_action(token), applied)
        self.assertEqual(ops.claims["event_id"], "evt")

    def test_start_is_idempotent_when_thread_alive(self):  # line 587
        service = self._service()
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        service.thread = alive
        service.start()  # returns immediately without spawning a new thread
        self.assertIs(service.thread, alive)
        gate.set()
        alive.join(2)


class ServiceLoopTests(unittest.TestCase):
    """Single-iteration exercises of WebPushService._run driven to stop deterministically."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _service(self, operations, **kwargs):
        return WebPushService(operations, self.tmp.name, {}, **kwargs)

    def _drain(self, service, ready_signal):
        service.start()
        self.assertTrue(ready_signal.wait(2), "loop iteration did not run")
        service.stop_event.set()
        service.wake_event.set()
        service.thread.join(2)
        self.assertFalse(service.thread.is_alive())

    def test_run_handles_bootstrap_failure(self):  # lines 538-545
        reached = threading.Event()

        def resolver(_c):
            reached.set()
            raise WebPushError("no node")

        service = self._service(None, node_resolver=resolver)
        self._drain(service, reached)
        self.assertEqual(service.runtime_error, "bootstrap_unavailable")

    def test_run_handles_claim_failure(self):  # lines 551-555
        reached = threading.Event()

        class Ops:
            def notification_claim_delivery(self, _origins):
                reached.set()
                raise RuntimeError("db down")

        service = self._service(Ops())
        service.helper = types.SimpleNamespace(stop=lambda: None)
        service.runtime_state = "ready"
        self._drain(service, reached)
        self.assertEqual(service.runtime_error, "claim_failed")

    def test_run_handles_send_failure_and_finish_failure_codes(self):  # 560-567, 578-581
        finished = []

        class SendRaiseHelper:
            def send(self, _job, timeout=15):
                raise RuntimeError("helper down")
            def stop(self):
                pass

        class Ops:
            def __init__(self, service):
                self.service = service
                self.n = 0
            def notification_claim_delivery(self, _origins):
                self.n += 1
                if self.n == 1:
                    return {"id": "d1", "event_id": "e1",
                            "subscription": {"endpoint": "https://x", "keys": {}},
                            "event": {"kind": "stall"}}
                return None
            def notification_finish_delivery(self, delivery_id, result):
                finished.append((delivery_id, result))
                self.service.stop_event.set()

        service = self._service(None)
        service.operations = Ops(service)
        service.helper = SendRaiseHelper()
        service.action_codec = None
        service.runtime_state = "ready"
        service.start()
        service.thread.join(3)
        self.assertFalse(service.thread.is_alive())
        self.assertEqual(len(finished), 1)
        self.assertFalse(finished[0][1]["ok"])
        # send raised -> fallback code drove the ready-state failure record
        self.assertEqual(service.runtime_error, "helper_unavailable")

    def test_run_normalizes_unsafe_failure_code(self):  # line 580
        class BadCodeHelper:
            def send(self, _job, timeout=15):
                return {"ok": False, "code": "NOT A SAFE CODE!!"}
            def stop(self):
                pass

        class Ops:
            def __init__(self, service):
                self.service = service
                self.n = 0
            def notification_claim_delivery(self, _origins):
                self.n += 1
                if self.n == 1:
                    return {"id": "d1", "event_id": "e1",
                            "subscription": {"endpoint": "https://x", "keys": {}},
                            "event": {"kind": "stall"}}
                return None
            def notification_finish_delivery(self, _delivery_id, _result):
                self.service.stop_event.set()

        service = self._service(None)
        service.operations = Ops(service)
        service.helper = BadCodeHelper()
        service.action_codec = None
        service.runtime_state = "ready"
        service.start()
        service.thread.join(3)
        self.assertFalse(service.thread.is_alive())
        self.assertEqual(service.runtime_error, "delivery_failed")

    def test_run_handles_finish_persistence_failure(self):  # lines 571-572
        class OkHelper:
            def send(self, _job, timeout=15):
                return {"ok": True, "status": 201}
            def stop(self):
                pass

        class Ops:
            def __init__(self, service):
                self.service = service
                self.n = 0
            def notification_claim_delivery(self, _origins):
                self.n += 1
                if self.n == 1:
                    return {"id": "d1", "event_id": "e1",
                            "subscription": {"endpoint": "https://x", "keys": {}},
                            "event": {"kind": "question", "session_id": "s"}}
                return None
            def notification_finish_delivery(self, _delivery_id, _result):
                self.service.stop_event.set()
                raise OperationsError("persist failed")

        service = self._service(None)
        service.operations = Ops(service)
        service.helper = OkHelper()
        service.action_codec = None
        service.runtime_state = "ready"
        service.start()
        service.thread.join(3)
        self.assertFalse(service.thread.is_alive())
        self.assertEqual(service.runtime_error, "finish_failed")


if __name__ == "__main__":
    unittest.main()
