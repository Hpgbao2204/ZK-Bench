#!/usr/bin/env python3
"""Merge arithmetic raw CSVs without changing observations.

The serial and parallel runs are intentionally separate processes so each can
configure its Rayon pool once. This utility concatenates their rows only after
checking that all files use the same schema; the ``threads`` and
``execution_mode`` columns remain untouched for downstream grouping.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def merge(sources: list[Path], destination: Path) -> int:
    if not sources:
        raise ValueError("at least one source CSV is required")
    header: list[str] | None = None
    rows = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer: csv.writer | None = None
        for source in sources:
            with source.open(newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                current = next(reader, None)
                if current is None:
                    raise ValueError(f"empty arithmetic CSV: {source}")
                if header is None:
                    header = current
                    writer = csv.writer(output)
                    writer.writerow(header)
                elif current != header:
                    raise ValueError(f"schema mismatch in {source}")
                assert writer is not None
                for row in reader:
                    if len(row) != len(header):
                        raise ValueError(f"column count mismatch in {source}")
                    writer.writerow(row)
                    rows += 1
    if rows == 0:
        raise ValueError("merged arithmetic CSV contains no observations")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = merge(args.sources, args.output)
    print(f"arithmetic merge: {rows} rows -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
