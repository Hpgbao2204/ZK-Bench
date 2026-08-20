#!/usr/bin/env python3
"""Derive EVM gas and economic rows from measured proof bundles."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = REPO / "configs" / "reproduction-gas-model.json"


def proof_bytes_from_summary(path: Path) -> float:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    candidates = [
        row
        for row in rows
        if row.get("phase") == "adapter_process_wall"
        and row.get("invalid_proof_kind", "") == ""
    ]
    if not candidates:
        raise ValueError(f"{path}: no valid adapter_process_wall summary row")
    row = candidates[0]
    for name in ("p50_proof_bytes", "proof_bytes", "mean_proof_bytes"):
        value = row.get(name, "")
        if value:
            parsed = float(value)
            if parsed > 1:
                return parsed
    raise ValueError(f"{path}: no nonboundary proof-size summary")


def modeled_rows(model: dict, bundle_root: Path, workload: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    nonzero_fraction = float(model["assumed_nonzero_fraction"])
    byte_gas = (
        nonzero_fraction * float(model["nonzero_calldata_gas_per_byte"])
        + (1 - nonzero_fraction) * float(model["zero_calldata_gas_per_byte"])
    )
    for adapter, compute_gas in model["verifier_compute_gas"].items():
        summary = bundle_root / f"{workload}-{adapter}-final-v1" / "summary.csv"
        proof_bytes = proof_bytes_from_summary(summary)
        fixed_gas = (
            float(model["transaction_intrinsic_gas"])
            + float(compute_gas)
            + proof_bytes * byte_gas
        )
        for batch_size in model["batch_sizes"]:
            amortized = (
                fixed_gas / int(batch_size)
                + float(model["residual_batch_gas_per_application_unit"])
            )
            for gas_price in model["gas_prices_gwei"]:
                usd = amortized * float(gas_price) * 1e-9 * float(model["eth_usd"])
                rows.append(
                    {
                        "model_id": model["model_id"],
                        "evidence_class": "modeled",
                        "workload": workload,
                        "adapter": adapter,
                        "proof_bytes": round(proof_bytes, 6),
                        "verifier_compute_gas": int(compute_gas),
                        "fixed_gas": round(fixed_gas, 6),
                        "batch_size": int(batch_size),
                        "gas_price_gwei": int(gas_price),
                        "eth_usd": float(model["eth_usd"]),
                        "amortized_gas_per_application_unit": round(amortized, 6),
                        "modeled_cost_usd": round(usd, 6),
                    }
                )
    return rows


def write_rows(rows: list[dict[str, object]], output: Path) -> None:
    if not rows:
        raise ValueError("gas model produced no rows")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--workload", choices=("identity", "state", "pcas"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = json.loads(args.model.read_text(encoding="utf-8"))
    rows = modeled_rows(model, args.bundle_root, args.workload)
    write_rows(rows, args.output)
    print(f"modeled gas rows: PASS ({len(rows)} rows -> {args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
