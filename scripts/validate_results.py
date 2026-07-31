#!/usr/bin/env python3
"""Validate the content and lineage of a public ZK Bench result bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.result_validation import validate_result_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--repo", type=Path, default=REPO)
    args = parser.parse_args()
    errors = validate_result_bundle(args.bundle, repo=args.repo)
    if errors:
        print("result bundle: INVALID", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"result bundle: PASS ({args.bundle.resolve()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
