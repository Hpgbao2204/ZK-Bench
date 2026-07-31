from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.system_metrics import (  # noqa: E402
    LinuxProcProcessCounterProvider,
    default_process_counter_provider,
)


class SystemMetricsTests(unittest.TestCase):
    def test_current_process_counters(self) -> None:
        counters = default_process_counter_provider().capture(os.getpid())
        if os.name == "nt":
            self.assertTrue(counters.supported, counters.unavailable_reason)
            self.assertEqual(counters.provider, "windows-psapi")
            self.assertIsNotNone(counters.peak_rss_bytes)
            self.assertGreater(counters.peak_rss_bytes, 1)
            self.assertIsNotNone(counters.page_faults)
        elif sys.platform.startswith("linux"):
            self.assertTrue(counters.supported, counters.unavailable_reason)
            self.assertEqual(counters.provider, "linux-procfs")
            self.assertIsNotNone(counters.peak_rss_bytes)
        else:
            self.assertFalse(counters.supported)

    def test_linux_proc_parser_labels_swap_allocation(self) -> None:
        local = Path(__file__).resolve().parents[1] / ".local"
        local.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=local) as temp:
            process = Path(temp) / "42"
            process.mkdir()
            (process / "status").write_text(
                "VmHWM:\t2048 kB\nVmSwap:\t32 kB\nRssAnon:\t1024 kB\n",
                encoding="utf-8",
            )
            (process / "io").write_text(
                "read_bytes: 4096\nwrite_bytes: 8192\n", encoding="utf-8"
            )
            # pid (comm), then fields 3..12; minflt=7 and majflt=5.
            (process / "stat").write_text(
                "42 (adapter worker) S 1 1 1 0 0 0 7 0 5 0\n", encoding="utf-8"
            )
            counters = LinuxProcProcessCounterProvider(Path(temp)).capture(42)
        self.assertTrue(counters.supported, counters.unavailable_reason)
        self.assertEqual(counters.peak_rss_bytes, 2048 * 1024)
        self.assertEqual(counters.swap_bytes, 32 * 1024)
        self.assertEqual(counters.page_faults, 12)
        self.assertEqual(counters.read_bytes, 4096)
        self.assertEqual(counters.write_bytes, 8192)
        self.assertIsNone(counters.private_bytes)


if __name__ == "__main__":
    unittest.main()
