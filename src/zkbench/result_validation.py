"""Content-level validation for public benchmark evidence bundles."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .adapter_protocol import COARSE_PHASES
from .runner import canonical_hash


REQUIRED_FILES = ("raw_results.csv", "summary.csv", "config.json", "environment.json")
REQUIRED_RAW_COLUMNS = {
    "run_id",
    "claim_id",
    "experiment_id",
    "adapter_commit",
    "config_hash",
    "phase",
    "phase_supported",
    "latency_ms",
    "verify_ok",
    "exit_code",
    "invalid_proof_kind",
    "recorded",
    "run_role",
    "boundary_reason",
}
REQUIRED_SUMMARY_COLUMNS = {
    "claim_id",
    "experiment_id",
    "adapter_commit",
    "config_hash",
    "phase",
    "recorded",
    "run_role",
    "expected_outcomes",
}
RAW_BOUNDARY_FIELDS = (
    "latency_ms",
    "cpu_time_ms",
    "peak_rss_mb",
    "peak_swap_mb",
    "rejection_latency_ms",
)
SUMMARY_QUANTITATIVE_FIELDS = (
    "mean_latency_ms",
    "stdev_latency_ms",
    "p50_latency_ms",
    "p95_latency_ms",
    "min_latency_ms",
    "max_latency_ms",
    "median_cpu_time_ms",
    "median_peak_rss_mb",
    "median_page_faults",
    "speedup_vs_one_thread",
    "speedup_ci_low",
    "speedup_ci_high",
    "parallel_efficiency",
    "scaling_coefficient_a",
    "scaling_exponent_b",
    "scaling_exponent_ci_low",
    "scaling_exponent_ci_high",
    "scaling_r_squared",
)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_processes(config: dict[str, Any]) -> int:
    scales = len(config["scales"])
    threads = len(config["threads"])
    parameter_sets = config.get("parameter_sets") or [
        {"id": "default", "parameters": {}}
    ]
    parameter_set_count = len(parameter_sets)
    primers = (
        parameter_set_count
        * scales
        * threads
        * int(config["os_cache_primer_runs"])
    )
    measurements = (
        parameter_set_count * scales * threads * int(config["repetitions"])
    )
    invalid = sum(
        len(
            case.get(
                "parameter_set_ids",
                [parameter_set["id"] for parameter_set in parameter_sets],
            )
        )
        * len(case["scales"])
        * len(case["threads"])
        * int(case["repetitions"])
        for case in config.get("invalid_cases", [])
    )
    return primers + measurements + invalid


def validate_result_bundle(bundle: Path, *, repo: Path | None = None) -> list[str]:
    bundle = bundle.resolve()
    errors: list[str] = []
    for name in REQUIRED_FILES:
        if not (bundle / name).is_file():
            errors.append(f"missing {name}")
    if errors:
        return errors
    try:
        config = json.loads((bundle / "config.json").read_text(encoding="utf-8"))
        environment = json.loads(
            (bundle / "environment.json").read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        return [f"invalid JSON metadata: {error}"]
    raw_fields, raw = _read_csv(bundle / "raw_results.csv")
    summary_fields, summary = _read_csv(bundle / "summary.csv")
    missing_raw = REQUIRED_RAW_COLUMNS - set(raw_fields)
    missing_summary = REQUIRED_SUMMARY_COLUMNS - set(summary_fields)
    parameter_sets = config.get("parameter_sets")
    if parameter_sets is not None:
        parameter_columns = {"parameter_set", "parameters_json"}
        missing_raw.update(parameter_columns - set(raw_fields))
        missing_summary.update(parameter_columns - set(summary_fields))
    if missing_raw:
        errors.append(f"raw_results.csv missing columns: {sorted(missing_raw)}")
    if missing_summary:
        errors.append(f"summary.csv missing columns: {sorted(missing_summary)}")
    if missing_raw or missing_summary:
        return errors
    if not raw:
        errors.append("raw_results.csv contains no observations")
        return errors
    if not summary:
        errors.append("summary.csv contains no derived rows")

    expected_hash = canonical_hash(config)
    if environment.get("config_hash") != expected_hash:
        errors.append("environment config_hash does not match canonical config")
    raw_hashes = {row["config_hash"] for row in raw}
    if raw_hashes != {expected_hash}:
        errors.append("raw rows do not share the canonical config_hash")
    adapter_commit = environment.get("adapter_commit")
    raw_commits = {row["adapter_commit"] for row in raw}
    if raw_commits != {adapter_commit}:
        errors.append("raw adapter_commit does not match environment")
    if parameter_sets is not None:
        expected_parameters = {
            item["id"]: json.dumps(
                item["parameters"], sort_keys=True, separators=(",", ":")
            )
            for item in parameter_sets
        }
        for row_number, row in enumerate(raw, start=2):
            parameter_set = row["parameter_set"]
            if parameter_set not in expected_parameters:
                errors.append(
                    f"raw row {row_number} has unknown parameter_set {parameter_set}"
                )
            elif row["parameters_json"] != expected_parameters[parameter_set]:
                errors.append(
                    f"raw row {row_number} has broken parameter-set lineage"
                )

    runs: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in raw:
        runs[row["run_id"]].append(row)
        for field in RAW_BOUNDARY_FIELDS:
            value = row.get(field, "")
            if value and float(value) in (0.0, 1.0) and not row["boundary_reason"]:
                errors.append(
                    f"run {row['run_id']} has unexplained boundary in {field}"
                )
    expected_processes = _expected_processes(config)
    if len(runs) != expected_processes:
        errors.append(
            f"expected {expected_processes} processes but found {len(runs)}"
        )
    for run_id, rows in runs.items():
        process_rows = [row for row in rows if row["phase"] == "adapter_process_wall"]
        if len(process_rows) != 1:
            errors.append(
                f"run {run_id} requires one adapter_process_wall row; "
                f"found {len(process_rows)}"
            )
            continue
        phases = [row["phase"] for row in rows if row["phase"] != "adapter_process_wall"]
        if len(phases) != len(set(phases)):
            errors.append(f"run {run_id} contains duplicate phase rows")
        missing = COARSE_PHASES - set(phases)
        if missing:
            errors.append(f"run {run_id} missing coarse phases: {sorted(missing)}")
        process = process_rows[0]
        if process["exit_code"] != "0" or process["phase_status"] != "ok":
            errors.append(f"run {run_id} did not exit successfully")
        expected_verify = "false" if process["invalid_proof_kind"] else "true"
        if process["verify_ok"] != expected_verify:
            errors.append(f"run {run_id} has unexpected verification outcome")

    for index, row in enumerate(summary, start=2):
        if row["config_hash"] != expected_hash or row["adapter_commit"] != adapter_commit:
            errors.append(f"summary row {index} has broken config/commit lineage")
        if row["recorded"] != "true" or row["run_role"] != "measurement":
            errors.append(f"summary row {index} includes a primer/non-recorded run")
        if row["expected_outcomes"] == "unexpected-outcome-present":
            errors.append(f"summary row {index} reports an unexpected outcome")
        if parameter_sets is not None:
            parameter_set = row["parameter_set"]
            if parameter_set not in expected_parameters:
                errors.append(
                    f"summary row {index} has unknown parameter_set {parameter_set}"
                )
            elif row["parameters_json"] != expected_parameters[parameter_set]:
                errors.append(
                    f"summary row {index} has broken parameter-set lineage"
                )
        for field in SUMMARY_QUANTITATIVE_FIELDS:
            value = row.get(field, "")
            if value and float(value) in (0.0, 1.0):
                errors.append(f"summary row {index} contains boundary value in {field}")
        if row["phase"] == "prove_total" and row["threads"] == "1":
            if row.get("speedup_vs_one_thread") or row.get("parallel_efficiency"):
                errors.append(
                    f"summary row {index} exposes fixed one-thread speedup/efficiency"
                )

    if repo is not None:
        repo = repo.resolve()
        cargo_lock = repo / "Cargo.lock"
        expected_lock_hash = environment.get("cargo_lock_sha256")
        if cargo_lock.is_file() and expected_lock_hash:
            if _sha256(cargo_lock) != expected_lock_hash:
                errors.append("Cargo.lock hash does not match environment")
    return errors
