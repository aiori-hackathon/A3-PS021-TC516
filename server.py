"""Threadline HTTP server: JSON API + operator dashboard (standard library only).

    python server.py                      # http://localhost:8080
    python server.py --port 9000 --db :memory:
    python server.py --fresh              # wipe the demo event log before starting
"""

import argparse
import json
import mimetypes
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from threadline import __version__
from threadline.service import ApiError, Threadline
from threadline.store import EventStore

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
MAX_BODY = 1_000_000
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
CONV = r"/api/conversations/(?P<cid>[A-Za-z0-9_.:-]{1,80})"


def make_handler(app):
    """Build a request handler bound to one Threadline instance."""

    routes = [
        ("GET", r"/api/health", lambda m, b: {"ok": True, "service": "threadline", "version": __version__}),
        ("GET", r"/api/scenarios", lambda m, b: app.catalogue()),
        ("POST", r"/api/experiments", lambda m, b: run_experiment(b)),
        ("GET", r"/api/conversations", lambda m, b: {"conversations": app.list_conversations()}),
        ("GET", CONV, lambda m, b: app.snapshot(m["cid"])),
        ("POST", CONV + r"/events", lambda m, b: app.ingest_external(m["cid"], b)),
        ("POST", CONV + r"/recover", lambda m, b: app.recover(m["cid"])),
        ("POST", CONV + r"/deliver", lambda m, b: app.deliver_pending(m["cid"])),
        ("POST", CONV + r"/resume", lambda m, b: app.resume(m["cid"])),
        ("GET", CONV + r"/recoveries", lambda m, b: {"recoveries": app.store.recoveries(m["cid"])}),
        ("POST", r"/api/reset", lambda m, b: reset()),
    ]
    compiled = [(verb, re.compile(pattern + r"/?$"), fn) for verb, pattern, fn in routes]

    def run_experiment(body):
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_body", "Body must be a JSON object.")
        faults = body.get("faults")
        if faults is None and body.get("inject_interruption"):  # prototype compatibility
            faults = ["adapter_timeout"]
        return app.run_experiment(body.get("scenario"), faults)

    def reset():
        app.store.reset()
        return {"ok": True}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Threadline/" + __version__

        def send_json(self, payload, status=200):
            raw = json.dumps(payload, indent=None).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def read_body(self):
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "invalid_length", "Content-Length must be an integer.")
            if size > MAX_BODY:
                raise ApiError(413, "body_too_large", f"Body exceeds {MAX_BODY} bytes.")
            raw = self.rfile.read(size) if size else b""
            self.body_read = True
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ApiError(400, "invalid_json", f"Request body is not valid JSON: {exc}")

        def discard_unread_body(self):
            # Closing a socket with unread input resets the connection, and the
            # client can lose the response (e.g. a JSON 405 for PUT with a body).
            if self.body_read:
                return
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if 0 < size <= MAX_BODY:
                self.rfile.read(size)

        def dispatch(self, verb):
            path = urlparse(self.path).path
            self.body_read = False
            try:
                if not path.startswith("/api/"):
                    if verb == "GET":
                        return self.serve_static(path)
                    raise ApiError(405, "method_not_allowed", f"{verb} is not allowed on {path}.")
                allowed = False
                for route_verb, pattern, fn in compiled:
                    match = pattern.match(path)
                    if not match:
                        continue
                    allowed = True
                    if route_verb == verb:
                        body = self.read_body() if verb == "POST" else None
                        return self.send_json(fn(match.groupdict(), body))
                if allowed:
                    verbs = sorted({v for v, p, _ in compiled if p.match(path)})
                    raise ApiError(405, "method_not_allowed", f"{verb} is not allowed on {path}; use {' or '.join(verbs)}.")
                raise ApiError(404, "not_found", f"No API route for {verb} {path}.")
            except ApiError as err:
                return self.send_json(err.payload(), err.status)
            except Exception:  # never drop the connection without a JSON answer
                traceback.print_exc()
                return self.send_json({"error": {"code": "internal_error", "message": "Unexpected server error."}}, 500)
            finally:
                self.discard_unread_body()

        def serve_static(self, path):
            filename = "index.html" if path in ("", "/") else path.lstrip("/")
            target = os.path.normpath(os.path.join(STATIC, filename))
            if os.path.commonpath([target, STATIC]) != STATIC or not os.path.isfile(target):
                raise ApiError(404, "not_found", f"No file at {path}.")
            with open(target, "rb") as f:
                raw = f.read()
            self.send_response(200)
            ctype = CONTENT_TYPES.get(os.path.splitext(target)[1]) or mimetypes.guess_type(target)[0]
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_PUT(self):
            self.dispatch("PUT")

        def do_DELETE(self):
            self.dispatch("DELETE")

        def do_PATCH(self):
            self.dispatch("PATCH")

        def do_OPTIONS(self):
            self.dispatch("OPTIONS")

        def do_HEAD(self):
            # HEAD is not part of this API; answer 405 without a body (HEAD responses carry none).
            self.send_response(405)
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):
            if os.environ.get("THREADLINE_ACCESS_LOG"):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return Handler


def create_server(host="127.0.0.1", port=8080, db=":memory:"):
    if db != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    app = Threadline(EventStore(db))
    return ThreadingHTTPServer((host, port), make_handler(app)), app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Threadline dialog correlation & recovery lab")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("THREADLINE_DB", os.path.join(ROOT, "data", "threadline.db")),
                        help="SQLite file for the event log (':memory:' for a throwaway run)")
    parser.add_argument("--fresh", action="store_true", help="delete all demo data in the event log before starting")
    args = parser.parse_args(argv)
    httpd, app = create_server(args.host, args.port, args.db)
    if args.fresh:
        app.store.reset()
    print(f"Threadline {__version__} listening on http://localhost:{args.port}  (event log: {args.db})", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
