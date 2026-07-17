import io
import json
import time
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace

from server import Handler


class ServerHandlerTest(unittest.TestCase):
    def handler(self, path="/api/context"):
        handler = Handler.__new__(Handler)
        handler.path = path
        handler.headers = {}
        handler._request_started = time.perf_counter()
        handler._request_route = path.split("?", 1)[0]
        return handler

    def test_all_conversation_surfaces_share_bounded_reverse_pagination(self):
        messages = [{"role": "assistant", "text": str(index)} for index in range(205)]
        for route in ("/api/context", "/api/closed_context", "/api/agent_context"):
            handler = self.handler(route + "?limit=50")
            first = Handler.paginate_context(handler, {"ok": True, "messages": messages})
            self.assertEqual(len(first["messages"]), 50)
            self.assertEqual(first["messages"][0]["text"], "155")
            self.assertEqual(first["message_total"], 205)
            self.assertEqual(first["next_cursor"], 155)

            handler.path = route + "?limit=50&cursor=155"
            older = Handler.paginate_context(handler, {"ok": True, "messages": messages})
            self.assertEqual(older["messages"][0]["text"], "105")
            self.assertEqual(older["next_cursor"], 105)

            handler.path = route + "?cursor=999"
            invalid = Handler.paginate_context(handler, {"ok": True, "messages": messages})
            self.assertFalse(invalid["ok"])
            self.assertIn("pagination", invalid["error"])

    def test_get_and_post_exceptions_return_bounded_errors(self):
        for method in (Handler.do_GET, Handler.do_POST):
            handler = self.handler("/api/fleet")
            handler.begin_request = lambda: setattr(handler, "_request_route", "/api/fleet")
            if method is Handler.do_GET:
                handler._do_GET = lambda: (_ for _ in ()).throw(RuntimeError("secret detail"))
            else:
                handler._do_POST = lambda: (_ for _ in ()).throw(RuntimeError("secret detail"))
            replies = []
            handler.error_reply = lambda message: replies.append(message)
            method(handler)
            self.assertEqual(replies, ["request failed"])

    def test_reply_swallows_disconnects_during_headers_or_body(self):
        for fail_at in ("headers", "body"):
            handler = self.handler("/api/fleet")
            handler.send_response = lambda code: (
                (_ for _ in ()).throw(BrokenPipeError()) if fail_at == "headers" else None)
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler.wfile = SimpleNamespace(write=lambda body: (
                (_ for _ in ()).throw(ConnectionResetError()) if fail_at == "body" else None))
            Handler.reply(handler, 200, "application/json", b"{}")

    def test_post_body_is_bounded_and_read_has_a_timeout(self):
        handler = self.handler("/api/act")
        timeouts = []
        handler.connection = SimpleNamespace(settimeout=lambda seconds: timeouts.append(seconds))
        handler.eng = SimpleNamespace(cfg={"act_token": "token"},
                                      act=lambda action: {"ok": True, "action": action})
        handler.headers = {"X-Act-Token": "token", "Content-Length": "16"}
        handler.rfile = io.BytesIO(b'{"type":"ping"}')
        replies = []
        handler.reply = lambda code, ctype, body: replies.append(
            (code, ctype, json.loads(body)))
        Handler._do_POST(handler)
        self.assertEqual(timeouts, [5])
        self.assertTrue(replies[0][2]["ok"])

        handler.headers["Content-Length"] = "65537"
        replies.clear()
        Handler._do_POST(handler)
        self.assertEqual(replies[0][0], 413)

    def test_settings_audit_logs_only_field_names(self):
        handler = self.handler("/api/settings")
        handler.connection = SimpleNamespace(settimeout=lambda _seconds: None)
        handler.eng = SimpleNamespace(cfg={"act_token": "token"},
                                      update_settings=lambda action: {"ok": True})
        payload = json.dumps({"reader_width": "secret-value"}).encode()
        handler.headers = {"X-Act-Token": "token",
                           "Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.reply = lambda *_args: None
        audit = io.StringIO()
        with redirect_stderr(audit):
            Handler._do_POST(handler)
        self.assertIn("field_count", audit.getvalue())
        self.assertNotIn("secret-value", audit.getvalue())

    def test_notifications_route_requires_auth_and_forwards_bounded_query(self):
        calls = []
        handler = self.handler(
            "/api/notifications?device=phone-1&cursor=42&limit=20&state=active,snoozed"
            "&kind=question,approval&id=evt-1")
        handler.eng = SimpleNamespace(
            cfg={"act_token": "token"},
            notifications_snapshot=lambda *args: calls.append(args) or
            {"ok": True, "events": []})
        replies = []
        handler.reply = lambda code, ctype, body: replies.append(
            (code, ctype, json.loads(body)))

        Handler._do_GET(handler)
        self.assertEqual(replies[0][0], 403)
        self.assertEqual(calls, [])

        handler.headers = {"X-Act-Token": "token"}
        replies.clear()
        Handler._do_GET(handler)
        self.assertEqual(replies[0][0], 200)
        self.assertEqual(calls, [("phone-1", "42", "20",
                                  ["active", "snoozed"],
                                  ["question", "approval"], "evt-1")])

    def test_push_get_routes_require_auth_and_return_only_engine_projection(self):
        calls = []
        for route, method in (("/api/push/config?device=phone-1", "push_config"),
                              ("/api/push/devices?device=phone-1", "push_devices")):
            handler = self.handler(route)
            handler.eng = SimpleNamespace(cfg={"act_token": "token"}, **{
                method: lambda device, method=method: calls.append((method, device)) or
                    {"ok": True, "current_device": {"id": device}}})
            replies = []
            handler.reply = lambda code, ctype, body: replies.append(
                (code, json.loads(body)))
            Handler._do_GET(handler)
            self.assertEqual(replies[0][0], 403)
            handler.headers = {"X-Act-Token": "token"}
            replies.clear()
            Handler._do_GET(handler)
            self.assertEqual(replies[0][1]["current_device"]["id"], "phone-1")
        self.assertEqual(calls, [("push_config", "phone-1"),
                                 ("push_devices", "phone-1")])

    def test_notification_mutation_routes_are_token_gated_and_dispatch_exact_payload(self):
        routes = {
            "/api/notifications/read": "notifications_mark_read",
            "/api/notifications/snooze": "notifications_snooze",
            "/api/notifications/wake": "notifications_wake",
            "/api/notifications/mute": "notifications_mute",
            "/api/notifications/retry": "notifications_retry",
        }
        payload = {"device_id": "phone-1", "event_id": "evt-1",
                   "source_revision": "rev-1", "cursor": 3,
                   "until": time.time() + 900, "muted": True,
                   "delivery_id": "delivery-1"}
        for route, method in routes.items():
            with self.subTest(route=route):
                calls = []
                handler = self.handler(route)
                handler.connection = SimpleNamespace(settimeout=lambda _seconds: None)
                handler.eng = SimpleNamespace(cfg={"act_token": "token"}, **{
                    method: lambda action, method=method: calls.append((method, action)) or
                        {"ok": True}})
                body = json.dumps(payload).encode()
                handler.headers = {"Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                replies = []
                handler.reply = lambda code, ctype, data: replies.append(
                    (code, json.loads(data)))
                Handler._do_POST(handler)
                self.assertEqual(replies[0][0], 403)
                self.assertEqual(calls, [])

                handler.headers["X-Act-Token"] = "token"
                handler.rfile = io.BytesIO(body)
                replies.clear()
                Handler._do_POST(handler)
                self.assertEqual(replies[0], (200, {"ok": True}))
                self.assertEqual(calls, [(method, payload)])

    def test_push_capability_route_ignores_act_token_and_accepts_only_capability_body(self):
        calls = []
        handler = self.handler("/api/push/capability-action")
        handler.connection = SimpleNamespace(settimeout=lambda _seconds: None)
        handler.eng = SimpleNamespace(
            cfg={"act_token": "secret"},
            push_capability_action=lambda action: calls.append(action) or {"ok": True})
        payload = json.dumps({"capability": "signed-token"}).encode()
        handler.headers = {"X-Act-Token": "wrong", "Content-Type": "application/json",
                           "Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        replies = []
        handler.reply = lambda code, ctype, data: replies.append((code, json.loads(data)))
        Handler._do_POST(handler)
        self.assertEqual(replies, [(200, {"ok": True})])
        self.assertEqual(calls, [{"capability": "signed-token"}])

        bad = json.dumps({"capability": "signed-token", "event_id": "evt-forged"}).encode()
        handler.headers["Content-Length"] = str(len(bad))
        handler.rfile = io.BytesIO(bad)
        replies.clear()
        Handler._do_POST(handler)
        self.assertEqual(replies[0][0], 400)
        self.assertEqual(calls, [{"capability": "signed-token"}])

    def test_legacy_ntfy_test_is_token_gated_and_has_no_client_payload(self):
        handler = self.handler("/api/legacy-ntfy/test")
        handler.connection = SimpleNamespace(settimeout=lambda _seconds: None)
        calls = []
        handler.eng = SimpleNamespace(
            cfg={"act_token": "secret"},
            legacy_ntfy_test=lambda: calls.append(True) or {"ok": True, "queued": True})
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        replies = []
        handler.reply = lambda code, ctype, data: replies.append((code, json.loads(data)))
        Handler._do_POST(handler)
        self.assertEqual(replies[0][0], 403)
        self.assertEqual(calls, [])

        handler.headers["X-Act-Token"] = "secret"
        handler.rfile = io.BytesIO(b"{}")
        replies.clear()
        Handler._do_POST(handler)
        self.assertEqual(replies, [(200, {"ok": True, "queued": True})])
        self.assertEqual(calls, [True])

    def test_push_subscription_post_never_logs_subscription_material(self):
        handler = self.handler("/api/push/subscription")
        handler.connection = SimpleNamespace(settimeout=lambda _seconds: None)
        captured = []
        handler.eng = SimpleNamespace(
            cfg={"act_token": "token"},
            push_subscription=lambda payload: captured.append(payload) or
                {"ok": True, "device": {"id": payload["device_id"]}})
        payload = json.dumps({"device_id": "secret-device-sentinel",
            "permission_state": "secret-permission-sentinel", "subscription": {
            "endpoint": "https://web.push.apple.com/private-endpoint",
            "keys": {"p256dh": "private-key", "auth": "private-auth"}}}).encode()
        handler.headers = {"X-Act-Token": "token", "Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        replies = []
        handler.reply = lambda code, ctype, body: replies.append(json.loads(body))
        audit = io.StringIO()
        with redirect_stderr(audit):
            Handler._do_POST(handler)
        self.assertTrue(replies[0]["ok"])
        self.assertEqual(captured[0]["subscription"]["keys"]["auth"], "private-auth")
        self.assertNotIn("private-endpoint", audit.getvalue())
        self.assertNotIn("private-key", audit.getvalue())
        self.assertNotIn("private-auth", audit.getvalue())
        self.assertNotIn("secret-device-sentinel", audit.getvalue())
        self.assertNotIn("secret-permission-sentinel", audit.getvalue())


if __name__ == "__main__":
    unittest.main()
