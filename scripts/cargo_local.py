#!/usr/bin/env python3
"""Run Cargo with every task-created cache and build artefact inside the repo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LOCAL = REPO / ".local"


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO.resolve())
        return True
    except ValueError:
        return False


def cargo_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    cargo_home = LOCAL / "cargo-home"
    target_dir = LOCAL / "cargo-target"
    git_home = LOCAL / "git-home"
    for path in (cargo_home, target_dir, git_home):
        if not _inside_repo(path):
            raise RuntimeError(f"local Cargo path escaped repository: {path}")
        path.mkdir(parents=True, exist_ok=True)
    environment["CARGO_HOME"] = str(cargo_home)
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    environment["GIT_CONFIG_GLOBAL"] = str(git_home / "gitconfig")
    environment["ZKBENCH_REPO_ROOT"] = str(REPO)
    return environment


def main() -> int:
    if len(sys.argv) == 1:
        print("usage: cargo_local.py <cargo arguments>", file=sys.stderr)
        return 2
    environment = cargo_environment()
    result = subprocess.run(
        ["cargo", *sys.argv[1:]],
        cwd=REPO,
        env=environment,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
