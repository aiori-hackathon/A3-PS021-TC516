"""Loopback-only mock order service with its own durable effect ledger."""

import argparse
from contextlib import closing
from hmac import compare_digest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


KEY = re.compile(r"idk_[a-f0-9]{24}")
ORDER_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


class OrderLedger:
    def __init__(self, db, *, protect=True):
        self.db = db
        self.protect = protect
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS replacements (
                       receipt_id TEXT PRIMARY KEY,
                       operation_key TEXT NOT NULL,
                       order_id TEXT NOT NULL
                   )"""
            )
            if protect:
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_effect ON replacements(operation_key)")

    def replace(self, key, order_id):
        # Check the key and record the effect within the service's own
        # transaction, independently of the planner's task database.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if self.protect:
                saved = conn.execute(
                    "SELECT receipt_id, order_id FROM replacements WHERE operation_key = ?", (key,)
                ).fetchone()
                if saved is not None:
                    receipt_id, original_order = saved
                    if order_id != original_order:
                        return 409, {"error": "operation_key_reused_for_different_order"}
                    return 200, {"receipt_id": receipt_id, "replayed": True}
            receipt_id = "RPL-" + uuid.uuid4().hex[:10]
            conn.execute("INSERT INTO replacements VALUES (?, ?, ?)", (receipt_id, key, order_id))
        return 201, {"receipt_id": receipt_id, "replayed": False}

    def receipts(self, key):
        with closing(sqlite3.connect(self.db)) as conn:
            return [row[0] for row in conn.execute(
                "SELECT receipt_id FROM replacements WHERE operation_key = ? ORDER BY rowid", (key,)
            )]


def make_handler(ledger, token=None):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.authorized():
                return
            prefix = "/replacements/"
            key = self.path[len(prefix):]
            if not self.path.startswith(prefix) or not KEY.fullmatch(key):
                return self.send_json(404, {"error": "not_found"})
            self.send_json(200, {"receipt_ids": ledger.receipts(key)})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/replacements":
                return self.send_json(404, {"error": "not_found"})
            key = self.headers.get("Idempotency-Key", "")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("invalid body length")
                body = json.loads(self.rfile.read(length))
                order_id = body["order_id"]
                if (set(body) != {"order_id"} or not isinstance(order_id, str)
                        or not ORDER_ID.fullmatch(order_id)
                        or not KEY.fullmatch(key)):
                    raise ValueError("invalid order or key")
            except (ValueError, TypeError, KeyError):
                return self.send_json(400, {"error": "invalid_replacement"})

            status, result = ledger.replace(key, order_id)
            if status == 201 and self.headers.get("X-Lab-Drop-Reply") == "1":
                # SQLite has committed. Close the real HTTP connection before
                # sending any response bytes, leaving the caller uncertain.
                self.close_connection = True
                return
            self.send_json(status, result)

        def authorized(self):
            supplied = self.headers.get("Authorization", "").encode("utf-8")
            if token is not None and not compare_digest(supplied, ("Bearer " + token).encode("utf-8")):
                self.send_json(401, {"error": "unauthorized"})
                return False
            return True

        def log_message(self, *_args):
            pass

    return Handler


def request(port, method, path, *, key=None, order_id=None, drop_reply=False, token=None):
    headers = {}
    body = None
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if key is not None:
        headers["Idempotency-Key"] = key
    if order_id is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps({"order_id": order_id})
    if drop_reply:
        headers["X-Lab-Drop-Reply"] = "1"
    with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--without-deduplication", action="store_true")
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    ledger = OrderLedger(args.db, protect=not args.without_deduplication)
    token = os.environ.get("THREADLINE_ORDER_TOKEN")
    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ledger, token)) as server:
        ready_tmp = args.ready_file.with_suffix(".tmp")
        ready_tmp.write_text(str(server.server_port), encoding="ascii")
        ready_tmp.replace(args.ready_file)
        if args.parent_pid is None:
            server.serve_forever()
        else:
            server.timeout = 0.25
            while os.getppid() == args.parent_pid:
                server.handle_request()


if __name__ == "__main__":
    main()
