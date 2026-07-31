"""Small process fixture for adapter orchestration tests."""

from __future__ import annotations

import json
import sys
import time


request = json.load(sys.stdin)
allocation = bytearray(2 * 1024 * 1024)
allocation[1024] = 7
time.sleep(0.05)

adapter = "fake-adapter"
for phase in (
    "native_execution",
    "setup_or_preprocess",
    "witness",
    "prove_total",
    "serialize",
    "verify_core",
    "verify_total",
):
    print(
        json.dumps(
            {
                "schema_version": "1.0",
                "event_type": "phase",
                "run_id": request["run_id"],
                "adapter": adapter,
                "phase": phase,
                "supported": True,
                "status": "ok",
                "thread_count": request["threads"],
                "elapsed_ns": 25,
                "metrics": {},
                "unavailable_reason": None,
                "boundary_reason": None,
            }
        )
    )
print(
    json.dumps(
        {
            "schema_version": "1.0",
            "event_type": "phase",
            "run_id": request["run_id"],
            "adapter": adapter,
            "phase": "key_load",
            "supported": False,
            "status": "unsupported",
            "thread_count": request["threads"],
            "elapsed_ns": None,
            "metrics": {},
            "unavailable_reason": "fixture has no proving key",
            "boundary_reason": None,
        }
    )
)
invalid = request.get("invalid_case") is not None
print(
    json.dumps(
        {
            "schema_version": "1.0",
            "event_type": "result",
            "run_id": request["run_id"],
            "adapter": adapter,
            "verify_ok": not invalid,
            "proof_bytes": 128,
            "native_work_units": request["scale"],
            "public_inputs": 2,
            "constraints": request["scale"],
            "invalid_case": request.get("invalid_case"),
            "error_type": "cryptographic_rejection" if invalid else None,
        }
    )
)
