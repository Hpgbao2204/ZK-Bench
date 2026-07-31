from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.campaign import run_adapter_campaign, validate_campaign_config  # noqa: E402


def campaign_config() -> dict[str, object]:
    return {
        "adapter": "fake-adapter",
        "campaign_kind": "adapter-process",
        "claim_id": "TEST-CAMPAIGN",
        "command": [
            sys.executable,
            str(REPO / "tests" / "fixtures" / "fake_adapter.py"),
        ],
        "evidence_class": "measured",
        "energy": {
            "provider": "unavailable",
            "unavailable_reason": "fixture has no energy counter",
        },
        "experiment_id": "campaign-unit",
        "invalid_cases": [
            {
                "kind": "wrong_public_input",
                "repetitions": 2,
                "scales": [8],
                "threads": [2],
            }
        ],
        "mode": "cold",
        "os_cache_primer_runs": 1,
        "repetitions": 2,
        "require_clean_git": False,
        "result_scope": "unit-test",
        "sampling_interval_ms": 2,
        "scales": [8],
        "schema_version": "1.0",
        "seed": 20260731,
        "threads": [1, 2],
        "timeout_seconds": 5,
        "variant": "fake-adapter",
        "workload": "controlled_kernel",
    }


class CampaignTests(unittest.TestCase):
    def test_rejects_warm_label_for_fresh_process_campaign(self) -> None:
        config = campaign_config()
        config["mode"] = "warm"
        with self.assertRaisesRegex(ValueError, "cold mode"):
            validate_campaign_config(config)

    def test_writes_auditable_bundle_and_excludes_primers(self) -> None:
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            output = Path(temp)
            run_adapter_campaign(campaign_config(), output, repo=REPO)
            for name in ("raw_results.csv", "summary.csv", "config.json", "environment.json"):
                self.assertTrue((output / name).is_file())
            with (output / "raw_results.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                raw = list(csv.DictReader(handle))
            with (output / "summary.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                summary = list(csv.DictReader(handle))
            environment = json.loads(
                (output / "environment.json").read_text(encoding="utf-8")
            )
        self.assertTrue(any(row["run_role"] == "os_cache_primer" for row in raw))
        self.assertTrue(any(row["invalid_proof_kind"] for row in raw))
        rejection_rows = [
            row
            for row in raw
            if row["invalid_proof_kind"] and row["phase"] == "verify_core"
        ]
        self.assertTrue(rejection_rows)
        self.assertTrue(all(row["rejection_latency_ms"] for row in rejection_rows))
        self.assertTrue(
            all(
                not row["rejection_latency_ms"]
                for row in raw
                if row["phase"] == "adapter_process_wall"
            )
        )
        self.assertTrue(all(row["recorded"] == "true" for row in summary))
        self.assertTrue(
            all(row["expected_outcomes"] != "unexpected-outcome-present" for row in summary)
        )
        self.assertEqual(environment["runner"], "adapter-process-campaign")
        self.assertEqual(len(environment["config_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
