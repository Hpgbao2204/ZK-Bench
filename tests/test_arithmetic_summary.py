from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "summarize_arithmetic", REPO / "scripts" / "summarize_arithmetic.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ArithmeticSummaryTests(unittest.TestCase):
    def test_summary_contains_robust_quantiles_and_throughput(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "raw.csv"
            output = root / "summary.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    ["curve", "operation", "size", "repetition", "elapsed_ns", "operations"]
                )
                for repetition, elapsed in enumerate((100, 200, 300)):
                    writer.writerow(["BN254", "field_mul", 8, repetition, elapsed, 8])

            self.assertEqual(MODULE.summarize(source, output), 1)
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["repetitions"], "3")
            self.assertEqual(row["threads"], "1")
            self.assertEqual(row["execution_mode"], "serial")
            self.assertEqual(row["elapsed_ns_p50"], "200.000000")
            self.assertGreater(float(row["throughput_ops_per_s_p50"]), 1.0)

    def test_summary_rejects_boundary_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "raw.csv"
            output = root / "summary.csv"
            source.write_text(
                "curve,operation,size,repetition,elapsed_ns,operations\n"
                "BN254,field_add,2,0,1,2\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                MODULE.summarize(source, output)

    def test_summary_keeps_parallelism_dimensions_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "raw.csv"
            output = root / "summary.csv"
            source.write_text(
                "curve,operation,size,repetition,threads,execution_mode,elapsed_ns,operations\n"
                "BN254,msm,8,0,1,serial,100,64\n"
                "BN254,msm,8,1,1,serial,120,64\n"
                "BN254,msm,8,0,4,parallel,40,64\n"
                "BN254,msm,8,1,4,parallel,50,64\n",
                encoding="utf-8",
            )
            self.assertEqual(MODULE.summarize(source, output), 2)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {(row["threads"], row["execution_mode"]) for row in rows},
                {("1", "serial"), ("4", "parallel")},
            )


if __name__ == "__main__":
    unittest.main()
