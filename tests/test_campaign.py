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
                "repetitions": 3,
                "scales": [8],
                "threads": [2],
            }
        ],
        "mode": "cold",
        "os_cache_primer_runs": 1,
        "repetitions": 3,
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
        process_summary = [
            row for row in summary if row["phase"] == "adapter_process_wall"
        ]
        self.assertTrue(process_summary)
        self.assertTrue(
            all(row["relation_unit"] == "r1cs_constraints" for row in process_summary)
        )
        self.assertTrue(
            all(row["native_relation_size"] == "8" for row in process_summary)
        )
        self.assertTrue(
            all(row["expected_outcomes"] != "unexpected-outcome-present" for row in summary)
        )
        self.assertEqual(environment["runner"], "adapter-process-campaign")
        self.assertEqual(len(environment["config_hash"]), 64)

    def test_progress_reports_process_counts(self) -> None:
        messages: list[str] = []
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            run_adapter_campaign(
                campaign_config(),
                Path(temp),
                repo=REPO,
                progress=messages.append,
            )
        self.assertIn("11 total processes", messages[0])
        self.assertTrue(any("[11/11]" in message for message in messages))
        self.assertTrue(messages[-1].startswith("campaign complete:"))

    def test_parameter_sets_are_carried_into_raw_and_summary(self) -> None:
        config = campaign_config()
        config["parameter_sets"] = [
            {
                "id": "depth-16",
                "parameters": {
                    "merkle_depth": 16,
                    "range_bits": 64,
                    "ablation": "full",
                },
            },
            {
                "id": "depth-32",
                "parameters": {
                    "merkle_depth": 32,
                    "range_bits": 64,
                    "ablation": "full",
                },
            },
        ]
        config["invalid_cases"][0]["parameter_set_ids"] = ["depth-32"]
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            output = Path(temp)
            run_adapter_campaign(config, output, repo=REPO)
            with (output / "raw_results.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                raw = list(csv.DictReader(handle))
            with (output / "summary.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                summary = list(csv.DictReader(handle))
        self.assertEqual(
            {row["parameter_set"] for row in raw},
            {"depth-16", "depth-32"},
        )
        self.assertTrue(
            all(
                row["parameter_set"] == "depth-32"
                for row in raw
                if row["invalid_proof_kind"]
            )
        )
        self.assertTrue(
            any('"merkle_depth":16' in row["parameters_json"] for row in summary)
        )
        self.assertEqual(
            len({row["run_id"] for row in raw}),
            19,
        )

    def test_rejects_boundary_parameter_values(self) -> None:
        config = campaign_config()
        config["parameter_sets"] = [
            {"id": "invalid-depth", "parameters": {"merkle_depth": 1}}
        ]
        with self.assertRaisesRegex(ValueError, "numeric parameter"):
            validate_campaign_config(config)

    def test_refuses_to_overwrite_existing_evidence(self) -> None:
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            output = Path(temp)
            (output / "raw_results.csv").write_text("existing\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                run_adapter_campaign(campaign_config(), output, repo=REPO)
            self.assertEqual(
                (output / "raw_results.csv").read_text(encoding="utf-8"),
                "existing\n",
            )


if __name__ == "__main__":
    unittest.main()
