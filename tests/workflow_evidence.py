"""Reproduce five fault cases against the workflow used by the dashboard."""

from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

from threadline.effect_service import request
from threadline.order_workflow import CLAIM_SECONDS, OrderWorkflow
from threadline.receipt_lab import effect_service


FAULT_EXIT = 75
ALLOWED = {
    "queued": {"sending"},
    "sending": {"sending", "retry_wait", "needs_review", "complete"},
    "retry_wait": {"sending"},
    "needs_review": {"complete"},
    "complete": set(),
}
EVENT_STATE = {
    "queued": "queued", "sending": "sending", "retry_wait": "retry_wait",
    "needs_review": "needs_review", "receipt_replayed": "complete",
    "receipt_saved": "complete", "reconciled": "complete",
}


def check(condition, message):
    if not condition:
        raise AssertionError(message)


class Trial:
    def __init__(self, path, case):
        self.planner = path / "planner.sqlite"
        self.ledger = path / "orders.sqlite"
        self.token = secrets.token_urlsafe(24)
        self.now = 1000
        self.payload = {"request_id": "verify-" + case, "order_id": "PUNE-42"}

    def queue(self, port):
        return OrderWorkflow(self.planner, port, self.token, demo_faults=True, clock=lambda: self.now)

    def provider(self):
        return effect_service(self.ledger, protect=True, token=self.token)

    def crash_planner(self, stage, port=0):
        command = [sys.executable, "-m", "tests.workflow_evidence", "_worker",
                   stage, str(self.planner), str(port), str(self.now)]
        payload = json.dumps({"token": self.token, "submission": self.payload})
        result = subprocess.run(command, input=payload, text=True, capture_output=True, timeout=10)
        check(result.returncode == FAULT_EXIT, "planner did not reach the intended crash point: " + result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def rows(self):
        with closing(sqlite3.connect(self.ledger)) as db:
            return db.execute("SELECT operation_key, order_id, receipt_id FROM replacements").fetchall()

    def verify(self, queue, original, attempts, receipt=None):
        task = queue.get(original["task_id"])
        identity = ("dialog_id", "task_id", "operation_key")
        same_identity = all(task[name] == original[name] for name in identity)
        check(same_identity, "dialog ID, task ID or operation key changed")
        check(task["state"] == "complete" and task["attempts"] == attempts, "task did not complete as expected")
        rows = self.rows()
        check(rows == [(task["operation_key"], task["order_id"], task["receipt_id"])],
              "separate ledger disagrees with the completed task")
        if receipt is not None:
            check(task["receipt_id"] == receipt, "retry returned a different receipt")
        states = [EVENT_STATE[item["event"]] for item in task["events"] if item["event"] in EVENT_STATE]
        check(states[0] == "queued" and states[-1] == "complete"
              and all(new in ALLOWED[old] for old, new in zip(states, states[1:])),
              "audit records an invalid state transition")
        return same_identity, len(rows), task["attempts"]


def planner_exits_before_dispatch(trial):
    original = trial.crash_planner("after_enqueue")
    with trial.provider() as (port, _):
        queue = trial.queue(port)
        queued = queue.list_tasks()["tasks"]
        check(len(queued) == 1 and queued[0]["state"] == "queued", "planner lost its saved handoff")
        check(not trial.rows() and queue.run_next(), "queued task did not dispatch on restart")
        return trial.verify(queue, original, attempts=1)


def receiver_unavailable_before_dispatch(trial):
    # Keep this port reserved but without a listening socket.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        queue = trial.queue(reserved.getsockname()[1])
        task = queue.submit(trial.payload)
        check(queue.run_next() and queue.get(task["task_id"])["state"] == "retry_wait",
              "connection failure did not schedule a retry")
    with trial.provider() as (port, _):
        check(not trial.rows(), "unavailable service unexpectedly recorded an effect")
        trial.now += 1
        queue = trial.queue(port)
        check(queue.run_next(), "task did not retry when service became available")
        return trial.verify(queue, task, attempts=2)


def reply_lost_after_commit(trial):
    with trial.provider() as (port, _):
        queue = trial.queue(port)
        task = queue.submit({**trial.payload, "drop_reply": True})
        check(queue.run_next() and queue.get(task["task_id"])["state"] == "retry_wait",
              "dropped reply did not schedule a retry")
        check(len(trial.rows()) == 1, "service did not commit before dropping its reply")
        receipt = trial.rows()[0][2]
    trial.now += 1
    with trial.provider() as (port, _):
        queue = trial.queue(port)
        check(queue.run_next(), "task did not retry after the service restarted")
        return trial.verify(queue, task, attempts=2, receipt=receipt)


def overlapping_expired_attempt(trial):
    with trial.provider() as (port, _):
        queue = trial.queue(port)
        task = queue.submit(trial.payload)
        first = queue.claim()
        trial.now += CLAIM_SECONDS + 1
        second = queue.claim()
        check(first["attempts"] == 1 and second["attempts"] == 2, "task was not reclaimed")
        status, saved = request(port, "POST", "/replacements", key=first["operation_key"],
                                order_id=first["order_id"], token=trial.token)
        check(status == 201, "first dispatch did not create a receipt")
        queue.finish(first, receipt=saved["receipt_id"])
        queue.finish(first, retryable=True, detail="Late timeout.")
        current = queue.get(task["task_id"])
        check(current["state"] == "sending" and current["attempts"] == 2,
              "expired worker overwrote the current attempt")
        status, reply = request(port, "POST", "/replacements", key=second["operation_key"],
                                order_id=second["order_id"], token=trial.token)
        check(status == 200 and reply["receipt_id"] == saved["receipt_id"], "overlapping attempt duplicated the effect")
        queue.finish(second, receipt=reply["receipt_id"], replayed=True)
        return trial.verify(queue, task, attempts=2, receipt=saved["receipt_id"])


def planner_exits_before_saving_receipt(trial):
    with trial.provider() as (port, _):
        queue = trial.queue(port)
        task = queue.submit(trial.payload)
        trial.crash_planner("before_ack", port)
        pending = queue.get(task["task_id"])
        check(pending["state"] == "sending" and pending["receipt_id"] is None and len(trial.rows()) == 1,
              "planner did not exit after the effect and before saving the receipt")
        receipt = trial.rows()[0][2]
        trial.now += CLAIM_SECONDS + 1
        restarted = trial.queue(port)
        check(restarted.run_next(), "restarted worker did not recover the expired claim")
        return trial.verify(restarted, task, attempts=2, receipt=receipt)


CASES = {
    "planner_exits_before_dispatch": planner_exits_before_dispatch,
    "receiver_unavailable_before_send": receiver_unavailable_before_dispatch,
    "reply_lost_after_commit": reply_lost_after_commit,
    "overlapping_expired_attempt": overlapping_expired_attempt,
    "planner_exits_before_ack": planner_exits_before_saving_receipt,
}


def run_cases():
    failures = 0
    print("Threadline dashboard workflow | two SQLite databases and a loopback HTTP receiver", flush=True)
    print(f"{'Case':<34} {'Same identity':<14} {'Ledger rows':<12} {'Attempts':<9} Result", flush=True)
    for name, action in CASES.items():
        try:
            with TemporaryDirectory(prefix="threadline-evidence-") as directory:
                same, effects, attempts = action(Trial(Path(directory), name))
        except Exception as exc:
            failures += 1
            print(f"{name:<34} {'—':<14} {'—':<12} {'—':<9} FAIL: {exc}", flush=True)
        else:
            print(f"{name:<34} {str(same).lower():<14} {effects:<12} {attempts:<9} PASS", flush=True)
    print(f"{len(CASES) - failures}/{len(CASES)} passed.", flush=True)
    return int(bool(failures))


def worker(stage, db, port, now):
    data = json.load(sys.stdin)
    queue = OrderWorkflow(db, int(port), data["token"], clock=lambda: int(now))
    if stage == "after_enqueue":
        print(json.dumps(queue.submit(data["submission"])), flush=True)
        os._exit(FAULT_EXIT)

    def interrupt_before_ack(_task, *, receipt=None, **_):
        check(receipt is not None, "worker did not receive a receipt before the process exit")
        os._exit(FAULT_EXIT)

    queue.finish = interrupt_before_ack
    queue.run_next()


if __name__ == "__main__":
    if sys.argv[1:2] == ["_worker"]:
        worker(*sys.argv[2:])
    else:
        sys.exit(run_cases())
