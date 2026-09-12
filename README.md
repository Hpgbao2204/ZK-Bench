# ZK-Bench

ZK-Bench is an application-driven benchmark for zero-knowledge proof
implementation stacks. It evaluates Groth16, PLONK, Winterfell STARK, and
Bulletproofs across credential, batched-state, and private-swap workloads.

The runner retains per-run setup, proving and verification time, proof size,
CPU time, peak memory, native relation size, configuration hashes, invalid-proof
rejection, and explicit unavailable-metric reasons. Cross-ecosystem results are
implementation-stack comparisons, not protocol-wide rankings.

## Public artifact map

- `adapters/` and `crates/`: pinned Rust implementations and adapter entry points.
- `src/zkbench/`: orchestration, measurement, workload, and validation code.
- `configs/`: versioned experiment matrices, fixtures, and model inputs.
- `results/paper-scale/`: all retained raw and summary rows for the 12 main
  workload/stack campaigns, plus clearly labeled modeled gas and hybrid outputs.
- `results/million-swap-audit-v1/`: raw, summary, configuration, and environment
  evidence for the confirmatory 1,048,576-unit WSL2 guest swap audit.
- `results/base-sepolia/`: public transaction evidence for proof publication as
  calldata. These transactions did not perform EVM cryptographic verification.
- `scripts/` and `tests/`: reproduction, validation, and test commands.

Each measured benchmark bundle contains `raw_results.csv`, `summary.csv`,
`config.json`, and `environment.json`. Summaries are generated from raw rows.
The environment file records the source commit, config hash, dependency-lock
hash, adapter-binary hash, CPU topology, and observed runtime environment.

## Requirements

The recorded campaigns used Windows with WSL2, Python 3.14.4, Rust 1.97.1,
Zig 0.16.0, 16 visible logical CPUs, and the hardware details retained in each
`environment.json`. Python 3.12 or newer is supported. The repository-local
bootstrap keeps downloaded toolchains and build output under ignored `.local/`.

From PowerShell in the cloned repository, resolve its WSL path without assuming
a particular drive or directory name:

```powershell
$wslRepo = (& wsl.exe -- wslpath -a (Resolve-Path .)).Trim()
wsl.exe -- bash -lc "cd '$wslRepo' && ./scripts/bootstrap_wsl_toolchain.sh"
wsl.exe -- bash -lc "cd '$wslRepo' && ./scripts/wsl_cargo.sh build --workspace --release --locked"
```

The bootstrap downloads the pinned Rust and Zig toolchains and verifies the
downloaded archives before use.

## Verify the source and published evidence

Run the Python and Rust test suites:

```powershell
python -m unittest discover -s tests -p 'test_*.py'
$wslRepo = (& wsl.exe -- wslpath -a (Resolve-Path .)).Trim()
wsl.exe -- bash -lc "cd '$wslRepo' && ./scripts/wsl_cargo.sh test --workspace --locked"
```

Validate representative measured bundles and the public-chain bundle:

```powershell
python scripts\validate_results.py results\paper-scale\state-plonk-final-v1
python scripts\validate_results.py results\million-swap-audit-v1\plonk
python scripts\validate_chain_results.py results\base-sepolia\identity-publication-final-v1
```

## Reproduce measurements

List the workload/adapter matrix, then run one short smoke campaign inside WSL:

```powershell
python scripts\run_reproduction_campaigns.py --list
$wslRepo = (& wsl.exe -- wslpath -a (Resolve-Path .)).Trim()
wsl.exe -- bash -lc "cd '$wslRepo' && PYTHONPATH=src python3 scripts/run_reproduction_campaigns.py --campaign identity-groth16 --smoke"
```

Run all main campaigns by replacing the final arguments with `--all`. Full runs
write to `.local/reproductions/paper-scale/` and can take hours depending on the
host; the smoke run is the recommended installation check.

Estimate or rerun the confirmatory million-unit swap audit:

```powershell
.\scripts\run_swap_audit.ps1 -EstimateOnly
.\scripts\run_swap_audit.ps1
```

This audit measures Linux/WSL2 guest counters (`VmSwap`, `/proc/meminfo`, and
`pswpin`/`pswpout`). It does not measure Windows host pagefile traffic.

## Evidence interpretation

- `measured` means retained observations produced by this repository.
- `modeled` means deterministic calculation from stated inputs; it is not an
  on-chain or prover measurement.
- Native relation units differ by stack and must not be interpreted as one
  universal constraint type.
- Absolute paths in historical `environment.json` files record where the run
  occurred; reproduction commands discover the current clone path dynamically.

## Release boundary

Public commits contain source, tests, configs, dependency locks, and approved
evidence. Credentials, manuscripts, reviews, plotting code, paper figures,
images, and PDFs stay local and are ignored. Before committing public artifacts:

```powershell
python scripts\release_guard.py --repo . --staged
```
