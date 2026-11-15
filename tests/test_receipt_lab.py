from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from threadline.effect_service import request
from threadline.receipt_lab import compare, effect_service, pending_handoff, plan, read_receipts


class ReceiptLab(unittest.TestCase):
    def test_lost_http_reply_and_restart_with_and_without_deduplication(self):
        control, protected, review = compare("judge-8042", show_unknown=True)
        self.assertEqual(control["effect_count"], 2)
        self.assertNotEqual(control["first_receipt"], control["retry_receipt"])
        self.assertEqual(protected["effect_count"], 1)
        self.assertEqual(protected["first_receipt"], protected["retry_receipt"])
        self.assertEqual(protected["changed_intent_status"], 409)
        self.assertEqual(review["task_state"], "needs_review")
        self.assertEqual(review["remote_receipts"], [review["first_receipt"]])
        self.assertIsNone(review["retry_tool_pid"])
        self.assertIsNone(review["retry_receipt"])
        for result in (control, protected):
            self.assertNotEqual(result["first_service_pid"], result["restarted_service_pid"])
            self.assertNotEqual(result["first_tool_pid"], result["retry_tool_pid"])

    def test_concurrent_retries_share_one_receipt_but_distinct_tasks_do_not(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            task_db = path / "planner.sqlite"
            for task_id in ("a", "b"):
                plan(task_db, "one-dialog", task_id, "42")
            key_a = pending_handoff(task_db, "a")["operation_key"]
            key_b = pending_handoff(task_db, "b")["operation_key"]
            with effect_service(path / "orders.sqlite", protect=True) as (port, _):
                def send(key):
                    return request(port, "POST", "/replacements", key=key, order_id="42")

                with ThreadPoolExecutor(max_workers=4) as pool:
                    outcomes = list(pool.map(send, [key_a] * 4))
                self.assertEqual(sorted(status for status, _ in outcomes), [200, 200, 200, 201])
                self.assertEqual(len({body["receipt_id"] for _, body in outcomes}), 1)
                status, receipt_b = send(key_b)
                self.assertEqual(status, 201)
                self.assertEqual(read_receipts(port, key_a), [outcomes[0][1]["receipt_id"]])
                self.assertEqual(read_receipts(port, key_b), [receipt_b["receipt_id"]])
                self.assertNotEqual(receipt_b["receipt_id"], outcomes[0][1]["receipt_id"])
