from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.runner import run_reference  # noqa: E402
from zkbench.runner import write_summary  # noqa: E402


class RunnerTests(unittest.TestCase):
    def test_writes_complete_bundle(self) -> None:
        config = {
            "adapter": "reference-predicate",
            "claim_id": "TEST",
            "experiment_id": "unit",
            "repetitions": 2,
            "seed": 7,
            "warmups": 1,
            "workloads": ["credential", "batched_state", "private_swap"],
        }
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            output = Path(temp)
            run_reference(config, output)
            for name in ("raw_results.csv", "summary.csv", "config.json", "environment.json"):
                self.assertTrue((output / name).is_file())
            with (output / "raw_results.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(row["verify_ok"] == "true" for row in rows))
            self.assertIn("process_read_bytes", rows[0])
            self.assertIn("peak_swap_mb", rows[0])
            self.assertNotIn("swap_read_bytes", rows[0])
            environment = json.loads((output / "environment.json").read_text(encoding="utf-8"))
            self.assertEqual(environment["runner"], "reference-predicate")

    def test_single_observation_does_not_invent_zero_variance(self) -> None:
        config = {
            "adapter": "reference-predicate",
            "claim_id": "TEST",
            "experiment_id": "single",
            "repetitions": 1,
            "seed": 7,
            "warmups": 0,
            "workloads": ["credential"],
        }
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            output = Path(temp)
            run_reference(config, output)
            with (output / "summary.csv").open(newline="", encoding="utf-8") as handle:
                summary = next(csv.DictReader(handle))
        self.assertEqual(summary["stdev_latency_ms"], "")
        self.assertIn("two observations", summary["stdev_unavailable_reason"])

    def test_summary_excludes_exact_numeric_boundaries(self) -> None:
        rows = [
            {
                "claim_id": "TEST",
                "experiment_id": "boundary",
                "workload": "credential",
                "variant": "fixture",
                "latency_ms": value,
                "result_scope": "unit-test",
            }
            for value in ("0.500000", "1.500000")
        ]
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            path = Path(temp) / "summary.csv"
            write_summary(rows, path)
            with path.open(newline="", encoding="utf-8") as handle:
                summary = next(csv.DictReader(handle))
        self.assertEqual(summary["mean_latency_ms"], "")
        self.assertIn("mean_latency_ms", summary["excluded_boundary_metrics"])


if __name__ == "__main__":
    unittest.main()
