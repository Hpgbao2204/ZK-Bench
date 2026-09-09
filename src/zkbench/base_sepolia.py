"""Measured proof-publication transactions on Base Sepolia.

This module deliberately measures publication only. A successful transaction
to the benchmark wallet proves that the serialized proof bytes were included
as calldata; it does not claim cryptographic verification by the EVM.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chain import BASE_SEPOLIA_CHAIN_ID, TransactionBudget, require_base_sepolia


USER_AGENT = "zkbench/0.1 Base-Sepolia-publication-benchmark"


def quantity(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(value, 16)


class JsonRpcClient:
    def __init__(self, url: str, timeout_seconds: float = 20.0) -> None:
        if not url.startswith(("https://", "http://")):
            raise ValueError("RPC URL must be HTTP(S)")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params,
            }
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            value = json.loads(response.read())
        if "error" in value:
            raise RuntimeError(f"RPC {method} failed: {value['error']}")
        return value["result"]

    def chain_id(self) -> int:
        chain_id = int(self.call("eth_chainId", []), 16)
        require_base_sepolia(chain_id)
        return chain_id

    def balance(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def pending_nonce(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "pending"]), 16)

    def wait_for_receipt(self, transaction_hash: str, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            receipt = self.call("eth_getTransactionReceipt", [transaction_hash])
            if receipt is not None:
                return receipt
            time.sleep(1.0)
        raise TimeoutError(f"receipt timeout for {transaction_hash}")

    def wait_for_balance_change(
        self, address: str, previous_balance: int, timeout_seconds: float = 15.0
    ) -> int:
        deadline = time.monotonic() + timeout_seconds
        latest = previous_balance
        while time.monotonic() < deadline:
            latest = self.balance(address)
            if latest != previous_balance:
                return latest
            time.sleep(0.5)
        return latest


@dataclass(frozen=True)
class Artifact:
    system: str
    path: Path
    data: bytes
    sha256: str

    @classmethod
    def load(cls, system: str, path: Path) -> "Artifact":
        data = path.read_bytes()
        if len(data) <= 1:
            raise ValueError(f"proof artifact is empty or a sentinel: {path}")
        return cls(system, path, data, hashlib.sha256(data).hexdigest())


def load_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    if "BASE_SEPOLIA_PRIVATE_KEY" not in values and "PRIVATE_KEY" in values:
        values["BASE_SEPOLIA_PRIVATE_KEY"] = values["PRIVATE_KEY"]
    values.setdefault("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")
    return values


def transaction_row(
    *,
    artifact: Artifact,
    repetition: int,
    receipt: dict[str, Any],
    submitted_at: str,
    receipt_latency_ms: float,
    balance_before_wei: int,
    balance_after_wei: int,
) -> dict[str, Any]:
    gas_used = quantity(receipt.get("gasUsed"))
    effective_gas_price = quantity(receipt.get("effectiveGasPrice"))
    if gas_used is None or effective_gas_price is None:
        raise ValueError("receipt omitted gasUsed or effectiveGasPrice")
    l2_execution_fee = gas_used * effective_gas_price
    balance_delta = balance_before_wei - balance_after_wei
    if balance_delta < 0:
        raise ValueError("wallet balance increased during zero-value publication transaction")
    l1_fee = quantity(receipt.get("l1Fee"))
    operator_fee = quantity(receipt.get("operatorFee"))
    receipt_total_fee = l2_execution_fee + (l1_fee or 0) + (operator_fee or 0)
    return {
        "schema_version": "1.0",
        "evidence_class": "measured",
        "measurement_scope": "proof_publication_only_no_evm_verification",
        "chain_id": BASE_SEPOLIA_CHAIN_ID,
        "system": artifact.system,
        "repetition": repetition,
        "proof_bytes": len(artifact.data),
        "proof_sha256": artifact.sha256,
        "submitted_at": submitted_at,
        "transaction_hash": receipt["transactionHash"],
        "block_number": quantity(receipt.get("blockNumber")),
        "status": quantity(receipt.get("status")),
        "gas_used": gas_used,
        "effective_gas_price_wei": effective_gas_price,
        "l2_execution_fee_wei": l2_execution_fee,
        "l1_fee_wei": l1_fee,
        "l1_gas_used": quantity(receipt.get("l1GasUsed")),
        "l1_gas_price_wei": quantity(receipt.get("l1GasPrice")),
        "l1_base_fee_scalar": quantity(receipt.get("l1BaseFeeScalar")),
        "l1_blob_base_fee_wei": quantity(receipt.get("l1BlobBaseFee")),
        "l1_blob_base_fee_scalar": quantity(receipt.get("l1BlobBaseFeeScalar")),
        "blob_gas_used": quantity(receipt.get("blobGasUsed")),
        "da_footprint_gas_scalar": quantity(receipt.get("daFootprintGasScalar")),
        "operator_fee_wei": operator_fee,
        "operator_fee_scalar": quantity(receipt.get("operatorFeeScalar")),
        "operator_fee_constant": quantity(receipt.get("operatorFeeConstant")),
        "receipt_total_fee_wei": receipt_total_fee,
        "total_paid_wei_balance_delta": balance_delta or None,
        "receipt_latency_ms": receipt_latency_ms,
    }


def reserve_transaction(
    budget: TransactionBudget,
    *,
    gas_limit: int,
    max_fee_per_gas: int,
    l1_fee_reserve_wei: int,
) -> int:
    estimated = gas_limit * max_fee_per_gas + l1_fee_reserve_wei
    budget.reserve(estimated)
    return estimated
