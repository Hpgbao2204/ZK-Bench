#!/usr/bin/env python3
"""Measure serialized proof publication cost on Base Sepolia receipts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from eth_account import Account  # noqa: E402

from zkbench.base_sepolia import (  # noqa: E402
    Artifact,
    JsonRpcClient,
    load_env_values,
    quantity,
    reserve_transaction,
    transaction_row,
)
from zkbench.chain import TransactionBudget  # noqa: E402


DEFAULT_CONFIG = REPO / "configs" / "base-sepolia-publication.json"
DEFAULT_OUTPUT = (
    REPO / ".local" / "reproductions" / "base-sepolia" / "identity-publication-final-v1"
)


def canonical_hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summary_rows(rows: list[dict]) -> list[dict]:
    output = []
    for system in sorted({row["system"] for row in rows}):
        selected = [row for row in rows if row["system"] == system]
        summary = {
            "schema_version": "1.0",
            "evidence_class": "measured",
            "measurement_scope": "proof_publication_only_no_evm_verification",
            "chain_id": 84532,
            "system": system,
            "n": len(selected),
            "proof_bytes": selected[0]["proof_bytes"],
            "proof_sha256": selected[0]["proof_sha256"],
        }
        for source, stem in [
            ("gas_used", "gas_used"),
            ("l2_execution_fee_wei", "l2_execution_fee_wei"),
            ("receipt_total_fee_wei", "total_paid_wei"),
            ("receipt_latency_ms", "receipt_latency_ms"),
        ]:
            values = [float(row[source]) for row in selected]
            summary[f"{stem}_mean"] = statistics.mean(values)
            summary[f"{stem}_p50"] = statistics.median(values)
            summary[f"{stem}_p95"] = percentile(values, 0.95)
            summary[f"{stem}_stdev"] = statistics.stdev(values) if len(values) > 1 else None
        l1_values = [row["l1_fee_wei"] for row in selected if row["l1_fee_wei"] is not None]
        summary["l1_fee_receipt_samples"] = len(l1_values)
        summary["l1_fee_wei_p50"] = statistics.median(l1_values) if l1_values else None
        output.append(summary)
    return output


def fee_parameters(client: JsonRpcClient) -> tuple[int, int]:
    gas_price = int(client.call("eth_gasPrice", []), 16)
    try:
        priority = int(client.call("eth_maxPriorityFeePerGas", []), 16)
    except RuntimeError:
        priority = max(2, gas_price // 10)
    latest = client.call("eth_getBlockByNumber", ["latest", False])
    base_fee = quantity(latest.get("baseFeePerGas")) or gas_price
    max_fee = max(gas_price * 2, 2 * base_fee + priority)
    return max_fee, priority


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--system", action="append")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.repetitions is not None:
        if args.repetitions < 1:
            parser.error("--repetitions must be positive")
        config["repetitions"] = args.repetitions
    systems = list(args.system or config["artifacts"].keys())
    unknown = sorted(set(systems) - set(config["artifacts"]))
    if unknown:
        parser.error(f"unknown systems: {', '.join(unknown)}")
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite output bundle: {args.output}")
    args.output.mkdir(parents=True)

    env_path = REPO / config["credentials_file"]
    values = load_env_values(env_path)
    private_key = values.get("BASE_SEPOLIA_PRIVATE_KEY")
    if not private_key:
        raise ValueError("credential file lacks PRIVATE_KEY/BASE_SEPOLIA_PRIVATE_KEY")
    account = Account.from_key(private_key)
    if account.address.lower() != config["expected_public_address"].lower():
        raise RuntimeError("derived wallet address does not match campaign config")

    rpc_url = values.get("BASE_SEPOLIA_RPC_URL", config["rpc_default"])
    client = JsonRpcClient(rpc_url)
    chain_id = client.chain_id()
    artifacts = {
        system: Artifact.load(system, REPO / config["artifacts"][system])
        for system in systems
    }
    budget = TransactionBudget(config["max_transactions"], config["max_total_wei"])
    tasks = [
        (repetition, system)
        for repetition in range(1, config["repetitions"] + 1)
        for system in systems
    ]
    random.Random(84532).shuffle(tasks)
    environment = {
        "schema_version": "1.0",
        "chain_id": chain_id,
        "public_address": account.address,
        "rpc_host": urlsplit(rpc_url).hostname,
        "rpc_client_version": client.call("web3_clientVersion", []),
        "config_hash": canonical_hash(config),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "tracked_worktree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=REPO,
                text=True,
            ).strip()
        ),
        "started_at": datetime.now(UTC).isoformat(),
        "dry_run": args.dry_run,
    }
    (args.output / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output / "environment.json").write_text(
        json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    estimates = []
    rows: list[dict] = []
    cumulative_receipt_fee_wei = 0
    for index, (repetition, system) in enumerate(tasks, start=1):
        artifact = artifacts[system]
        client.chain_id()  # mandatory network gate immediately before signing
        estimate_request = {
            "from": account.address,
            "to": account.address,
            "value": "0x0",
            "data": "0x" + artifact.data.hex(),
        }
        estimated_gas = int(client.call("eth_estimateGas", [estimate_request]), 16)
        gas_limit = math.ceil(estimated_gas * config["gas_limit_multiplier"])
        max_fee, priority_fee = fee_parameters(client)
        reserved = reserve_transaction(
            budget,
            gas_limit=gas_limit,
            max_fee_per_gas=max_fee,
            l1_fee_reserve_wei=config["l1_fee_reserve_wei_per_transaction"],
        )
        estimates.append(
            {
                "system": system,
                "repetition": repetition,
                "proof_bytes": len(artifact.data),
                "estimated_gas": estimated_gas,
                "gas_limit": gas_limit,
                "max_fee_per_gas_wei": max_fee,
                "max_priority_fee_per_gas_wei": priority_fee,
                "reserved_max_wei": reserved,
            }
        )
        print(
            f"[{index}/{len(tasks)}] {system} rep={repetition} "
            f"bytes={len(artifact.data)} estimate_gas={estimated_gas}",
            flush=True,
        )
        if args.dry_run:
            continue

        nonce = client.pending_nonce(account.address)
        transaction = {
            "chainId": chain_id,
            "nonce": nonce,
            "to": account.address,
            "value": 0,
            "data": artifact.data,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "type": 2,
        }
        balance_before = client.balance(account.address)
        submitted_at = datetime.now(UTC).isoformat()
        start = time.perf_counter()
        signed = Account.sign_transaction(transaction, private_key)
        transaction_hash = client.call(
            "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()]
        )
        receipt = client.wait_for_receipt(
            transaction_hash, config["receipt_timeout_seconds"]
        )
        receipt_latency_ms = (time.perf_counter() - start) * 1000
        balance_after = client.wait_for_balance_change(account.address, balance_before)
        # Base may expose a provisional L1 fee in the first receipt response.
        # Re-fetch after the wallet balance changes so fee fields agree with
        # the finalized account deduction.
        refreshed_receipt = client.call("eth_getTransactionReceipt", [transaction_hash])
        if refreshed_receipt is not None:
            receipt = refreshed_receipt
        row = transaction_row(
            artifact=artifact,
            repetition=repetition,
            receipt=receipt,
            submitted_at=submitted_at,
            receipt_latency_ms=receipt_latency_ms,
            balance_before_wei=balance_before,
            balance_after_wei=balance_after,
        )
        if row["status"] != 1:
            raise RuntimeError(f"publication transaction failed: {transaction_hash}")
        cumulative_receipt_fee_wei += row["receipt_total_fee_wei"]
        row["cumulative_receipt_fee_wei"] = cumulative_receipt_fee_wei
        if cumulative_receipt_fee_wei > config["max_total_wei"]:
            raise RuntimeError("actual cumulative transaction fees exceeded campaign cap")
        rows.append(row)
        with (args.output / "raw_results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        write_csv(args.output / "raw_results.csv", rows)
        write_csv(args.output / "summary.csv", summary_rows(rows))
        print(
            f"  receipt={transaction_hash} gas={row['gas_used']} "
            f"paid_wei={row['receipt_total_fee_wei']}",
            flush=True,
        )
        time.sleep(config["transaction_delay_seconds"])

    (args.output / "estimates.json").write_text(
        json.dumps(estimates, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not args.dry_run:
        write_csv(args.output / "raw_results.csv", rows)
        write_csv(args.output / "summary.csv", summary_rows(rows))
    print(f"campaign complete: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
