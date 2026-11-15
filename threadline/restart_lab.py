"""Offline PS-021 restart experiment using two separate mock adapter processes.

Run: python3 -m threadline.restart_lab [--scenario after_commit] [--json]
The planner and tool exchange native payloads over subprocess stdin/stdout.
Each experiment uses a temporary SQLite file; the dashboard DB is untouched.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import uuid

from .adapters import from_wire, to_wire
from .events import validate
from .simulator import build_envelope

SCHEMA_VERSION = "threadline-experimental-v1"
DISCONNECT = 75
SCENARIOS = {
    "before_delivery": "Planner exits after saving handoff, before delivering it",
    "before_commit": "Tool exits after inserting effect, before committing",
    "after_commit": "Tool commits effect, then exits without sending its reply",
    "overlapping_retry": "Two tool processes receive overlapping delivery attempts",
    "before_ack": "Planner receives reply, then exits before acknowledging it",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    dialog_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    handoff TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'committed', 'complete')),
    PRIMARY KEY (dialog_id, task_id)
);
CREATE TABLE IF NOT EXISTS replacements (
    operation_key TEXT PRIMARY KEY REFERENCES tasks(operation_key),
    replacement_id TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS guard_task_transition
BEFORE UPDATE OF state ON tasks WHEN NOT (
    OLD.state = NEW.state OR
    (OLD.state = 'pending' AND NEW.state = 'committed') OR
    (OLD.state = 'committed' AND NEW.state = 'complete')
)
BEGIN SELECT RAISE(ABORT, 'illegal task transition'); END;
"""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def worker(db, action, data, disconnect):
    """Execute exactly one adapter action in the current process."""
    with closing(sqlite3.connect(db, isolation_level=None, timeout=5)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        if action == "plan":
            cid, task = data["dialog_id"], data["task_id"]
            step = (1, "planner-agent", "assistant", "handoff", "tool-agent", "replace order #4821")
            env = build_envelope(cid, step, None, variant=task)
            # The dialog in this experiment has exactly one handoff. The
            # existing envelope key identifies that slot, even across restarts.
            key = env["idempotency_key"]
            wire = to_wire(env)
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, 'pending')",
                         (cid, task, key, json.dumps(wire)))
            reply = request(cid, task, wire)
        else:
            require(data["schema"] == SCHEMA_VERSION, "unsupported schema")
            cid, task = data["dialog_id"], data["task_id"]
            row = conn.execute(
                """SELECT t.operation_key, t.handoff, t.state, r.replacement_id
                   FROM tasks t LEFT JOIN replacements r USING (operation_key)
                   WHERE dialog_id = ? AND task_id = ?""", (cid, task)
            ).fetchone()
            require(row is not None, "unknown dialog/task pair")
            key, saved_wire, state, replacement_id = row
            handoff = json.loads(saved_wire)
            if action == "recover":
                reply = {**request(cid, task, handoff), "state": state, "replacement_id": replacement_id}
            elif action == "tool":
                env = from_wire("planner-agent", data["wire"])
                require(not validate(env) and data["wire"] == handoff, "handoff differs from saved request")
                duplicate = state != "pending"
                if not duplicate:
                    replacement_id = "RPL-" + uuid.uuid4().hex[:10]
                    conn.execute("INSERT INTO replacements VALUES (?, ?)", (key, replacement_id))
                    conn.execute("UPDATE tasks SET state = 'committed' WHERE operation_key = ?", (key,))
                    if disconnect == "before_commit":
                        os._exit(DISCONNECT)
                reply = {**request(cid, task, tool_result(cid, task, env["message_id"], replacement_id)),
                         "operation_key": key, "duplicate": duplicate}
            elif action == "ack":
                env = from_wire("tool-agent", data["wire"])
                expected_wire = tool_result(cid, task, from_wire("planner-agent", handoff)["message_id"],
                                            replacement_id)
                require(state in ("committed", "complete") and not validate(env)
                        and data["operation_key"] == key and data["wire"] == expected_wire,
                        "reply does not match the committed operation")
                if disconnect == "before_ack":
                    os._exit(DISCONNECT)
                conn.execute("UPDATE tasks SET state = 'complete' WHERE operation_key = ?", (key,))
                reply = {"state": "complete", "replacement_id": replacement_id}
            else:
                raise ValueError(f"unknown action {action}")
        conn.commit()
        if disconnect in ("after_commit", "before_delivery"):
            os._exit(DISCONNECT)
        return reply


def request(cid, task, wire):
    return {"schema": SCHEMA_VERSION, "dialog_id": cid, "task_id": task, "wire": wire}


def tool_result(cid, task, cause, replacement_id):
    step = (2, "tool-agent", "tool", "message", None, replacement_id)
    return to_wire(build_envelope(cid, step, cause, variant=task))


def call(db, action, data, disconnect="none"):
    args = [sys.executable, "-m", "threadline.restart_lab", "--worker", action,
            "--db", str(db), "--disconnect", disconnect]
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as proc:
        try:
            output, err = proc.communicate(json.dumps(data), timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    want = 0 if disconnect == "none" else DISCONNECT
    if proc.returncode != want:
        raise RuntimeError(f"{action} exited {proc.returncode}; expected {want}: {err}")
    return {"action": action, "pid": proc.pid, "exit": proc.returncode, "fault": disconnect,
            "reply": json.loads(output) if output else None}


def effect_count(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT COUNT(*) FROM replacements").fetchone()[0]


def run_trial(scenario, db):
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(SCHEMA)
    identity = {"schema": SCHEMA_VERSION, "dialog_id": "dialog-" + uuid.uuid4().hex[:8],
                "task_id": "task-" + uuid.uuid4().hex[:8]}
    steps = []

    def invoke(action, payload, fault="none"):
        item = call(db, action, payload, fault)
        steps.append({k: v for k, v in item.items() if k != "reply"})
        return item["reply"]

    planned = invoke("plan", identity, scenario if scenario == "before_delivery" else "none")
    checkpoint = invoke("recover", identity)
    require(checkpoint["state"] == "pending" and checkpoint["replacement_id"] is None,
            "planner must recover its pending handoff")
    if planned is not None:
        require(checkpoint["wire"] == planned["wire"], "planner changed the handoff on restart")

    checkpoint_after_fault = None
    if scenario in ("before_commit", "after_commit"):
        invoke("tool", checkpoint, scenario)
        checkpoint_after_fault = invoke("recover", identity)
        expected = 1 if scenario == "after_commit" else 0
        require(effect_count(db) == expected
                and checkpoint_after_fault["state"] == ("committed" if expected else "pending"),
                "durable state and effect must agree after tool exit")
        reply = invoke("tool", checkpoint_after_fault)
        if expected:
            require(reply["duplicate"], "lost reply triggered a second execution")
            require(from_wire("tool-agent", reply["wire"])["content"]
                    == checkpoint_after_fault["replacement_id"], "retry did not return committed result")
    elif scenario == "overlapping_retry":
        with ThreadPoolExecutor(max_workers=2) as pool:
            deliveries = list(pool.map(lambda _: call(db, "tool", checkpoint), range(2)))
        steps.extend({k: v for k, v in item.items() if k != "reply"} for item in deliveries)
        a, b = (item["reply"] for item in deliveries)
        require(a["duplicate"] != b["duplicate"] and a["wire"] == b["wire"],
                "concurrent retry did not return the same result")
        reply = a
    else:
        reply = invoke("tool", checkpoint)
        if scenario == "before_ack":
            invoke("ack", reply, scenario)
            checkpoint_after_fault = invoke("recover", identity)
            require(checkpoint_after_fault["state"] == "committed", "reply was acknowledged before exit")
            retry = invoke("tool", checkpoint_after_fault)
            require(retry["duplicate"] and retry["wire"] == reply["wire"],
                    "caller restart did not preserve the committed result")
            reply = retry

    state_after_fault = checkpoint_after_fault["state"] if checkpoint_after_fault else None
    if scenario == "before_delivery":
        state_after_fault = checkpoint["state"]
    acknowledged = invoke("ack", reply)
    recovered = invoke("recover", identity)
    late = invoke("tool", recovered)
    count = effect_count(db)
    require(all(recovered[k] == reply[k] == identity[k] for k in identity), "task identity changed")
    require(recovered["wire"] == checkpoint["wire"] and recovered["state"] == "complete"
            and recovered["replacement_id"] == acknowledged["replacement_id"], "completion was not durable")
    require(late["duplicate"] and late["wire"] == reply["wire"] and count == 1,
            "late retry changed the result or repeated the effect")

    return {"scenario": scenario, **identity, "operation_key": reply["operation_key"],
            "replacement_id": recovered["replacement_id"], "effects": count,
            "state": recovered["state"],
            "state_after_fault": state_after_fault,
            "committed_before_retry": checkpoint_after_fault["replacement_id"] if checkpoint_after_fault else None,
            "tool_retry_reused_result": reply["duplicate"] or scenario == "overlapping_retry",
            "late_retry_reused_result": late["duplicate"], "processes": steps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--summary", action="store_true", help="show the five-case evidence table")
    parser.add_argument("--worker", choices=("plan", "recover", "tool", "ack"), help=argparse.SUPPRESS)
    parser.add_argument("--db", help=argparse.SUPPRESS)
    parser.add_argument("--disconnect", default="none", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.db, args.worker, json.load(sys.stdin), args.disconnect)))
        return
    with TemporaryDirectory(prefix="threadline-restart-") as tmp:
        results = [run_trial(s, Path(tmp) / (s + ".db")) for s in ([args.scenario] if args.scenario else SCENARIOS)]
    if args.json:
        print(json.dumps(results, indent=2))
        return
    print(f"Threadline restart lab | {SCHEMA_VERSION}\n")
    if args.summary:
        print(f"{'Case':<19} {'Separate processes':<20} {'Identity preserved':<20} {'Ledger rows':<12} Result")
        for row in results:
            separate = "yes" if len({item["pid"] for item in row["processes"]}) > 1 else "no"
            # run_trial raises unless the identity is preserved and the ledger has one effect.
            print(f"{row['scenario']:<19} {separate:<20} {'yes':<20} {row['effects']:<12} PASS")
        return
    for row in results:
        print(f"PASS  {SCENARIOS[row['scenario']]}")
        if row["state_after_fault"]:
            saved = row["committed_before_retry"]
            print(f"      Reopened SQLite after exit: {row['state_after_fault']}"
                  + (f" ({saved} already on disk)" if saved else ""))
        print(f"      {row['dialog_id']} / {row['task_id']} | {row['operation_key']}")
        for i, proc in enumerate(row["processes"]):
            if proc["exit"] == DISCONNECT:
                restarted = next(p for p in row["processes"][i + 1:] if p["action"] == proc["action"]
                                 or (proc["action"] == "plan" and p["action"] == "recover"))
                print(f"      {proc['action']} exited without reply (PID {proc['pid']}); "
                      f"{restarted['action']} ran in a new process (PID {restarted['pid']})")
        if row["tool_retry_reused_result"]:
            print("      Retried tool call returned the existing result; no new effect")
        print(f"      {row['replacement_id']} | {row['state']} | effect rows: {row['effects']} "
              f"| late retry returned the same replacement\n")


if __name__ == "__main__":
    main()
