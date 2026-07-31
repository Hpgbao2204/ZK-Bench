from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.adapter_protocol import AdapterRequest  # noqa: E402
from zkbench.adapter_runner import execute_adapter  # noqa: E402


class AdapterRunnerTests(unittest.TestCase):
    def test_executes_and_samples_adapter_process(self) -> None:
        request = AdapterRequest(
            run_id="runner-valid",
            workload="controlled_kernel",
            scale=8,
            threads=2,
            seed=7,
        )
        execution = execute_adapter(
            [sys.executable, str(REPO / "tests" / "fixtures" / "fake_adapter.py")],
            request,
            timeout_seconds=5,
            sampling_interval_ms=2,
        )
        self.assertTrue(execution.succeeded, execution.protocol_error)
        self.assertIsNotNone(execution.result)
        self.assertTrue(execution.result.verify_ok)
        self.assertGreater(execution.process.samples, 1)
        self.assertIsNotNone(execution.process.peak_rss_bytes)

    def test_invalid_case_must_cryptographically_reject(self) -> None:
        request = AdapterRequest(
            run_id="runner-invalid",
            workload="controlled_kernel",
            scale=8,
            threads=2,
            seed=7,
            invalid_case="wrong_public_input",
        )
        execution = execute_adapter(
            [sys.executable, str(REPO / "tests" / "fixtures" / "fake_adapter.py")],
            request,
            timeout_seconds=5,
            sampling_interval_ms=2,
        )
        self.assertTrue(execution.succeeded, execution.protocol_error)
        self.assertFalse(execution.result.verify_ok)
        self.assertEqual(execution.result.error_type, "cryptographic_rejection")


if __name__ == "__main__":
    unittest.main()
