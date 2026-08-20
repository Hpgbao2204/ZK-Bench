#!/usr/bin/env python3
"""Run one or more full-scale reproduction campaigns from the frozen matrix."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.campaign import run_adapter_campaign, validate_campaign_config  # noqa: E402


MATRIX_PATH = REPO / "configs" / "reproduction-scale-campaigns.json"


def load_matrix(path: Path = MATRIX_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def campaign_names(matrix: dict) -> tuple[str, ...]:
    return tuple(
        f"{workload}-{adapter}"
        for workload in matrix["workloads"]
        for adapter in matrix["adapters"]
    )


def build_campaign(matrix: dict, name: str, *, smoke: bool = False) -> dict:
    try:
        workload_name, adapter_name = name.split("-", 1)
        workload = matrix["workloads"][workload_name]
        adapter = matrix["adapters"][adapter_name]
    except (KeyError, ValueError) as error:
        raise ValueError(f"unknown paper campaign: {name}") from error

    config = copy.deepcopy(matrix["defaults"])
    config.update(copy.deepcopy(adapter))
    config.update(
        {
            "claim_id": workload["claim_id"],
            "experiment_id": f"paper-{name}-v1",
            "invalid_cases": copy.deepcopy(workload["invalid_cases"]),
            "parameter_sets": [
                {
                    "id": f"{workload_name}-paper-r1",
                    "parameters": copy.deepcopy(workload["parameters"]),
                }
            ],
            "relation": copy.deepcopy(workload["relation"]),
            "result_scope": workload["result_scope"],
            "scales": list(workload["scales"]),
            "workload": workload["workload"],
        }
    )
    if smoke:
        smoke_scale = 8192 if workload_name == "pcas" else 1024
        config.update(
            {
                "experiment_id": f"paper-{name}-smoke-v1",
                "invalid_cases": [],
                "os_cache_primer_runs": 1,
                "repetitions": 3,
                "require_clean_git": False,
                "scales": [smoke_scale],
                "threads": [16],
                "timeout_seconds": 300,
            }
        )
    validate_campaign_config(config)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", action="append", help="Campaign name; repeat as needed")
    parser.add_argument("--all", action="store_true", help="Run every campaign")
    parser.add_argument("--smoke", action="store_true", help="Use one 256-unit trial")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO / ".local" / "reproductions" / "paper-scale",
    )
    parser.add_argument("--list", action="store_true", help="List campaign names and exit")
    args = parser.parse_args()

    matrix = load_matrix()
    names = campaign_names(matrix)
    if args.list:
        print("\n".join(names))
        return 0
    selected = list(names) if args.all else list(args.campaign or [])
    if not selected:
        parser.error("select --campaign NAME (repeatable) or --all")

    for name in selected:
        config = build_campaign(matrix, name, smoke=args.smoke)
        suffix = "smoke" if args.smoke else "final"
        output = args.output_root / f"{name}-{suffix}-v1"
        print(f"[{name}] output={output}", flush=True)
        run_adapter_campaign(
            config,
            output,
            repo=REPO,
            progress=lambda message, current=name: print(
                f"[{current}] {message}", flush=True
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
