# ZK-Bench

ZK-Bench is an in-progress, application-driven benchmark for heterogeneous
zero-knowledge proof implementations. The repository is being rebuilt from
executable relations and per-run evidence before any paper-level comparison is
reported.

## Current status

The common runner currently records phase events, cold process latency, CPU
time, peak RSS, page faults, proof size, invalid-proof rejection, native
relation size, configuration hashes, binary hashes, dependency-lock hashes,
and environment metadata. Unsupported counters are left unavailable rather
than encoded as numeric zero.

| Component | Implementation status | Evidence status |
| --- | --- | --- |
| Common JSON adapter protocol and campaign runner | Implemented and tested | Used by both controlled pilots |
| Arkworks Groth16 on BN254 | Controlled and application relations implemented | Controlled pilot validated |
| Jellyfish TurboPlonk 0.8.0 with KZG on BN254 | Controlled relation implemented | Controlled pilot validated |
| Credential, batched-state, and private-swap workloads | Implemented for Groth16 | Pilot configs exist; not yet measured |
| PLONK application relations | Not yet implemented | No application evidence |
| Transparent/STARK adapter | Not yet implemented | No evidence |
| Bulletproofs range-proof baseline | Not yet implemented | No evidence |
| On-chain verifier measurements | Measurement scaffolding only | No measured gas bundle |

The two controlled bundles are deliberately labelled as pilots. They contain
three recorded repetitions per valid cell and must not be treated as
paper-final evidence:

- `results/controlled-groth16-pilot-v1/`
- `results/controlled-plonk-pilot-v1/`

Both bundles pass `scripts/validate_results.py`. Native relation units remain
distinct (`r1cs_constraints` and `plonk_domain_rows`); they must not be merged
onto a generic constraint axis. The implementations also use different
Arkworks versions, so results are implementation-stack evidence unless a
stronger controlled claim is justified explicitly.

## Reproduce the current checks

All Rust dependencies and build outputs are kept under this repository's
`.local/` directory. Do not install or mutate a global toolchain as part of a
benchmark run.

When Windows Git and WSL share this worktree, set line-ending normalization in
the repository only:

```powershell
git config --local core.autocrlf true
```

Run Rust checks inside WSL:

```powershell
wsl bash -lc 'cd /mnt/d/ZK\ Bench && ./scripts/wsl_cargo.sh test --workspace'
```

Run the Python suite from PowerShell:

```powershell
python -m unittest discover -s tests -p 'test_*.py'
```

Validate the published pilots:

```powershell
python scripts\validate_results.py results\controlled-groth16-pilot-v1
python scripts\validate_results.py results\controlled-plonk-pilot-v1
```

Linux adapter campaigns must run with the Python runner inside WSL so
`linux-procfs` measures the adapter itself:

```powershell
wsl bash -lc 'cd /mnt/d/ZK\ Bench && python3 scripts/run_bench.py --config configs/controlled-plonk-pilot.json --output .local/reproductions/controlled-plonk-pilot-v1'
```

The runner refuses to overwrite an existing evidence bundle. Never run a Linux
ELF adapter directly from Windows Python.

## Evidence and release boundary

Public commits may contain implementation code, tests, configs, dependency
locks, environment metadata, and approved raw/summary result files. Paper
sources, reviews, plotting code, paper-ready figures, response letters,
credentials, and private research notes remain local and are rejected by
`scripts/release_guard.py`.

Before every public commit:

```powershell
python scripts\release_guard.py --repo . --staged
```

## Next milestones

1. Audit the matched Groth16 and PLONK pilots and freeze the final repetition,
   scale, thread, and outlier protocol.
2. Implement the credential, batched-state, and private-swap semantics in the
   PLONK adapter with cross-adapter fixtures and negative tests.
3. Add a pinned transparent-proof adapter and a specialized Bulletproofs
   range-proof baseline.
4. Run application pilots, sensitivity studies, and final campaigns.
5. Generate contribution-driven figures and exact tables from frozen evidence.
6. Rewrite the marked and clean manuscripts only after the evidence freeze.

No current pilot result is a final paper claim.
