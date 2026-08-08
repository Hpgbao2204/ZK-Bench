#!/usr/bin/env python3
"""Summarize raw arithmetic-backend measurements without inventing values.

The Rust arithmetic runner intentionally emits one row per operation, size,
and repetition.  This script computes robust group summaries for plotting and
tables.  It uses only the Python standard library so that analysis does not
require a global package install.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path


REQUIRED_COLUMNS = {
    "curve",
    "operation",
    "size",
    "repetition",
    "elapsed_ns",
    "operations",
}


def _finite(values: list[float]) -> list[float]:
    return [value for value in values if math.isfinite(value)]


def _quantile(values: list[float], probability: float) -> float:
    """Linear-interpolated quantile with no dependency on numpy/pandas."""

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _reportable(value: float) -> float | None:
    """Return a value unless it is an exact boundary of a normalized metric."""

    if not math.isfinite(value) or value in (0.0, 1.0):
        return None
    return value


def summarize(source: Path, destination: Path) -> int:
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or ())
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"missing columns: {', '.join(sorted(missing))}")

        groups: dict[tuple[str, str, int], list[tuple[int, int]]] = {}
        for row_number, row in enumerate(reader, start=2):
            try:
                key = (row["curve"], row["operation"], int(row["size"]))
                repetition = int(row["repetition"])
                elapsed_ns = int(row["elapsed_ns"])
                operations = int(row["operations"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid row {row_number}: {error}") from error
            if repetition < 0 or elapsed_ns <= 0 or operations <= 0:
                raise ValueError(f"non-positive measurement at row {row_number}")
            groups.setdefault(key, []).append((elapsed_ns, operations))

    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "curve",
        "operation",
        "size",
        "repetitions",
        "elapsed_ns_p50",
        "elapsed_ns_p95",
        "elapsed_ns_mean",
        "elapsed_ns_cv",
        "operations",
        "throughput_ops_per_s_p50",
    ]
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for (curve, operation, size), rows in sorted(groups.items()):
            elapsed = [float(item[0]) for item in rows]
            operations = rows[0][1]
            mean = statistics.fmean(elapsed)
            deviation = statistics.stdev(elapsed) if len(elapsed) >= 2 else float("nan")
            cv = deviation / mean if mean else float("nan")
            p50 = _quantile(elapsed, 0.50)
            p95 = _quantile(elapsed, 0.95)
            throughput = operations * 1_000_000_000 / p50
            writer.writerow(
                {
                    "curve": curve,
                    "operation": operation,
                    "size": size,
                    "repetitions": len(rows),
                    "elapsed_ns_p50": f"{p50:.6f}",
                    "elapsed_ns_p95": f"{p95:.6f}",
                    "elapsed_ns_mean": f"{mean:.6f}",
                    "elapsed_ns_cv": (
                        f"{_reportable(cv):.8f}" if _reportable(cv) is not None else ""
                    ),
                    "operations": operations,
                    "throughput_ops_per_s_p50": f"{throughput:.6f}",
                }
            )
    return len(groups)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    groups = summarize(args.source, args.output)
    print(f"arithmetic summary: {groups} groups -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
