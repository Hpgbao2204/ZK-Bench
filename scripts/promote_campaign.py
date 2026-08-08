#!/usr/bin/env python3
"""Create a reproducible final-candidate campaign config from a pilot config."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.campaign import validate_campaign_config  # noqa: E402


def _integer_list(raw: str, name: str, minimum: int) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values or any(value < minimum for value in values):
        raise ValueError(f"{name} must contain integers >= {minimum}")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def promote(
    source: Path,
    output: Path,
    *,
    repetitions: int,
    os_cache_primer_runs: int,
    scales: list[int],
    threads: list[int],
    invalid_scales: list[int],
    invalid_threads: list[int],
) -> None:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing config: {output}")
    config = json.loads(source.read_text(encoding="utf-8"))
    config["experiment_id"] = f"{config['experiment_id'].removesuffix('-pilot-v1')}-final-v1"
    config["repetitions"] = repetitions
    config["os_cache_primer_runs"] = os_cache_primer_runs
    config["scales"] = scales
    config["threads"] = threads
    config["result_scope"] = (
        "final-candidate evidence; validate bundle before paper reporting"
    )
    for case in config.get("invalid_cases", []):
        case["repetitions"] = repetitions
        case["scales"] = list(invalid_scales)
        case["threads"] = list(invalid_threads)
    validate_campaign_config(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote final-candidate config: {output}")
    print(f"experiment_id: {config['experiment_id']}")
    print(f"repetitions: {config['repetitions']}")
    print(f"scales: {config['scales']}; threads: {config['threads']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--os-cache-primer-runs", type=int, default=2)
    parser.add_argument("--scales", default="2,4,8,16")
    parser.add_argument("--threads", default="1,2,4,8")
    parser.add_argument("--invalid-scales", default="2,8,16")
    parser.add_argument("--invalid-threads", default="1,4")
    args = parser.parse_args()
    if args.repetitions < 10:
        parser.error("final-candidate configs require at least 10 repetitions")
    if args.os_cache_primer_runs < 2:
        parser.error("final-candidate configs require at least two cache primers")
    promote(
        args.source,
        args.output,
        repetitions=args.repetitions,
        os_cache_primer_runs=args.os_cache_primer_runs,
        scales=_integer_list(args.scales, "scales", 2),
        threads=_integer_list(args.threads, "threads", 1),
        invalid_scales=_integer_list(args.invalid_scales, "invalid-scales", 2),
        invalid_threads=_integer_list(args.invalid_threads, "invalid-threads", 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
