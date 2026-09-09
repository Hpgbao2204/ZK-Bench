# ZK-Bench

ZK-Bench is an application-driven benchmark for zero-knowledge proof
implementation stacks. It evaluates Groth16, PLONK, Winterfell STARK, and
Bulletproofs across credential, batched-state, and private-swap workloads.

The runner records reproducible per-run evidence such as setup, proving and
verification time, proof size, CPU time, peak memory, native relation size,
configuration hashes, and invalid-proof rejection. Cross-ecosystem results are
implementation-stack comparisons, not protocol-wide rankings.

## Layout

- `adapters/`: proof-system adapters.
- `src/zkbench/`: runner, workloads, metrics, and analysis.
- `configs/`: versioned experiment definitions.
- `results/`: approved raw, summary, config, and environment evidence.
- `scripts/`: benchmark, validation, and modeling commands.
- `tests/`: Python test suite.

## Quick checks

Run Python tests:

```powershell
python -m unittest discover -s tests -p 'test_*.py'
```

Run Rust tests inside WSL:

```powershell
wsl bash -lc 'cd /mnt/d/ZK\ Bench && ./scripts/wsl_cargo.sh test --workspace'
```

List the reproduction matrix:

```powershell
python scripts\run_reproduction_campaigns.py --list
```

Validate an evidence bundle:

```powershell
python scripts\validate_results.py results\controlled-groth16-pilot-v1
```

## Release boundary

Public commits contain implementation code, tests, configs, dependency locks,
and approved evidence files. Credentials, manuscripts, reviews, plotting code,
figures, images, and PDFs stay local and are ignored.

Before committing public artifacts, run:

```powershell
python scripts\release_guard.py --repo . --staged
```
