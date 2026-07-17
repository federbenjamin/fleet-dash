import base64
import json
import os
import stat
import tempfile
import time
import unittest

from briefing import FleetOperations
from web_push import (PushSecretStore, WebPushError, WebPushHelper,
                      WebPushService, generate_vapid_keys, resolve_node)


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def generated_keys():
    return {"public_key": encoded(b"\x04" + b"v" * 64),
            "private_key": encoded(b"p" * 32)}


def subscription():
    return {"endpoint": "https://web.push.apple.com/Qworker",
            "expirationTime": None,
            "keys": {"p256dh": encoded(b"\x04" + b"s" * 64),
                     "auth": encoded(b"a" * 16)}}


class FakeHelper:
    sent = []

    def __init__(self, *_args, **_kwargs):
        self.ready = False

    def ensure_started(self):
        self.ready = True

    def send(self, job, timeout=15):
        self.sent.append(job)
        return {"type": "result", "request_id": job["request_id"],
                "ok": True, "status": 201, "code": "http_201"}

    def stop(self):
        self.ready = False

    def status(self):
        return {"ready": self.ready, "restarts": 0,
                "state": "ready" if self.ready else "unavailable"}


class WebPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_secret_store_is_atomic_private_and_never_regenerates_corruption(self):
        path = os.path.join(self.tmp.name, "push-secrets.json")
        calls = []
        store = PushSecretStore(path, lambda: calls.append(True) or generated_keys())
        first = store.load_or_create()
        self.assertEqual(len(calls), 1)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(store.load_or_create(), first)
        self.assertEqual(len(calls), 1)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("not json")
        os.chmod(path, 0o600)
        with self.assertRaises(WebPushError):
            store.load_or_create()
        self.assertEqual(len(calls), 1)

    def test_secret_store_never_replaces_a_racing_winner(self):
        path = os.path.join(self.tmp.name, "push-secrets.json")
        winner = PushSecretStore(path, generated_keys).load_or_create()
        different = {"public_key": encoded(b"\x04" + b"w" * 64),
                     "private_key": encoded(b"q" * 32)}
        store = PushSecretStore(path, lambda: different)
        with self.assertRaises(FileExistsError):
            store._write(store._validated({"version": 1,
                "vapid_public_key": different["public_key"],
                "vapid_private_key": different["private_key"],
                "action_secret": encoded(b"z" * 32)}))
        self.assertEqual(store.load_or_create(), winner)

    def test_secret_store_rejects_permissions_and_symlinks(self):
        path = os.path.join(self.tmp.name, "push-secrets.json")
        store = PushSecretStore(path, generated_keys)
        store.load_or_create()
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(WebPushError, "0600"):
            store.load_or_create()
        os.unlink(path)
        target = os.path.join(self.tmp.name, "target")
        with open(target, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "vapid_public_key": generated_keys()["public_key"],
                       "vapid_private_key": generated_keys()["private_key"],
                       "action_secret": encoded(b"a" * 32)}, stream)
        os.chmod(target, 0o600)
        os.symlink(target, path)
        with self.assertRaisesRegex(WebPushError, "regular file"):
            store.load_or_create()

    def test_real_helper_initializes_and_bounds_invalid_jobs(self):
        try:
            node = resolve_node()
        except WebPushError as exc:
            self.skipTest(str(exc))
        worker = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "web_push_worker.js")
        vapid = generate_vapid_keys(node, worker)
        helper = WebPushHelper(node, worker, vapid={
            "vapid_public_key": vapid["public_key"],
            "vapid_private_key": vapid["private_key"]},
            subject="mailto:fleet-dash@localhost.invalid")
        try:
            helper.ensure_started()
            self.assertTrue(helper.status()["ready"])
            result = helper.send({"type": "send", "request_id": "bad",
                                  "unexpected": "field"})
            self.assertEqual(result["code"], "invalid_job")
            self.assertNotIn(vapid["private_key"], repr(result))
            helper.process.kill()
            helper.process.wait(timeout=1)
            with self.assertRaisesRegex(WebPushError, "restarting"):
                helper.send({"type": "send", "request_id": "during-restart"})
            helper.next_start_at = 0
            recovered = helper.send({"type": "send", "request_id": "recovered",
                                     "unexpected": "field"})
            self.assertEqual(recovered["code"], "invalid_job")
            self.assertGreaterEqual(helper.status()["restarts"], 1)
        finally:
            helper.stop()

    def test_service_delivers_minimal_test_without_exposing_secrets(self):
        ledger = os.path.join(self.tmp.name, "ledger.db")
        operations = FleetOperations(ledger)
        operations.notification_register_device(
            "phone", "Phone", "iOS", subscription())
        PushSecretStore(os.path.join(self.tmp.name, "push-secrets.json"),
                        generated_keys).load_or_create()
        FakeHelper.sent = []
        service = WebPushService(
            operations, self.tmp.name, {}, helper_factory=FakeHelper,
            node_resolver=lambda _configured: "/usr/bin/false")
        service.start()
        delivery = service.enqueue_test("phone")
        deadline = time.time() + 3
        while time.time() < deadline:
            if operations.notification_delivery_status(delivery["id"])["status"] == "sent":
                break
            time.sleep(.02)
        self.assertEqual(operations.notification_delivery_status(
            delivery["id"])["status"], "sent")
        self.assertEqual(len(FakeHelper.sent), 1)
        sent = FakeHelper.sent[0]
        payload = json.loads(sent["payload"])
        self.assertEqual(payload["title"], "Fleet notification test")
        self.assertEqual(payload["body"], "Web Push delivery is working.")
        self.assertNotIn("subscription", payload)
        self.assertNotIn("session_id", payload)
        projected = service.status()
        self.assertTrue(projected["configured"])
        self.assertNotIn("private", repr(projected))
        self.assertNotIn("web.push.apple.com", repr(projected))
        service.stop()


if __name__ == "__main__":
    unittest.main()
