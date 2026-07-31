from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.adapter_protocol import PhaseEvent, parse_json_lines  # noqa: E402


class AdapterProtocolTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
