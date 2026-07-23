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
import hashlib, json, os, resource, subprocess, sys, time, threading, secrets
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fleetdash.engine import Engine, load_config, BASE, PROJECTS  # noqa: E402
from fleetdash.search_index import SearchIndex  # noqa: E402

APP_ROOT = os.path.dirname(os.path.abspath(__file__))

STATIC_FILES = {
    "/static/fleet.css": ("static/fleet.css", "text/css; charset=utf-8", "no-cache", {}),
    "/static/app.js": ("static/app.js", "text/javascript; charset=utf-8", "no-cache", {}),
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
        if route not in ("/api/act", "/api/upload-image", "/api/settings", "/api/search/rebuild",
                         "/api/notifications/read", "/api/notifications/snooze",
                         "/api/notifications/wake", "/api/notifications/mute",
                         "/api/notifications/retry", "/api/push/subscription",
                         "/api/notification-policy",
                         "/api/push/test", "/api/push/device-settings",
                         "/api/push/capability-action", "/api/legacy-ntfy/test"):
            return self.reply(404, "text/plain", b"not found")
        if route == "/api/push/capability-action":
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
        if not self.token_ok():
            print(f"{route} denied: no/bad token (open the ?token= URL once on this device)",
                  file=sys.stderr, flush=True)
            return self.reply(403, "application/json",
                              b'{"ok": false, "error": "bad or missing act token"}')
        if route == "/api/upload-image":
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
        if route == "/api/settings":
            result = self.eng.update_settings(action)
            audit = {"ok": bool(result.get("ok")), "field_count": min(len(action), 1000)}
            print(f"settings: {json.dumps(audit)[:200]}", file=sys.stderr, flush=True)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/notifications/read":
            result = self.eng.notifications_mark_read(action)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/notifications/snooze":
            result = self.eng.notifications_snooze(action)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/notifications/wake":
            result = self.eng.notifications_wake(action)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/notifications/mute":
            result = self.eng.notifications_mute(action)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/notifications/retry":
            result = self.eng.notifications_retry(action)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/notification-policy":
            result = self.eng.notification_policy_update(action)
            return self.reply(200 if result.get("ok") else 409, "application/json",
                              json.dumps(result).encode())
        if route == "/api/push/subscription":
            result = self.eng.push_subscription(action)
            device_ref = hashlib.sha256(
                str(action.get("device_id") or "").encode()).hexdigest()[:12]
            audit = {"ok": bool(result.get("ok")), "device_ref": device_ref,
                     "operation": ("forget" if action.get("forget") else
                                   "remove" if action.get("remove") else "register")}
            print(f"push subscription: {json.dumps(audit)}", file=sys.stderr, flush=True)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/push/device-settings":
            result = self.eng.push_device_settings(action)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/push/test":
            result = self.eng.push_test(action)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if route == "/api/legacy-ntfy/test":
            result = self.eng.legacy_ntfy_test()
            status = 200 if result.get("ok") else 409
            return self.reply(status, "application/json", json.dumps(result).encode())
        if route == "/api/search/rebuild":
            search = getattr(self.eng, "search", None)
            try:
                result = (search.rebuild() if search else
                          {"ok": False, "error": "search index is unavailable"})
            except Exception as exc:
                print(f"search rebuild failed: {exc}", file=sys.stderr, flush=True)
                result = {"ok": False, "error": "search index is temporarily unavailable"}
            print("search: rebuild requested", file=sys.stderr, flush=True)
            return self.reply(200, "application/json", json.dumps(result).encode())
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
        self.reply(200, "application/json", json.dumps(result).encode())

    def query(self, key):
        return (parse_qs(urlparse(self.path).query).get(key) or [""])[0]

    def paginate_context(self, out):
        if not isinstance(out, dict) or not out.get("ok"):
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
        if route in ("/api/search", "/api/search/status", "/api/search/context",
                     "/api/handoff", "/api/repo", "/api/outbox", "/api/diagnostics",
                     "/api/notifications", "/api/push/config", "/api/push/devices",
                     "/api/notification-policy"):
            if not self.token_ok():
                return self.reply(403, "application/json",
                                  b'{"ok": false, "error": "bad or missing act token"}')
            if route == "/api/handoff":
                out = self.eng.handoff_preview(self.query("sid"), self.query("provider"))
                return self.reply(200, "application/json", json.dumps(out).encode())
            if route == "/api/repo":
                out = self.eng.repository_snapshot(
                    self.query("root"), self.query("worktree"),
                    self.query("force") in ("1", "true"))
                return self.reply(200, "application/json", json.dumps(out).encode())
            if route == "/api/outbox":
                out = self.eng.outbox_snapshot(self.query("state"), self.query("cursor") or 0,
                                               self.query("limit") or 100)
                return self.reply(200, "application/json", json.dumps(out).encode())
            if route == "/api/notifications":
                states = [item for item in self.query("state").split(",") if item]
                kinds = [item for item in self.query("kind").split(",") if item]
                out = self.eng.notifications_snapshot(
                    self.query("device") or "default", self.query("cursor") or None,
                    self.query("limit") or 100, states, kinds, self.query("id") or None)
                return self.reply(200, "application/json", json.dumps(out).encode())
            if route == "/api/notification-policy":
                out = self.eng.notification_policy_snapshot()
                return self.reply(200 if out.get("ok") else 503, "application/json",
                                  json.dumps(out).encode())
            if route == "/api/push/config":
                device = self.query("device") or self.headers.get("X-Fleet-Device-ID") or ""
                out = self.eng.push_config(device)
                return self.reply(200, "application/json", json.dumps(out).encode())
            if route == "/api/push/devices":
                device = self.query("device") or self.headers.get("X-Fleet-Device-ID") or ""
                out = self.eng.push_devices(device)
                return self.reply(200, "application/json", json.dumps(out).encode())
            if route == "/api/diagnostics":
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
            search = getattr(self.eng, "search", None)
            if not search:
                return self.reply(503, "application/json",
                                  b'{"ok": false, "error": "search index is unavailable"}')
            try:
                if route == "/api/search/status":
                    out = search.status()
                elif route == "/api/search/context":
                    out = self.project_search_file_ids(
                        search.context(self.query("id"), self.query("radius") or 12))
                else:
                    out = self.project_search_file_ids(search.search(
                        query=self.query("q"), provider=self.query("provider"),
                        kind=self.query("kind"), project=self.query("project"),
                        cursor=self.query("cursor") or 0,
                        limit=self.query("limit") or 30))
            except Exception as exc:
                print(f"search request failed: {exc}", file=sys.stderr, flush=True)
                out = {"ok": False, "error": "search index is temporarily unavailable"}
            return self.reply(200, "application/json", json.dumps(out).encode())
        if route == "/api/context":
            out = self.paginate_context(self.eng.session_context(self.query("sid")))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/closed_context":
            out = self.paginate_context(self.eng.closed_context(self.query("sid")))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/agent_context":
            out = self.paginate_context(
                self.eng.agent_context(self.query("sid"), self.query("aid")))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/file":
            # reads file bytes off disk -> token-gated like /api/act
            if not self.token_ok():
                return self.reply(403, "text/plain",
                                  b"missing act token (open the ?token= URL once on this device)")
            ctype, data, err = self.eng.file_content(self.query("sid"), self.query("fid"))
            if err:
                return self.reply(404, "text/plain", err.encode())
            self.reply(200, ctype, data,
                       extra_headers={"X-Content-Type-Options": "nosniff"})
        elif route == "/api/commands":
            # reads command/skill names + descriptions off disk -> token-gated
            if not self.token_ok():
                return self.reply(403, "application/json",
                                  b'{"ok": false, "error": "bad or missing act token"}')
            out = self.eng.commands(self.query("sid"))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/insights":
            try:
                days = max(1, min(90, int(self.query("days") or 7)))
            except ValueError:
                days = 7
            self.reply(200, "application/json", json.dumps(self.eng.insights(days)).encode())
        elif route == "/api/briefing":
            out = self.eng.briefing_snapshot(
                self.query("device") or "default", self.query("cursor") or None,
                self.query("limit") or 100)
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/budgets":
            spawn = {key: self.query(key) for key in ("provider", "model", "project", "cwd")
                     if self.query(key)}
            self.reply(200, "application/json",
                       json.dumps(self.eng.budgets_snapshot(spawn or None)).encode())
        elif route == "/api/workstreams":
            self.reply(200, "application/json",
                       json.dumps(self.eng.workstreams_snapshot()).encode())
        elif route == "/api/evidence":
            out = self.eng.state_history(self.query("sid"), self.query("cursor") or 0,
                                         self.query("limit") or 40)
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/history":
            out = self.eng.history_snapshot(
                self.query("cursor") or 0, self.query("limit") or 100,
                self.query("q"), self.query("provider"), self.query("access"),
                self.query("sid"))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/fleet":
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
            self.reply(200, "application/json", json.dumps(snap).encode())
        elif route in STATIC_FILES:
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
                    self.reply(200, content_type, body, cache_control=cache_control,
                               extra_headers=headers)
            except FileNotFoundError:
                self.reply(404, "text/plain", b"asset missing")
        elif route == "/" or route.startswith("/index"):
            try:
                with open(os.path.join(APP_ROOT, "dashboard.html"), "rb") as f:
                    self.reply(200, "text/html; charset=utf-8", f.read())
            except FileNotFoundError:
                self.reply(500, "text/plain", b"dashboard.html missing")
        else:
            self.reply(404, "text/plain", b"not found")

    def error_reply(self, message):
        if getattr(self, "_request_route", "").startswith("/api/"):
            body = json.dumps({"ok": False, "error": str(message)[:300]}).encode()
            return self.reply(500, "application/json", body)
        return self.reply(500, "text/plain; charset=utf-8", b"request failed")

    def reply(self, code, ctype, body, *, cache_control="no-store", extra_headers=None):
        elapsed_ms = ((time.perf_counter() - getattr(self, "_request_started",
                                                     time.perf_counter())) * 1000)
        route = getattr(self, "_request_route", self.path.split("?", 1)[0])
        with self.metrics_lock:
            metrics = self.route_metrics[route]
            metrics["elapsed_ms"].append(elapsed_ms)
            metrics["payload_bytes"].append(len(body))
            metrics["statuses"].append(int(code))
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Server-Timing", f"app;dur={elapsed_ms:.3f}")
            self.send_header("X-Fleet-Payload-Bytes", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            for key, value in (extra_headers or {}).items():
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


if __name__ == "__main__":
    main()
