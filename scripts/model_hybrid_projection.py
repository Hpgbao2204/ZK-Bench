#!/usr/bin/env python3
"""Evaluate the manuscript's explicitly modeled STARK-in-SNARK sensitivity."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def prover_ms(bundle_root: Path, adapter: str) -> float:
    rows = read_csv(bundle_root / f"pcas-{adapter}-final-v1" / "summary.csv")
    matches = [
        row
        for row in rows
        if row["phase"] == "prove_total"
        and row["input_scale"] == "65536"
        and row.get("invalid_proof_kind", "") == ""
    ]
    if len(matches) != 1 or int(matches[0]["n"]) != 10:
        raise ValueError(f"missing ten-run PCAS prover row for {adapter}")
    return float(matches[0]["mean_latency_ms"])


def outer_gas(gas_csv: Path, adapter: str) -> float:
    rows = read_csv(gas_csv)
    matches = [
        row
        for row in rows
        if row["adapter"] == adapter
        and row["batch_size"] == "1"
        and row["gas_price_gwei"] == "20"
    ]
    if len(matches) != 1:
        raise ValueError(f"missing outer gas row for {adapter}")
    return float(matches[0]["fixed_gas"])


def project(model: dict, bundle_root: Path, gas_csv: Path) -> list[dict[str, object]]:
    inner_ms = prover_ms(bundle_root, model["inner_adapter"])
    comparison_ms = prover_ms(bundle_root, model["comparison_adapter"])
    verifier_gas = outer_gas(gas_csv, model["outer_adapter"])
    break_even = comparison_ms - inner_ms
    return [
        {
            "model_id": model["model_id"],
            "evidence_class": "modeled",
            "measured_inner_stark_prover_ms": round(inner_ms, 6),
            "assumed_outer_wrapper_prover_ms": float(assumption),
            "hybrid_prover_ms": round(inner_ms + float(assumption), 6),
            "hybrid_verifier_gas": round(verifier_gas, 6),
            "measured_comparison_plonk_prover_ms": round(comparison_ms, 6),
            "break_even_outer_prover_ms": round(break_even, 6),
            "beats_comparison_latency": float(assumption) < break_even,
        }
        for assumption in model["outer_prover_assumptions_ms"]
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        type=Path,
        default=REPO / "configs" / "reproduction-hybrid-model.json",
    )
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--gas-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = json.loads(args.model.read_text(encoding="utf-8"))
    rows = project(model, args.bundle_root, args.gas_csv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"hybrid projection: PASS ({len(rows)} rows -> {args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
