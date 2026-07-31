"""Base Sepolia safety gates and transaction-budget accounting."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping


BASE_SEPOLIA_CHAIN_ID = 84532
BASE_SEPOLIA_PUBLIC_RPC = "https://sepolia.base.org"


@dataclass(frozen=True)
class WalletCredentials:
    rpc_url: str
    public_address: str
    private_key: str = field(repr=False)


@dataclass
class TransactionBudget:
    max_transactions: int
    max_total_wei: int
    transactions: int = 0
    total_wei: int = 0

    def reserve(self, estimated_wei: int) -> None:
        if estimated_wei <= 1:
            raise ValueError("estimated transaction cost must exceed boundary/sentinel values")
        if self.transactions + 1 > self.max_transactions:
            raise RuntimeError("transaction-count cap exceeded")
        if self.total_wei + estimated_wei > self.max_total_wei:
            raise RuntimeError("test-ETH cap exceeded")
        self.transactions += 1
        self.total_wei += estimated_wei


def load_wallet_credentials(environment: Mapping[str, str] | None = None) -> WalletCredentials:
    values = os.environ if environment is None else environment
    rpc_url = values.get("BASE_SEPOLIA_RPC_URL", "")
    private_key = values.get("BASE_SEPOLIA_PRIVATE_KEY", "")
    public_address = values.get("BASE_SEPOLIA_PUBLIC_ADDRESS", "")
    if not rpc_url.startswith(("https://", "http://")):
        raise ValueError("BASE_SEPOLIA_RPC_URL must be an HTTP(S) endpoint")
    normalized_key = private_key[2:] if private_key.startswith("0x") else private_key
    if not re.fullmatch(r"[0-9a-fA-F]{64}", normalized_key):
        raise ValueError("BASE_SEPOLIA_PRIVATE_KEY must be a 32-byte hex value")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", public_address):
        raise ValueError("BASE_SEPOLIA_PUBLIC_ADDRESS must be an EVM address")
    return WalletCredentials(rpc_url, public_address, "0x" + normalized_key)


def parse_chain_id(response: bytes) -> int:
    value = json.loads(response)
    if "error" in value:
        raise RuntimeError(f"RPC returned an error: {value['error']}")
    return int(value["result"], 16)


def query_chain_id(rpc_url: str, timeout_seconds: float = 10.0) -> int:
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 84532}
    ).encode()
    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "zkbench/0.1 research-benchmark",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return parse_chain_id(response.read())


def require_base_sepolia(chain_id: int) -> None:
    if chain_id != BASE_SEPOLIA_CHAIN_ID:
        raise RuntimeError(
            f"refusing transaction: expected Base Sepolia chain {BASE_SEPOLIA_CHAIN_ID}, "
            f"received {chain_id}"
        )
