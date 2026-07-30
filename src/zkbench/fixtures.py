"""Deterministic positive and negative workload fixtures."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from .workloads import digest


def credential_fixture() -> dict[str, Any]:
    private = {
        "subject_id": "subject-001",
        "birth_year": 1995,
        "nonce": "fixture-nonce",
        "issuer_authorized": True,
    }
    public = {
        "current_year": 2026,
        "min_age": 18,
        "credential_commitment": digest(
            {
                "subject_id": private["subject_id"],
                "birth_year": private["birth_year"],
                "nonce": private["nonce"],
            }
        ),
    }
    return {"public": public, "private": private}


def batched_state_fixture(batch_size: int = 8) -> dict[str, Any]:
    modulus = 2**61 - 1
    state = 17
    previous_hash = digest({"genesis": "zkbench"})
    blocks = []
    for index in range(batch_size):
        block = {"parent_hash": previous_hash, "delta": index + 3}
        blocks.append(block)
        state = (state + block["delta"]) % modulus
        previous_hash = digest(block)
    return {
        "public": {
            "initial_state": 17,
            "claimed_final_state": state,
            "state_modulus": modulus,
            "genesis_hash": digest({"genesis": "zkbench"}),
            "batch_size": batch_size,
            "batch_commitment": digest(blocks),
        },
        "private": {"blocks": blocks},
    }


def private_swap_fixture() -> dict[str, Any]:
    secret = "correct horse battery staple"
    private = {
        "secret": secret,
        "amount_a": 1_000,
        "amount_b": 2_000,
        "membership_valid": True,
        "authorization_valid": True,
        "session_nonce": "session-007",
    }
    public = {
        "hashlock": hashlib.sha256(secret.encode()).hexdigest(),
        "range_bits": 32,
        "price_num": 2,
        "price_den": 1,
        "slippage_bps": 50,
        "current_time": 1_000,
        "expiry": 2_000,
        "ledger_a": "ledger-a",
        "ledger_b": "ledger-b",
        "domain_tag": digest(
            {
                "ledger_a": "ledger-a",
                "ledger_b": "ledger-b",
                "session_nonce": private["session_nonce"],
            }
        ),
    }
    return {"public": public, "private": private}


def invalid_variants(workload: str, valid: dict[str, Any]) -> dict[str, dict[str, Any]]:
    variants: dict[str, dict[str, Any]] = {}

    def changed(name: str) -> dict[str, Any]:
        variants[name] = copy.deepcopy(valid)
        return variants[name]

    if workload == "credential":
        changed("under_age")["private"]["birth_year"] = 2015
        changed("bad_commitment")["public"]["credential_commitment"] = "00" * 32
        changed("unauthorized_issuer")["private"]["issuer_authorized"] = False
    elif workload == "batched_state":
        changed("bad_final_state")["public"]["claimed_final_state"] += 1
        changed("broken_link")["private"]["blocks"][1]["parent_hash"] = "00" * 32
        changed("bad_batch_commitment")["public"]["batch_commitment"] = "00" * 32
    elif workload == "private_swap":
        changed("bad_secret")["private"]["secret"] = "wrong"
        changed("expired")["public"]["current_time"] = 3_000
        changed("bad_membership")["private"]["membership_valid"] = False
        changed("excess_slippage")["private"]["amount_b"] = 3_000
    else:
        raise ValueError(f"unknown workload: {workload}")
    return variants
