from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.system_metrics import default_process_counter_provider  # noqa: E402


class SystemMetricsTests(unittest.TestCase):
    def test_current_process_counters(self) -> None:
        counters = default_process_counter_provider().capture(os.getpid())
        if os.name == "nt":
            self.assertTrue(counters.supported, counters.unavailable_reason)
            self.assertEqual(counters.provider, "windows-psapi")
            self.assertIsNotNone(counters.peak_rss_bytes)
            self.assertGreater(counters.peak_rss_bytes, 1)
            self.assertIsNotNone(counters.page_faults)
        else:
            self.assertFalse(counters.supported)


if __name__ == "__main__":
    unittest.main()
