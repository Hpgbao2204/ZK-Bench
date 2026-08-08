#!/usr/bin/env python3
"""Check that application adapters agree on deterministic public fixtures."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.adapter_protocol import (  # noqa: E402
    AdapterRequest,
    AdapterResult,
    PhaseEvent,
    parse_json_lines,
)


@dataclass(frozen=True)
class FixtureObservation:
    workload: str
    scale: int
    invalid_case: str | None
    relation_digest: float
    public_inputs: int
    native_work_units: int
    verify_ok: bool
    relation_unit: str


def fixture_requests(seed: int = 20260808) -> tuple[AdapterRequest, ...]:
    common = {
        "credential": {"age_bits": 8, "hash_rounds": 5},
        "batched_state": {"update_bits": 16, "hash_rounds": 5},
        "private_swap": {
            "ablation": "full",
            "hash_rounds": 5,
            "membership_paths": 2,
            "merkle_depth": 8,
            "range_bits": 32,
            "time_bits": 16,
        },
    }
    requests: list[AdapterRequest] = []
    for workload, parameters in common.items():
        for scale in (2, 8):
            for invalid_case in (None, "wrong_public_input"):
                requests.append(
                    AdapterRequest(
                        run_id=(
                            f"cross-{workload}-{scale}-"
                            f"{invalid_case or 'valid'}"
                        ),
                        workload=workload,
                        scale=scale,
                        threads=1,
                        seed=seed,
                        mode="cold",
                        invalid_case=invalid_case,
                        parameters=dict(parameters),
                    )
                )
    return tuple(requests)


def _execute(
    command: Sequence[str], request: AdapterRequest
) -> FixtureObservation:
    process = subprocess.run(
        list(command),
        cwd=REPO,
        input=request.to_json(),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"{request.run_id}: adapter exited {process.returncode}: "
            f"{process.stderr.strip()}"
        )
    try:
        events = parse_json_lines(process.stdout.splitlines())
    except ValueError as error:
        raise RuntimeError(f"{request.run_id}: invalid adapter transcript: {error}") from error
    results = [event for event in events if isinstance(event, AdapterResult)]
    native = [
        event
        for event in events
        if isinstance(event, PhaseEvent) and event.phase == "native_execution"
    ]
    if len(results) != 1 or len(native) != 1:
        raise RuntimeError(
            f"{request.run_id}: expected one result and one native phase; "
            f"received {len(results)} and {len(native)}"
        )
    result = results[0]
    digest = native[0].metrics.get("relation_digest")
    if digest is None or digest in (0.0, 1.0):
        raise RuntimeError(f"{request.run_id}: missing nonboundary relation_digest")
    if result.native_work_units != request.scale:
        raise RuntimeError(f"{request.run_id}: native work-unit mismatch")
    expected_verify = request.invalid_case is None
    if result.verify_ok != expected_verify:
        raise RuntimeError(f"{request.run_id}: unexpected verification outcome")
    return FixtureObservation(
        workload=request.workload,
        scale=request.scale,
        invalid_case=request.invalid_case,
        relation_digest=float(digest),
        public_inputs=result.public_inputs,
        native_work_units=result.native_work_units,
        verify_ok=result.verify_ok,
        relation_unit=result.relation_unit,
    )


def compare_fixture(
    request: AdapterRequest,
    left: FixtureObservation,
    right: FixtureObservation,
) -> None:
    if left.relation_digest != right.relation_digest:
        raise RuntimeError(
            f"{request.run_id}: relation digest mismatch "
            f"{left.relation_digest} != {right.relation_digest}"
        )
    for field in ("public_inputs", "native_work_units", "verify_ok"):
        if getattr(left, field) != getattr(right, field):
            raise RuntimeError(
                f"{request.run_id}: {field} mismatch "
                f"{getattr(left, field)!r} != {getattr(right, field)!r}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groth-command", nargs="+", required=True)
    parser.add_argument("--plonk-command", nargs="+", required=True)
    parser.add_argument("--seed", type=int, default=20260808)
    args = parser.parse_args()

    checked = 0
    for request in fixture_requests(args.seed):
        groth = _execute(args.groth_command, request)
        plonk = _execute(args.plonk_command, request)
        compare_fixture(request, groth, plonk)
        print(
            json.dumps(
                {
                    "run_id": request.run_id,
                    "workload": request.workload,
                    "scale": request.scale,
                    "invalid_case": request.invalid_case,
                    "relation_digest": groth.relation_digest,
                    "public_inputs": groth.public_inputs,
                    "groth_relation_unit": groth.relation_unit,
                    "plonk_relation_unit": plonk.relation_unit,
                },
                sort_keys=True,
            )
        )
        checked += 1
    print(f"cross-adapter fixture check: PASS ({checked} matched cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
