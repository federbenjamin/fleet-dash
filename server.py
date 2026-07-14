#!/usr/bin/env python3
"""fleet-dash server: background poll loop + tiny HTTP API.

GET /            dashboard.html (re-read per request, edit without restart)
GET /api/fleet   latest fleet snapshot JSON
"""
import json, os, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine, load_config, BASE  # noqa: E402


def poll_loop(eng):
    while True:
        try:
            fleet = eng.scan()
            eng.check_notifications(fleet)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr, flush=True)
        time.sleep(eng.cfg["poll_seconds"])


class Handler(BaseHTTPRequestHandler):
    eng = None

    def token_ok(self):
        want = self.eng.cfg.get("act_token", "")
        cookies = self.headers.get("Cookie", "")
        return want and (f"act_token={want}" in cookies
                         or self.headers.get("X-Act-Token") == want)

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route not in ("/api/act", "/api/settings"):
            return self.reply(404, "text/plain", b"not found")
        if not self.token_ok():
            print(f"{route} denied: no/bad token (open the ?token= URL once on this device)",
                  file=sys.stderr, flush=True)
            return self.reply(403, "application/json",
                              b'{"ok": false, "error": "bad or missing act token"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
            action = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except Exception:
            return self.reply(400, "application/json", b'{"ok": false, "error": "bad json"}')
        if route == "/api/settings":
            result = self.eng.update_settings(action)
            print(f"settings: {json.dumps(action)[:200]}", file=sys.stderr, flush=True)
            return self.reply(200, "application/json", json.dumps(result).encode())
        if action.get("type") != "ping":    # audit trail: exactly what was requested
            print(f"act: {json.dumps(action)[:300]}", file=sys.stderr, flush=True)
        result = self.eng.act(action)
        if not result.get("ok"):
            print(f"act failed: {json.dumps(action)[:200]} -> {result.get('error')}",
                  file=sys.stderr, flush=True)
        self.reply(200, "application/json", json.dumps(result).encode())

    def query(self, key):
        return (parse_qs(urlparse(self.path).query).get(key) or [""])[0]

    def do_GET(self):
        route = self.path.split("?", 1)[0]
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
        elif route == "/api/fleet":
            with self.eng.lock:
                snap = dict(self.eng.snapshot_cache)
            try:  # page version: lets stale tabs self-reload on dashboard.html changes
                snap["page_v"] = int(os.path.getmtime(os.path.join(BASE, "dashboard.html")))
            except OSError:
                pass
            self.reply(200, "application/json", json.dumps(snap).encode())
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
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    cfg = load_config()
    eng = Engine(cfg)
    eng.scan()
    threading.Thread(target=poll_loop, args=(eng,), daemon=True).start()
    Handler.eng = eng
    srv = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    print(f"fleet-dash on http://{cfg['bind']}:{cfg['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
