from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from threadline.restart_lab import SCENARIOS, run_trial


class RestartLab(unittest.TestCase):
    def test_five_process_restart_and_retry_cases(self):
        with TemporaryDirectory() as tmp:
            for scenario in SCENARIOS:
                with self.subTest(scenario=scenario):
                    result = run_trial(scenario, Path(tmp) / (scenario + ".db"))
                    self.assertEqual(result["state"], "complete")
                    self.assertEqual(result["effects"], 1)
                    self.assertTrue(result["late_retry_reused_result"])
                    if scenario in ("after_commit", "before_ack"):
                        self.assertEqual(result["committed_before_retry"], result["replacement_id"])
