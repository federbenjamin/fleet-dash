#!/usr/bin/env python3
"""fleet-dash server: background poll loop + tiny HTTP API.

GET /            dashboard.html (re-read per request, edit without restart)
GET /api/fleet   latest fleet snapshot JSON
GET /api/workstreams lazy repository/project rollup
GET /api/evidence durable session placement history
GET /api/handoff authenticated editable provider-handoff preview
GET /api/repo authenticated repository outcome/action preview
GET /api/outbox authenticated scheduled-message list and audit trail
GET /api/briefing deterministic operational briefing and per-device cursor
GET /api/notifications authenticated canonical notification event stream
GET /api/push/config authenticated PWA/Web Push capability and current-device health
GET /api/push/devices authenticated redacted registered-device list
GET /api/budgets measured budget state and forecasts
GET /api/history paginated closed-session metadata
GET /api/diagnostics authenticated latency, payload, and memory measurements
"""
from collections import defaultdict, deque
import gzip, hashlib, json, os, resource, subprocess, sys, time, threading, secrets
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fleetdash.config import load_config  # noqa: E402
from fleetdash.engine import Engine  # noqa: E402
from fleetdash.paths import BASE, PROJECTS  # noqa: E402
from fleetdash.search_index import SearchIndex  # noqa: E402

APP_ROOT = os.path.dirname(os.path.abspath(__file__))

APP_MODULES = [
    "main.js", "state-store.js", "nav.js", "search.js", "ui-utils.js",
    "outbox.js", "push.js", "notifications.js", "context.js",
    "viewer-handoff.js", "overlays.js", "workspace.js", "cards.js",
    "settings-actions.js", "history-spawn.js", "insights.js",
]

# Self-hosted Console webfonts (design-system): immutable binaries, long cache.
APP_FONTS = [
    "SpaceGrotesk-var.woff2", "IBMPlexMono-Regular.woff2",
    "IBMPlexMono-Medium.woff2", "IBMPlexMono-SemiBold.woff2",
]

STATIC_FILES = {
    "/static/fleet.css": ("static/fleet.css", "text/css; charset=utf-8", "no-cache", {}),
    **{f"/static/fonts/{name}": (f"static/fonts/{name}",
        "font/woff2", "public, max-age=31536000, immutable", {}) for name in APP_FONTS},
    **{f"/static/js/{name}": (f"static/js/{name}",
        "text/javascript; charset=utf-8", "no-cache", {}) for name in APP_MODULES},
    "/static/manifest.webmanifest": ("static/manifest.webmanifest",
        "application/manifest+json; charset=utf-8", "no-cache", {}),
    "/static/offline.html": ("static/offline.html", "text/html; charset=utf-8", "no-cache", {}),
    "/static/icons/fleet.svg": ("static/icons/fleet.svg", "image/svg+xml", "public, max-age=86400", {}),
    "/static/icons/fleet-192.png": ("static/icons/fleet-192.png", "image/png",
        "public, max-age=86400", {}),
    "/static/icons/fleet-512.png": ("static/icons/fleet-512.png", "image/png",
        "public, max-age=86400", {}),
    "/static/icons/fleet-maskable-512.png": ("static/icons/fleet-maskable-512.png", "image/png",
        "public, max-age=86400", {}),
    "/sw.js": ("static/sw.js", "text/javascript; charset=utf-8", "no-cache",
               {"Service-Worker-Allowed": "/"}),
}


def poll_loop(eng):
    while True:
        try:
            search = getattr(eng, "search", None)
            if search:
                search.ensure_process()
            eng.scan()
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr, flush=True)
        time.sleep(eng.cfg["poll_seconds"])


def outbox_loop(eng):
    while True:
        try:
            eng.run_outbox()
        except Exception as exc:
            print(f"outbox scheduler error: {exc}", file=sys.stderr, flush=True)
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    eng = None
    metrics_lock = threading.Lock()
    route_metrics = defaultdict(lambda: {"elapsed_ms": deque(maxlen=480),
                                         "payload_bytes": deque(maxlen=480),
                                         "statuses": deque(maxlen=480)})

    def begin_request(self):
        self._request_started = time.perf_counter()
        self._request_route = self.path.split("?", 1)[0]

    @classmethod
    def diagnostics(cls):
        def percentile(values, quantile):
            ordered = sorted(values)
            if not ordered:
                return 0.0
            index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
            return round(ordered[index], 3)

        with cls.metrics_lock:
            routes = {
                route: {
                    "count": len(metrics["elapsed_ms"]),
                    "p50_ms": percentile(metrics["elapsed_ms"], .5),
                    "p95_ms": percentile(metrics["elapsed_ms"], .95),
                    "payload_p50_bytes": int(percentile(metrics["payload_bytes"], .5)),
                    "payload_p95_bytes": int(percentile(metrics["payload_bytes"], .95)),
                    "last_status": (metrics["statuses"][-1]
                                    if metrics["statuses"] else None),
                }
                for route, metrics in cls.route_metrics.items()
            }
        current_rss = None
        try:
            proc = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                                  check=True, capture_output=True, text=True, timeout=2)
            current_rss = int(proc.stdout.strip()) * 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            peak_rss *= 1024
        return {"routes": routes, "memory": {"rss_bytes": current_rss,
                                               "peak_rss_bytes": int(peak_rss)}}

    def token_ok(self):
        want = self.eng.cfg.get("act_token", "")
        if not want:
            return False
        supplied = self.headers.get("X-Act-Token") or ""
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            mode = ("staging" if self.eng.cfg.get("instance_mode") == "staging"
                    else "production")
            cookie_name = f"act_token_{mode}"
            if not supplied and cookies.get(cookie_name):
                supplied = cookies[cookie_name].value
        except CookieError:
            return False
        return secrets.compare_digest(str(want), str(supplied))

    def project_search_file_ids(self, out):
        """Remove indexed local paths and expose only validated session file IDs."""
        if not isinstance(out, dict):
            return out

        def project(item, fallback_sid=""):
            if not isinstance(item, dict):
                return
            raw_path = item.pop("artifact_path", None)
            if not raw_path:
                return
            sid = str(item.get("session_id") or fallback_sid or "")
            selector = self.eng.file_selector_for_path(sid, raw_path) if sid else None
            if selector:
                item["file_id"] = selector

        for item in out.get("results") or []:
            project(item)
        source = out.get("source") or {}
        source_sid = str(source.get("session_id") or "") if isinstance(source, dict) else ""
        project(source)
        for item in out.get("messages") or []:
            project(item, source_sid)
        return out

    # ------------------------------------------------------------- POST routes
    # route -> (auth, handler name). auth "token" requires the act token and a
    # JSON body dict is parsed for the handler; "self" routes own their whole
    # request cycle including any credential decision (capability actions are
    # deliberately token-less, uploads read a raw body after the token check).
    POST_ROUTES = {
        "/api/push/capability-action": ("self", "post_capability_action"),
        "/api/upload-image": ("self", "post_upload_image"),
        "/api/act": ("token", "post_act"),
        "/api/settings": ("token", "post_settings"),
        "/api/search/rebuild": ("token", "post_search_rebuild"),
        "/api/notifications/read": ("token", "post_notifications_read"),
        "/api/notifications/snooze": ("token", "post_notifications_snooze"),
        "/api/notifications/wake": ("token", "post_notifications_wake"),
        "/api/notifications/mute": ("token", "post_notifications_mute"),
        "/api/notifications/retry": ("token", "post_notifications_retry"),
        "/api/notification-policy": ("token", "post_notification_policy"),
        "/api/push/subscription": ("token", "post_push_subscription"),
        "/api/push/device-settings": ("token", "post_push_device_settings"),
        "/api/push/test": ("token", "post_push_test"),
        "/api/legacy-ntfy/test": ("token", "post_legacy_ntfy_test"),
    }

    def do_POST(self):
        self.begin_request()
        try:
            return self._do_POST()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return None
        except Exception as exc:
            print(f"POST {self._request_route} failed ({type(exc).__name__})",
                  file=sys.stderr, flush=True)
            return self.error_reply("request failed")

    def _do_POST(self):
        route = self.path.split("?", 1)[0]
        auth, handler = self.POST_ROUTES.get(route, (None, None))
        if handler is None:
            return self.reply(404, "text/plain", b"not found")
        if auth == "self":
            return getattr(self, handler)()
        if not self.token_ok():
            print(f"{route} denied: no/bad token (open the ?token= URL once on this device)",
                  file=sys.stderr, flush=True)
            return self.reply(403, "application/json",
                              b'{"ok": false, "error": "bad or missing act token"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > 65536:
                return self.reply(413, "application/json",
                                  b'{"ok": false, "error": "request too large"}')
            self.connection.settimeout(5)
            action = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(action, dict):
                raise ValueError("JSON body must be an object")
        except Exception:
            return self.reply(400, "application/json", b'{"ok": false, "error": "bad json"}')
        return getattr(self, handler)(action)

    def post_capability_action(self):
        # Deliberately credential-omitting: the HMAC capability inside the body
        # is the whole authorization (invariant 48). Never check the act token.
        if self.headers.get("Transfer-Encoding") or \
           self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() \
           != "application/json":
            return self.reply(400, "application/json",
                              b'{"ok": false, "error": "notification capability is unavailable"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 4096:
                raise ValueError("invalid capability body")
            self.connection.settimeout(5)
            action = json.loads(self.rfile.read(n))
            if (not isinstance(action, dict) or set(action) != {"capability"} or
                    not isinstance(action.get("capability"), str)):
                raise ValueError("invalid capability body")
        except Exception:
            return self.reply(400, "application/json",
                              b'{"ok": false, "error": "notification capability is unavailable"}')
        result = self.eng.push_capability_action(action)
        status = 200 if result.get("ok") else 409
        return self.reply(status, "application/json", json.dumps(result).encode())

    def post_upload_image(self):
        if not self.token_ok():
            print("/api/upload-image denied: no/bad token (open the ?token= URL once on this device)",
                  file=sys.stderr, flush=True)
            return self.reply(403, "application/json",
                              b'{"ok": false, "error": "bad or missing act token"}')
        if self.headers.get("Transfer-Encoding"):
            return self.reply(400, "application/json",
                              b'{"ok": false, "error": "chunked uploads are unsupported"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > 10 * 1024 * 1024:
            return self.reply(413, "application/json",
                              b'{"ok": false, "error": "image must be 10 MB or smaller"}')
        self.connection.settimeout(20)
        data = self.rfile.read(n)
        if len(data) != n:
            return self.reply(400, "application/json",
                              b'{"ok": false, "error": "incomplete image upload"}')
        result = self.eng.store_image_upload(
            self.query("sid"), self.query("id"), self.query("name"),
            self.headers.get("Content-Type", ""), data)
        status = 200 if result.get("ok") else 400
        return self.reply(status, "application/json", json.dumps(result).encode())

    def post_settings(self, action):
        result = self.eng.update_settings(action)
        audit = {"ok": bool(result.get("ok")), "field_count": min(len(action), 1000)}
        print(f"settings: {json.dumps(audit)[:200]}", file=sys.stderr, flush=True)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_notifications_read(self, action):
        result = self.eng.notifications_mark_read(action)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_notifications_snooze(self, action):
        result = self.eng.notifications_snooze(action)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_notifications_wake(self, action):
        result = self.eng.notifications_wake(action)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_notifications_mute(self, action):
        result = self.eng.notifications_mute(action)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_notifications_retry(self, action):
        result = self.eng.notifications_retry(action)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_notification_policy(self, action):
        result = self.eng.notification_policy_update(action)
        return self.reply(200 if result.get("ok") else 409, "application/json",
                          json.dumps(result).encode())

    def post_push_subscription(self, action):
        result = self.eng.push_subscription(action)
        device_ref = hashlib.sha256(
            str(action.get("device_id") or "").encode()).hexdigest()[:12]
        audit = {"ok": bool(result.get("ok")), "device_ref": device_ref,
                 "operation": ("forget" if action.get("forget") else
                               "remove" if action.get("remove") else "register")}
        print(f"push subscription: {json.dumps(audit)}", file=sys.stderr, flush=True)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_push_device_settings(self, action):
        result = self.eng.push_device_settings(action)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_push_test(self, action):
        result = self.eng.push_test(action)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_legacy_ntfy_test(self, action):
        result = self.eng.legacy_ntfy_test()
        status = 200 if result.get("ok") else 409
        return self.reply(status, "application/json", json.dumps(result).encode())

    def post_search_rebuild(self, action):
        search = getattr(self.eng, "search", None)
        try:
            result = (search.rebuild() if search else
                      {"ok": False, "error": "search index is unavailable"})
        except Exception as exc:
            print(f"search rebuild failed: {exc}", file=sys.stderr, flush=True)
            result = {"ok": False, "error": "search index is temporarily unavailable"}
        print("search: rebuild requested", file=sys.stderr, flush=True)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def post_act(self, action):
        audit = dict(action)
        if action.get("type") != "ping":
            # Keep the action/identity audit trail without persisting message or
            # handoff bodies in the daemon log. Provider transcripts own that text.
            for key in ("text", "preview", "message", "body"):
                if key in audit:
                    audit[key] = f"[{len(str(audit[key] or ''))} chars omitted]"
            print(f"act: {json.dumps(audit)[:500]}", file=sys.stderr, flush=True)
        result = self.eng.act(action)
        if not result.get("ok"):
            print(f"act failed: {json.dumps(audit)[:300]} -> {result.get('error')}",
                  file=sys.stderr, flush=True)
        return self.reply(200, "application/json", json.dumps(result).encode())

    def query(self, key):
        return (parse_qs(urlparse(self.path).query).get(key) or [""])[0]

    def paginate_context(self, out):
        if not isinstance(out, dict) or not out.get("ok"):
            return out
        # A projection that paged itself owns its own cursor. Claude sessions
        # page by transcript BYTE OFFSET so "load older" can leave the live ring
        # entirely; slicing that result by index here would truncate a page and
        # replace a meaningful cursor with a meaningless one.
        if out.get("paged"):
            return out
        messages = list(out.get("messages") or [])
        try:
            limit = max(1, min(100, int(self.query("limit") or 50)))
            cursor = len(messages) if self.query("cursor") == "" else int(self.query("cursor"))
            if cursor < 0 or cursor > len(messages):
                raise ValueError
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid conversation pagination"}
        start = max(0, cursor - limit)
        return {**out, "messages": messages[start:cursor], "message_total": len(messages),
                "next_cursor": start if start > 0 else None}

    # -------------------------------------------------------------- GET routes
    # route -> (auth, handler name). "open" serves without credentials (bounded
    # projections that expose no local paths); "token" requires the act token
    # and answers a JSON 403; "token-text" is /api/file's plain-text 403.
    GET_ROUTES = {
        "/api/fleet": ("open", "get_fleet"),
        "/api/context": ("open", "get_context"),
        "/api/closed_context": ("open", "get_closed_context"),
        "/api/agent_context": ("open", "get_agent_context"),
        "/api/insights": ("open", "get_insights"),
        "/api/briefing": ("open", "get_briefing"),
        "/api/budgets": ("open", "get_budgets"),
        "/api/workstreams": ("open", "get_workstreams"),
        "/api/evidence": ("open", "get_evidence"),
        "/api/history": ("open", "get_history"),
        "/api/file": ("token-text", "get_file"),
        "/api/screen": ("token", "get_screen"),
        "/api/tool-result": ("token", "get_tool_result"),
        "/api/prompt-options": ("token", "get_prompt_options"),
        "/api/act-receipt": ("token", "get_act_receipt"),
        "/api/commands": ("token", "get_commands"),
        "/api/search": ("token", "get_search"),
        "/api/search/status": ("token", "get_search_status"),
        "/api/search/context": ("token", "get_search_context"),
        "/api/handoff": ("token", "get_handoff"),
        "/api/repo": ("token", "get_repo"),
        "/api/outbox": ("token", "get_outbox"),
        "/api/notifications": ("token", "get_notifications"),
        "/api/notification-policy": ("token", "get_notification_policy"),
        "/api/push/config": ("token", "get_push_config"),
        "/api/push/devices": ("token", "get_push_devices"),
        "/api/diagnostics": ("token", "get_diagnostics"),
    }

    def do_GET(self):
        self.begin_request()
        try:
            return self._do_GET()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return None
        except Exception as exc:
            print(f"GET {self._request_route} failed ({type(exc).__name__})",
                  file=sys.stderr, flush=True)
            return self.error_reply("request failed")

    def _do_GET(self):
        route = self.path.split("?", 1)[0]
        auth, handler = self.GET_ROUTES.get(route, (None, None))
        if handler is not None:
            if auth == "token-text" and not self.token_ok():
                return self.reply(403, "text/plain",
                                  b"missing act token (open the ?token= URL once on this device)")
            if auth == "token" and not self.token_ok():
                return self.reply(403, "application/json",
                                  b'{"ok": false, "error": "bad or missing act token"}')
            return getattr(self, handler)()
        if route in STATIC_FILES:
            return self.get_static(route)
        if route == "/" or route.startswith("/index"):
            return self.get_index()
        return self.reply(404, "text/plain", b"not found")

    def get_handoff(self):
        out = self.eng.handoff_preview(self.query("sid"), self.query("provider"))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_repo(self):
        out = self.eng.repository_snapshot(
            self.query("root"), self.query("worktree"),
            self.query("force") in ("1", "true"))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_outbox(self):
        out = self.eng.outbox_snapshot(self.query("state"), self.query("cursor") or 0,
                                       self.query("limit") or 100)
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_notifications(self):
        states = [item for item in self.query("state").split(",") if item]
        kinds = [item for item in self.query("kind").split(",") if item]
        out = self.eng.notifications_snapshot(
            self.query("device") or "default", self.query("cursor") or None,
            self.query("limit") or 100, states, kinds, self.query("id") or None)
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_notification_policy(self):
        out = self.eng.notification_policy_snapshot()
        return self.reply(200 if out.get("ok") else 503, "application/json",
                          json.dumps(out).encode())

    def get_push_config(self):
        device = self.query("device") or self.headers.get("X-Fleet-Device-ID") or ""
        out = self.eng.push_config(device)
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_push_devices(self):
        device = self.query("device") or self.headers.get("X-Fleet-Device-ID") or ""
        out = self.eng.push_devices(device)
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_diagnostics(self):
        out = self.diagnostics()
        with self.eng.lock:
            out["engine"] = dict(
                (self.eng.snapshot_cache.get("diagnostics") or {}))
        search = getattr(self.eng, "search", None)
        if search:
            try:
                out["search"] = search.status()
            except Exception as exc:
                out["search"] = {"ok": False, "error": str(exc)}
        push_diagnostics = getattr(self.eng, "push_diagnostics", None)
        if push_diagnostics:
            out["web_push"] = push_diagnostics()
        legacy_diagnostics = getattr(self.eng, "legacy_ntfy_diagnostics", None)
        if legacy_diagnostics:
            out["legacy_ntfy"] = legacy_diagnostics()
        return self.reply(200, "application/json", json.dumps(out).encode())

    def _search_index(self):
        search = getattr(self.eng, "search", None)
        if not search:
            self.reply(503, "application/json",
                       b'{"ok": false, "error": "search index is unavailable"}')
        return search

    def get_search(self):
        search = self._search_index()
        if not search:
            return
        try:
            out = self.project_search_file_ids(search.search(
                query=self.query("q"), provider=self.query("provider"),
                kind=self.query("kind"), project=self.query("project"),
                cursor=self.query("cursor") or 0,
                limit=self.query("limit") or 30))
        except Exception as exc:
            print(f"search request failed: {exc}", file=sys.stderr, flush=True)
            out = {"ok": False, "error": "search index is temporarily unavailable"}
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_search_status(self):
        search = self._search_index()
        if not search:
            return
        try:
            out = search.status()
        except Exception as exc:
            print(f"search request failed: {exc}", file=sys.stderr, flush=True)
            out = {"ok": False, "error": "search index is temporarily unavailable"}
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_search_context(self):
        search = self._search_index()
        if not search:
            return
        try:
            out = self.project_search_file_ids(
                search.context(self.query("id"), self.query("radius") or 12))
        except Exception as exc:
            print(f"search request failed: {exc}", file=sys.stderr, flush=True)
            out = {"ok": False, "error": "search index is temporarily unavailable"}
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_context(self):
        cursor = self.query("cursor")
        try:
            limit = max(1, min(200, int(self.query("limit") or 50)))
            before = int(cursor) if cursor not in ("", None) else None
        except (TypeError, ValueError):
            return self.reply(400, "application/json", json.dumps(
                {"ok": False, "error": "invalid conversation pagination"}).encode())
        out = self.paginate_context(
            self.eng.session_context(self.query("sid"), before=before, limit=limit))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_closed_context(self):
        out = self.paginate_context(self.eng.closed_context(self.query("sid")))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_agent_context(self):
        out = self.paginate_context(
            self.eng.agent_context(self.query("sid"), self.query("aid")))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_file(self):
        # reads file bytes off disk -> token-gated like /api/act
        ctype, data, err = self.eng.file_content(self.query("sid"), self.query("fid"))
        if err:
            return self.reply(404, "text/plain", err.encode())
        return self.reply(200, ctype, data,
                          extra_headers={"X-Content-Type-Options": "nosniff"})

    def get_screen(self):
        # reads a live terminal's rendered screen -> token-gated like /api/file
        out = self.eng.session_screen(self.query("sid"))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_tool_result(self):
        # one tool call's output, read from the transcript on demand. The
        # conversation ring carries a one-line preview only, so expanding a row
        # in the chat asks for the rest here rather than the server holding it.
        out = self.eng.tool_result(self.query("sid"), self.query("tuid"))
    def get_prompt_options(self):
        # the option rows a live prompt is rendering; same capture and the same
        # refusals as /api/screen, which is why it is token-gated the same way
        out = self.eng.prompt_options(self.query("sid"))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_act_receipt(self):
        # how a browser that lost its response learns whether the action landed
        out = self.eng.act_receipt(self.query("rid"))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_commands(self):
        # reads command/skill names + descriptions off disk -> token-gated
        out = self.eng.commands(self.query("sid"))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_insights(self):
        try:
            days = max(1, min(90, int(self.query("days") or 7)))
        except ValueError:
            days = 7
        return self.reply(200, "application/json",
                          json.dumps(self.eng.insights(days)).encode())

    def get_briefing(self):
        out = self.eng.briefing_snapshot(
            self.query("device") or "default", self.query("cursor") or None,
            self.query("limit") or 100)
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_budgets(self):
        spawn = {key: self.query(key) for key in ("provider", "model", "project", "cwd")
                 if self.query(key)}
        return self.reply(200, "application/json",
                          json.dumps(self.eng.budgets_snapshot(spawn or None)).encode())

    def get_workstreams(self):
        return self.reply(200, "application/json",
                          json.dumps(self.eng.workstreams_snapshot()).encode())

    def get_evidence(self):
        out = self.eng.state_history(self.query("sid"), self.query("cursor") or 0,
                                     self.query("limit") or 40)
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_history(self):
        out = self.eng.history_snapshot(
            self.query("cursor") or 0, self.query("limit") or 100,
            self.query("q"), self.query("provider"), self.query("access"),
            self.query("sid"))
        return self.reply(200, "application/json", json.dumps(out).encode())

    def get_fleet(self):
        with self.eng.lock:
            snap = dict(self.eng.snapshot_cache)
        closed = list(snap.get("closed") or [])
        snap["closed_total"] = len(closed)
        snap["closed_ids"] = [item.get("session_id") for item in closed
                              if item.get("session_id")]
        # Pinned history remains on the main fleet surface. Everything else
        # is fetched only while the History page or a closed overlay needs it.
        snap["closed"] = [item for item in closed if item.get("pinned")]
        try:  # page version: lets stale tabs self-reload on dashboard.html changes
            assets = [os.path.join(APP_ROOT, "dashboard.html")]
            assets.extend(os.path.join(APP_ROOT, spec[0]) for spec in STATIC_FILES.values())
            snap["page_v"] = max(int(os.path.getmtime(path)) for path in assets)
        except OSError:
            pass
        return self.reply(200, "application/json", json.dumps(snap).encode())

    def get_static(self, route):
        try:
            path, content_type, cache_control, headers = STATIC_FILES[route]
            with open(os.path.join(APP_ROOT, path), "rb") as f:
                body = f.read()
                if route == "/static/manifest.webmanifest" and \
                   self.eng.cfg.get("instance_mode") == "staging":
                    manifest = json.loads(body)
                    manifest.update(name="Fleet Staging", short_name="Staging",
                                    description="Isolated Fleet Dash staging app")
                    body = json.dumps(manifest).encode()
                return self.reply(200, content_type, body, cache_control=cache_control,
                                  extra_headers=headers)
        except FileNotFoundError:
            return self.reply(404, "text/plain", b"asset missing")

    def get_index(self):
        try:
            with open(os.path.join(APP_ROOT, "dashboard.html"), "rb") as f:
                return self.reply(200, "text/html; charset=utf-8", f.read())
        except FileNotFoundError:
            return self.reply(500, "text/plain", b"dashboard.html missing")

    def error_reply(self, message):
        if getattr(self, "_request_route", "").startswith("/api/"):
            body = json.dumps({"ok": False, "error": str(message)[:300]}).encode()
            return self.reply(500, "application/json", body)
        return self.reply(500, "text/plain; charset=utf-8", b"request failed")

    # The fleet snapshot is JSON re-sent every two seconds over a tailnet, and it
    # compresses to about 18% of itself for ~1.3 ms of CPU (measured 2026-07-24:
    # 225,852 -> 40,929 bytes). Level 4 rather than the default 6: the extra 0.6 ms
    # per poll buys 1,354 bytes. Already-compressed image/PDF bodies are excluded.
    GZIP_TYPES = ("application/json", "text/", "image/svg+xml",
                  "application/javascript", "application/manifest+json")
    GZIP_MIN_BYTES = 1400            # below one MTU compression is not worth a header
    GZIP_LEVEL = 4

    def _accepts_gzip(self):
        for part in (self.headers.get("Accept-Encoding") or "").split(","):
            token, _, params = part.strip().partition(";")
            if token.lower() != "gzip":
                continue
            quality = ""
            for param in params.split(";"):
                key, _, value = param.strip().partition("=")
                if key.lower() == "q":
                    quality = value.strip()
            try:
                return float(quality) > 0 if quality else True
            except ValueError:
                return True
        return False

    def reply(self, code, ctype, body, *, cache_control="no-store", extra_headers=None):
        elapsed_ms = ((time.perf_counter() - getattr(self, "_request_started",
                                                     time.perf_counter())) * 1000)
        route = getattr(self, "_request_route", self.path.split("?", 1)[0])
        # metrics and X-Fleet-Payload-Bytes keep meaning the size the app produced
        payload_bytes = len(body)
        with self.metrics_lock:
            metrics = self.route_metrics[route]
            metrics["elapsed_ms"].append(elapsed_ms)
            metrics["payload_bytes"].append(payload_bytes)
            metrics["statuses"].append(int(code))
        headers = dict(extra_headers or {})
        if (payload_bytes >= self.GZIP_MIN_BYTES and
                any(ctype.startswith(prefix) for prefix in self.GZIP_TYPES) and
                not any(key.lower() == "content-encoding" for key in headers) and
                self._accepts_gzip()):
            try:
                body = gzip.compress(body, self.GZIP_LEVEL)
                headers["Content-Encoding"] = "gzip"
                headers["Vary"] = "Accept-Encoding"
            except Exception:      # never fail a response over compression
                body = body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Server-Timing", f"app;dur={elapsed_ms:.3f}")
            self.send_header("X-Fleet-Payload-Bytes", str(payload_bytes))
            self.send_header("Cache-Control", cache_control)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # Browser navigation and cancelled searches can close the socket
            # after the response has already been prepared. That is not a
            # daemon failure and should not create a traceback in the log.
            pass

    def log_message(self, *a):
        pass


def main():
    cfg = load_config()
    eng = Engine(cfg)
    eng.start_web_push()
    if cfg.get("search_enabled", True):
        eng.search = SearchIndex(os.path.join(BASE, "search.db"), PROJECTS,
                                 os.path.expanduser("~/.codex/sessions"),
                                 discover_seconds=cfg.get("search_discover_seconds", 2),
                                 batch_rows=cfg.get("search_batch_rows", 250))
        eng.search.start_process()
    Handler.eng = eng
    srv = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    print(f"fleet-dash on http://{cfg['bind']}:{cfg['port']}", flush=True)
    # Bind before the first provider scan. One slow or malformed session must
    # never make the dashboard's HTTP server disappear during startup.
    threading.Thread(target=poll_loop, args=(eng,), daemon=True).start()
    threading.Thread(target=outbox_loop, args=(eng,), daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":  # pragma: no cover - process entrypoint guard
    main()
