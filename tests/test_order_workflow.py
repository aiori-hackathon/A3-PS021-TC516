from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

from threadline.effect_service import request
from threadline.order_workflow import CLAIM_SECONDS, OrderWorkflow
from threadline.receipt_lab import effect_service
from threadline.service import ApiError
from threadline.simulator import build_envelope
from threadline.task_protocol import decode_task, encode_task


class OrderWorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "planner.sqlite"
        self.ledger = Path(tmp.name) / "orders.sqlite"
        self.now = 1000
        self.token = "test-order-service"

    def workflow(self, port, *, faults=True):
        return OrderWorkflow(self.db, port, self.token, demo_faults=faults, clock=lambda: self.now)

    def submit(self, queue, **extra):
        return queue.submit({"request_id": "request-42", "order_id": "JURY-42", **extra})

    def test_both_mock_formats_round_trip_task_identity(self):
        queue = self.workflow(0)
        task = self.submit(queue)
        claim = queue.claim()
        frame = json.loads(claim["handoff"])
        planner_task, planner_key, handoff = decode_task(frame, "planner-agent")
        reply = build_envelope(task["dialog_id"], (2, "tool-agent", "tool", "message", None, "RPL-0000000000"),
                               handoff["message_id"], variant=task["task_id"])
        tool_frame = json.loads(json.dumps(encode_task(planner_task, planner_key, reply)))
        tool_task, tool_key, decoded = decode_task(tool_frame, "tool-agent")
        identity = (task["dialog_id"], task["task_id"], task["operation_key"])
        self.assertNotEqual(frame["wire"]["protocol"], tool_frame["wire"]["protocol"])
        self.assertEqual((handoff["conversation_id"], planner_task, planner_key), identity)
        self.assertEqual((decoded["conversation_id"], tool_task, tool_key), identity)
        self.assertEqual(decoded["causation_id"], handoff["message_id"])

    def test_unknown_schema_goes_to_review_without_creating_an_effect(self):
        with effect_service(self.ledger, protect=True, token=self.token) as (port, _):
            queue = self.workflow(port)
            task_id = self.submit(queue)["task_id"]
            with queue.write() as db:
                frame = json.loads(db.execute("SELECT handoff FROM order_tasks WHERE task_id = ?", (task_id,))
                                   .fetchone()["handoff"])
                frame["schema"] = "unrecognized-version"
                db.execute("UPDATE order_tasks SET handoff = ? WHERE task_id = ?", (json.dumps(frame), task_id))
            self.assertTrue(queue.run_next())
            snapshot = queue.get(task_id)
            self.assertEqual(snapshot["state"], "needs_review")
            self.assertIn("unsupported task schema", snapshot["events"][-1]["detail"])
            self.assertEqual(queue.evidence(task_id)["effect_count"], 0)

    def test_illegal_state_transition_is_rejected_by_database(self):
        queue = self.workflow(0)
        task_id = self.submit(queue)["task_id"]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "illegal order task transition"), queue.write() as db:
            db.execute("UPDATE order_tasks SET state = 'complete', receipt_id = 'RPL-0000000000' WHERE task_id = ?",
                       (task_id,))
        self.assertEqual(queue.get(task_id)["state"], "queued")

    def test_reopens_both_databases_and_recovers_expired_claim(self):
        with effect_service(self.ledger, protect=True, token=self.token) as (port, _):
            queue = self.workflow(port)
            task = self.submit(queue)
            first_claim = queue.claim()
            original = request(port, "POST", "/replacements", token=self.token,
                               key=task["operation_key"], order_id=task["order_id"])[1]["receipt_id"]
            # Simulate a worker disappearing before its completion is recorded.
        self.now += CLAIM_SECONDS + 1
        with effect_service(self.ledger, protect=True, token=self.token) as (port, _):
            queue = self.workflow(port)
            self.assertTrue(queue.run_next())
            saved = queue.get(task["task_id"])
            queue.finish(first_claim, receipt="RPL-0000000000")
            queue.finish(first_claim, retryable=True, detail="Late timeout.")
            queue.finish(first_claim, detail="Late permanent error.")
            self.assertEqual(queue.get(task["task_id"]), saved)
            self.assertEqual((saved["state"], saved["receipt_id"], saved["attempts"]), ("complete", original, 2))
            self.assertEqual(queue.evidence(task["task_id"]), {"receipt_ids": [original], "effect_count": 1})
            self.assertEqual([e["event"] for e in saved["events"]],
                             ["queued", "sending", "worker_recovered", "sending", "receipt_replayed"])

    def test_dropped_reply_retries_same_key_and_repeated_submission_is_deduplicated(self):
        with effect_service(self.ledger, protect=True, token=self.token) as (port, _):
            queue = self.workflow(port)
            task = self.submit(queue, drop_reply=True)
            self.assertEqual(self.submit(queue, drop_reply=True)["task_id"], task["task_id"])
            with self.assertRaises(ApiError) as conflict:
                queue.submit({"request_id": "request-42", "order_id": "DIFFERENT", "drop_reply": True})
            self.assertEqual(conflict.exception.status, 409)
            self.assertTrue(queue.run_next())
            self.assertEqual(queue.get(task["task_id"])["state"], "retry_wait")
            observed = queue.evidence(task["task_id"])["receipt_ids"]
            self.assertEqual(len(observed), 1)
            self.assertFalse(queue.run_next())
            self.now += 1
            self.assertTrue(queue.run_next())
            self.assertEqual(queue.get(task["task_id"])["receipt_id"], observed[0])
            self.assertEqual(queue.evidence(task["task_id"])["receipt_ids"], observed)
            self.assertEqual(len(queue.list_tasks()["tasks"]), 1)
            with closing(sqlite3.connect(self.db)) as db, self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM order_events")

    def test_review_requires_ledger_verification_and_never_resends(self):
        with effect_service(self.ledger, protect=True, token=self.token) as (port, _):
            queue = self.workflow(port)
            task = self.submit(queue, policy="review", drop_reply=True)
            task_id = task["task_id"]
            self.assertTrue(queue.run_next())
            self.assertEqual(queue.get(task_id)["state"], "needs_review")
            self.now += 100
            self.assertFalse(queue.run_next())
            with self.assertRaises(ApiError) as mismatch:
                queue.reconcile(task_id, {"receipt_id": "RPL-0000000000"})
            self.assertEqual(mismatch.exception.code, "receipt_unverified")
            original = queue.evidence(task_id)["receipt_ids"][0]
            body = {"receipt_id": original}
            saved = queue.reconcile(task_id, body)
            self.assertEqual((saved["state"], saved["receipt_id"], saved["attempts"]), ("complete", original, 1))
            self.assertEqual(queue.reconcile(task_id, body), saved)
            self.assertEqual(queue.evidence(task_id)["effect_count"], 1)

    def test_retries_are_bounded_and_faults_require_opt_in(self):
        with effect_service(self.ledger, protect=True, token=self.token) as (port, _):
            queue = self.workflow(port, faults=False)
            with self.assertRaises(ApiError) as fault:
                self.submit(queue, drop_reply=True)
            self.assertEqual(fault.exception.status, 403)
            task = self.submit(queue)
            self.assertEqual(request(port, "GET", "/replacements/" + task["operation_key"])[0], 401)
        for _ in range(3):
            self.assertTrue(queue.run_next())
            self.now += 10
        task = queue.get(task["task_id"])
        self.assertEqual((task["state"], task["attempts"]), ("needs_review", 3))
        self.assertFalse(queue.run_next())
