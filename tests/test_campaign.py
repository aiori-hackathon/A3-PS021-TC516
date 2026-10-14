"""Seeded random fault campaign: Threadline must stay exact where naive recovery fails."""

import random
import unittest

from threadline import campaign


class CampaignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = campaign.run(trials=200, seed=7, timing=False)

    def test_threadline_recovers_every_random_trial_exactly(self):
        r = self.result
        t = r["strategies"]["threadline"]
        self.assertEqual(t["exact"], r["trials"])
        self.assertEqual(t["duplicate_replies"], 0)
        self.assertEqual(t["order_errors"], 0)
        self.assertEqual(t["silent_loss"], 0)
        self.assertEqual(t["false_alarm"], 0)
        self.assertEqual(t["loss_surfaced"], r["trials_with_loss"])
        self.assertEqual(r["threadline_deterministic"], r["trials"])
        self.assertEqual(r["threadline_order_invariant"], r["trials"])

    def test_campaign_injects_every_fault_type(self):
        f = self.result["faults_injected"]
        for kind in ("redeliveries", "retries", "delayed", "lost"):
            self.assertGreater(f[kind], 0, kind)

    def test_naive_baselines_leak_duplicates_misorder_and_hide_loss(self):
        r = self.result
        for name in ("naive_arrival", "naive_timestamp"):
            s = r["strategies"][name]
            self.assertLess(s["exact"], r["trials"] // 2, name)
            self.assertEqual(s["duplicate_replies"], r["faults_injected"]["retries"], name)
            self.assertGreater(s["order_errors"], 0, name)
            self.assertEqual(s["silent_loss"], r["trials_with_loss"], name)

    def test_same_seed_gives_identical_results(self):
        self.assertEqual(campaign.run(trials=200, seed=7, timing=False), self.result)
        other = campaign.run(trials=200, seed=8, timing=False)
        self.assertNotEqual(other["faults_injected"], self.result["faults_injected"])

    def test_scorer_rejects_wrong_contexts(self):
        _, _, truth, _ = campaign._trial(random.Random(1), 0, campaign.DEFAULTS)
        good = [{"sequence": s, "idempotency_key": truth["keys"][s]} for s in truth["delivered"]]
        self.assertTrue(campaign._score(good, truth, False)["exact"])
        self.assertFalse(campaign._score(good[:-1], truth, False)["exact"])
        self.assertFalse(campaign._score(good[1:2] + good[:1] + good[2:], truth, False)["exact"])
        self.assertEqual(campaign._score(good + good[-1:], truth, False)["duplicate_replies"], 1)
        forged = [{**good[0], "idempotency_key": "idk_forged"}] + good[1:]
        self.assertFalse(campaign._score(forged, truth, False)["exact"])


if __name__ == "__main__":
    unittest.main()
