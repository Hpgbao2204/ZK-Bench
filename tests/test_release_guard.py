from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.release_guard import rejection_reason  # noqa: E402


class ReleaseGuardTests(unittest.TestCase):
    def test_allows_public_implementation_and_evidence_paths(self) -> None:
        allowed = [
            "Cargo.lock",
            "README.md",
            "README-CHECKPOINT.md",
            "adapters/jellyfish-plonk/src/main.rs",
            "configs/controlled-plonk-pilot.json",
            "scripts/run_bench.py",
            "tests/test_campaign.py",
            "results/pilot-v1/raw_results.csv",
            "results/pilot-v1/environment.json",
            "results/arithmetic-v1/parallelism-v1.csv",
        ]
        self.assertEqual(
            {path: rejection_reason(path) for path in allowed},
            {path: None for path in allowed},
        )

    def test_rejects_private_paper_and_non_evidence_outputs(self) -> None:
        rejected = [
            ".private/claim-registry.json",
            "Paper/main.tex",
            "scripts/plot_results.py",
            "figures/fig01.pdf",
            "results/pilot-v1/debug.log",
            "results/arithmetic-v1/notes.txt",
            "wallet.key",
        ]
        self.assertTrue(all(rejection_reason(path) for path in rejected))


if __name__ == "__main__":
    unittest.main()
