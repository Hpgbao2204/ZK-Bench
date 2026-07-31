from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.adapter_protocol import (  # noqa: E402
    AdapterRequest,
    AdapterResult,
    PhaseEvent,
    parse_json_lines,
)


class AdapterProtocolTests(unittest.TestCase):
    def test_request_rejects_boundary_scale(self) -> None:
        request = AdapterRequest(
            run_id="r1",
            workload="controlled_kernel",
            scale=1,
            threads=2,
            seed=7,
        )
        with self.assertRaisesRegex(ValueError, "boundary"):
            request.validate()

    def test_valid_phase_round_trip(self) -> None:
        event = PhaseEvent(
            run_id="r1",
            adapter="example",
            phase="msm",
            supported=True,
            status="ok",
            thread_count=4,
            elapsed_ns=12_345,
            metrics={"peak_rss_mb": 512.5},
        )
        parsed = parse_json_lines([event.to_json()])
        self.assertEqual(parsed, [event])

    def test_unsupported_phase_has_reason_and_no_zero(self) -> None:
        event = PhaseEvent(
            run_id="r1",
            adapter="example",
            phase="fft_ntt",
            supported=False,
            status="unsupported",
            thread_count=1,
            unavailable_reason="library exposes no phase hook",
        )
        event.validate()
        self.assertIsNone(event.elapsed_ns)

    def test_numeric_boundary_requires_reason(self) -> None:
        event = PhaseEvent(
            run_id="r1",
            adapter="example",
            phase="commitment",
            supported=True,
            status="ok",
            thread_count=2,
            elapsed_ns=100,
            metrics={"energy_joules": 1.0},
        )
        with self.assertRaisesRegex(ValueError, "excluded boundary"):
            event.validate()

    def test_boolean_is_not_quantitative_metric(self) -> None:
        event = PhaseEvent(
            run_id="r1",
            adapter="example",
            phase="verify_total",
            supported=True,
            status="ok",
            thread_count=2,
            elapsed_ns=100,
            metrics={"verify_ok": True},
        )
        with self.assertRaisesRegex(ValueError, "not boolean"):
            event.validate()

    def test_result_keeps_boolean_and_nonboundary_counts(self) -> None:
        result = AdapterResult(
            run_id="r1",
            adapter="ark-groth16",
            verify_ok=True,
            proof_bytes=128,
            native_work_units=1024,
            public_inputs=2,
            constraints=1024,
        )
        result.validate()
        self.assertTrue(result.verify_ok)

    def test_failed_result_requires_error_class(self) -> None:
        result = AdapterResult(
            run_id="r1",
            adapter="ark-groth16",
            verify_ok=False,
            proof_bytes=128,
            native_work_units=1024,
            public_inputs=2,
            constraints=1024,
        )
        with self.assertRaisesRegex(ValueError, "error_type"):
            result.validate()


if __name__ == "__main__":
    unittest.main()
