#!/usr/bin/env python3
"""Re-derive a campaign summary from its immutable raw observations."""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.campaign import write_campaign_summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    bundle = args.bundle.resolve()
    raw_path = bundle / "raw_results.csv"
    output = args.output.resolve()
    if not raw_path.is_file():
        parser.error(f"missing raw campaign observations: {raw_path}")
    if output.exists() and not args.overwrite:
        parser.error(f"refusing to overwrite existing summary: {output}")

    with raw_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        parser.error("raw campaign contains no observations")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        write_campaign_summary(rows, temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"re-derived {output} from {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
