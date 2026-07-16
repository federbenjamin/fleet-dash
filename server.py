#!/usr/bin/env python3
"""fleet-dash server: background poll loop + tiny HTTP API.

GET /            dashboard.html (re-read per request, edit without restart)
GET /api/fleet   latest fleet snapshot JSON
GET /api/workstreams lazy repository/project rollup
GET /api/evidence durable session placement history
GET /api/handoff authenticated editable provider-handoff preview
"""
import json, os, sys, time, threading, secrets
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine, load_config, BASE, PROJECTS  # noqa: E402
from search_index import SearchIndex  # noqa: E402


STATIC_FILES = {
    "/static/fleet.css": "text/css; charset=utf-8",
    "/static/app.js": "text/javascript; charset=utf-8",
}


def poll_loop(eng):
    while True:
        try:
            search = getattr(eng, "search", None)
            if search:
                search.ensure_process()
            fleet = eng.scan()
            eng.check_notifications(fleet)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr, flush=True)
        time.sleep(eng.cfg["poll_seconds"])


class Handler(BaseHTTPRequestHandler):
    eng = None

    def token_ok(self):
        want = self.eng.cfg.get("act_token", "")
        if not want:
            return False
        supplied = self.headers.get("X-Act-Token") or ""
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            if not supplied and cookies.get("act_token"):
                supplied = cookies["act_token"].value
        except CookieError:
            return False
        return secrets.compare_digest(str(want), str(supplied))

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route not in ("/api/act", "/api/settings", "/api/search/rebuild"):
            return self.reply(404, "text/plain", b"not found")
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
            action = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(action, dict):
                raise ValueError("JSON body must be an object")
        except Exception:
            return self.reply(400, "application/json", b'{"ok": false, "error": "bad json"}')
        if route == "/api/settings":
            result = self.eng.update_settings(action)
            print(f"settings: {json.dumps(action)[:200]}", file=sys.stderr, flush=True)
            return self.reply(200, "application/json", json.dumps(result).encode())
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
            for key in ("text", "preview"):
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

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in ("/api/search", "/api/search/status", "/api/search/context",
                     "/api/handoff"):
            if not self.token_ok():
                return self.reply(403, "application/json",
                                  b'{"ok": false, "error": "bad or missing act token"}')
            if route == "/api/handoff":
                out = self.eng.handoff_preview(self.query("sid"), self.query("provider"))
                return self.reply(200, "application/json", json.dumps(out).encode())
            search = getattr(self.eng, "search", None)
            if not search:
                return self.reply(503, "application/json",
                                  b'{"ok": false, "error": "search index is unavailable"}')
            try:
                if route == "/api/search/status":
                    out = search.status()
                elif route == "/api/search/context":
                    out = search.context(self.query("id"), self.query("radius") or 12)
                else:
                    out = search.search(query=self.query("q"), provider=self.query("provider"),
                                        kind=self.query("kind"), project=self.query("project"),
                                        cursor=self.query("cursor") or 0,
                                        limit=self.query("limit") or 30)
            except Exception as exc:
                print(f"search request failed: {exc}", file=sys.stderr, flush=True)
                out = {"ok": False, "error": "search index is temporarily unavailable"}
            return self.reply(200, "application/json", json.dumps(out).encode())
        if route == "/api/context":
            out = self.eng.session_context(self.query("sid"))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/closed_context":
            out = self.eng.closed_context(self.query("sid"))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/agent_context":
            out = self.eng.agent_context(self.query("sid"), self.query("aid"))
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/file":
            # reads file bytes off disk -> token-gated like /api/act
            if not self.token_ok():
                return self.reply(403, "text/plain",
                                  b"missing act token (open the ?token= URL once on this device)")
            ctype, data, err = self.eng.file_content(self.query("sid"), self.query("p"))
            if err:
                return self.reply(404, "text/plain", err.encode())
            self.reply(200, ctype, data)
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
        elif route == "/api/workstreams":
            self.reply(200, "application/json",
                       json.dumps(self.eng.workstreams_snapshot()).encode())
        elif route == "/api/evidence":
            out = self.eng.state_history(self.query("sid"), self.query("cursor") or 0,
                                         self.query("limit") or 40)
            self.reply(200, "application/json", json.dumps(out).encode())
        elif route == "/api/fleet":
            with self.eng.lock:
                snap = dict(self.eng.snapshot_cache)
            try:  # page version: lets stale tabs self-reload on dashboard.html changes
                assets = [os.path.join(BASE, "dashboard.html")]
                assets.extend(os.path.join(BASE, route.removeprefix("/"))
                              for route in STATIC_FILES)
                snap["page_v"] = max(int(os.path.getmtime(path)) for path in assets)
            except OSError:
                pass
            self.reply(200, "application/json", json.dumps(snap).encode())
        elif route in STATIC_FILES:
            try:
                with open(os.path.join(BASE, route.removeprefix("/")), "rb") as f:
                    self.reply(200, STATIC_FILES[route], f.read())
            except FileNotFoundError:
                self.reply(404, "text/plain", b"asset missing")
        elif route == "/" or route.startswith("/index"):
            try:
                with open(os.path.join(BASE, "dashboard.html"), "rb") as f:
                    self.reply(200, "text/html; charset=utf-8", f.read())
            except FileNotFoundError:
                self.reply(500, "text/plain", b"dashboard.html missing")
        else:
            self.reply(404, "text/plain", b"not found")

    def reply(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Browser navigation and cancelled searches can close the socket
            # after the response has already been prepared. That is not a
            # daemon failure and should not create a traceback in the log.
            pass

    def log_message(self, *a):
        pass


def main():
    cfg = load_config()
    eng = Engine(cfg)
    eng.scan()
    if cfg.get("search_enabled", True):
        eng.search = SearchIndex(os.path.join(BASE, "search.db"), PROJECTS,
                                 os.path.expanduser("~/.codex/sessions"),
                                 discover_seconds=cfg.get("search_discover_seconds", 2),
                                 batch_rows=cfg.get("search_batch_rows", 250))
        eng.search.start_process()
    threading.Thread(target=poll_loop, args=(eng,), daemon=True).start()
    Handler.eng = eng
    srv = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    print(f"fleet-dash on http://{cfg['bind']}:{cfg['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
