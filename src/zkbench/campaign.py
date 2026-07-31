"""Randomized adapter campaigns with auditable raw and derived CSV evidence."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .adapter_protocol import AdapterRequest, PhaseEvent
from .adapter_runner import AdapterExecution, execute_adapter
from .analysis import (
    bootstrap_power_law_exponent_interval,
    bootstrap_speedup_interval,
    fit_power_law,
    parallel_profile,
)
from .runner import RAW_FIELDS, canonical_hash, percentile


SUMMARY_FIELDS = [
    "claim_id",
    "experiment_id",
    "adapter_commit",
    "config_hash",
    "workload",
    "variant",
    "parameter_set",
    "parameters_json",
    "phase",
    "input_scale",
    "threads",
    "invalid_proof_kind",
    "supported",
    "n",
    "mean_latency_ms",
    "stdev_latency_ms",
    "p50_latency_ms",
    "p95_latency_ms",
    "min_latency_ms",
    "max_latency_ms",
    "median_cpu_time_ms",
    "median_peak_rss_mb",
    "median_page_faults",
    "proof_bytes",
    "expected_outcomes",
    "stdev_unavailable_reason",
    "excluded_boundary_metrics",
    "metric_unavailable_reason",
    "speedup_vs_one_thread",
    "speedup_ci_low",
    "speedup_ci_high",
    "parallel_efficiency",
    "saturation_threads",
    "scaling_coefficient_a",
    "scaling_exponent_b",
    "scaling_exponent_ci_low",
    "scaling_exponent_ci_high",
    "scaling_r_squared",
    "evidence_class",
    "result_scope",
    "run_role",
    "recorded",
]


def _integer_list(config: dict[str, Any], name: str, *, minimum: int) -> list[int]:
    values = config.get(name)
    if not isinstance(values, list) or not values:
        raise ValueError(f"{name} must be a nonempty list")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < minimum
        for value in values
    ):
        raise ValueError(f"{name} values must be integers >= {minimum}")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def _parameter_sets(config: dict[str, Any]) -> list[dict[str, Any]]:
    configured = config.get("parameter_sets")
    if configured is None:
        return [{"id": "default", "parameters": {}}]
    if not isinstance(configured, list) or not configured:
        raise ValueError("parameter_sets must be a nonempty list")
    normalized: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for item in configured:
        if not isinstance(item, dict):
            raise ValueError("each parameter set must be an object")
        identifier = item.get("id")
        parameters = item.get("parameters")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("each parameter set requires a nonempty id")
        if identifier in identifiers:
            raise ValueError("parameter set ids must be unique")
        if not isinstance(parameters, dict):
            raise ValueError(f"parameter set {identifier} requires a parameters object")
        AdapterRequest(
            run_id="config-validation",
            workload="parameter-validation",
            scale=2,
            threads=1,
            seed=2,
            parameters=parameters,
        ).validate()
        identifiers.add(identifier)
        normalized.append({"id": identifier, "parameters": dict(parameters)})
    return normalized


def validate_campaign_config(config: dict[str, Any]) -> None:
    required_text = (
        "claim_id",
        "experiment_id",
        "adapter",
        "workload",
        "variant",
        "evidence_class",
        "result_scope",
    )
    for name in required_text:
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError(f"{name} must be a nonempty string")
    if config["evidence_class"] != "measured":
        raise ValueError("adapter campaigns emit measured evidence only")
    if config.get("schema_version") != "1.0":
        raise ValueError("unsupported campaign schema_version")
    if config.get("mode") != "cold":
        raise ValueError("one-process-per-request campaigns must use cold mode")
    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
    ):
        raise ValueError("command must be a nonempty string list")
    _integer_list(config, "scales", minimum=2)
    _integer_list(config, "threads", minimum=1)
    parameter_sets = _parameter_sets(config)
    parameter_set_ids = {item["id"] for item in parameter_sets}
    for name, minimum in (
        ("repetitions", 3),
        ("os_cache_primer_runs", 1),
        ("seed", 2),
    ):
        value = config.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    for name in ("timeout_seconds", "sampling_interval_ms"):
        value = config.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 1:
            raise ValueError(f"{name} must exceed excluded boundary values")
    energy = config.get("energy")
    if (
        not isinstance(energy, dict)
        or energy.get("provider") != "unavailable"
        or not isinstance(energy.get("unavailable_reason"), str)
        or not energy["unavailable_reason"].strip()
    ):
        raise ValueError(
            "current runner requires an explicit unavailable energy provider and reason"
        )
    invalid_cases = config.get("invalid_cases", [])
    if not isinstance(invalid_cases, list):
        raise ValueError("invalid_cases must be a list")
    for case in invalid_cases:
        if not isinstance(case, dict) or not isinstance(case.get("kind"), str):
            raise ValueError("each invalid case requires a kind")
        _integer_list(case, "scales", minimum=2)
        _integer_list(case, "threads", minimum=1)
        repetitions = case.get("repetitions")
        if (
            isinstance(repetitions, bool)
            or not isinstance(repetitions, int)
            or repetitions < 3
        ):
            raise ValueError("invalid-case repetitions must be >= 3")
        selected = case.get("parameter_set_ids")
        if selected is not None:
            if (
                not isinstance(selected, list)
                or not selected
                or any(not isinstance(item, str) or not item for item in selected)
            ):
                raise ValueError(
                    "invalid-case parameter_set_ids must be a nonempty string list"
                )
            if len(set(selected)) != len(selected):
                raise ValueError(
                    "invalid-case parameter_set_ids must not contain duplicates"
                )
            unknown = set(selected) - parameter_set_ids
            if unknown:
                raise ValueError(
                    f"invalid-case parameter_set_ids are unknown: {sorted(unknown)}"
                )


def _git_commit(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8"
    ).strip()


def _tracked_worktree_dirty(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return bool(result.stdout.strip())


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _cpu_model() -> str | None:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.is_file():
        return platform.processor() or None
    for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"model name", "Hardware"}:
            return value.strip()
    return None


def _physical_cores_visible() -> int | None:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.is_file():
        return None
    packages: set[tuple[str, str]] = set()
    current: dict[str, str] = {}
    for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines() + [""]:
        key, separator, value = line.partition(":")
        if separator:
            current[key.strip()] = value.strip()
            continue
        if "physical id" in current and "core id" in current:
            packages.add((current["physical id"], current["core id"]))
        current = {}
    return len(packages) or None


def _environment(
    repo: Path,
    config_hash: str,
    adapter_commit: str,
    command: list[str],
) -> dict[str, Any]:
    affinity = None
    if hasattr(os, "sched_getaffinity"):
        affinity = sorted(os.sched_getaffinity(0))
    return {
        "adapter_commit": adapter_commit,
        "adapter_binary_sha256": _file_sha256(Path(command[0])),
        "adapter_command": command,
        "cargo_lock_sha256": _file_sha256(repo / "Cargo.lock"),
        "config_hash": config_hash,
        "cpu_affinity_logical_ids": affinity,
        "cpu_model": _cpu_model(),
        "logical_cpus_visible": os.cpu_count(),
        "physical_cores_visible": _physical_cores_visible(),
        "platform": platform.platform(),
        "power_control": "not controlled by zkbench",
        "python": sys.version,
        "runner": "adapter-process-campaign",
        "thermal_control": "not controlled by zkbench",
    }


def _tasks(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    primers: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    base_seed = int(config["seed"])
    seed_rng = random.Random(base_seed)
    parameter_sets = _parameter_sets(config)
    parameter_sets_by_id = {item["id"]: item for item in parameter_sets}
    for parameter_set in parameter_sets:
        for scale in config["scales"]:
            for threads in config["threads"]:
                for repetition in range(config["os_cache_primer_runs"]):
                    primers.append(
                        {
                            "scale": scale,
                            "threads": threads,
                            "repetition": repetition,
                            "invalid_case": None,
                            "seed": seed_rng.randrange(2, 2**63),
                            "run_role": "os_cache_primer",
                            "recorded": False,
                            "parameter_set": parameter_set["id"],
                            "parameters": parameter_set["parameters"],
                        }
                    )
                for repetition in range(config["repetitions"]):
                    measurements.append(
                        {
                            "scale": scale,
                            "threads": threads,
                            "repetition": repetition,
                            "invalid_case": None,
                            "seed": seed_rng.randrange(2, 2**63),
                            "run_role": "measurement",
                            "recorded": True,
                            "parameter_set": parameter_set["id"],
                            "parameters": parameter_set["parameters"],
                        }
                    )
    for invalid in config.get("invalid_cases", []):
        selected_ids = invalid.get(
            "parameter_set_ids", list(parameter_sets_by_id)
        )
        for parameter_set_id in selected_ids:
            parameter_set = parameter_sets_by_id[parameter_set_id]
            for scale in invalid["scales"]:
                for threads in invalid["threads"]:
                    for repetition in range(invalid["repetitions"]):
                        measurements.append(
                            {
                                "scale": scale,
                                "threads": threads,
                                "repetition": repetition,
                                "invalid_case": invalid["kind"],
                                "seed": seed_rng.randrange(2, 2**63),
                                "run_role": "measurement",
                                "recorded": True,
                                "parameter_set": parameter_set["id"],
                                "parameters": parameter_set["parameters"],
                            }
                        )
    random.Random(base_seed ^ 0xC0FFEE).shuffle(measurements)
    return primers, measurements


def _quantitative_raw(value: float | None) -> tuple[str, str]:
    if value is None:
        return "", ""
    formatted = f"{value:.6f}"
    if float(formatted) in (0.0, 1.0):
        return formatted, "rounded raw value hits excluded numeric boundary"
    return formatted, ""


def _base_row(
    config: dict[str, Any],
    config_hash: str,
    adapter_commit: str,
    request: AdapterRequest,
    task: dict[str, Any],
    order_index: int,
) -> dict[str, Any]:
    row = {field: "" for field in RAW_FIELDS}
    row.update(
        {
            "run_id": request.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "claim_id": config["claim_id"],
            "experiment_id": config["experiment_id"],
            "workload": config["workload"],
            "variant": config["variant"],
            "adapter_commit": adapter_commit,
            "config_hash": config_hash,
            "seed": request.seed,
            "repetition": task["repetition"],
            "order_index": order_index,
            "parameter_set": task["parameter_set"],
            "parameters_json": json.dumps(
                request.parameters, sort_keys=True, separators=(",", ":")
            ),
            "input_scale": request.scale,
            "native_work_units": request.scale,
            "threads": request.threads,
            "cores_visible": os.cpu_count() or "",
            "cold_start": "true",
            "invalid_proof_kind": request.invalid_case or "",
            "evidence_class": config["evidence_class"],
            "result_scope": config["result_scope"],
            "run_role": task["run_role"],
            "recorded": str(task["recorded"]).lower(),
        }
    )
    return row


def execution_rows(
    execution: AdapterExecution,
    config: dict[str, Any],
    config_hash: str,
    adapter_commit: str,
    task: dict[str, Any],
    order_index: int,
) -> list[dict[str, Any]]:
    request = execution.request
    result = execution.result
    rows: list[dict[str, Any]] = []
    for event in execution.phases:
        row = _base_row(
            config, config_hash, adapter_commit, request, task, order_index
        )
        latency_ms, boundary_reason = _quantitative_raw(
            event.elapsed_ns / 1_000_000 if event.elapsed_ns is not None else None
        )
        row.update(
            {
                "phase": event.phase,
                "latency_ms": latency_ms,
                "boundary_reason": boundary_reason or event.boundary_reason or "",
                "metric_unavailable_reason": event.unavailable_reason or "",
                "measurement_scope": "phase",
                "phase_supported": str(event.supported).lower(),
                "phase_status": event.status,
                "phase_metrics_json": json.dumps(
                    event.metrics, sort_keys=True, separators=(",", ":")
                ),
                "rejection_latency_ms": (
                    latency_ms
                    if request.invalid_case and event.phase == "verify_core"
                    else ""
                ),
            }
        )
        rows.append(row)

    row = _base_row(config, config_hash, adapter_commit, request, task, order_index)
    latency_ms, boundary_reason = _quantitative_raw(execution.wall_time_ns / 1_000_000)
    cpu_time_ms, cpu_boundary = _quantitative_raw(
        execution.process.cpu_time_ns / 1_000_000
        if execution.process.cpu_time_ns is not None
        else None
    )
    peak_rss_mb, rss_boundary = _quantitative_raw(
        execution.process.peak_rss_bytes / (1024 * 1024)
        if execution.process.peak_rss_bytes is not None
        else None
    )
    peak_swap_mb, swap_boundary = _quantitative_raw(
        execution.process.peak_swap_bytes / (1024 * 1024)
        if execution.process.peak_swap_bytes is not None
        else None
    )
    boundary_reasons = [
        reason
        for reason in (boundary_reason, cpu_boundary, rss_boundary, swap_boundary)
        if reason
    ]
    unavailable_reasons = [
        reason
        for reason in (
            execution.process.unavailable_reason,
            config.get("energy", {}).get("unavailable_reason"),
        )
        if reason
    ]
    row.update(
        {
            "phase": "adapter_process_wall",
            "latency_ms": latency_ms,
            "cpu_time_ms": cpu_time_ms,
            "peak_rss_mb": peak_rss_mb,
            "proof_bytes": result.proof_bytes if result else "",
            "verify_ok": (
                str(result.verify_ok).lower() if result is not None else ""
            ),
            "exit_code": execution.exit_code,
            "error_type": (
                result.error_type
                if result is not None and result.error_type
                else execution.protocol_error or ""
            ),
            "page_faults": execution.process.page_faults or "",
            "process_read_bytes": execution.process.process_read_bytes or "",
            "process_write_bytes": execution.process.process_write_bytes or "",
            "peak_swap_mb": peak_swap_mb,
            "process_counter_provider": execution.process.provider,
            "measurement_scope": "process",
            "metric_unavailable_reason": "; ".join(unavailable_reasons),
            "boundary_reason": "; ".join(boundary_reasons),
            "phase_supported": "true",
            "phase_status": "ok" if execution.succeeded else "error",
            "phase_metrics_json": "{}",
            "constraints": result.constraints if result else "",
            "public_inputs": result.public_inputs if result else "",
            "counter_sampling_interval_ms": execution.process.sampling_interval_ms,
            "counter_samples": execution.process.samples,
        }
    )
    rows.append(row)
    return rows


def _nonboundary_values(rows: Iterable[dict[str, str]], field: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        raw = row.get(field, "")
        if raw == "":
            continue
        value = float(raw)
        if value not in (0.0, 1.0):
            values.append(value)
    return values


def write_campaign_summary(rows: list[dict[str, str]], path: Path) -> None:
    recorded = [row for row in rows if row["recorded"] == "true"]
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, str]]] = {}
    for row in recorded:
        key = (
            row["parameter_set"],
            row["parameters_json"],
            row["phase"],
            row["input_scale"],
            row["threads"],
            row["invalid_proof_kind"],
        )
        groups.setdefault(key, []).append(row)
    prove_samples: dict[tuple[str, str, int, int], list[float]] = {}
    for (
        parameter_set,
        parameters_json,
        phase,
        scale,
        threads,
        invalid_kind,
    ), group in groups.items():
        if phase != "prove_total" or invalid_kind:
            continue
        values = _nonboundary_values(group, "latency_ms")
        if values:
            prove_samples[
                (parameter_set, parameters_json, int(scale), int(threads))
            ] = values
    parameter_keys = sorted(
        {
            (parameter_set, parameters_json)
            for parameter_set, parameters_json, _, _ in prove_samples
        }
    )
    profiles = {}
    scaling = {}
    scaling_intervals = {}
    seed_base = int(recorded[0]["config_hash"][:16], 16) if recorded else 2
    for parameter_key in parameter_keys:
        parameter_set, parameters_json = parameter_key
        prove_scales = sorted(
            {
                scale
                for pset, pjson, scale, _ in prove_samples
                if (pset, pjson) == parameter_key
            }
        )
        prove_threads = sorted(
            {
                threads
                for pset, pjson, _, threads in prove_samples
                if (pset, pjson) == parameter_key
            }
        )
        for scale in prove_scales:
            samples = {
                threads: prove_samples[
                    (parameter_set, parameters_json, scale, threads)
                ]
                for threads in prove_threads
                if (parameter_set, parameters_json, scale, threads)
                in prove_samples
            }
            if 1 in samples:
                profiles[(parameter_key, scale)] = parallel_profile(samples)
        for threads in prove_threads:
            samples_by_scale = {
                scale: prove_samples[
                    (parameter_set, parameters_json, scale, threads)
                ]
                for scale in prove_scales
                if (parameter_set, parameters_json, scale, threads)
                in prove_samples
            }
            if len(samples_by_scale) >= 3:
                scaling[(parameter_key, threads)] = fit_power_law(
                    (scale, statistics.median(values))
                    for scale, values in samples_by_scale.items()
                )
                scaling_intervals[
                    (parameter_key, threads)
                ] = bootstrap_power_law_exponent_interval(
                    samples_by_scale,
                    seed=seed_base
                    ^ threads
                    ^ int(
                        canonical_hash({"parameter_set": parameter_set})[:16],
                        16,
                    ),
                )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for key, group in sorted(groups.items()):
            (
                parameter_set,
                parameters_json,
                phase,
                scale,
                threads,
                invalid_kind,
            ) = key
            latencies = _nonboundary_values(group, "latency_ms")
            excluded: list[str] = []

            def metric(name: str, value: float | None) -> str:
                if value is None:
                    return ""
                formatted = f"{value:.6f}"
                if float(formatted) in (0.0, 1.0):
                    excluded.append(name)
                    return ""
                return formatted

            supported = all(row["phase_supported"] == "true" for row in group)
            cpu_values = _nonboundary_values(group, "cpu_time_ms")
            rss_values = _nonboundary_values(group, "peak_rss_mb")
            fault_values = _nonboundary_values(group, "page_faults")
            proof_sizes = {
                row["proof_bytes"] for row in group if row["proof_bytes"] != ""
            }
            outcomes = [row["verify_ok"] for row in group if row["verify_ok"]]
            expected = "not-applicable"
            if outcomes:
                wanted = "false" if invalid_kind else "true"
                expected = (
                    "all-expected"
                    if all(value == wanted for value in outcomes)
                    else "unexpected-outcome-present"
                )
            stdev = statistics.stdev(latencies) if len(latencies) > 1 else None
            speedup = None
            efficiency = None
            speedup_ci: tuple[float, float] | None = None
            saturation = None
            fit = None
            exponent_ci: tuple[float, float] | None = None
            derived_unavailable_reason = None
            if phase == "prove_total" and not invalid_kind:
                scale_number = int(scale)
                thread_number = int(threads)
                parameter_key = (parameter_set, parameters_json)
                profile = profiles.get((parameter_key, scale_number))
                if profile:
                    saturation = profile.saturation_threads
                    point = next(
                        (
                            item
                            for item in profile.points
                            if item.threads == thread_number
                        ),
                        None,
                    )
                    if point:
                        speedup = point.speedup
                        efficiency = point.efficiency
                        try:
                            speedup_ci = bootstrap_speedup_interval(
                                prove_samples[
                                    (
                                        parameter_set,
                                        parameters_json,
                                        scale_number,
                                        1,
                                    )
                                ],
                                prove_samples[
                                    (
                                        parameter_set,
                                        parameters_json,
                                        scale_number,
                                        thread_number,
                                    )
                                ],
                                seed=seed_base ^ scale_number ^ thread_number,
                            )
                        except ValueError as error:
                            derived_unavailable_reason = str(error)
                fit = scaling.get((parameter_key, thread_number))
                exponent_ci = scaling_intervals.get(
                    (parameter_key, thread_number)
                )
            reasons = sorted(
                {
                    row["metric_unavailable_reason"]
                    for row in group
                    if row["metric_unavailable_reason"]
                }
            )
            if derived_unavailable_reason:
                reasons.append(derived_unavailable_reason)
            writer.writerow(
                {
                    "claim_id": group[0]["claim_id"],
                    "experiment_id": group[0]["experiment_id"],
                    "adapter_commit": group[0]["adapter_commit"],
                    "config_hash": group[0]["config_hash"],
                    "workload": group[0]["workload"],
                    "variant": group[0]["variant"],
                    "parameter_set": parameter_set,
                    "parameters_json": parameters_json,
                    "phase": phase,
                    "input_scale": scale,
                    "threads": threads,
                    "invalid_proof_kind": invalid_kind,
                    "supported": str(supported).lower(),
                    "n": len(latencies) if latencies else "",
                    "mean_latency_ms": metric(
                        "mean_latency_ms",
                        statistics.mean(latencies) if latencies else None,
                    ),
                    "stdev_latency_ms": metric("stdev_latency_ms", stdev),
                    "p50_latency_ms": metric(
                        "p50_latency_ms",
                        statistics.median(latencies) if latencies else None,
                    ),
                    "p95_latency_ms": metric(
                        "p95_latency_ms",
                        percentile(latencies, 0.95) if latencies else None,
                    ),
                    "min_latency_ms": metric(
                        "min_latency_ms", min(latencies) if latencies else None
                    ),
                    "max_latency_ms": metric(
                        "max_latency_ms", max(latencies) if latencies else None
                    ),
                    "median_cpu_time_ms": metric(
                        "median_cpu_time_ms",
                        statistics.median(cpu_values) if cpu_values else None,
                    ),
                    "median_peak_rss_mb": metric(
                        "median_peak_rss_mb",
                        statistics.median(rss_values) if rss_values else None,
                    ),
                    "median_page_faults": metric(
                        "median_page_faults",
                        statistics.median(fault_values) if fault_values else None,
                    ),
                    "proof_bytes": (
                        next(iter(proof_sizes)) if len(proof_sizes) == 1 else ""
                    ),
                    "expected_outcomes": expected,
                    "stdev_unavailable_reason": (
                        "requires at least two nonboundary observations"
                        if stdev is None and supported
                        else (
                            "exact boundary excluded"
                            if "stdev_latency_ms" in excluded
                            else ""
                        )
                    ),
                    "excluded_boundary_metrics": ";".join(excluded),
                    "metric_unavailable_reason": "; ".join(reasons),
                    "speedup_vs_one_thread": metric(
                        "speedup_vs_one_thread", speedup
                    ),
                    "speedup_ci_low": metric(
                        "speedup_ci_low",
                        speedup_ci[0] if speedup_ci else None,
                    ),
                    "speedup_ci_high": metric(
                        "speedup_ci_high",
                        speedup_ci[1] if speedup_ci else None,
                    ),
                    "parallel_efficiency": metric(
                        "parallel_efficiency", efficiency
                    ),
                    "saturation_threads": saturation or "",
                    "scaling_coefficient_a": metric(
                        "scaling_coefficient_a",
                        fit.coefficient_a if fit else None,
                    ),
                    "scaling_exponent_b": metric(
                        "scaling_exponent_b", fit.exponent_b if fit else None
                    ),
                    "scaling_exponent_ci_low": metric(
                        "scaling_exponent_ci_low",
                        exponent_ci[0] if exponent_ci else None,
                    ),
                    "scaling_exponent_ci_high": metric(
                        "scaling_exponent_ci_high",
                        exponent_ci[1] if exponent_ci else None,
                    ),
                    "scaling_r_squared": metric(
                        "scaling_r_squared", fit.r_squared if fit else None
                    ),
                    "evidence_class": group[0]["evidence_class"],
                    "result_scope": group[0]["result_scope"],
                    "run_role": "measurement",
                    "recorded": "true",
                }
            )


def run_adapter_campaign(
    config: dict[str, Any],
    output: Path,
    *,
    repo: Path,
    progress: Callable[[str], None] | None = None,
) -> None:
    validate_campaign_config(config)
    if config.get("require_clean_git", True) and _tracked_worktree_dirty(repo):
        raise RuntimeError("tracked worktree must be clean before a measured campaign")
    config_hash = canonical_hash(config)
    adapter_commit = _git_commit(repo)
    command = list(config["command"])
    executable = Path(command[0])
    if not executable.is_absolute():
        executable = (repo / executable).resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"adapter executable does not exist: {executable}")
    command[0] = str(executable)
    evidence_names = {
        "raw_results.csv",
        "summary.csv",
        "config.json",
        "environment.json",
    }
    if output.exists() and any((output / name).exists() for name in evidence_names):
        raise FileExistsError(
            f"refusing to overwrite existing benchmark evidence in {output}"
        )
    output.mkdir(parents=True, exist_ok=True)
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    (output / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "environment.json").write_text(
        json.dumps(
            _environment(repo, config_hash, adapter_commit, command),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    primers, measurements = _tasks(config)
    all_tasks = primers + measurements
    if progress:
        progress(
            f"campaign start: {len(primers)} primers, "
            f"{len(measurements)} measurements, {len(all_tasks)} total processes"
        )
    rows: list[dict[str, str]] = []
    raw_path = output / "raw_results.csv"
    with raw_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
        writer.writeheader()
        handle.flush()
        for order_index, task in enumerate(all_tasks):
            if progress:
                progress(
                    f"[{order_index + 1}/{len(all_tasks)}] "
                    f"{task['run_role']} scale={task['scale']} "
                    f"threads={task['threads']} "
                    f"parameters={task['parameter_set']} "
                    f"invalid={task['invalid_case'] or 'none'}"
                )
            request = AdapterRequest(
                run_id=str(uuid.uuid4()),
                workload=config["workload"],
                scale=task["scale"],
                threads=task["threads"],
                seed=task["seed"],
                mode=config["mode"],
                invalid_case=task["invalid_case"],
                parameters=task["parameters"],
            )
            execution = execute_adapter(
                command,
                request,
                timeout_seconds=float(config["timeout_seconds"]),
                sampling_interval_ms=float(config["sampling_interval_ms"]),
            )
            new_rows = execution_rows(
                execution,
                config,
                config_hash,
                adapter_commit,
                task,
                order_index,
            )
            writer.writerows(new_rows)
            handle.flush()
            rows.extend(new_rows)
            if progress:
                progress(
                    f"[{order_index + 1}/{len(all_tasks)}] "
                    f"status={'ok' if execution.succeeded else 'failed'} "
                    f"wall_ms={execution.wall_time_ns / 1_000_000:.3f}"
                )
            if not execution.succeeded:
                (logs / f"{request.run_id}.stdout.log").write_text(
                    execution.stdout, encoding="utf-8"
                )
                (logs / f"{request.run_id}.stderr.log").write_text(
                    execution.stderr, encoding="utf-8"
                )
                raise RuntimeError(
                    f"adapter run {request.run_id} failed: {execution.protocol_error}"
                )
    write_campaign_summary(rows, output / "summary.csv")
    if progress:
        progress(f"campaign complete: {output}")
