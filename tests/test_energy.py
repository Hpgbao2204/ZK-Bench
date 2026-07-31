from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zkbench.energy import UnavailableEnergyProvider  # noqa: E402


class EnergyTests(unittest.TestCase):
    def test_unavailable_provider_never_invents_joules(self) -> None:
        provider = UnavailableEnergyProvider("no trustworthy counter")
        reading = provider.stop(provider.start())
        self.assertFalse(reading.supported)
        self.assertIsNone(reading.joules)
        self.assertEqual(reading.reason, "no trustworthy counter")


if __name__ == "__main__":
    unittest.main()
