#!/usr/bin/env python3
"""Run a configured ZK Bench experiment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.runner import run_reference  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config.get("adapter") != "reference-predicate":
        parser.error("only the reference-predicate adapter is available in this dependency-free slice")
    run_reference(config, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
