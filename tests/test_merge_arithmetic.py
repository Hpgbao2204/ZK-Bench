from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "merge_arithmetic", REPO / "scripts" / "merge_arithmetic.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ArithmeticMergeTests(unittest.TestCase):
    def test_merge_preserves_parallel_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            header = "curve,operation,size,repetition,threads,execution_mode,elapsed_ns,operations\n"
            serial = root / "serial.csv"
            parallel = root / "parallel.csv"
            output = root / "all.csv"
            serial.write_text(header + "BN254,msm,8,0,1,serial,100,64\n", encoding="utf-8")
            parallel.write_text(header + "BN254,msm,8,0,4,parallel,50,64\n", encoding="utf-8")
            self.assertEqual(MODULE.merge([serial, parallel], output), 2)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["execution_mode"] for row in rows], ["serial", "parallel"])

    def test_merge_rejects_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.csv"
            second = root / "second.csv"
            output = root / "all.csv"
            first.write_text("a,b\n1,2\n", encoding="utf-8")
            second.write_text("a,c\n3,4\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.merge([first, second], output)


if __name__ == "__main__":
    unittest.main()
