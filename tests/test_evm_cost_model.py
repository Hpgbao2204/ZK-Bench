import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zkbench_evm_cost", REPO / "scripts" / "model_evm_cost.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PaperGasModelTests(unittest.TestCase):
    def test_model_uses_measured_proof_size_and_marks_rows_modeled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for adapter, proof_bytes in (("groth16", 288), ("plonk", 511), ("stark", 45500)):
                directory = root / f"identity-{adapter}-final-v1"
                directory.mkdir(parents=True)
                with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=(
                            "phase",
                            "invalid_proof_kind",
                            "p50_proof_bytes",
                        ),
                    )
                    writer.writeheader()
                    writer.writerow(
                        {
                            "phase": "adapter_process_wall",
                            "invalid_proof_kind": "",
                            "p50_proof_bytes": proof_bytes,
                        }
                    )
            model = {
                "model_id": "test",
                "assumed_nonzero_fraction": 1,
                "nonzero_calldata_gas_per_byte": 16,
                "zero_calldata_gas_per_byte": 4,
                "transaction_intrinsic_gas": 21000,
                "verifier_compute_gas": {"groth16": 154000, "plonk": 260000, "stark": 2000000},
                "residual_batch_gas_per_application_unit": 12000,
                "batch_sizes": [2],
                "gas_prices_gwei": [5],
                "eth_usd": 3500,
            }
            rows = MODULE.modeled_rows(model, root, "identity")
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["evidence_class"] == "modeled" for row in rows))
            groth = next(row for row in rows if row["adapter"] == "groth16")
            self.assertEqual(groth["proof_bytes"], 288)
            self.assertGreater(groth["fixed_gas"], 175000)


if __name__ == "__main__":
    unittest.main()
