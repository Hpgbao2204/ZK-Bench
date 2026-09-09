import csv
import importlib.util
import json
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
    def _write_summary(self, root: Path, adapter: str, sizes: list[tuple[int, int]]) -> None:
        directory = root / f"identity-{adapter}-final-v1"
        directory.mkdir(parents=True)
        with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "phase",
                    "input_scale",
                    "invalid_proof_kind",
                    "p50_proof_bytes",
                    "recorded",
                ),
            )
            writer.writeheader()
            for scale, proof_bytes in sizes:
                writer.writerow(
                    {
                        "phase": "adapter_process_wall",
                        "input_scale": scale,
                        "invalid_proof_kind": "",
                        "p50_proof_bytes": proof_bytes,
                        "recorded": "true",
                    }
                )

    def test_dual_profile_model_and_eip2537_arithmetic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_summary(root, "groth16", [(1024, 192)])
            self._write_summary(root, "plonk", [(1024, 977)])
            self._write_summary(root, "stark", [(1024, 45000), (65536, 100000)])
            model = json.loads(
                (REPO / "configs" / "reproduction-gas-model.json").read_text(
                    encoding="utf-8"
                )
            )
            model["batch_sizes"] = [1]
            model["gas_prices_gwei"] = [20]
            rows = MODULE.modeled_rows(model, root, "identity")
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(row["evidence_class"] == "modeled" for row in rows))

            current_groth = next(
                row
                for row in rows
                if row["adapter"] == "groth16"
                and row["profile_id"] == "pectra-bls12-381"
            )
            self.assertEqual(current_groth["verifier_compute_gas"], 198628)
            self.assertEqual(current_groth["fixed_gas"], 228844)
            self.assertEqual(current_groth["calldata_pricing_branch"], "standard")

            current_stark = next(
                row
                for row in rows
                if row["adapter"] == "stark"
                and row["profile_id"] == "pectra-bls12-381"
            )
            self.assertEqual(current_stark["native_proof_bytes"], 100000)
            self.assertEqual(current_stark["selected_input_scale"], 65536)
            self.assertEqual(current_stark["calldata_pricing_branch"], "eip7623-floor")
            self.assertEqual(current_stark["calldata_gas"], 2_002_560)

    def test_eip2537_msm_rejects_unconfigured_term_count(self) -> None:
        model = json.loads(
            (REPO / "configs" / "reproduction-gas-model.json").read_text(
                encoding="utf-8"
            )
        )
        schedule = model["profiles"]["pectra-bls12-381"]["precompile_schedule"]
        with self.assertRaisesRegex(ValueError, "k=5"):
            MODULE.eip2537_g1_msm_gas(schedule, 5)


if __name__ == "__main__":
    unittest.main()
