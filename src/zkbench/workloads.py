"""Deterministic reference predicates.

These predicates validate fixture semantics. They do not generate proofs and
their timings must never be presented as cryptographic benchmark results.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Evaluation:
    valid: bool
    checks: dict[str, bool]
    native_work_units: int


def credential(case: dict[str, Any]) -> Evaluation:
    private = case["private"]
    public = case["public"]
    committed = digest(
        {
            "subject_id": private["subject_id"],
            "birth_year": private["birth_year"],
            "nonce": private["nonce"],
        }
    )
    checks = {
        "commitment": committed == public["credential_commitment"],
        "age_range": 1900 <= private["birth_year"] <= public["current_year"],
        "age_threshold": public["current_year"] - private["birth_year"] >= public["min_age"],
        "issuer_authorized": bool(private["issuer_authorized"]),
    }
    return Evaluation(all(checks.values()), checks, native_work_units=4)


def batched_state(case: dict[str, Any]) -> Evaluation:
    public = case["public"]
    blocks = case["private"]["blocks"]
    state = int(public["initial_state"])
    previous_hash = public["genesis_hash"]
    linkage = True
    ranges = True
    for block in blocks:
        linkage &= block["parent_hash"] == previous_hash
        ranges &= 0 <= int(block["delta"]) < int(public["state_modulus"])
        state = (state + int(block["delta"])) % int(public["state_modulus"])
        previous_hash = digest({"parent_hash": block["parent_hash"], "delta": block["delta"]})
    checks = {
        "batch_size": len(blocks) == int(public["batch_size"]),
        "linkage": linkage,
        "delta_ranges": ranges,
        "final_state": state == int(public["claimed_final_state"]),
        "batch_commitment": digest(blocks) == public["batch_commitment"],
    }
    return Evaluation(all(checks.values()), checks, native_work_units=max(1, 4 * len(blocks)))


def private_swap(case: dict[str, Any]) -> Evaluation:
    private = case["private"]
    public = case["public"]
    amount_a = int(private["amount_a"])
    amount_b = int(private["amount_b"])
    bits = int(public["range_bits"])
    lhs = amount_b * int(public["price_den"])
    reference = amount_a * int(public["price_num"])
    deviation = abs(lhs - reference) * 10_000
    checks = {
        "hashlock": hashlib.sha256(private["secret"].encode()).hexdigest()
        == public["hashlock"],
        "amount_a_range": 0 < amount_a < 2**bits,
        "amount_b_range": 0 < amount_b < 2**bits,
        "price_slippage": deviation <= reference * int(public["slippage_bps"]),
        "redeem_window": int(public["current_time"]) < int(public["expiry"]),
        "membership": bool(private["membership_valid"]),
        "authorization": bool(private["authorization_valid"]),
        "domain_binding": public["domain_tag"]
        == digest(
            {
                "ledger_a": public["ledger_a"],
                "ledger_b": public["ledger_b"],
                "session_nonce": private["session_nonce"],
            }
        ),
    }
    return Evaluation(all(checks.values()), checks, native_work_units=8 + 2 * bits)


EVALUATORS = {
    "credential": credential,
    "batched_state": batched_state,
    "private_swap": private_swap,
}


def evaluate(workload: str, case: dict[str, Any]) -> Evaluation:
    try:
        return EVALUATORS[workload](case)
    except KeyError as exc:
        raise ValueError(f"unknown workload: {workload}") from exc
