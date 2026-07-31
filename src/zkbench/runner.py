"""Dependency-free benchmark runner and evidence bundle writer."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fixtures import batched_state_fixture, credential_fixture, private_swap_fixture
from .workloads import evaluate


RAW_FIELDS = [
    "run_id", "timestamp", "claim_id", "experiment_id", "workload", "variant",
    "adapter_commit", "config_hash", "seed", "repetition", "order_index",
    "parameter_set", "parameters_json", "input_scale",
    "native_work_units", "threads", "cores_visible", "phase", "latency_ms", "cpu_time_ms",
    "peak_rss_mb", "proof_bytes", "verify_ok", "exit_code", "error_type",
    "page_faults", "process_read_bytes", "process_write_bytes", "peak_swap_mb",
    "ram_per_application_unit", "process_counter_provider", "measurement_scope",
    "energy_joules", "energy_source", "cold_start", "invalid_proof_kind",
    "rejection_latency_ms", "metric_unavailable_reason", "boundary_reason",
    "evidence_class", "result_scope", "run_role", "recorded", "phase_supported",
    "phase_status", "phase_metrics_json", "constraints", "public_inputs",
    "native_relation_size", "relation_unit",
    "counter_sampling_interval_ms", "counter_samples",
]

FIXTURES = {
    "credential": credential_fixture,
    "batched_state": batched_state_fixture,
    "private_swap": private_swap_fixture,
}


def canonical_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def run_reference(config: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    seed = int(config["seed"])
    rng = random.Random(seed)
    config_hash = canonical_hash(config)
    tasks = [
        (workload, repetition)
        for workload in config["workloads"]
        for repetition in range(int(config["repetitions"]))
    ]
    rng.shuffle(tasks)
    rows: list[dict[str, Any]] = []
    for workload in config["workloads"]:
        fixture = FIXTURES[workload]()
        for _ in range(int(config["warmups"])):
            evaluate(workload, fixture)
    for order_index, (workload, repetition) in enumerate(tasks):
        fixture = FIXTURES[workload]()
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        result = evaluate(workload, fixture)
        cpu_ms = (time.process_time_ns() - cpu_start) / 1_000_000
        latency_ms = (time.perf_counter_ns() - wall_start) / 1_000_000
        rows.append(
            {
                "run_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "claim_id": config["claim_id"],
                "experiment_id": config["experiment_id"],
                "workload": workload,
                "variant": "reference-predicate",
                "adapter_commit": "not-applicable",
                "config_hash": config_hash,
                "seed": seed,
                "repetition": repetition,
                "order_index": order_index,
                "input_scale": result.native_work_units,
                "native_work_units": result.native_work_units,
                "threads": 1,
                "cores_visible": os.cpu_count() or 1,
                "phase": "semantic_validation",
                "latency_ms": f"{latency_ms:.6f}",
                "cpu_time_ms": f"{cpu_ms:.6f}",
                "peak_rss_mb": "",
                "proof_bytes": "",
                "verify_ok": str(result.valid).lower(),
                "exit_code": 0 if result.valid else 1,
                "error_type": "" if result.valid else "reference_predicate_failed",
                "page_faults": "",
                "process_read_bytes": "",
                "process_write_bytes": "",
                "peak_swap_mb": "",
                "ram_per_application_unit": "",
                "process_counter_provider": "",
                "measurement_scope": "phase",
                "energy_joules": "",
                "energy_source": "",
                "cold_start": "false",
                "invalid_proof_kind": "",
                "rejection_latency_ms": "",
                "metric_unavailable_reason": "reference adapter exposes no process/energy counters",
                "boundary_reason": "",
                "evidence_class": "measured",
                "result_scope": "validation-only-not-a-proof-benchmark",
                "run_role": "measurement",
                "recorded": "true",
                "phase_supported": "true",
                "phase_status": "ok",
                "phase_metrics_json": "{}",
                "constraints": "",
                "public_inputs": "",
                "counter_sampling_interval_ms": "",
                "counter_samples": "",
            }
        )
    with (output / "raw_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    write_summary(rows, output / "summary.csv")
    (output / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    environment = {
        "platform": platform.platform(),
        "python": sys.version,
        "logical_cpus_visible": os.cpu_count(),
        "runner": "reference-predicate",
        "config_hash": config_hash,
    }
    (output / "environment.json").write_text(
        json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_summary(rows: list[dict[str, Any]], path: Path) -> None:
    groups: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        groups.setdefault((row["workload"], row["variant"]), []).append(float(row["latency_ms"]))
    fields = [
        "claim_id", "experiment_id", "workload", "variant", "n", "mean_latency_ms",
        "stdev_latency_ms", "p50_latency_ms", "p95_latency_ms", "min_latency_ms",
        "max_latency_ms", "stdev_unavailable_reason", "excluded_boundary_metrics",
        "result_scope",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for (workload, variant), values in sorted(groups.items()):
            excluded: list[str] = []

            def quantitative(name: str, value: float) -> str:
                formatted = f"{value:.6f}"
                if float(formatted) in (0.0, 1.0):
                    excluded.append(name)
                    return ""
                return formatted

            stdev = statistics.stdev(values) if len(values) > 1 else None
            writer.writerow(
                {
                    "claim_id": rows[0]["claim_id"],
                    "experiment_id": rows[0]["experiment_id"],
                    "workload": workload,
                    "variant": variant,
                    "n": len(values),
                    "mean_latency_ms": quantitative(
                        "mean_latency_ms", statistics.mean(values)
                    ),
                    "stdev_latency_ms": quantitative("stdev_latency_ms", stdev)
                    if stdev is not None
                    else "",
                    "p50_latency_ms": quantitative(
                        "p50_latency_ms", percentile(values, 0.50)
                    ),
                    "p95_latency_ms": quantitative(
                        "p95_latency_ms", percentile(values, 0.95)
                    ),
                    "min_latency_ms": quantitative("min_latency_ms", min(values)),
                    "max_latency_ms": quantitative("max_latency_ms", max(values)),
                    "stdev_unavailable_reason": (
                        "requires at least two observations"
                        if stdev is None
                        else (
                            "exact boundary excluded"
                            if "stdev_latency_ms" in excluded
                            else ""
                        )
                    ),
                    "excluded_boundary_metrics": ";".join(excluded),
                    "result_scope": rows[0]["result_scope"],
                }
            )
