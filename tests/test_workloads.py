from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.fixtures import (  # noqa: E402
    batched_state_fixture,
    credential_fixture,
    invalid_variants,
    private_swap_fixture,
)
from zkbench.workloads import evaluate  # noqa: E402


class WorkloadTests(unittest.TestCase):
    def test_valid_fixtures(self) -> None:
        fixtures = {
            "credential": credential_fixture(),
            "batched_state": batched_state_fixture(),
            "private_swap": private_swap_fixture(),
        }
        for workload, fixture in fixtures.items():
            with self.subTest(workload=workload):
                result = evaluate(workload, fixture)
                self.assertTrue(result.valid, result.checks)

    def test_invalid_predicate_groups(self) -> None:
        fixtures = {
            "credential": credential_fixture(),
            "batched_state": batched_state_fixture(),
            "private_swap": private_swap_fixture(),
        }
        for workload, fixture in fixtures.items():
            for name, invalid in invalid_variants(workload, fixture).items():
                with self.subTest(workload=workload, variant=name):
                    self.assertFalse(evaluate(workload, invalid).valid)

    def test_evaluation_does_not_mutate_fixture(self) -> None:
        fixture = private_swap_fixture()
        before = copy.deepcopy(fixture)
        evaluate("private_swap", fixture)
        self.assertEqual(before, fixture)


if __name__ == "__main__":
    unittest.main()
