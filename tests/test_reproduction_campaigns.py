import importlib.util
import json
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zkbench_reproduction_campaigns", REPO / "scripts" / "run_reproduction_campaigns.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PaperCampaignTests(unittest.TestCase):
    def test_matrix_covers_three_workloads_and_four_adapters(self) -> None:
        matrix = MODULE.load_matrix()
        names = MODULE.campaign_names(matrix)
        self.assertEqual(len(names), 12)
        self.assertEqual(
            set(names),
            {
                f"{workload}-{adapter}"
                for workload in ("identity", "state", "pcas")
                for adapter in ("groth16", "plonk", "stark", "bulletproofs")
            },
        )

    def test_workload_claim_ids_exist_in_registry(self) -> None:
        matrix = MODULE.load_matrix()
        registry = json.loads(
            (REPO / "configs" / "reproduction-claim-registry.json").read_text(
                encoding="utf-8"
            )
        )
        registered = {claim["claim_id"] for claim in registry["claims"]}
        workload_claims = {
            workload["claim_id"] for workload in matrix["workloads"].values()
        }
        self.assertLessEqual(workload_claims, registered)

    def test_state_campaign_reaches_one_million_native_units(self) -> None:
        config = MODULE.build_campaign(MODULE.load_matrix(), "state-groth16")
        self.assertEqual(config["repetitions"], 10)
        self.assertEqual(config["threads"], [16])
        self.assertEqual(config["scales"][0], 16384)
        self.assertEqual(config["scales"][-1], 1048576)
        self.assertEqual(
            config["parameter_sets"][0]["parameters"]["scale_mode"],
            "target_native_size",
        )
        self.assertEqual(config["adapter"], "ark-groth16-0.6.0-bls12-381")

    def test_plonk_paper_campaign_uses_bls12_381(self) -> None:
        config = MODULE.build_campaign(MODULE.load_matrix(), "identity-plonk")
        self.assertIn("bls12-381", config["adapter"])

    def test_smoke_override_is_small_and_dirty_tree_safe(self) -> None:
        config = MODULE.build_campaign(
            MODULE.load_matrix(), "pcas-bulletproofs", smoke=True
        )
        self.assertEqual(config["scales"], [8192])
        self.assertEqual(config["repetitions"], 3)
        self.assertEqual(config["threads"], [16])
        self.assertFalse(config["require_clean_git"])


if __name__ == "__main__":
    unittest.main()
