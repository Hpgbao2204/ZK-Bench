from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tests.test_campaign import campaign_config  # noqa: E402
from zkbench.campaign import run_adapter_campaign  # noqa: E402
from zkbench.result_validation import validate_result_bundle  # noqa: E402


class ResultValidationTests(unittest.TestCase):
    def test_valid_campaign_bundle_passes(self) -> None:
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            bundle = Path(temp)
            run_adapter_campaign(campaign_config(), bundle, repo=REPO)
            errors = validate_result_bundle(bundle, repo=REPO)
        self.assertEqual(errors, [])

    def test_summary_boundary_is_rejected(self) -> None:
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            bundle = Path(temp)
            run_adapter_campaign(campaign_config(), bundle, repo=REPO)
            summary_path = bundle / "summary.csv"
            with summary_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
                fields = list(rows[0])
            rows[0]["mean_latency_ms"] = "1.000000"
            with summary_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            errors = validate_result_bundle(bundle, repo=REPO)
        self.assertTrue(any("boundary value" in error for error in errors))

    def test_parameter_set_lineage_is_validated(self) -> None:
        config = campaign_config()
        config["parameter_sets"] = [
            {
                "id": "depth-16",
                "parameters": {"merkle_depth": 16, "range_bits": 64},
            }
        ]
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            bundle = Path(temp)
            run_adapter_campaign(config, bundle, repo=REPO)
            self.assertEqual(validate_result_bundle(bundle, repo=REPO), [])
            raw_path = bundle / "raw_results.csv"
            with raw_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
                fields = list(rows[0])
            rows[0]["parameters_json"] = '{"merkle_depth":32,"range_bits":64}'
            with raw_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            errors = validate_result_bundle(bundle, repo=REPO)
        self.assertTrue(
            any("broken parameter-set lineage" in error for error in errors)
        )

    def test_dirty_worktree_is_rejected_outside_unit_test_scope(self) -> None:
        config = campaign_config()
        config["result_scope"] = "pilot"
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            bundle = Path(temp)
            run_adapter_campaign(config, bundle, repo=REPO)
            environment_path = bundle / "environment.json"
            environment = json.loads(environment_path.read_text(encoding="utf-8"))
            environment["tracked_worktree_dirty"] = True
            environment_path.write_text(
                json.dumps(environment, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            errors = validate_result_bundle(bundle, repo=REPO)
        self.assertIn(
            "measured evidence was produced from a dirty tracked worktree",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
