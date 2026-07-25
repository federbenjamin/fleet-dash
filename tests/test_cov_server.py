"""Coverage for server.py: routing tables, handlers, loops, and main().

Uses the same lightweight Handler harness as tests/test_server.py — a
``Handler.__new__(Handler)`` instance with a fake ``eng`` (SimpleNamespace /
MagicMock) and a captured ``reply`` — so nothing binds a socket or touches a
real ``~/.claude``.
"""
import gzip
import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr
from http.cookies import CookieError
from types import SimpleNamespace
from unittest import mock

import server
from server import Handler


class Break(Exception):
    """Sentinel to escape an intentionally infinite loop under test."""


def make_handler(path="/api/fleet", eng=None):
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.headers = {}
    handler._request_started = time.perf_counter()
    handler._request_route = path.split("?", 1)[0]
    handler.eng = eng
    handler._replies = []
    handler.reply = lambda code, ctype, body, **kw: handler._replies.append(
        (code, ctype, body, kw))
    return handler


class LoopTest(unittest.TestCase):
    def test_poll_loop_runs_scan_and_survives_scan_error(self):
        calls = []
        eng = SimpleNamespace(
            search=SimpleNamespace(ensure_process=lambda: calls.append("ensure")),
            scan=lambda: (_ for _ in ()).throw(RuntimeError("scan boom")),
            cfg={"poll_seconds": 0})
        audit = io.StringIO()
        with mock.patch.object(server.time, "sleep", side_effect=Break), \
             redirect_stderr(audit):
            with self.assertRaises(Break):
                server.poll_loop(eng)
        self.assertEqual(calls, ["ensure"])
        self.assertIn("poll error", audit.getvalue())

    def test_poll_loop_without_search_index(self):
        calls = []
        eng = SimpleNamespace(scan=lambda: calls.append("scan"),
                              cfg={"poll_seconds": 0})
        with mock.patch.object(server.time, "sleep", side_effect=Break):
            with self.assertRaises(Break):
                server.poll_loop(eng)
        self.assertEqual(calls, ["scan"])

    def test_outbox_loop_success_and_error(self):
        for run in (lambda: None,
                    lambda: (_ for _ in ()).throw(RuntimeError("outbox boom"))):
            eng = SimpleNamespace(run_outbox=run)
            audit = io.StringIO()
            with mock.patch.object(server.time, "sleep", side_effect=Break), \
                 redirect_stderr(audit):
                with self.assertRaises(Break):
                    server.outbox_loop(eng)


class DiagnosticsTest(unittest.TestCase):
    def test_diagnostics_aggregates_route_metrics_and_memory(self):
        Handler.route_metrics.clear()
        metrics = Handler.route_metrics["/api/fleet"]
        metrics["elapsed_ms"].extend([1.0, 2.0, 3.0])
        metrics["payload_bytes"].extend([10, 20, 30])
        metrics["statuses"].append(200)
        out = Handler.diagnostics()
        self.assertEqual(out["routes"]["/api/fleet"]["count"], 3)
        self.assertEqual(out["routes"]["/api/fleet"]["last_status"], 200)
        self.assertIn("peak_rss_bytes", out["memory"])
        Handler.route_metrics.clear()

    def test_diagnostics_handles_ps_failure_and_non_darwin(self):
        with mock.patch.object(server.subprocess, "run",
                               side_effect=OSError("no ps")), \
             mock.patch.object(server.sys, "platform", "linux"):
            out = Handler.diagnostics()
        self.assertIsNone(out["memory"]["rss_bytes"])


class TokenOkTest(unittest.TestCase):
    def test_empty_configured_token_is_never_ok(self):
        handler = make_handler(eng=SimpleNamespace(cfg={"act_token": ""}))
        self.assertFalse(handler.token_ok())

    def test_header_token_matches(self):
        handler = make_handler(eng=SimpleNamespace(cfg={"act_token": "t"}))
        handler.headers = {"X-Act-Token": "t"}
        self.assertTrue(handler.token_ok())

    def test_production_cookie_token_matches(self):
        handler = make_handler(eng=SimpleNamespace(cfg={"act_token": "t"}))
        handler.headers = {"Cookie": "act_token_production=t"}
        self.assertTrue(handler.token_ok())

    def test_staging_cookie_token_matches(self):
        handler = make_handler(eng=SimpleNamespace(
            cfg={"act_token": "t", "instance_mode": "staging"}))
        handler.headers = {"Cookie": "act_token_staging=t"}
        self.assertTrue(handler.token_ok())

    def test_cookie_error_is_rejected(self):
        handler = make_handler(eng=SimpleNamespace(cfg={"act_token": "t"}))
        handler.headers = {"Cookie": "whatever"}
        with mock.patch.object(server, "SimpleCookie", side_effect=CookieError):
            self.assertFalse(handler.token_ok())


class ProjectSearchFileIdsTest(unittest.TestCase):
    def test_projects_selectors_and_ignores_non_dict(self):
        handler = make_handler(eng=SimpleNamespace(
            file_selector_for_path=lambda sid, path:
                f"fid:{sid}" if path == "/keep" else None))
        self.assertEqual(handler.project_search_file_ids("scalar"), "scalar")
        out = handler.project_search_file_ids({
            "results": [{"session_id": "s1", "artifact_path": "/keep"},
                        {"session_id": "s1"},  # no artifact_path
                        "bad-item"],
            "source": {"session_id": "s2", "artifact_path": "/miss"},
            "messages": [{"artifact_path": "/keep"}]})
        self.assertEqual(out["results"][0]["file_id"], "fid:s1")
        self.assertNotIn("file_id", out["source"])
        # message inherits source_sid fallback
        self.assertEqual(out["messages"][0]["file_id"], "fid:s2")


class PostRoutingTest(unittest.TestCase):
    def test_unknown_post_route_is_404(self):
        handler = make_handler("/api/nope")
        Handler._do_POST(handler)
        self.assertEqual(handler._replies[0][0], 404)

    def test_do_post_swallows_broken_pipe(self):
        handler = make_handler("/api/fleet")
        handler.begin_request = lambda: None
        handler._do_POST = lambda: (_ for _ in ()).throw(BrokenPipeError())
        self.assertIsNone(Handler.do_POST(handler))

    def test_token_route_rejects_bad_json(self):
        handler = make_handler("/api/act",
            eng=SimpleNamespace(cfg={"act_token": "t"}))
        handler.headers = {"X-Act-Token": "t", "Content-Length": "5"}
        handler.connection = SimpleNamespace(settimeout=lambda s: None)
        handler.rfile = io.BytesIO(b"[1,2]")  # valid json, not a dict
        audit = io.StringIO()
        with redirect_stderr(audit):
            Handler._do_POST(handler)
        self.assertEqual(handler._replies[0][0], 400)


class CapabilityActionHeaderTest(unittest.TestCase):
    def test_wrong_content_type_is_rejected(self):
        handler = make_handler("/api/push/capability-action")
        handler.headers = {"Content-Type": "text/plain", "Content-Length": "10"}
        Handler.post_capability_action(handler)
        self.assertEqual(handler._replies[0][0], 400)

    def test_bad_content_length_is_rejected(self):
        handler = make_handler("/api/push/capability-action")
        handler.headers = {"Content-Type": "application/json", "Content-Length": "0"}
        handler.connection = SimpleNamespace(settimeout=lambda s: None)
        Handler.post_capability_action(handler)
        self.assertEqual(handler._replies[0][0], 400)


class UploadImageErrorTest(unittest.TestCase):
    def base(self):
        handler = make_handler(
            "/api/upload-image?sid=s&id=i&name=p.png",
            eng=SimpleNamespace(cfg={"act_token": "t"}))
        handler.headers = {"X-Act-Token": "t"}
        handler.connection = SimpleNamespace(settimeout=lambda s: None)
        return handler

    def test_bad_content_length_is_413(self):
        handler = self.base()
        handler.headers["Content-Length"] = "not-a-number"
        Handler.post_upload_image(handler)
        self.assertEqual(handler._replies[0][0], 413)

    def test_incomplete_body_is_400(self):
        handler = self.base()
        handler.headers["Content-Length"] = "10"
        handler.rfile = io.BytesIO(b"short")
        Handler.post_upload_image(handler)
        self.assertEqual(handler._replies[0][0], 400)


class SimplePostHandlerTest(unittest.TestCase):
    def test_notification_policy_conflict_status(self):
        handler = make_handler("/api/notification-policy", eng=SimpleNamespace(
            notification_policy_update=lambda a: {"ok": False}))
        Handler.post_notification_policy(handler, {})
        self.assertEqual(handler._replies[0][0], 409)

    def test_push_device_settings_and_test(self):
        handler = make_handler(eng=SimpleNamespace(
            push_device_settings=lambda a: {"ok": True, "device": {}},
            push_test=lambda a: {"ok": True}))
        Handler.post_push_device_settings(handler, {"device_id": "d"})
        Handler.post_push_test(handler, {"device_id": "d"})
        self.assertEqual([r[0] for r in handler._replies], [200, 200])

    def test_legacy_ntfy_conflict_status(self):
        handler = make_handler(eng=SimpleNamespace(
            legacy_ntfy_test=lambda: {"ok": False}))
        Handler.post_legacy_ntfy_test(handler, {})
        self.assertEqual(handler._replies[0][0], 409)

    def test_search_rebuild_present_absent_and_error(self):
        audit = io.StringIO()
        with redirect_stderr(audit):
            handler = make_handler(eng=SimpleNamespace(
                search=SimpleNamespace(rebuild=lambda: {"ok": True})))
            Handler.post_search_rebuild(handler, {})
            self.assertTrue(json.loads(handler._replies[0][2])["ok"])

            handler = make_handler(eng=SimpleNamespace(search=None))
            Handler.post_search_rebuild(handler, {})
            self.assertFalse(json.loads(handler._replies[0][2])["ok"])

            handler = make_handler(eng=SimpleNamespace(
                search=SimpleNamespace(
                    rebuild=lambda: (_ for _ in ()).throw(RuntimeError("x")))))
            Handler.post_search_rebuild(handler, {})
            self.assertFalse(json.loads(handler._replies[0][2])["ok"])

    def test_post_act_audit_omits_bodies_and_logs_failure(self):
        handler = make_handler(eng=SimpleNamespace(
            act=lambda a: {"ok": False, "error": "no"}))
        audit = io.StringIO()
        with redirect_stderr(audit):
            Handler.post_act(handler, {"type": "text", "session_id": "s",
                "text": "secret-body", "message": "m", "preview": "p", "body": "b"})
        log = audit.getvalue()
        self.assertIn("chars omitted", log)
        self.assertNotIn("secret-body", log)
        self.assertIn("act failed", log)

    def test_post_act_ping_is_not_audited(self):
        handler = make_handler(eng=SimpleNamespace(act=lambda a: {"ok": True}))
        audit = io.StringIO()
        with redirect_stderr(audit):
            Handler.post_act(handler, {"type": "ping"})
        self.assertNotIn("act:", audit.getvalue())


class GetRoutingTest(unittest.TestCase):
    def test_do_get_swallows_broken_pipe(self):
        handler = make_handler("/api/fleet")
        handler.begin_request = lambda: None
        handler._do_GET = lambda: (_ for _ in ()).throw(ConnectionResetError())
        self.assertIsNone(Handler.do_GET(handler))

    def test_token_text_route_denies_without_token(self):
        handler = make_handler("/api/file?sid=s&fid=f",
            eng=SimpleNamespace(cfg={"act_token": "t"}))
        Handler._do_GET(handler)
        self.assertEqual(handler._replies[0][0], 403)
        self.assertEqual(handler._replies[0][1], "text/plain")

    def test_token_route_denies_without_token(self):
        handler = make_handler("/api/commands?sid=s",
            eng=SimpleNamespace(cfg={"act_token": "t"}))
        Handler._do_GET(handler)
        self.assertEqual(handler._replies[0][0], 403)

    def test_unknown_get_route_is_404(self):
        handler = make_handler("/api/nope")
        Handler._do_GET(handler)
        self.assertEqual(handler._replies[0][0], 404)

    def test_index_and_static_routes_dispatch(self):
        served = []
        handler = make_handler("/")
        handler.get_index = lambda: served.append("index")
        Handler._do_GET(handler)
        handler = make_handler("/index.html")
        handler.get_index = lambda: served.append("index")
        Handler._do_GET(handler)
        handler = make_handler("/static/fleet.css")
        handler.get_static = lambda route: served.append(("static", route))
        Handler._do_GET(handler)
        self.assertEqual(served, ["index", "index", ("static", "/static/fleet.css")])


class GetHandlerTest(unittest.TestCase):
    def call(self, method, path, **eng_attrs):
        handler = make_handler(path, eng=SimpleNamespace(**eng_attrs))
        getattr(Handler, method)(handler)
        return handler._replies[0]

    def test_get_handoff_repo_outbox(self):
        self.assertEqual(self.call("get_handoff", "/api/handoff?sid=s&provider=claude",
            handoff_preview=lambda sid, prov: {"ok": True})[0], 200)
        self.assertEqual(self.call("get_repo",
            "/api/repo?root=/r&worktree=/w&force=1",
            repository_snapshot=lambda root, wt, force: {"ok": force})[0], 200)
        self.assertEqual(self.call("get_outbox", "/api/outbox?state=pending",
            outbox_snapshot=lambda state, cursor, limit: {"ok": True})[0], 200)

    def test_get_notifications_parses_lists(self):
        captured = {}
        handler = make_handler(
            "/api/notifications?device=d&cursor=1&limit=5&state=a,b&kind=x,y&id=e",
            eng=SimpleNamespace(notifications_snapshot=lambda *a:
                captured.setdefault("args", a) or {"ok": True}))
        Handler.get_notifications(handler)
        self.assertEqual(captured["args"], ("d", "1", "5", ["a", "b"], ["x", "y"], "e"))

    def test_get_notification_policy_ok_and_503(self):
        self.assertEqual(self.call("get_notification_policy", "/api/notification-policy",
            notification_policy_snapshot=lambda: {"ok": True})[0], 200)
        self.assertEqual(self.call("get_notification_policy", "/api/notification-policy",
            notification_policy_snapshot=lambda: {"ok": False})[0], 503)

    def test_get_push_config_and_devices_use_device_header(self):
        handler = make_handler("/api/push/config", eng=SimpleNamespace(
            push_config=lambda d: {"device": d}))
        handler.headers = {"X-Fleet-Device-ID": "hdr-device"}
        Handler.get_push_config(handler)
        self.assertEqual(json.loads(handler._replies[0][2])["device"], "hdr-device")

        handler = make_handler("/api/push/devices?device=q", eng=SimpleNamespace(
            push_devices=lambda d: {"device": d}))
        Handler.get_push_devices(handler)
        self.assertEqual(json.loads(handler._replies[0][2])["device"], "q")

    def test_context_routes_paginate(self):
        msgs = {"ok": True, "messages": [{"text": "1"}]}
        self.assertEqual(self.call("get_context", "/api/context?sid=s",
            session_context=lambda sid: dict(msgs))[0], 200)
        self.assertEqual(self.call("get_closed_context", "/api/closed_context?sid=s",
            closed_context=lambda sid: dict(msgs))[0], 200)
        self.assertEqual(self.call("get_agent_context",
            "/api/agent_context?sid=s&aid=a",
            agent_context=lambda sid, aid: dict(msgs))[0], 200)

    def test_get_screen_is_token_gated(self):
        status, _, body, _kw = self.call(
            "get_screen", "/api/screen?sid=s",
            session_screen=lambda sid: {"ok": True, "lines": ["x"], "session_id": sid})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["lines"], ["x"])
        self.assertEqual(Handler.GET_ROUTES["/api/screen"], ("token", "get_screen"))

    def test_get_act_receipt_is_token_gated(self):
        status, _, body, _kw = self.call(
            "get_act_receipt", "/api/act-receipt?rid=act-abcdef123456",
            act_receipt=lambda rid: {"ok": True, "found": True,
                                     "receipt": {"client_request_id": rid,
                                                 "state": "delivered"}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["receipt"]["state"], "delivered")
        self.assertEqual(Handler.GET_ROUTES["/api/act-receipt"],
                         ("token", "get_act_receipt"))

    def test_get_commands_insights_briefing_budgets(self):
        self.assertEqual(self.call("get_commands", "/api/commands?sid=s",
            commands=lambda sid: {"ok": True})[0], 200)
        self.assertEqual(self.call("get_insights", "/api/insights?days=3",
            insights=lambda days: {"days": days})[0], 200)
        # invalid days falls back to 7
        handler = make_handler("/api/insights?days=bad",
            eng=SimpleNamespace(insights=lambda days: {"days": days}))
        Handler.get_insights(handler)
        self.assertEqual(json.loads(handler._replies[0][2])["days"], 7)
        self.assertEqual(self.call("get_briefing", "/api/briefing?device=d",
            briefing_snapshot=lambda d, c, l: {"ok": True})[0], 200)
        self.assertEqual(self.call("get_budgets",
            "/api/budgets?provider=claude&cwd=/r",
            budgets_snapshot=lambda spawn: {"spawn": spawn})[0], 200)

    def test_workstreams_evidence_history(self):
        self.assertEqual(self.call("get_workstreams", "/api/workstreams",
            workstreams_snapshot=lambda: {"ok": True})[0], 200)
        self.assertEqual(self.call("get_evidence", "/api/evidence?sid=s",
            state_history=lambda sid, cursor, limit: {"ok": True})[0], 200)
        self.assertEqual(self.call("get_history", "/api/history?q=x",
            history_snapshot=lambda *a: {"ok": True})[0], 200)

    def test_get_file_success_and_missing(self):
        handler = make_handler("/api/file?sid=s&fid=f", eng=SimpleNamespace(
            file_content=lambda sid, fid: ("text/plain", b"body", None)))
        Handler.get_file(handler)
        code, ctype, body, kw = handler._replies[0]
        self.assertEqual((code, body), (200, b"body"))
        self.assertEqual(kw["extra_headers"], {"X-Content-Type-Options": "nosniff"})

        handler = make_handler("/api/file?sid=s&fid=f", eng=SimpleNamespace(
            file_content=lambda sid, fid: (None, None, "missing")))
        Handler.get_file(handler)
        self.assertEqual(handler._replies[0][0], 404)


class SearchGetTest(unittest.TestCase):
    def test_search_index_absent_returns_503(self):
        for method in ("get_search", "get_search_status", "get_search_context"):
            handler = make_handler(eng=SimpleNamespace(search=None))
            getattr(Handler, method)(handler)
            self.assertEqual(handler._replies[0][0], 503)

    def test_search_routes_success_and_error(self):
        search = SimpleNamespace(
            search=lambda **kw: {"ok": True, "results": []},
            status=lambda: {"ok": True},
            context=lambda cid, radius: {"ok": True, "messages": []})
        handler = make_handler("/api/search?q=hi", eng=SimpleNamespace(
            search=search, file_selector_for_path=lambda sid, p: None))
        Handler.get_search(handler)
        self.assertTrue(json.loads(handler._replies[0][2])["ok"])

        handler = make_handler("/api/search/status", eng=SimpleNamespace(search=search))
        Handler.get_search_status(handler)
        self.assertTrue(json.loads(handler._replies[0][2])["ok"])

        handler = make_handler("/api/search/context?id=c", eng=SimpleNamespace(
            search=search, file_selector_for_path=lambda sid, p: None))
        Handler.get_search_context(handler)
        self.assertTrue(json.loads(handler._replies[0][2])["ok"])

    def test_search_routes_swallow_exceptions(self):
        def boom(**kw):
            raise RuntimeError("index down")
        search = SimpleNamespace(search=boom,
            status=lambda: (_ for _ in ()).throw(RuntimeError("x")),
            context=lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
        audit = io.StringIO()
        with redirect_stderr(audit):
            handler = make_handler("/api/search?q=hi", eng=SimpleNamespace(search=search))
            Handler.get_search(handler)
            handler2 = make_handler("/api/search/status",
                                    eng=SimpleNamespace(search=search))
            Handler.get_search_status(handler2)
            handler3 = make_handler("/api/search/context?id=c",
                                    eng=SimpleNamespace(search=search))
            Handler.get_search_context(handler3)
        for handler in (handler, handler2, handler3):
            self.assertFalse(json.loads(handler._replies[0][2])["ok"])


class DiagnosticsRouteTest(unittest.TestCase):
    def test_get_diagnostics_merges_engine_search_push_legacy(self):
        import threading as _t
        eng = SimpleNamespace(
            lock=_t.Lock(),
            snapshot_cache={"diagnostics": {"scan_ms": 1}},
            search=SimpleNamespace(status=lambda: {"ok": True}),
            push_diagnostics=lambda: {"delivery": "ready"},
            legacy_ntfy_diagnostics=lambda: {"enabled": False})
        handler = make_handler("/api/diagnostics", eng=eng)
        Handler.get_diagnostics(handler)
        out = json.loads(handler._replies[0][2])
        self.assertEqual(out["engine"], {"scan_ms": 1})
        self.assertTrue(out["search"]["ok"])
        self.assertEqual(out["web_push"]["delivery"], "ready")
        self.assertIn("legacy_ntfy", out)

    def test_get_diagnostics_survives_search_status_error(self):
        import threading as _t
        eng = SimpleNamespace(
            lock=_t.Lock(), snapshot_cache={},
            search=SimpleNamespace(
                status=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        handler = make_handler("/api/diagnostics", eng=eng)
        Handler.get_diagnostics(handler)
        out = json.loads(handler._replies[0][2])
        self.assertFalse(out["search"]["ok"])


class GetFleetTest(unittest.TestCase):
    def test_get_fleet_projects_closed_and_page_v(self):
        import threading as _t
        eng = SimpleNamespace(lock=_t.Lock(), snapshot_cache={
            "closed": [{"session_id": "a", "pinned": True},
                       {"session_id": "b"}, {}]})
        handler = make_handler("/api/fleet", eng=eng)
        Handler.get_fleet(handler)
        snap = json.loads(handler._replies[0][2])
        self.assertEqual(snap["closed_total"], 3)
        self.assertEqual(snap["closed_ids"], ["a", "b"])
        self.assertEqual([c["session_id"] for c in snap["closed"]], ["a"])
        self.assertIn("page_v", snap)

    def test_get_fleet_page_v_missing_asset_is_tolerated(self):
        import threading as _t
        eng = SimpleNamespace(lock=_t.Lock(), snapshot_cache={"closed": []})
        handler = make_handler("/api/fleet", eng=eng)
        with mock.patch.object(server.os.path, "getmtime",
                               side_effect=OSError("gone")):
            Handler.get_fleet(handler)
        snap = json.loads(handler._replies[0][2])
        self.assertNotIn("page_v", snap)


class StaticAndIndexTest(unittest.TestCase):
    def test_get_static_serves_real_css(self):
        handler = make_handler("/static/fleet.css",
            eng=SimpleNamespace(cfg={"instance_mode": "production"}))
        Handler.get_static(handler, "/static/fleet.css")
        code, ctype, body, kw = handler._replies[0]
        self.assertEqual(code, 200)
        self.assertIn("text/css", ctype)

    def test_get_static_rewrites_staging_manifest(self):
        handler = make_handler("/static/manifest.webmanifest",
            eng=SimpleNamespace(cfg={"instance_mode": "staging"}))
        Handler.get_static(handler, "/static/manifest.webmanifest")
        body = json.loads(handler._replies[0][2])
        self.assertEqual(body["name"], "Fleet Staging")
        self.assertEqual(body["short_name"], "Staging")

    def test_get_static_missing_file_is_404(self):
        handler = make_handler("/static/fleet.css",
            eng=SimpleNamespace(cfg={}))
        with mock.patch.object(server, "APP_ROOT", "/no/such/dir"):
            Handler.get_static(handler, "/static/fleet.css")
        self.assertEqual(handler._replies[0][0], 404)

    def test_get_index_success_and_missing(self):
        handler = make_handler("/")
        Handler.get_index(handler)
        self.assertEqual(handler._replies[0][0], 200)

        handler = make_handler("/")
        with mock.patch.object(server, "APP_ROOT", "/no/such/dir"):
            Handler.get_index(handler)
        self.assertEqual(handler._replies[0][0], 500)


class ErrorReplyAndReplyTest(unittest.TestCase):
    def test_error_reply_api_vs_page(self):
        handler = make_handler("/api/fleet")
        handler._request_route = "/api/fleet"
        Handler.error_reply(handler, "secret detail")
        self.assertEqual(handler._replies[0][0], 500)
        self.assertEqual(handler._replies[0][1], "application/json")

        handler = make_handler("/")
        handler._request_route = "/"
        Handler.error_reply(handler, "boom")
        self.assertEqual(handler._replies[0][1], "text/plain; charset=utf-8")

    def test_reply_writes_headers_metrics_and_extra_headers(self):
        Handler.route_metrics.clear()
        handler = Handler.__new__(Handler)
        handler.path = "/api/fleet"
        handler._request_route = "/api/fleet"
        handler._request_started = time.perf_counter()
        sent = []
        handler.send_response = lambda code: sent.append(("status", code))
        handler.send_header = lambda k, v: sent.append((k, v))
        handler.end_headers = lambda: sent.append(("end",))
        handler.wfile = SimpleNamespace(write=lambda b: sent.append(("body", b)))
        Handler.reply(handler, 200, "application/json", b"{}",
                      extra_headers={"X-Extra": "1"})
        self.assertIn(("X-Extra", "1"), sent)
        self.assertIn(("body", b"{}"), sent)
        self.assertEqual(len(Handler.route_metrics["/api/fleet"]["elapsed_ms"]), 1)
        Handler.route_metrics.clear()

    def compress_probe(self, ctype="application/json", size=4000,
                       accept="gzip, deflate", extra_headers=None):
        """Run reply() against a recording socket and return (headers, body)."""
        Handler.route_metrics.clear()
        handler = Handler.__new__(Handler)
        handler.path = handler._request_route = "/api/fleet"
        handler._request_started = time.perf_counter()
        handler.headers = {"Accept-Encoding": accept} if accept is not None else {}
        sent, written = {}, []
        handler.send_response = lambda code: None
        handler.send_header = lambda key, value: sent.__setitem__(key, value)
        handler.end_headers = lambda: None
        handler.wfile = SimpleNamespace(write=written.append)
        body = json.dumps({"pad": "x" * size}).encode()
        Handler.reply(handler, 200, ctype, body, extra_headers=extra_headers)
        return sent, written[0], body

    def test_large_json_is_gzipped_and_metrics_stay_uncompressed(self):
        sent, wire, body = self.compress_probe()
        self.assertEqual(sent["Content-Encoding"], "gzip")
        self.assertEqual(sent["Vary"], "Accept-Encoding")
        self.assertEqual(gzip.decompress(wire), body)
        self.assertLess(len(wire), len(body))
        self.assertEqual(sent["Content-Length"], str(len(wire)))
        # the app's own size, so payload metrics keep meaning what they meant
        self.assertEqual(sent["X-Fleet-Payload-Bytes"], str(len(body)))
        self.assertEqual(Handler.route_metrics["/api/fleet"]["payload_bytes"][0],
                         len(body))
        Handler.route_metrics.clear()

    def test_compression_is_declined_where_it_would_not_help(self):
        for label, kwargs in (
                ("no Accept-Encoding", {"accept": None}),
                ("client refuses gzip", {"accept": "gzip;q=0, identity"}),
                ("other codec only", {"accept": "br, deflate"}),
                ("below one MTU", {"size": 40}),
                ("already-compressed bytes", {"ctype": "image/png"}),
                ("caller set its own encoding",
                 {"extra_headers": {"content-encoding": "identity"}})):
            with self.subTest(label):
                sent, wire, body = self.compress_probe(**kwargs)
                self.assertNotEqual(sent.get("Content-Encoding"), "gzip")
                self.assertEqual(wire, body)
        Handler.route_metrics.clear()

    def test_gzip_quality_and_malformed_accept_encoding(self):
        for accept, expected in (("gzip;q=0.5", True), ("GZIP", True),
                                 ("gzip;q=bogus", True), ("gzip;q=0.0", False),
                                 ("identity", False), ("", False)):
            with self.subTest(accept):
                handler = Handler.__new__(Handler)
                handler.headers = {"Accept-Encoding": accept}
                self.assertEqual(Handler._accepts_gzip(handler), expected)

    def test_a_compression_failure_never_fails_the_response(self):
        with mock.patch.object(server.gzip, "compress",
                               side_effect=RuntimeError("no zlib")):
            sent, wire, body = self.compress_probe()
        self.assertEqual(wire, body)
        self.assertNotIn("Content-Encoding", sent)
        Handler.route_metrics.clear()

    def test_log_message_is_silent(self):
        handler = Handler.__new__(Handler)
        self.assertIsNone(Handler.log_message(handler, "%s", "x"))


class MiscHandlerTest(unittest.TestCase):
    def test_begin_request_captures_route_without_query(self):
        handler = Handler.__new__(Handler)
        handler.path = "/api/context?sid=s&limit=5"
        Handler.begin_request(handler)
        self.assertEqual(handler._request_route, "/api/context")
        self.assertIsInstance(handler._request_started, float)

    def test_diagnostics_percentile_handles_empty_route(self):
        Handler.route_metrics.clear()
        # Touching the defaultdict key materializes empty metric deques.
        _ = Handler.route_metrics["/api/empty"]
        out = Handler.diagnostics()
        self.assertEqual(out["routes"]["/api/empty"]["p50_ms"], 0.0)
        Handler.route_metrics.clear()

    def test_paginate_context_passes_through_unpaginatable(self):
        handler = make_handler("/api/context?sid=s")
        self.assertEqual(handler.paginate_context({"ok": False}), {"ok": False})
        self.assertEqual(handler.paginate_context("scalar"), "scalar")


class MainTest(unittest.TestCase):
    def _run_main(self, cfg):
        with mock.patch.object(server, "load_config", return_value=cfg), \
             mock.patch.object(server, "Engine") as engine_cls, \
             mock.patch.object(server, "SearchIndex") as search_cls, \
             mock.patch.object(server, "ThreadingHTTPServer") as server_cls, \
             mock.patch.object(server.threading, "Thread") as thread_cls:
            server_cls.return_value.serve_forever = lambda: None
            thread_cls.return_value.start = lambda: None
            server.main()
        return engine_cls.return_value, search_cls

    def test_main_starts_search_when_enabled(self):
        eng, search_cls = self._run_main({
            "search_enabled": True, "bind": "127.0.0.1", "port": 0})
        eng.start_web_push.assert_called_once()
        search_cls.assert_called_once()

    def test_main_skips_search_when_disabled(self):
        eng, search_cls = self._run_main({
            "search_enabled": False, "bind": "127.0.0.1", "port": 0})
        search_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
