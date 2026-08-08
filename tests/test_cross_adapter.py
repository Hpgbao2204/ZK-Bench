import importlib.util
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "zkbench_cross_adapter_checker", REPO / "scripts" / "check_cross_adapter.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CrossAdapterCheckerTests(unittest.TestCase):
    def test_fixture_matrix_covers_valid_and_invalid_application_cases(self) -> None:
        requests = MODULE.fixture_requests()
        self.assertEqual(len(requests), 12)
        self.assertEqual(
            {(request.workload, request.invalid_case) for request in requests},
            {
                (workload, invalid)
                for workload in ("credential", "batched_state", "private_swap")
                for invalid in (None, "wrong_public_input")
            },
        )
        for request in requests:
            request.validate()

    def test_compare_fixture_accepts_matching_digest_and_semantics(self) -> None:
        request = MODULE.fixture_requests()[0]
        left = MODULE.FixtureObservation(
            workload=request.workload,
            scale=request.scale,
            invalid_case=request.invalid_case,
            relation_digest=1234.0,
            public_inputs=2,
            native_work_units=request.scale,
            verify_ok=True,
            relation_unit="r1cs_constraints",
        )
        right = MODULE.FixtureObservation(
            **{**left.__dict__, "relation_unit": "plonk_domain_rows"}
        )
        MODULE.compare_fixture(request, left, right)

    def test_compare_fixture_rejects_digest_mismatch(self) -> None:
        request = MODULE.fixture_requests()[0]
        left = MODULE.FixtureObservation(
            request.workload,
            request.scale,
            request.invalid_case,
            1234.0,
            2,
            request.scale,
            True,
            "r1cs_constraints",
        )
        right = MODULE.FixtureObservation(
            request.workload,
            request.scale,
            request.invalid_case,
            1235.0,
            2,
            request.scale,
            True,
            "plonk_domain_rows",
        )
        with self.assertRaisesRegex(RuntimeError, "relation digest mismatch"):
            MODULE.compare_fixture(request, left, right)


if __name__ == "__main__":
    unittest.main()
