from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "summarize_parallelism", REPO / "scripts" / "summarize_parallelism.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ParallelismSummaryTests(unittest.TestCase):
    def test_pairs_serial_and_parallel_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "summary.csv"
            output = root / "parallel.csv"
            source.write_text(
                "curve,operation,size,threads,execution_mode,elapsed_ns_p50\n"
                "BN254,msm,8,1,serial,100\n"
                "BN254,msm,8,2,parallel,60\n"
                "BN254,msm,8,4,parallel,40\n",
                encoding="utf-8",
            )
            self.assertEqual(MODULE.summarize(source, output), 2)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["threads"], "2")
            self.assertAlmostEqual(float(rows[0]["speedup"]), 100 / 60, places=6)

    def test_missing_baseline_is_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "summary.csv"
            output = root / "parallel.csv"
            source.write_text(
                "curve,operation,size,threads,execution_mode,elapsed_ns_p50\n"
                "BN254,msm,8,2,parallel,60\n",
                encoding="utf-8",
            )
            self.assertEqual(MODULE.summarize(source, output), 0)


if __name__ == "__main__":
    unittest.main()
