"""End-to-end tests of the gateway service with every simulated scenario."""

import unittest

from threadline.service import ApiError, Threadline
from threadline.simulator import SCENARIOS

# scenario -> (status, dialog_state, total, accepted, duplicates, gaps, conflicts, reordered, resumable)
EXPECTED = {
    "happy_path":          ("consistent", "complete", 9, 9, 0, [], 0, 0, False),
    "interrupted_handoff": ("recovered", "interrupted_handoff", 8, 7, 0, [], 0, 0, True),
    "duplicates":          ("recovered", "complete", 11, 9, 2, [], 0, 0, False),
    "out_of_order":        ("recovered", "complete", 9, 9, 0, [], 0, 2, False),
    "delayed":             ("incomplete", "ended_with_gaps", 8, 8, 0, [6], 0, 0, False),
    "missing":             ("incomplete", "ended_with_gaps", 8, 8, 0, [5], 0, 0, False),
    "conflict":            ("recovered_with_conflicts", "complete", 10, 9, 0, [], 1, 0, False),
    "chaos":               ("incomplete", "interrupted_handoff", 10, 6, 2, [6], 1, 1, False),
}


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.app = Threadline()

    def run_and_recover(self, name):
        cid = self.app.run_experiment(name)["conversation"]["conversation_id"]
        return cid, self.app.recover(cid)

    def test_every_scenario_matches_documented_outcome(self):
        self.assertEqual(set(EXPECTED), set(SCENARIOS))
        for name, exp in EXPECTED.items():
            with self.subTest(scenario=name):
                _, r = self.run_and_recover(name)
                m = r["metrics"]
                got = (r["status"], r["dialog_state"], m["total_events"], m["accepted_events"],
                       m["duplicates_removed"], r["gaps"], m["conflicts"], m["reordered_events"],
                       r["resume"]["resumable"])
                self.assertEqual(got, exp)

    def test_scenarios_are_deterministic(self):
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                _, a = self.run_and_recover(name)
                _, b = self.run_and_recover(name)
                strip = lambda r: [(d["sequence"], d["adapter"], d["verdict"], d["rule"]) for d in r["decisions"]]
                self.assertEqual(strip(a), strip(b))
                self.assertEqual(a["metrics"], b["metrics"])

    def test_recovery_is_idempotent(self):
        cid, first = self.run_and_recover("chaos")
        second = self.app.recover(cid)
        self.assertEqual(second["idempotency_check"]["verdict"], "identical")
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(len(self.app.store.events(cid)), first["metrics"]["total_events"])  # recovery never writes events

    def test_delayed_message_closes_gap(self):
        cid, r = self.run_and_recover("delayed")
        self.assertEqual(r["gaps"], [6])
        out = self.app.deliver_pending(cid)
        self.assertEqual(out["delivered"], 1)
        self.assertEqual(out["outcomes"][0]["outcome"], "accepted")
        r2 = self.app.recover(cid)
        self.assertEqual(r2["gaps"], [])
        self.assertEqual(r2["status"], "recovered")
        self.assertEqual(r2["metrics"]["reordered_events"], 1)
        self.assertEqual(r2["idempotency_check"]["verdict"], "log_changed")
        self.assertEqual(self.app.deliver_pending(cid)["delivered"], 0)

    def test_interrupted_handoff_resumes_exactly_once(self):
        cid, r = self.run_and_recover("interrupted_handoff")
        self.assertEqual(r["resume"]["resume_adapter"], "tool-agent")
        out = self.app.resume(cid)
        self.assertEqual([o["event"]["sequence"] for o in out["outcomes"]], [8, 9])
        self.assertTrue(all(o["processed"] for o in out["outcomes"]))
        with self.assertRaises(ApiError) as ctx:
            self.app.resume(cid)
        self.assertEqual(ctx.exception.status, 409)
        final = self.app.recover(cid)
        self.assertEqual(final["dialog_state"], "complete")
        self.assertEqual([c["sequence"] for c in final["context"]], list(range(1, 10)))
        self.assertEqual(final["context"][7]["causation_id"], final["context"][6]["message_id"])

    def test_missing_message_blocks_resume(self):
        cid = self.app.run_experiment(faults=["missing", "adapter_timeout"])["conversation"]["conversation_id"]
        r = self.app.recover(cid)
        self.assertEqual(r["gaps"], [5])
        with self.assertRaises(ApiError) as ctx:
            self.app.resume(cid)
        self.assertIn("missing sequence 5", ctx.exception.details[0])

    def test_chaos_full_workflow(self):
        cid, r = self.run_and_recover("chaos")
        self.assertFalse(r["resume"]["resumable"])
        self.app.deliver_pending(cid)
        r = self.app.recover(cid)
        self.assertEqual(r["status"], "recovered_with_conflicts")
        self.assertTrue(r["resume"]["resumable"])
        self.app.resume(cid)
        r = self.app.recover(cid)
        self.assertEqual(r["dialog_state"], "complete")
        self.assertEqual(r["gaps"], [])
        assistants = [c for c in r["context"] if c["role"] == "assistant" and c["kind"] == "message"]
        self.assertEqual(len({c["sequence"] for c in assistants}), len(assistants))

    def test_live_ingest_classifies_duplicates(self):
        env = {"message_id": "x1", "sequence": 1, "adapter": "web-chat", "role": "user", "content": "hello"}
        self.assertEqual(self.app.ingest_external("conv_ext", env)["outcome"], "accepted")
        self.assertEqual(self.app.ingest_external("conv_ext", env)["outcome"], "duplicate")
        retry = {**env, "message_id": "x2", "content": "hello again"}
        self.assertEqual(self.app.ingest_external("conv_ext", retry)["outcome"], "duplicate")
        other = {**env, "message_id": "x3", "role": "assistant"}
        self.assertEqual(self.app.ingest_external("conv_ext", other)["outcome"], "conflict")
        with self.assertRaises(ApiError) as ctx:
            self.app.resume("conv_ext")
        self.assertEqual(ctx.exception.status, 409)

    def test_correlation_fields_across_adapters(self):
        from threadline.events import make_idempotency_key
        cid = self.app.run_experiment("interrupted_handoff")["conversation"]["conversation_id"]
        self.app.recover(cid)
        self.app.resume(cid)
        events = self.app.store.events(cid)
        dialog = [e for e in events if e["kind"] != "signal"]
        self.assertEqual({e["conversation_id"] for e in events}, {cid})                  # stable conversation_id
        self.assertEqual(len({e["message_id"] for e in events}), len(events))            # unique message_id
        self.assertEqual(sorted(e["sequence"] for e in dialog), list(range(1, 10)))       # contiguous sequences
        self.assertEqual({e["adapter"] for e in dialog}, {"web-chat", "planner-agent", "tool-agent"})
        by_id = {e["message_id"]: e for e in dialog}
        for e in dialog[1:]:                                                             # causation links back one step
            self.assertEqual(by_id[e["causation_id"]]["sequence"], e["sequence"] - 1)
        for e in events:                                                                 # deterministic keys
            self.assertEqual(e["idempotency_key"], make_idempotency_key(
                cid, e["sequence"], e["role"], e["causation_id"], e["kind"]))

    def test_bad_inputs(self):
        cases = [
            (lambda: self.app.run_experiment("nope"), 400, "unknown_scenario"),
            (lambda: self.app.run_experiment(faults=["meteor"]), 400, "unknown_fault"),
            (lambda: self.app.run_experiment(faults="duplicate"), 400, "invalid_faults"),
            (lambda: self.app.recover("conv_missing"), 404, "conversation_not_found"),
            (lambda: self.app.ingest_external("c", {"adapter": "fax", "message_id": "a"}), 400, "unknown_adapter"),
            (lambda: self.app.ingest_external("c", {"adapter": "web-chat"}), 400, "invalid_envelope"),
            (lambda: self.app.ingest_external("c", {"conversation_id": "d", "adapter": "web-chat"}), 400, "conversation_mismatch"),
        ]
        for fn, status, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ApiError) as ctx:
                    fn()
                self.assertEqual((ctx.exception.status, ctx.exception.code), (status, code))

    def test_custom_fault_mix_is_named(self):
        snap = self.app.run_experiment(faults=["duplicate", "missing"])
        self.assertEqual(snap["conversation"]["scenario"], "custom")
        snap = self.app.run_experiment(faults=["conflict"])
        self.assertEqual(snap["conversation"]["scenario"], "conflict")


if __name__ == "__main__":
    unittest.main()
