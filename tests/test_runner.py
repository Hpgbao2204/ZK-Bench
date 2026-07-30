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
            environment = json.loads((output / "environment.json").read_text(encoding="utf-8"))
            self.assertEqual(environment["runner"], "reference-predicate")


if __name__ == "__main__":
    unittest.main()
