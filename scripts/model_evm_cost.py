#!/usr/bin/env python3
"""Derive fork-aware EVM gas and economic rows from measured proof bundles."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = REPO / "configs" / "reproduction-gas-model.json"


def _number(row: dict[str, str], names: tuple[str, ...], path: Path) -> float:
    for name in names:
        value = row.get(name, "")
        if value:
            parsed = float(value)
            if parsed > 1:
                return parsed
    raise ValueError(f"{path}: no nonboundary proof-size summary")


def proof_metrics_from_summary(path: Path) -> tuple[float, int]:
    """Return proof bytes from the largest measured relation and that relation size."""
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    candidates = [
        row
        for row in rows
        if row.get("phase") == "adapter_process_wall"
        and row.get("invalid_proof_kind", "") == ""
        and row.get("recorded", "true").lower() != "false"
    ]
    if not candidates:
        raise ValueError(f"{path}: no valid adapter_process_wall summary row")
    row = max(candidates, key=lambda item: int(float(item.get("input_scale") or 0)))
    proof_bytes = _number(
        row, ("p50_proof_bytes", "proof_bytes", "mean_proof_bytes"), path
    )
    return proof_bytes, int(float(row.get("input_scale") or 0))


def pairing_gas(schedule: dict, pairs: int) -> int:
    return int(schedule["pairing_base_gas"]) + pairs * int(
        schedule["pairing_per_pair_gas"]
    )


def eip2537_g1_msm_gas(schedule: dict, terms: int) -> int:
    discounts = schedule["g1_msm_discounts_per_mille"]
    try:
        discount = int(discounts[str(terms)])
    except KeyError as error:
        raise ValueError(f"missing EIP-2537 G1 MSM discount for k={terms}") from error
    return terms * int(schedule["g1_mul_gas"]) * discount // 1000


def verifier_compute_gas(profile: dict, adapter: str, public_inputs: int) -> int:
    adapter_model = profile["adapters"][adapter]["verifier_model"]
    schedule = profile["precompile_schedule"]
    kind = adapter_model["kind"]
    if kind == "fixed":
        return int(adapter_model["gas"])
    if kind == "bn254-groth16":
        vk_x = public_inputs * int(schedule["g1_mul_gas"])
        vk_x += public_inputs * int(schedule["g1_add_gas"])
        return vk_x + pairing_gas(schedule, int(adapter_model["pairing_pairs"]))
    if kind == "bn254-plonk":
        total = pairing_gas(schedule, int(adapter_model["pairing_pairs"]))
        for terms in adapter_model["g1_msm_terms"]:
            terms = int(terms)
            total += terms * int(schedule["g1_mul_gas"])
            total += (terms - 1) * int(schedule["g1_add_gas"])
        return total
    if kind == "eip2537-groth16":
        terms = public_inputs + int(adapter_model["public_input_msm_offset"])
        return eip2537_g1_msm_gas(schedule, terms) + pairing_gas(
            schedule, int(adapter_model["pairing_pairs"])
        )
    if kind == "eip2537-plonk":
        total = pairing_gas(schedule, int(adapter_model["pairing_pairs"]))
        total += sum(
            eip2537_g1_msm_gas(schedule, int(terms))
            for terms in adapter_model["g1_msm_terms"]
        )
        return total
    raise ValueError(f"unsupported verifier model kind: {kind}")


def transaction_gas(
    model: dict, profile: dict, payload_bytes: float, compute_gas: int
) -> tuple[float, float, float, str]:
    """Return total, calldata component, calldata tokens, and active pricing branch."""
    nonzero_fraction = float(model["assumed_nonzero_fraction"])
    nonzero_bytes = payload_bytes * nonzero_fraction
    zero_bytes = payload_bytes - nonzero_bytes
    pricing = profile["calldata_pricing"]
    intrinsic = float(model["transaction_intrinsic_gas"])
    if pricing["mode"] == "eip2028":
        calldata = zero_bytes * float(pricing["zero_byte_gas"])
        calldata += nonzero_bytes * float(pricing["nonzero_byte_gas"])
        return intrinsic + calldata + compute_gas, calldata, 0.0, "eip2028"
    if pricing["mode"] == "eip7623":
        tokens = zero_bytes + 4 * nonzero_bytes
        standard = float(pricing["standard_token_cost"]) * tokens + compute_gas
        floor = float(pricing["total_cost_floor_per_token"]) * tokens
        branch = "standard" if standard >= floor else "eip7623-floor"
        effective_data_gas = max(standard, floor) - compute_gas
        return intrinsic + max(standard, floor), effective_data_gas, tokens, branch
    raise ValueError(f"unsupported calldata pricing mode: {pricing['mode']}")


def modeled_rows(model: dict, bundle_root: Path, workload: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    public_inputs = int(model["public_inputs_by_workload"][workload])
    for profile_id, profile in model["profiles"].items():
        for adapter, adapter_profile in profile["adapters"].items():
            summary = bundle_root / f"{workload}-{adapter}-final-v1" / "summary.csv"
            native_proof_bytes, selected_scale = proof_metrics_from_summary(summary)
            configured_bytes = adapter_profile["proof_calldata_bytes"]
            proof_calldata_bytes = (
                native_proof_bytes if configured_bytes == "measured" else float(configured_bytes)
            )
            payload_bytes = proof_calldata_bytes + public_inputs * float(
                model["public_input_bytes"]
            )
            compute_gas = verifier_compute_gas(profile, adapter, public_inputs)
            fixed_gas, calldata_gas, calldata_tokens, branch = transaction_gas(
                model, profile, payload_bytes, compute_gas
            )
            for batch_size in model["batch_sizes"]:
                amortized = fixed_gas / int(batch_size) + float(
                    model["residual_batch_gas_per_application_unit"]
                )
                for gas_price in model["gas_prices_gwei"]:
                    usd = amortized * float(gas_price) * 1e-9 * float(model["eth_usd"])
                    rows.append(
                        {
                            "model_id": model["model_id"],
                            "evidence_class": model.get("evidence_class", "modeled"),
                            "profile_id": profile_id,
                            "profile_role": profile["role"],
                            "profile_label": profile["label"],
                            "security_bits": profile["security_bits"],
                            "evm_fork": profile["evm_fork"],
                            "workload": workload,
                            "adapter": adapter,
                            "selected_input_scale": selected_scale,
                            "native_proof_bytes": round(native_proof_bytes, 6),
                            "proof_calldata_bytes": round(proof_calldata_bytes, 6),
                            "public_inputs": public_inputs,
                            "calldata_payload_bytes": round(payload_bytes, 6),
                            "calldata_tokens": round(calldata_tokens, 6),
                            "calldata_gas": round(calldata_gas, 6),
                            "calldata_pricing_branch": branch,
                            "verifier_compute_gas": compute_gas,
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
