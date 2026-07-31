#!/usr/bin/env python3
"""Reject staged paths that violate the public-artifact release boundary."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path, PurePosixPath

PUBLIC_ROOT_FILES = {
    ".gitignore",
    "Cargo.lock",
    "Cargo.toml",
    "LICENSE",
    "README.md",
    "pyproject.toml",
}
PUBLIC_DIRECTORIES = {
    ".github",
    "adapters",
    "benchmarks",
    "configs",
    "contracts",
    "crates",
    "scripts",
    "src",
    "tests",
}
RESULT_FILENAMES = {
    "config.json",
    "config.yaml",
    "environment.json",
    "raw_results.csv",
    "summary.csv",
}
FORBIDDEN_COMPONENTS = {
    ".codex",
    ".local",
    ".private",
    "paper",
}
FORBIDDEN_NAME_FRAGMENTS = {
    "cover_letter",
    "figure",
    "manuscript",
    "paper",
    "plot",
    "rebuttal",
    "response",
    "review",
}
SECRET_SUFFIXES = {".env", ".key", ".keystore", ".secret"}


def rejection_reason(raw_path: str) -> str | None:
    normalized = raw_path.replace("\\", "/").strip("/")
    if not normalized:
        return "empty path"
    path = PurePosixPath(normalized)
    lower_parts = [part.lower() for part in path.parts]
    lower_name = path.name.lower()

    if any(part in FORBIDDEN_COMPONENTS for part in lower_parts):
        return "local/private path"
    if any(fragment in lower_name for fragment in FORBIDDEN_NAME_FRAGMENTS):
        return "paper, review, response, plot, or figure artifact"
    if lower_name == ".env" or path.suffix.lower() in SECRET_SUFFIXES:
        return "secret-bearing filename"

    if len(path.parts) == 1:
        if path.name in PUBLIC_ROOT_FILES:
            return None
        return "root file is not allowlisted"

    top = lower_parts[0]
    if top == "results":
        if path.name not in RESULT_FILENAMES:
            return "result bundle file is not allowlisted"
        return None
    if top in PUBLIC_DIRECTORIES:
        return None
    return "top-level directory is not allowlisted"


def staged_paths(repo: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMRDTUXB",
            "-z",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="inspect the current Git index",
    )
    args = parser.parse_args()
    if not args.staged:
        parser.error("--staged is required")

    repo = args.repo.resolve()
    rejected = [
        (path, reason)
        for path in staged_paths(repo)
        if (reason := rejection_reason(path)) is not None
    ]
    if rejected:
        print("release guard: FAIL")
        for path, reason in rejected:
            print(f"  {path}: {reason}")
        return 1

    print(f"release guard: PASS ({len(staged_paths(repo))} staged paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
