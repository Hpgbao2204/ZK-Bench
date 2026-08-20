import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zkbench_hybrid_projection", REPO / "scripts" / "model_hybrid_projection.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class HybridProjectionTests(unittest.TestCase):
    def test_projection_keeps_assumption_separate_from_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for adapter, latency in (("stark", 6000), ("plonk", 12000)):
                directory = root / f"pcas-{adapter}-final-v1"
                directory.mkdir(parents=True)
                with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=("phase", "input_scale", "invalid_proof_kind", "n", "mean_latency_ms"),
                    )
                    writer.writeheader()
                    writer.writerow(
                        {
                            "phase": "prove_total",
                            "input_scale": 65536,
                            "invalid_proof_kind": "",
                            "n": 10,
                            "mean_latency_ms": latency,
                        }
                    )
            gas = root / "pcas.csv"
            with gas.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("adapter", "batch_size", "gas_price_gwei", "fixed_gas"),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "adapter": "groth16",
                        "batch_size": 1,
                        "gas_price_gwei": 20,
                        "fixed_gas": 200000,
                    }
                )
            model = {
                "model_id": "hybrid-test",
                "inner_adapter": "stark",
                "outer_adapter": "groth16",
                "comparison_adapter": "plonk",
                "outer_prover_assumptions_ms": [800, 7000],
            }
            rows = MODULE.project(model, root, gas)
            self.assertEqual(rows[0]["hybrid_prover_ms"], 6800)
            self.assertTrue(rows[0]["beats_comparison_latency"])
            self.assertFalse(rows[1]["beats_comparison_latency"])
            self.assertTrue(all(row["evidence_class"] == "modeled" for row in rows))


if __name__ == "__main__":
    unittest.main()
