"""Unit tests for the pure recovery engine (threadline.recovery.replay)."""

import random
import unittest

from tests.helpers import CID, ev, linear, renumber
from threadline.recovery import replay


def verdicts(result):
    return {d["event_id"]: (d["verdict"], d["rule"]) for d in result["decisions"]}


class CleanDialog(unittest.TestCase):
    def test_clean_log_is_consistent(self):
        r = replay(linear(5), CID)
        self.assertEqual(r["status"], "consistent")
        self.assertEqual([c["sequence"] for c in r["context"]], [1, 2, 3, 4, 5])
        self.assertEqual(r["metrics"]["accepted_events"], 5)
        self.assertEqual(r["gaps"], [])

    def test_empty_log(self):
        r = replay([], CID)
        self.assertEqual(r["status"], "empty")
        self.assertFalse(r["resume"]["resumable"])

    def test_correlation_stable_across_adapter_switches(self):
        r = replay(linear(6), CID)
        self.assertEqual({c["adapter"] for c in r["context"]}, {"web-chat", "planner-agent", "tool-agent"})
        for prev, cur in zip(r["context"], r["context"][1:]):
            self.assertEqual(cur["causation_id"], prev["message_id"])
            self.assertEqual(cur["warnings"], [])


class Duplicates(unittest.TestCase):
    def test_redelivery_same_message_id_removed(self):
        log = renumber(linear(3) + [linear(3)[0]])
        r = replay(log, CID)
        self.assertEqual(r["metrics"]["duplicates_removed"], 1)
        self.assertEqual(verdicts(r)["evt_000004"], ("duplicate", "DUPLICATE_DELIVERY"))
        self.assertEqual(len(r["context"]), 3)

    def test_retry_with_new_message_id_yields_single_assistant_response(self):
        base = linear(2)
        retry = ev(3, 2, adapter="planner-agent", role="assistant", cause="m1", msg="m2-retry",
                   content="different wording")
        r = replay(base + [retry], CID)
        self.assertEqual(verdicts(r)["evt_000003"], ("duplicate", "DUPLICATE_RETRY"))
        assistant = [c for c in r["context"] if c["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["message_id"], "m2")  # first arrival wins
        self.assertIn("wording differed", r["decisions"][2]["reason"])

    def test_causation_via_duplicate_alias_resolves(self):
        # seq 3 was caused by the retry's message_id; it must still link to accepted seq 2.
        log = [ev(1, 1, "web-chat", "user"), ev(2, 2, cause="m1"),
               ev(3, 2, cause="m1", msg="m2-retry"), ev(4, 3, "tool-agent", "tool", cause="m2-retry")]
        r = replay(log, CID)
        self.assertEqual(r["context"][-1]["causation_id"], "m2")
        self.assertEqual(r["context"][-1]["warnings"], [])

    def test_message_id_reused_for_other_step_rejected(self):
        log = linear(2) + [ev(3, 3, "tool-agent", "tool", cause="m2", msg="m1")]
        r = replay(log, CID)
        self.assertEqual(verdicts(r)["evt_000003"], ("rejected", "MESSAGE_ID_REUSED"))


class Ordering(unittest.TestCase):
    def test_out_of_order_events_reordered_by_sequence(self):
        a = linear(5)
        log = renumber([a[0], a[2], a[1], a[4], a[3]])
        r = replay(log, CID)
        self.assertEqual([c["sequence"] for c in r["context"]], [1, 2, 3, 4, 5])
        self.assertEqual(r["metrics"]["reordered_events"], 2)
        self.assertEqual(r["status"], "recovered")

    def test_ordering_never_uses_timestamps(self):
        log = linear(5)
        rng = random.Random(21)
        skewed = [{**e, "sent_at": f"2026-01-{rng.randint(1, 28):02d}", "received_at": f"2025-{rng.randint(1, 12):02d}-01"}
                  for e in log]
        self.assertEqual(replay(log, CID)["context_hash"], replay(skewed, CID)["context_hash"])

    def test_any_arrival_order_yields_same_context(self):
        log = linear(7)
        expected = replay(log, CID)["context_hash"]
        rng = random.Random(2026)
        for _ in range(25):
            shuffled = log[:]
            rng.shuffle(shuffled)
            self.assertEqual(replay(renumber(shuffled), CID)["context_hash"], expected)


class GapsAndConflicts(unittest.TestCase):
    def test_missing_sequence_is_surfaced(self):
        log = [e for e in linear(5) if e["sequence"] != 3]
        r = replay(log, CID)
        self.assertEqual(r["gaps"], [3])
        self.assertEqual(r["status"], "incomplete")
        seq4 = next(c for c in r["context"] if c["sequence"] == 4)
        self.assertTrue(any("not in the recovered context" in w for w in seq4["warnings"]))
        self.assertTrue(any("follows missing seq 3" in w for w in seq4["warnings"]))

    def test_leading_gap_detected(self):
        log = [e for e in linear(3) if e["sequence"] != 1]
        self.assertEqual(replay(log, CID)["gaps"], [1])

    def test_conflict_resolved_by_handoff_ownership(self):
        log = [
            ev(1, 1, "web-chat", "user"),
            ev(2, 2, "planner-agent", "assistant", cause="m1", kind="handoff", target="tool-agent"),
            ev(3, 3, "planner-agent", "assistant", cause="m2", msg="rogue"),  # arrives first
            ev(4, 3, "tool-agent", "tool", cause="m2"),
        ]
        r = replay(log, CID)
        self.assertEqual(r["conflicts"][0]["rule"], "HANDOFF_OWNERSHIP")
        self.assertEqual(r["context"][-1]["adapter"], "tool-agent")
        self.assertEqual(verdicts(r)["evt_000003"], ("rejected", "CONFLICT_LOST"))
        self.assertEqual(r["status"], "recovered_with_conflicts")

    def test_conflict_resolved_by_causal_link(self):
        log = linear(2) + [ev(3, 3, "tool-agent", "tool", cause="m1", msg="stale"),
                           ev(4, 3, "tool-agent", "tool", cause="m2", msg="good", content="x")]
        r = replay(log, CID)
        self.assertEqual(r["conflicts"][0]["rule"], "CAUSAL_LINK")
        self.assertEqual(r["context"][-1]["message_id"], "good")

    def test_conflict_falls_back_to_first_arrival(self):
        log = linear(2) + [ev(3, 3, "tool-agent", "tool", cause="m2", msg="a"),
                           ev(4, 3, "web-chat", "user", cause="m2", msg="b")]
        r = replay(log, CID)
        self.assertEqual(r["conflicts"][0]["rule"], "FIRST_ARRIVAL")
        self.assertEqual(r["context"][-1]["message_id"], "a")


class Validation(unittest.TestCase):
    def test_invalid_envelopes_rejected_with_reason(self):
        bad_role = ev(2, 2, role="robot", cause="m1", key="idk_x")
        no_seq = {**ev(3, 3, cause="m2"), "sequence": None}
        log = [ev(1, 1, "web-chat", "user"), bad_role, no_seq]
        r = replay(log, CID)
        v = verdicts(r)
        self.assertEqual(v["evt_000002"], ("rejected", "INVALID_ENVELOPE"))
        self.assertEqual(v["evt_000003"], ("rejected", "INVALID_ENVELOPE"))
        self.assertIn("unknown role", r["decisions"][1]["reason"])

    def test_forged_idempotency_key_rejected(self):
        log = linear(1) + [ev(2, 2, cause="m1", key="idk_forged")]
        r = replay(log, CID)
        self.assertIn("deterministic derivation", r["decisions"][1]["reason"])

    def test_foreign_conversation_rejected(self):
        log = linear(1) + [ev(2, 2, cause="m1", cid="conv_other")]
        self.assertEqual(verdicts(replay(log, CID))["evt_000002"], ("rejected", "FOREIGN_CONVERSATION"))


class ResumeAndIdempotency(unittest.TestCase):
    def handoff_log(self):
        return [
            ev(1, 1, "web-chat", "user"),
            ev(2, 2, cause="m1", kind="handoff", target="tool-agent"),
            ev(3, None, "tool-agent", "system", cause="m2", kind="signal", msg="sig"),
        ]

    def test_open_handoff_is_resumable_with_target_adapter(self):
        r = replay(self.handoff_log(), CID)
        self.assertEqual(r["dialog_state"], "interrupted_handoff")
        self.assertTrue(r["resume"]["resumable"])
        self.assertEqual(r["resume"]["resume_adapter"], "tool-agent")
        self.assertEqual(r["resume"]["next_sequence"], 3)
        self.assertEqual(r["resume"]["causation_id"], "m2")
        self.assertEqual(r["metrics"]["signals"], 1)

    def test_gap_blocks_resume(self):
        log = [e for e in self.handoff_log()] + [ev(4, 4, cause="m3", kind="handoff", target="tool-agent")]
        r = replay(log, CID)
        self.assertFalse(r["resume"]["resumable"])
        self.assertIn("missing sequence 3", r["resume"]["blockers"][0])

    def test_replay_is_idempotent_and_pure(self):
        log = renumber(linear(4) + [linear(4)[1]])
        snapshot = [dict(e) for e in log]
        first, second = replay(log, CID), replay(log, CID)
        self.assertEqual(first, second)
        self.assertEqual(log, snapshot)  # input not mutated

    def test_every_event_gets_an_explained_decision(self):
        log = renumber(linear(4) + [linear(4)[0]])
        r = replay(log, CID)
        self.assertEqual(len(r["decisions"]), len(log))
        for d in r["decisions"]:
            self.assertTrue(d["rule"] and d["reason"])


if __name__ == "__main__":
    unittest.main()
