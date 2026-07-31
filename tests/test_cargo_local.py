from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from cargo_local import cargo_environment  # noqa: E402


class CargoLocalTests(unittest.TestCase):
    def test_all_task_created_paths_are_local(self) -> None:
        environment = cargo_environment({})
        for name in ("CARGO_HOME", "CARGO_TARGET_DIR", "GIT_CONFIG_GLOBAL"):
            path = Path(environment[name]).resolve()
            path.relative_to(REPO.resolve())

    def test_does_not_override_rustup_home(self) -> None:
        environment = cargo_environment({"RUSTUP_HOME": "preexisting-toolchain"})
        self.assertEqual(environment["RUSTUP_HOME"], "preexisting-toolchain")


if __name__ == "__main__":
    unittest.main()
