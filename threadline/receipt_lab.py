"""Compare retries against a separate HTTP service with and without deduplication."""

import argparse
from contextlib import closing, contextmanager
from http.client import RemoteDisconnected
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

from .adapters import from_wire, to_wire
from .effect_service import request
from .events import sha, validate
from .restart_lab import DISCONNECT, require, tool_result
from .simulator import build_envelope


SCHEMA = "threadline-receipt-lab-v1"
TASKS = """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        dialog_id TEXT NOT NULL,
        operation_key TEXT UNIQUE NOT NULL,
        handoff TEXT NOT NULL,
        receipt_id TEXT,
        needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1))
    )
"""


def plan(db, dialog_id, task_id, order_id):
    """Persist the original intent and operation key before sending anything."""
    step = (1, "planner-agent", "assistant", "handoff", "tool-agent", order_id)
    envelope = build_envelope(dialog_id, step, None, variant=task_id)
    identity = json.dumps(["replace_order", dialog_id, task_id], separators=(",", ":"))
    key = "idk_" + sha(identity, 24)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(TASKS)
        conn.execute("INSERT INTO tasks (task_id, dialog_id, operation_key, handoff) VALUES (?, ?, ?, ?)",
                     (task_id, dialog_id, key, json.dumps(to_wire(envelope))))


def pending_handoff(db, task_id):
    with closing(sqlite3.connect(db)) as conn:
        dialog_id, key, wire = conn.execute(
            "SELECT dialog_id, operation_key, handoff FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    return {"schema": SCHEMA, "dialog_id": dialog_id, "task_id": task_id,
            "operation_key": key, "wire": json.loads(wire)}


def acknowledge(db, handoff, reply):
    source = from_wire("planner-agent", handoff["wire"])
    result = from_wire("tool-agent", reply["wire"])
    require(all(reply[k] == handoff[k] for k in ("schema", "dialog_id", "task_id", "operation_key"))
            and not validate(result) and result["conversation_id"] == handoff["dialog_id"]
            and result["causation_id"] == source["message_id"]
            and isinstance(result["content"], str) and result["content"].startswith("RPL-"),
            "tool receipt does not match the saved handoff")
    with closing(sqlite3.connect(db)) as conn, conn:
        change = conn.execute(
            """UPDATE tasks SET receipt_id = ?
               WHERE task_id = ? AND operation_key = ? AND needs_review = 0
                 AND (receipt_id IS NULL OR receipt_id = ?)""",
            (result["content"], handoff["task_id"], handoff["operation_key"], result["content"])
        )
        require(change.rowcount == 1, "task already acknowledged a different receipt")
    return result["content"]


def hold_for_review(db, task_id):
    with closing(sqlite3.connect(db)) as conn, conn:
        change = conn.execute(
            "UPDATE tasks SET needs_review = 1 WHERE task_id = ? AND receipt_id IS NULL", (task_id,)
        )
        require(change.rowcount == 1, "can only hold an unresolved task")


def recovery_decision(db, task_id):
    with closing(sqlite3.connect(db)) as conn:
        receipt, needs_review = conn.execute(
            "SELECT receipt_id, needs_review FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    if receipt is not None:
        return "complete"
    return "needs_review" if needs_review else "retry"


@contextmanager
def effect_service(db, *, protect, token=None):
    ready = db.with_suffix(".ready")
    if ready.exists():
        ready.unlink()
    cmd = [sys.executable, "-m", "threadline.effect_service", "--db", str(db), "--ready-file", str(ready)]
    if os.name == "posix":
        cmd.extend(["--parent-pid", str(os.getpid())])
    if not protect:
        cmd.append("--without-deduplication")
    env = os.environ.copy()
    env.pop("THREADLINE_ORDER_TOKEN", None)
    if token is not None:
        env["THREADLINE_ORDER_TOKEN"] = token
    with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env) as proc:
        try:
            deadline = time.monotonic() + 8
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.015)
            if not ready.exists():
                detail = proc.stderr.read() if proc.poll() is not None else "startup timed out"
                raise RuntimeError(f"order service failed to start: {detail}")
            yield int(ready.read_text(encoding="ascii")), proc.pid
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def tool(port, handoff, *, drop_reply):
    envelope = from_wire("planner-agent", handoff["wire"])
    require(handoff["schema"] == SCHEMA and not validate(envelope)
            and envelope["conversation_id"] == handoff["dialog_id"]
            and envelope["kind"] == "handoff" and envelope["target_adapter"] == "tool-agent",
            "invalid planner handoff")
    try:
        status, result = request(port, "POST", "/replacements", key=handoff["operation_key"],
                                 order_id=envelope["content"], drop_reply=drop_reply)
    except RemoteDisconnected:
        os._exit(DISCONNECT)
    require(status in (200, 201), f"order service refused the request: {result}")
    reply = {**handoff, "wire": tool_result(handoff["dialog_id"], handoff["task_id"],
                                           envelope["message_id"], result["receipt_id"])}
    return {"reply": reply, "replayed": result["replayed"]}


def call_tool(port, handoff, *, drop_reply=False):
    cmd = [sys.executable, "-m", "threadline.receipt_lab", "--tool-port", str(port)]
    if drop_reply:
        cmd.append("--drop-reply")
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as proc:
        try:
            out, err = proc.communicate(json.dumps(handoff), timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    expected = DISCONNECT if drop_reply else 0
    require(proc.returncode == expected and bool(out) == (not drop_reply),
            f"tool exited {proc.returncode}, expected {expected}: {err or out}")
    return proc.pid, json.loads(out) if out else None


def read_receipts(port, key):
    status, body = request(port, "GET", f"/replacements/{key}")
    require(status == 200, f"order ledger lookup failed: {body}")
    return body["receipt_ids"]


def run_trial(workdir, order_id, *, protect, hold_on_disconnect=False):
    """Observe effects from the remote ledger, independently of the tool reply."""
    task_db, effect_db = workdir / "planner.sqlite", workdir / "orders.sqlite"
    dialog_id, task_id = "dialog-" + uuid.uuid4().hex[:8], "task-" + uuid.uuid4().hex[:8]
    plan(task_db, dialog_id, task_id, order_id)
    handoff = pending_handoff(task_db, task_id)
    key = handoff["operation_key"]

    with effect_service(effect_db, protect=protect) as (port, first_service):
        # Record the review policy before sending. A crash during the call
        # must not turn an unresolved, unprotected effect into an automatic retry.
        if hold_on_disconnect:
            hold_for_review(task_db, task_id)
        first_tool, _ = call_tool(port, handoff, drop_reply=True)
        first_receipts = read_receipts(port, key)
        require(len(first_receipts) == 1, "HTTP reply must be dropped after exactly one effect")

    # Restart the service as well as the tool. Read back the persisted handoff.
    # The first receipt above is evidence only: the retry receives just the handoff.
    with effect_service(effect_db, protect=protect) as (port, restarted_service):
        recovered = pending_handoff(task_db, task_id)
        require(recovered == handoff, "planner checkpoint changed")
        decision = recovery_decision(task_db, task_id)
        next_tool, outcome, receipt = None, None, None
        if decision == "retry":
            next_tool, outcome = call_tool(port, recovered)
            receipt = acknowledge(task_db, recovered, outcome["reply"])
        observed = read_receipts(port, key)
        conflict = None
        if protect:
            other_order = "different" if order_id != "different" else "other"
            conflict, _ = request(port, "POST", "/replacements", key=key, order_id=other_order)
            require(conflict == 409 and read_receipts(port, key) == observed,
                    "changed intent created an effect under the same key")

    if hold_on_disconnect:
        require(decision == "needs_review" and observed == first_receipts,
                "an uncertain operation must remain held without a second effect")
    else:
        require(outcome is not None and outcome["replayed"] == protect
                and len(observed) == (1 if protect else 2),
                "remote ledger did not match the selected retry policy")
        require(receipt == observed[-1] and first_receipts[0] == observed[0],
                "acknowledged receipt or first committed effect was missing")
    mode = "review" if hold_on_disconnect else ("protected" if protect else "control")
    return {"mode": mode, "order_id": order_id,
            "dialog_id": dialog_id, "task_id": task_id, "operation_key": key,
            "first_receipt": first_receipts[0], "retry_receipt": receipt,
            "remote_receipts": observed, "effect_count": len(observed),
            "task_state": recovery_decision(task_db, task_id),
            "replayed": outcome["replayed"] if outcome else False,
            "first_service_pid": first_service, "restarted_service_pid": restarted_service,
            "first_tool_pid": first_tool, "retry_tool_pid": next_tool, "changed_intent_status": conflict}


def run_comparison(root, order_id, show_unknown):
    results = []
    modes = [("control", False, False), ("protected", True, False)]
    if show_unknown:
        modes.append(("review", False, True))
    for name, protect, hold in modes:
        workdir = root / name
        workdir.mkdir()
        results.append(run_trial(workdir, order_id, protect=protect, hold_on_disconnect=hold))
    return results


def compare(order_id, *, show_unknown=False, evidence_dir=None):
    if evidence_dir is not None:
        evidence_dir.mkdir()
        results = run_comparison(evidence_dir, order_id, show_unknown)
        (evidence_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        return results
    with TemporaryDirectory(prefix="threadline-receipt-") as temp:
        return run_comparison(Path(temp), order_id, show_unknown)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order", default="4821", help="mock order ID to replace")
    parser.add_argument("--json", action="store_true", help="emit machine-readable evidence")
    parser.add_argument("--show-unknown", action="store_true",
                        help="also demonstrate holding an unresolved operation for review")
    parser.add_argument("--evidence-dir", type=Path, help="save both databases in a new directory")
    parser.add_argument("--tool-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--drop-reply", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.tool_port is not None:
        print(json.dumps(tool(args.tool_port, json.load(sys.stdin), drop_reply=args.drop_reply)))
        return
    if not 0 < len(args.order) <= 64 or not all(c.isascii() and (c.isalnum() or c == "-") for c in args.order):
        parser.error("--order must be 1–64 ASCII letters, digits, or hyphens")
    try:
        results = compare(args.order, show_unknown=args.show_unknown, evidence_dir=args.evidence_dir)
    except FileExistsError as exc:
        parser.error(f"evidence directory already exists: {exc.filename}")
    if args.json:
        print(json.dumps(results, indent=2))
        return
    print(f"Threadline HTTP receipt lab | order {args.order}")
    print("Each run: order service commits, drops the HTTP reply, and restarts.\n")
    labels = {"control": "CONTROL · deduplication off",
              "protected": "PROTECTED · stable operation key",
              "review": "REVIEW · receiver has no safe retry contract"}
    for row in results:
        print(labels[row["mode"]])
        print(f"  Dialog {row['dialog_id']} | task {row['task_id']}")
        print(f"  Operation key {row['operation_key']}")
        print(f"  Order service PID {row['first_service_pid']} → {row['restarted_service_pid']}")
        if row["task_state"] == "needs_review":
            print("  Planner kept the task in review and sent no retry.")
        else:
            print(f"  Tool PID {row['first_tool_pid']} → {row['retry_tool_pid']}")
            print(f"  First receipt {row['first_receipt']} | retry receipt {row['retry_receipt']}")
        print(f"  Effects observed in the order service: {row['effect_count']}")
        if row["changed_intent_status"]:
            print("  Same key, different order: HTTP 409; effect count unchanged")
        print()


if __name__ == "__main__":
    main()
