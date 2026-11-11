import asyncio
import os
import unittest

from core.agent.router import TieredRouter
from scripts.run_eval import run_eval_async


class TestGoldenEval(unittest.TestCase):
    def test_vague_open_pronouns_do_not_trigger_tier0_app_launch(self):
        router = TieredRouter()
        for phrase in ("open that", "open this", "open it", "launch that"):
            dec = router.route(phrase)
            self.assertEqual(dec.tier, 1, f"'{phrase}' should not execute Tier 0 app launch")
            self.assertIsNone(dec.tool_name)

    def test_golden_command_dataset_meets_count_and_accuracy_threshold(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dataset_path = os.path.join(root, "tests", "eval", "golden_commands.yaml")
        report = asyncio.run(run_eval_async(dataset_path, live=False))

        self.assertGreaterEqual(report["total"], 100)
        self.assertGreaterEqual(
            report["accuracy"],
            0.95,
            f"Golden eval accuracy dropped below 95%: {report['accuracy']} failures={report['failures']}",
        )
        for cat, stats in report["categories"].items():
            self.assertGreaterEqual(
                stats["accuracy"],
                0.85,
                f"Category '{cat}' below 85%: {stats}",
            )

