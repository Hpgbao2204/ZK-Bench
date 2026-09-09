#!/usr/bin/env python3
"""Validate a measured Base Sepolia proof-publication result bundle."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


SYSTEMS = {"groth16", "plonk", "stark", "bulletproofs"}
TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    errors: list[str] = []
    required = ["config.json", "environment.json", "raw_results.csv", "summary.csv"]
    for name in required:
        if not (args.bundle / name).is_file():
            errors.append(f"missing {name}")
    if errors:
        raise SystemExit("chain result bundle: FAIL\n" + "\n".join(errors))

    config = json.loads((args.bundle / "config.json").read_text(encoding="utf-8"))
    environment = json.loads((args.bundle / "environment.json").read_text(encoding="utf-8"))
    rows = list(csv.DictReader((args.bundle / "raw_results.csv").open(encoding="utf-8")))
    summaries = list(csv.DictReader((args.bundle / "summary.csv").open(encoding="utf-8")))
    expected_repetitions = int(config["repetitions"])
    expected_systems = set(config["artifacts"])
    expected_rows = expected_repetitions * len(expected_systems)

    if config.get("chain_id") != 84532 or environment.get("chain_id") != 84532:
        errors.append("chain ID is not Base Sepolia 84532")
    if environment.get("dry_run") is not False:
        errors.append("environment marks bundle as dry-run")
    if len(rows) != expected_rows:
        errors.append(f"expected {expected_rows} rows, received {len(rows)}")
    counts = Counter(row.get("system") for row in rows)
    for system in expected_systems:
        if counts[system] != expected_repetitions:
            errors.append(f"{system} has {counts[system]} rows")
    hashes = [row.get("transaction_hash", "") for row in rows]
    if len(set(hashes)) != len(hashes) or any(not TX_HASH.fullmatch(value) for value in hashes):
        errors.append("transaction hashes are invalid or duplicated")

    cumulative = 0
    artifact_shapes: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(rows, start=1):
        prefix = f"row {index}"
        if row.get("measurement_scope") != "proof_publication_only_no_evm_verification":
            errors.append(f"{prefix} has wrong measurement scope")
        if row.get("evidence_class") != "measured" or row.get("status") != "1":
            errors.append(f"{prefix} is not a successful measured receipt")
        numeric = {}
        for name in [
            "proof_bytes",
            "gas_used",
            "effective_gas_price_wei",
            "l2_execution_fee_wei",
            "l1_fee_wei",
            "receipt_total_fee_wei",
            "total_paid_wei_balance_delta",
        ]:
            try:
                numeric[name] = int(row[name])
                if numeric[name] <= 1:
                    raise ValueError
            except (KeyError, ValueError):
                errors.append(f"{prefix} has invalid {name}")
        if len(numeric) != 7:
            continue
        operator = int(row["operator_fee_wei"]) if row.get("operator_fee_wei") else 0
        expected_total = numeric["l2_execution_fee_wei"] + numeric["l1_fee_wei"] + operator
        if numeric["receipt_total_fee_wei"] != expected_total:
            errors.append(f"{prefix} receipt fee components do not sum")
        if numeric["total_paid_wei_balance_delta"] != expected_total:
            errors.append(f"{prefix} receipt fee differs from wallet balance delta")
        cumulative += expected_total
        if int(row["cumulative_receipt_fee_wei"]) != cumulative:
            errors.append(f"{prefix} cumulative fee is inconsistent")
        shape = (row["proof_bytes"], row["proof_sha256"])
        previous = artifact_shapes.setdefault(row["system"], shape)
        if previous != shape:
            errors.append(f"{prefix} proof artifact changed within one system")

    if cumulative > int(config["max_total_wei"]):
        errors.append("actual cumulative fee exceeded config cap")
    if len(rows) > int(config["max_transactions"]):
        errors.append("transaction count exceeded config cap")
    if {row.get("system") for row in summaries} != expected_systems:
        errors.append("summary system set differs from raw rows")
    for row in summaries:
        if int(row["n"]) != expected_repetitions:
            errors.append(f"summary n is wrong for {row.get('system')}")

    forbidden_names = {"private_key", "base_sepolia_private_key", "privatekey"}
    for path in required:
        value = json.dumps(json.loads((args.bundle / path).read_text(encoding="utf-8"))) \
            if path.endswith(".json") else (args.bundle / path).read_text(encoding="utf-8")
        lowered = value.lower()
        if any(name in lowered for name in forbidden_names):
            errors.append(f"{path} contains a forbidden credential field")

    if errors:
        print(f"chain result bundle: FAIL ({args.bundle})")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"chain result bundle: PASS ({args.bundle})")
    print(f"transactions={len(rows)} total_fee_wei={cumulative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
