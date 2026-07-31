from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.analysis import (  # noqa: E402
    application_throughput,
    bootstrap_interval,
    bootstrap_speedup_interval,
    fit_power_law,
    native_overhead,
    parallel_profile,
)


class AnalysisTests(unittest.TestCase):
    def test_power_law_fit(self) -> None:
        samples = [(4.0, 16.0), (16.0, 128.0), (64.0, 1024.0)]
        fit = fit_power_law(samples)
        self.assertTrue(math.isclose(fit.coefficient_a, 2.0, rel_tol=1e-12))
        self.assertTrue(math.isclose(fit.exponent_b, 1.5, rel_tol=1e-12))
        self.assertIsNone(fit.r_squared)

    def test_parallel_profile_omits_fixed_one_thread_points(self) -> None:
        profile = parallel_profile(
            {
                1: [100.2, 101.2, 99.2],
                2: [60.2, 61.2, 59.2],
                4: [50.2, 51.2, 49.2],
                8: [48.2, 49.2, 47.2],
                16: [47.2, 48.2, 46.2],
            },
            saturation_threshold=0.10,
        )
        self.assertNotIn(1, [point.threads for point in profile.points])
        self.assertEqual(profile.saturation_threads, 8)

    def test_exact_unit_overhead_is_excluded(self) -> None:
        self.assertIsNone(native_overhead(10.2, 10.2))

    def test_application_throughput(self) -> None:
        value = application_throughput(64, 125.5)
        self.assertIsNotNone(value)
        self.assertGreater(value, 500)

    def test_bootstrap_intervals_are_seeded(self) -> None:
        values = [10.2, 11.4, 12.1, 13.3, 14.8]
        first = bootstrap_interval(values, lambda sample: sum(sample) / len(sample), seed=9)
        second = bootstrap_interval(values, lambda sample: sum(sample) / len(sample), seed=9)
        self.assertEqual(first, second)
        self.assertLess(first[0], first[1])

    def test_bootstrap_speedup_interval(self) -> None:
        interval = bootstrap_speedup_interval(
            [100.2, 102.4, 98.7, 101.3],
            [55.2, 57.1, 54.8, 56.4],
            seed=11,
        )
        self.assertGreater(interval[0], 1.5)
        self.assertLess(interval[1], 2.2)


if __name__ == "__main__":
    unittest.main()
