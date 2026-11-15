"""Durable planner queue and tool adapter for an independent order service."""

from contextlib import closing, contextmanager
from http.client import HTTPException
import json
import logging
import re
import sqlite3
import threading
import time
import uuid

from .effect_service import ORDER_ID, request
from .events import sha
from .service import ApiError
from .simulator import build_envelope
from .task_protocol import decode_task, encode_task


REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
RECEIPT = re.compile(r"RPL-[a-f0-9]{10}")
MAX_ATTEMPTS = 3
# Longer than the tool's HTTP timeout. A late worker cannot finish a newer claim.
CLAIM_SECONDS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_tasks (
    task_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    dialog_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    handoff TEXT NOT NULL,
    policy TEXT NOT NULL CHECK (policy IN ('protected', 'review')),
    drop_reply INTEGER NOT NULL CHECK (drop_reply IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN ('queued', 'sending', 'retry_wait', 'needs_review', 'complete')),
    attempts INTEGER NOT NULL DEFAULT 0,
    due_at REAL NOT NULL,
    created_at REAL NOT NULL,
    receipt_id TEXT,
    CHECK ((state = 'complete') = (receipt_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS order_tasks_due ON order_tasks(state, due_at);
CREATE TRIGGER IF NOT EXISTS order_tasks_guard_state
BEFORE UPDATE OF state ON order_tasks
WHEN NOT (
    (OLD.state IN ('queued', 'retry_wait') AND NEW.state = 'sending')
    OR (OLD.state = 'sending' AND NEW.state IN ('sending', 'retry_wait', 'needs_review', 'complete'))
    OR (OLD.state = 'needs_review' AND NEW.state = 'complete')
)
BEGIN SELECT RAISE(ABORT, 'illegal order task transition'); END;
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES order_tasks(task_id),
    at REAL NOT NULL,
    event TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_events_task ON order_events(task_id, id);
CREATE TRIGGER IF NOT EXISTS order_events_no_update
BEFORE UPDATE ON order_events BEGIN SELECT RAISE(ABORT, 'order audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS order_events_no_delete
BEFORE DELETE ON order_events BEGIN SELECT RAISE(ABORT, 'order audit is append-only'); END;
"""


class OrderWorkflow:
    def __init__(self, db, port, token, *, demo_faults=False, clock=time.time):
        self.db = db
        self.port = port
        self.token = token
        self.demo_faults = demo_faults
        self.clock = clock
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def write(self):
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def event(self, conn, task_id, name, attempt, detail):
        conn.execute("INSERT INTO order_events (task_id, at, event, attempt, detail) VALUES (?, ?, ?, ?, ?)",
                     (task_id, self.clock(), name, attempt, detail))

    @staticmethod
    def public(row):
        return {key: row[key] for key in (
            "task_id", "request_id", "dialog_id", "order_id", "operation_key",
            "policy", "state", "attempts", "receipt_id", "created_at",
        )}

    def submit(self, body):
        if not isinstance(body, dict) or set(body) - {"request_id", "order_id", "policy", "drop_reply"}:
            raise ApiError(400, "invalid_order_task", "Provide a request ID, order ID and optional recovery policy.")
        request_id, order_id = body.get("request_id"), body.get("order_id")
        policy, drop_reply = body.get("policy", "protected"), body.get("drop_reply", False)
        if (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)
                or not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id)
                or policy not in ("protected", "review") or type(drop_reply) is not bool):
            raise ApiError(400, "invalid_order_task", "Use a valid request ID, order ID and recovery policy.")
        if drop_reply and not self.demo_faults:
            raise ApiError(403, "faults_disabled", "Fault injection is disabled.")

        with self.write() as conn:
            previous = conn.execute("SELECT * FROM order_tasks WHERE request_id = ?", (request_id,)).fetchone()
            if previous is not None:
                if (previous["order_id"], previous["policy"], bool(previous["drop_reply"])) != (
                    order_id, policy, drop_reply
                ):
                    raise ApiError(409, "request_conflict", "This request ID already refers to a different task.")
                return self.public(previous)

            task_id, dialog_id = str(uuid.uuid4()), str(uuid.uuid4())
            key = "idk_" + sha(json.dumps(["replace_order", dialog_id, task_id]), 24)
            step = (1, "planner-agent", "assistant", "handoff", "tool-agent", order_id)
            envelope = build_envelope(dialog_id, step, None, variant=task_id)
            handoff = encode_task(task_id, key, envelope)
            now = self.clock()
            conn.execute(
                """INSERT INTO order_tasks (task_id, request_id, dialog_id, order_id, operation_key,
                       handoff, policy, drop_reply, state, due_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (task_id, request_id, dialog_id, order_id, key, json.dumps(handoff), policy, drop_reply, now, now),
            )
            self.event(conn, task_id, "queued", 0, "Planner saved the handoff and operation key.")
            row = conn.execute("SELECT * FROM order_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self.public(row)

    def list_tasks(self):
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM order_tasks ORDER BY created_at DESC, rowid DESC LIMIT 50")
            return {"tasks": [self.public(row) for row in rows], "demo_faults": self.demo_faults}

    def get(self, task_id):
        with closing(self.connect()) as conn:
            # Keep the task and its timeline in the same read snapshot.
            conn.execute("BEGIN")
            return self.snapshot(conn, task_id)

    def snapshot(self, conn, task_id):
        row = conn.execute("SELECT * FROM order_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise ApiError(404, "task_not_found", "No order task has that ID.")
        events = conn.execute(
            "SELECT at, event, attempt, detail FROM order_events WHERE task_id = ? ORDER BY id", (task_id,)
        )
        return {**self.public(row), "events": [dict(item) for item in events]}

    def claim(self):
        with self.write() as conn:
            now = self.clock()
            while True:
                row = conn.execute(
                    """SELECT * FROM order_tasks
                       WHERE state IN ('queued', 'sending', 'retry_wait') AND due_at <= ?
                       ORDER BY due_at, rowid LIMIT 1""", (now,)
                ).fetchone()
                if row is None:
                    return None
                if row["state"] == "sending":
                    if row["policy"] == "review" or row["attempts"] >= MAX_ATTEMPTS:
                        self.review(conn, row, "Worker stopped with an unresolved attempt. Verify the order ledger.")
                        continue
                    self.event(conn, row["task_id"], "worker_recovered", row["attempts"],
                               "Previous claim expired. Recovery uses the saved operation key.")
                attempt = row["attempts"] + 1
                conn.execute("UPDATE order_tasks SET state = 'sending', attempts = ?, due_at = ? WHERE task_id = ?",
                             (attempt, now + CLAIM_SECONDS, row["task_id"]))
                self.event(conn, row["task_id"], "sending", attempt, "Worker claimed the saved handoff for dispatch.")
                return {**dict(row), "attempts": attempt}

    def review(self, conn, task, detail):
        conn.execute("UPDATE order_tasks SET state = 'needs_review' WHERE task_id = ?", (task["task_id"],))
        self.event(conn, task["task_id"], "needs_review", task["attempts"], detail)

    def finish(self, task, *, receipt=None, replayed=False, retryable=False, detail=""):
        task_id, attempt = task["task_id"], task["attempts"]
        with self.write() as conn:
            current = conn.execute("SELECT state, attempts FROM order_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if (current["state"], current["attempts"]) != ("sending", attempt):
                return
            if receipt is not None:
                conn.execute("UPDATE order_tasks SET state = 'complete', receipt_id = ? WHERE task_id = ?",
                             (receipt, task_id))
                action = "replayed its saved" if replayed else "issued"
                self.event(conn, task_id, "receipt_replayed" if replayed else "receipt_saved", attempt,
                           f"Order service {action} receipt {receipt}.")
            elif retryable and task["policy"] == "protected" and attempt < MAX_ATTEMPTS:
                conn.execute("UPDATE order_tasks SET state = 'retry_wait', due_at = ? WHERE task_id = ?",
                             (self.clock() + 2 ** (attempt - 1), task_id))
                self.event(conn, task_id, "retry_wait", attempt, detail + " The next attempt reuses the same operation key.")
            else:
                self.review(conn, task, detail + " An operator must verify the outcome.")

    def run_next(self):
        task = self.claim()
        if task is None:
            return False
        try:
            task_id, operation_key, envelope = decode_task(json.loads(task["handoff"]), "planner-agent")
        except ValueError as exc:
            self.finish(task, detail=f"The saved tool handoff could not be decoded: {exc}.")
            return True
        if (task_id != task["task_id"] or operation_key != task["operation_key"]
                or envelope["conversation_id"] != task["dialog_id"] or envelope["content"] != task["order_id"]
                or envelope["kind"] != "handoff" or envelope["target_adapter"] != "tool-agent"):
            self.finish(task, detail="The saved tool handoff failed validation.")
            return True
        try:
            status, body = request(self.port, "POST", "/replacements", token=self.token,
                                   key=operation_key, order_id=envelope["content"],
                                   drop_reply=bool(task["drop_reply"] and task["attempts"] == 1))
        except (OSError, HTTPException, ValueError):
            self.finish(task, retryable=True, detail="The tool received no usable HTTP reply.")
            return True
        receipt = body.get("receipt_id") if isinstance(body, dict) else None
        if status in (200, 201) and isinstance(receipt, str) and RECEIPT.fullmatch(receipt):
            step = (2, "tool-agent", "tool", "message", None, receipt)
            response = build_envelope(task["dialog_id"], step, envelope["message_id"], variant=task["task_id"])
            frame = encode_task(task_id, operation_key, response)
            reply_task, reply_key, decoded = decode_task(frame, "tool-agent")
            if (reply_task, reply_key, decoded["conversation_id"], decoded["causation_id"]) != (
                task_id, operation_key, task["dialog_id"], envelope["message_id"]
            ):
                self.finish(task, detail="Reply identity did not match the saved handoff.")
            else:
                self.finish(task, receipt=decoded["content"], replayed=status == 200)
        else:
            self.finish(task, retryable=status >= 500, detail=f"Order service returned HTTP {status}.")
        return True

    def reconcile(self, task_id, body):
        receipt = body.get("receipt_id") if isinstance(body, dict) and set(body) == {"receipt_id"} else None
        if not isinstance(receipt, str) or not RECEIPT.fullmatch(receipt):
            raise ApiError(400, "invalid_receipt", "Enter the receipt ID from the order ledger.")
        task = self.get(task_id)
        if task["state"] == "complete" and task["receipt_id"] == receipt:
            return task
        if task["state"] != "needs_review":
            raise ApiError(409, "not_in_review", "Only a task in review can be reconciled.")
        if self.evidence(task_id)["receipt_ids"] != [receipt]:
            raise ApiError(409, "receipt_unverified", "The ledger did not confirm this receipt for this task.")
        with self.write() as conn:
            change = conn.execute(
                "UPDATE order_tasks SET state = 'complete', receipt_id = ? WHERE task_id = ? AND state = 'needs_review'",
                (receipt, task_id),
            )
            if change.rowcount:
                self.event(conn, task_id, "reconciled", task["attempts"], "Operator verified receipt " + receipt + ".")
            current = self.snapshot(conn, task_id)
            if current["receipt_id"] != receipt:
                raise ApiError(409, "task_changed", "The task changed during reconciliation.")
            return current

    def evidence(self, task_id):
        task = self.get(task_id)
        try:
            status, result = request(self.port, "GET", "/replacements/" + task["operation_key"], token=self.token)
        except (OSError, HTTPException, ValueError) as exc:
            raise ApiError(503, "order_ledger_unavailable", "The order ledger is unavailable.") from exc
        ids = result.get("receipt_ids") if isinstance(result, dict) else None
        if status != 200 or not isinstance(ids, list):
            raise ApiError(503, "order_ledger_unavailable", "The order ledger is unavailable.")
        return {"receipt_ids": ids, "effect_count": len(ids)}

    def serve(self, stop):
        while not stop.is_set():
            try:
                if self.run_next():
                    continue
            except sqlite3.Error:
                logging.getLogger(__name__).exception("Order worker could not process the queue")
            stop.wait(0.2)

    @contextmanager
    def worker(self):
        stop = threading.Event()
        thread = threading.Thread(target=self.serve, args=(stop,), daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
