#!/usr/bin/env python3
"""Run and summarize the confirmatory million-unit WSL2 swap audit."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from run_reproduction_campaigns import build_campaign, load_matrix  # noqa: E402
from zkbench.campaign import run_adapter_campaign  # noqa: E402
from zkbench.result_validation import validate_result_bundle  # noqa: E402
from zkbench.runner import percentile  # noqa: E402


DEFAULT_CONFIG = REPO / "configs" / "million-swap-audit.json"
SYSTEM_LABELS = {
    "groth16": "Groth16",
    "plonk": "PLONK",
    "stark": "zk-STARK",
    "bulletproofs": "Bulletproofs",
}
AUDIT_RAW_FIELDS = {
    "latency_ms",
    "peak_rss_mb",
    "peak_swap_mb",
    "system_mem_available_min_mb",
    "system_mem_total_mb",
    "system_swap_total_mb",
    "system_swap_used_peak_mb",
    "system_swap_in_mb_delta",
    "system_swap_out_mb_delta",
    "system_swap_io_observed",
    "system_counter_provider",
    "system_counter_samples",
}
SUMMARY_FIELDS = [
    "system",
    "variant",
    "native_relation_unit",
    "input_scale",
    "threads",
    "n",
    "wall_median_s",
    "wall_p95_s",
    "peak_rss_median_gib",
    "peak_rss_p95_gib",
    "process_vmswap_median_mib",
    "process_vmswap_max_mib",
    "guest_mem_total_gib",
    "mem_available_median_min_gib",
    "mem_available_absolute_min_gib",
    "guest_swap_used_median_peak_gib",
    "guest_swap_used_max_peak_gib",
    "guest_swap_total_gib",
    "swap_in_median_delta_mib",
    "swap_in_max_delta_mib",
    "swap_out_median_delta_mib",
    "swap_out_max_delta_mib",
    "runs_with_swap_io",
    "swap_io_observed",
    "system_counter_provider",
    "system_counter_samples_total",
    "evidence_class",
    "result_scope",
    "zero_value_reason",
]


def load_audit_config(path: Path = DEFAULT_CONFIG) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != "1.0":
        raise ValueError("unsupported swap-audit schema_version")
    systems = config.get("systems")
    if not isinstance(systems, list) or not systems:
        raise ValueError("swap-audit config requires a nonempty systems list")
    unknown = set(systems) - set(SYSTEM_LABELS)
    if unknown:
        raise ValueError(f"unknown swap-audit systems: {sorted(unknown)}")
    return config


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def instrumentation_hashes() -> dict[str, str]:
    paths = (
        REPO / "src" / "zkbench" / "system_metrics.py",
        REPO / "src" / "zkbench" / "adapter_runner.py",
        REPO / "src" / "zkbench" / "campaign.py",
        Path(__file__).resolve(),
    )
    return {path.relative_to(REPO).as_posix(): file_sha256(path) for path in paths}


def build_audit_campaign(
    spec: dict,
    system: str,
    *,
    repetitions: int,
    primers: int,
    require_clean_git: bool,
) -> dict:
    matrix_path = REPO / spec["base_matrix"]
    config = build_campaign(load_matrix(matrix_path), f"state-{system}")
    config.update(
        {
            "claim_id": spec["claim_id"],
            "experiment_id": f"million-swap-audit-{system}-v1",
            "invalid_cases": [],
            "os_cache_primer_runs": primers,
            "repetitions": repetitions,
            "require_clean_git": require_clean_git,
            "result_scope": (
                "confirmatory million-unit WSL2/Linux memory-pressure audit; "
                "timing is not merged with the paper-scale campaign"
            ),
            "sampling_interval_ms": spec["process_sampling_interval_ms"],
            "scales": [spec["scale"]],
            "seed": spec["seed"],
            "system_memory_provider": "linux-procfs-system",
            "system_sampling_interval_ms": spec["system_sampling_interval_ms"],
            "threads": [spec["threads"]],
            "timeout_seconds": spec["timeout_seconds"],
            "instrumentation_source_sha256": instrumentation_hashes(),
        }
    )
    return config


def _historical_medians() -> dict[str, float]:
    root = REPO / ".local" / "reproductions" / "paper-scale"
    medians: dict[str, float] = {}
    for system in SYSTEM_LABELS:
        raw = root / f"state-{system}-final-v1" / "raw_results.csv"
        if not raw.is_file():
            continue
        with raw.open(newline="", encoding="utf-8") as handle:
            values = [
                float(row["latency_ms"]) / 1000
                for row in csv.DictReader(handle)
                if row.get("phase") == "adapter_process_wall"
                and row.get("recorded") == "true"
                and not row.get("invalid_proof_kind")
                and row.get("input_scale") == "1048576"
            ]
        if values:
            medians[system] = statistics.median(values)
    return medians


def print_estimate(systems: Iterable[str], repetitions: int, primers: int) -> None:
    medians = _historical_medians()
    total = 0.0
    print("Estimated runtime from the previous 1,048,576-unit campaign:")
    print("system         median/run   processes   estimate")
    for system in systems:
        per_run = medians.get(system)
        processes = repetitions + primers
        if per_run is None:
            print(f"{SYSTEM_LABELS[system]:<14} unavailable  {processes:>9}   unavailable")
            continue
        estimate = per_run * processes
        total += estimate
        print(
            f"{SYSTEM_LABELS[system]:<14} {per_run:>8.1f} s  "
            f"{processes:>9}   {estimate / 60:>7.1f} min"
        )
    if total:
        print(f"Total compute estimate: {total / 60:.1f} min")
        print("Planning allowance (+20%): " f"{total * 1.2 / 60:.1f} min")


def _numeric(rows: list[dict[str, str]], field: str) -> list[float]:
    values = [float(row[field]) for row in rows if row.get(field, "") != ""]
    if len(values) != len(rows):
        raise ValueError(f"{field} is unavailable in one or more recorded rows")
    return values


def summarize_bundle(system: str, bundle: Path) -> dict[str, str]:
    with (bundle / "raw_results.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = AUDIT_RAW_FIELDS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{bundle} missing audit fields: {sorted(missing)}")
        rows = [
            row
            for row in reader
            if row.get("phase") == "adapter_process_wall"
            and row.get("recorded") == "true"
            and not row.get("invalid_proof_kind")
        ]
    if not rows:
        raise ValueError(f"{bundle} contains no recorded process rows")
    latency_s = [value / 1000 for value in _numeric(rows, "latency_ms")]
    rss_gib = [value / 1024 for value in _numeric(rows, "peak_rss_mb")]
    process_swap = _numeric(rows, "peak_swap_mb")
    mem_available_gib = [
        value / 1024 for value in _numeric(rows, "system_mem_available_min_mb")
    ]
    mem_total_gib = [value / 1024 for value in _numeric(rows, "system_mem_total_mb")]
    swap_used_gib = [
        value / 1024 for value in _numeric(rows, "system_swap_used_peak_mb")
    ]
    swap_total_gib = [
        value / 1024 for value in _numeric(rows, "system_swap_total_mb")
    ]
    swap_in_mib = _numeric(rows, "system_swap_in_mb_delta")
    swap_out_mib = _numeric(rows, "system_swap_out_mb_delta")
    observed_values = [row["system_swap_io_observed"] for row in rows]
    if any(value not in {"true", "false"} for value in observed_values):
        raise ValueError(f"{bundle} has unavailable swap-I/O observations")
    observed_count = sum(value == "true" for value in observed_values)
    providers = sorted({row["system_counter_provider"] for row in rows})
    if providers != ["linux-procfs-system"]:
        raise ValueError(f"unexpected system counter providers: {providers}")
    samples = sum(int(row["system_counter_samples"]) for row in rows)

    def six(value: float) -> str:
        return f"{value:.6f}"

    return {
        "system": SYSTEM_LABELS[system],
        "variant": rows[0]["variant"],
        "native_relation_unit": rows[0]["relation_unit"],
        "input_scale": rows[0]["input_scale"],
        "threads": rows[0]["threads"],
        "n": str(len(rows)),
        "wall_median_s": six(statistics.median(latency_s)),
        "wall_p95_s": six(percentile(latency_s, 0.95)),
        "peak_rss_median_gib": six(statistics.median(rss_gib)),
        "peak_rss_p95_gib": six(percentile(rss_gib, 0.95)),
        "process_vmswap_median_mib": six(statistics.median(process_swap)),
        "process_vmswap_max_mib": six(max(process_swap)),
        "guest_mem_total_gib": six(statistics.median(mem_total_gib)),
        "mem_available_median_min_gib": six(statistics.median(mem_available_gib)),
        "mem_available_absolute_min_gib": six(min(mem_available_gib)),
        "guest_swap_used_median_peak_gib": six(statistics.median(swap_used_gib)),
        "guest_swap_used_max_peak_gib": six(max(swap_used_gib)),
        "guest_swap_total_gib": six(statistics.median(swap_total_gib)),
        "swap_in_median_delta_mib": six(statistics.median(swap_in_mib)),
        "swap_in_max_delta_mib": six(max(swap_in_mib)),
        "swap_out_median_delta_mib": six(statistics.median(swap_out_mib)),
        "swap_out_max_delta_mib": six(max(swap_out_mib)),
        "runs_with_swap_io": f"{observed_count}/{len(rows)}",
        "swap_io_observed": "yes" if observed_count else "no",
        "system_counter_provider": providers[0],
        "system_counter_samples_total": str(samples),
        "evidence_class": rows[0]["evidence_class"],
        "result_scope": rows[0]["result_scope"],
        "zero_value_reason": (
            "Measured exact zero retained for the reviewer-specific swap audit"
            if not observed_count
            else ""
        ),
    }


def write_summary(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _latex_number(value: str, digits: int = 2) -> str:
    return f"{float(value):,.{digits}f}"


def write_latex_table(rows: list[dict[str, str]], path: Path) -> None:
    lines = [
        "% Requires: \\usepackage{booktabs,multirow,graphicx}",
        "\\begin{table*}[t]",
        "\\centering",
        "\\caption{Confirmatory memory-pressure and swap-I/O audit at "
        "$1{,}048{,}576$ native relation units.}",
        "\\label{tab:million-swap-audit}",
        "\\scriptsize",
        "\\setlength{\\tabcolsep}{3.5pt}",
        "\\resizebox{\\textwidth}{!}{%",
        "\\begin{tabular}{llrrrrrrrrc}",
        "\\toprule",
        "\\multirow{2}{*}{Stack} & \\multirow{2}{*}{Statistic} & "
        "\\multirow{2}{*}{$n$} & \\multicolumn{1}{c}{Runtime} & "
        "\\multicolumn{2}{c}{Process memory} & "
        "\\multicolumn{2}{c}{WSL2 guest memory} & "
        "\\multicolumn{3}{c}{Guest swap I/O} \\\\",
        "\\cmidrule(lr){4-4} \\cmidrule(lr){5-6} \\cmidrule(lr){7-8} "
        "\\cmidrule(lr){9-11}",
        " & & & Wall (s) & Peak RSS (GiB) & VmSwap (MiB) & "
        "MemAvailable / total (GiB) & Swap used / total (GiB) & "
        "$\\Delta$in (MiB) & "
        "$\\Delta$out (MiB) & Observed \\\\",
        "\\midrule",
    ]
    for index, row in enumerate(rows):
        assessment = (
            "No"
            if row["swap_io_observed"] == "no"
            else f"Yes ({row['runs_with_swap_io']})"
        )
        lines.extend(
            [
                f"\\multirow{{2}}{{*}}{{{row['system']}}} & Median & "
                f"\\multirow{{2}}{{*}}{{{row['n']}}} & "
                f"{_latex_number(row['wall_median_s'])} & "
                f"{_latex_number(row['peak_rss_median_gib'])} & "
                f"{_latex_number(row['process_vmswap_median_mib'], 3)} & "
                f"{_latex_number(row['mem_available_median_min_gib'])} / "
                f"{_latex_number(row['guest_mem_total_gib'])} & "
                f"{_latex_number(row['guest_swap_used_median_peak_gib'], 3)} / "
                f"{_latex_number(row['guest_swap_total_gib'], 2)} & "
                f"{_latex_number(row['swap_in_median_delta_mib'], 3)} & "
                f"{_latex_number(row['swap_out_median_delta_mib'], 3)} & "
                f"\\multirow{{2}}{{*}}{{{assessment}}} \\\\",
                " & P95 / max$^{a}$ & & "
                f"{_latex_number(row['wall_p95_s'])} & "
                f"{_latex_number(row['peak_rss_p95_gib'])} & "
                f"{_latex_number(row['process_vmswap_max_mib'], 3)} & "
                f"{_latex_number(row['mem_available_absolute_min_gib'])} / "
                f"{_latex_number(row['guest_mem_total_gib'])} & "
                f"{_latex_number(row['guest_swap_used_max_peak_gib'], 3)} / "
                f"{_latex_number(row['guest_swap_total_gib'], 2)} & "
                f"{_latex_number(row['swap_in_max_delta_mib'], 3)} & "
                f"{_latex_number(row['swap_out_max_delta_mib'], 3)} & \\\\ ",
            ]
        )
        if index != len(rows) - 1:
            lines.append("\\addlinespace[2pt]")
    lines.extend(
        [
            "\\bottomrule",
            "\\end{tabular}%",
            "}",
            "\\begin{minipage}{\\textwidth}",
            "\\footnotesize",
            "$^{a}$P95 is reported for runtime and peak RSS; maxima are reported "
            "for VmSwap, guest swap use, and per-run swap-in/out deltas. "
            "MemAvailable reports the absolute minimum in this row. "
            "Exact zero swap deltas are retained because zero is the reviewer-specific "
            "audit outcome. Counters are system-wide within the Linux/WSL2 guest; "
            "Windows host pagefile traffic is not measured.",
            "\\end{minipage}",
            "\\end{table*}",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outputs(spec: dict, systems: list[str], output_root: Path) -> list[dict[str, str]]:
    rows = [summarize_bundle(system, output_root / system) for system in systems]
    summary_path = output_root / "swap_audit_summary.csv"
    table_csv = REPO / spec["table_csv"]
    table_tex = REPO / spec["table_tex"]
    write_summary(rows, summary_path)
    write_summary(rows, table_csv)
    write_latex_table(rows, table_tex)
    manifest = {
        "schema_version": "1.0",
        "generator": "scripts/run_swap_audit.py",
        "input_bundles": [
            {
                "path": (output_root / system).relative_to(REPO).as_posix(),
                "raw_results_sha256": file_sha256(
                    output_root / system / "raw_results.csv"
                ),
            }
            for system in systems
        ],
        "outputs": {
            summary_path.relative_to(REPO).as_posix(): file_sha256(summary_path),
            table_csv.relative_to(REPO).as_posix(): file_sha256(table_csv),
            table_tex.relative_to(REPO).as_posix(): file_sha256(table_tex),
        },
        "limitations": spec["limitations"],
    }
    manifest_path = output_root / "swap_audit_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--system", action="append", choices=tuple(SYSTEM_LABELS))
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--primers", type=int)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    spec = load_audit_config(args.config.resolve())
    systems = args.system or list(spec["systems"])
    repetitions = args.repetitions or int(spec["repetitions"])
    primers = args.primers or int(spec["os_cache_primer_runs"])
    if repetitions < 3:
        parser.error("--repetitions must be at least 3")
    if primers < 1:
        parser.error("--primers must be at least 1")
    output_root = REPO / spec["output_root"]

    print_estimate(systems, repetitions, primers)
    if args.estimate_only:
        return 0
    if not args.summarize_only:
        for system in systems:
            output = output_root / system
            print(f"[{system}] output={output}", flush=True)
            config = build_audit_campaign(
                spec,
                system,
                repetitions=repetitions,
                primers=primers,
                require_clean_git=not args.allow_dirty,
            )
            run_adapter_campaign(
                config,
                output,
                repo=REPO,
                progress=lambda message, current=system: print(
                    f"[{current}] {message}", flush=True
                ),
            )
            if not args.allow_dirty:
                errors = validate_result_bundle(output, repo=REPO)
                if errors:
                    raise RuntimeError(
                        f"{system} result validation failed: {'; '.join(errors)}"
                    )
    rows = write_outputs(spec, systems, output_root)
    print(f"Wrote {output_root / 'swap_audit_summary.csv'}")
    print(f"Wrote {REPO / spec['table_csv']}")
    print(f"Wrote {REPO / spec['table_tex']}")
    print(
        "Swap I/O observed: "
        + ", ".join(f"{row['system']}={row['swap_io_observed']}" for row in rows)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
