from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from run_swap_audit import summarize_bundle, write_latex_table  # noqa: E402


class SwapAuditTests(unittest.TestCase):
    def test_summary_and_multilevel_latex_table(self) -> None:
        fields = [
            "phase",
            "recorded",
            "invalid_proof_kind",
            "latency_ms",
            "peak_rss_mb",
            "peak_swap_mb",
            "system_mem_available_min_mb",
            "system_mem_total_mb",
            "system_swap_total_mb",
            "system_swap_used_peak_mb",
            "system_swap_in_mb_delta",
            "system_swap_out_mb_delta",
            "system_swap_io_observed",
            "system_counter_provider",
            "system_counter_samples",
            "variant",
            "relation_unit",
            "input_scale",
            "threads",
            "evidence_class",
            "result_scope",
        ]
        rows = []
        for repetition, latency in enumerate((2000, 3000, 4000)):
            rows.append(
                {
                    "phase": "adapter_process_wall",
                    "recorded": "true",
                    "invalid_proof_kind": "",
                    "latency_ms": str(latency),
                    "peak_rss_mb": str(1024 + repetition * 128),
                    "peak_swap_mb": "0.000000",
                    "system_mem_available_min_mb": str(16384 - repetition * 512),
                    "system_mem_total_mb": "32768",
                    "system_swap_total_mb": "8192",
                    "system_swap_used_peak_mb": "0.000000",
                    "system_swap_in_mb_delta": "0.000000",
                    "system_swap_out_mb_delta": "0.000000",
                    "system_swap_io_observed": "false",
                    "system_counter_provider": "linux-procfs-system",
                    "system_counter_samples": "20",
                    "variant": "fixture",
                    "relation_unit": "r1cs_constraints",
                    "input_scale": "1048576",
                    "threads": "16",
                    "evidence_class": "measured",
                    "result_scope": "unit-test",
                }
            )
        local = REPO / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            bundle = Path(temp)
            with (bundle / "raw_results.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            summary = summarize_bundle("groth16", bundle)
            table = bundle / "swap_audit.tex"
            write_latex_table([summary], table)
            latex = table.read_text(encoding="utf-8")
        self.assertEqual(summary["wall_median_s"], "3.000000")
        self.assertEqual(summary["swap_io_observed"], "no")
        self.assertEqual(summary["runs_with_swap_io"], "0/3")
        self.assertIn("\\multicolumn{3}{c}{Guest swap I/O}", latex)
        self.assertIn("\\multirow{2}{*}{Groth16}", latex)
        self.assertIn("P95 / max$^{a}$", latex)


if __name__ == "__main__":
    unittest.main()
