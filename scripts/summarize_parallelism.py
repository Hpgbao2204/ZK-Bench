#!/usr/bin/env python3
"""Derive parallel speedup and efficiency from measured arithmetic summaries."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path


def reportable(value: float) -> str:
    if not math.isfinite(value) or value in (0.0, 1.0):
        return ""
    return f"{value:.8f}"


def summarize(source: Path, destination: Path) -> int:
    with source.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"curve", "operation", "size", "threads", "execution_mode", "elapsed_ns_p50"}
    missing = required - set(rows[0] if rows else ())
    if missing:
        raise ValueError(f"missing columns: {', '.join(sorted(missing))}")

    baseline: dict[tuple[str, str, int], float] = {}
    parallel: list[tuple[str, str, int, int, float]] = []
    for row in rows:
        try:
            key = (row["curve"], row["operation"], int(row["size"]))
            threads = int(row["threads"])
            elapsed = float(row["elapsed_ns_p50"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid parallelism row: {error}") from error
        if not math.isfinite(elapsed) or elapsed <= 1 or threads <= 0:
            raise ValueError("non-positive or boundary elapsed/thread value")
        if row["execution_mode"] == "serial" and threads == 1:
            baseline[key] = elapsed
        elif row["execution_mode"] == "parallel":
            parallel.append((*key, threads, elapsed))

    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "curve",
        "operation",
        "size",
        "threads",
        "serial_elapsed_ns_p50",
        "parallel_elapsed_ns_p50",
        "speedup",
        "efficiency",
    ]
    count = 0
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for curve, operation, size, threads, elapsed in sorted(parallel):
            serial = baseline.get((curve, operation, size))
            if serial is None:
                continue
            speedup = serial / elapsed
            efficiency = speedup / threads
            writer.writerow(
                {
                    "curve": curve,
                    "operation": operation,
                    "size": size,
                    "threads": threads,
                    "serial_elapsed_ns_p50": f"{serial:.6f}",
                    "parallel_elapsed_ns_p50": f"{elapsed:.6f}",
                    "speedup": reportable(speedup),
                    "efficiency": reportable(efficiency),
                }
            )
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = summarize(args.source, args.output)
    print(f"parallelism summary: {count} matched rows -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
