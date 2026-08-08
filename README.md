# ZK-Bench

ZK-Bench is an in-progress, application-driven benchmark for heterogeneous
zero-knowledge proof implementations. The repository is being rebuilt from
executable relations and per-run evidence before any paper-level comparison is
reported.

## Current status

The common runner currently records phase events, cold process latency, CPU
time, peak RSS, page faults, proof size, invalid-proof rejection, native
relation size, application throughput, native-overhead ratio, configuration
hashes, binary hashes, dependency-lock hashes, and environment metadata.
Unsupported counters are left unavailable rather than encoded as numeric zero.

| Component | Implementation status | Evidence status |
| --- | --- | --- |
| Common JSON adapter protocol and campaign runner | Implemented and tested | Used by both controlled pilots |
| Arkworks Groth16 on BN254 | Controlled and application relations implemented | Controlled pilot validated |
| Jellyfish TurboPlonk 0.8.0 with KZG on BN254 | Controlled relation implemented | Controlled pilot validated |
| Credential, batched-state, and private-swap workloads | Implemented for Groth16 | Final-candidate bundles published; paper claim freeze pending |
| PLONK application relations | Implemented and unit-tested | Final-candidate bundles published; paper claim freeze pending |
| Transparent/STARK adapter | Planned next (Winterfell/Stwo selection pending) | No evidence |
| Bulletproofs range-proof baseline | Implemented as a specialized Ristretto range adapter | Pilot config added; no measured evidence yet |
| Arithmetic backend across curve families | Planned | No evidence |
| Common exponentiation/SHA-256 circuit backend | Planned | No evidence |
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

The application pilots are kept under `.local/reproductions/` while the final
repetition protocol is being frozen. `scripts/check_cross_adapter.py` verifies
shared credential, batched-state, and private-swap fixtures across Groth16 and
PLONK before matched campaigns are promoted.

The six final-candidate bundles are now public under `results/`:

- `results/final-groth16-credential-v1/`
- `results/final-groth16-state-v1/`
- `results/final-groth16-swap-v1/`
- `results/final-jellyfish-plonk-credential-v1/`
- `results/final-jellyfish-plonk-state-v1/`
- `results/final-jellyfish-plonk-swap-v1/`

Each valid cell has ten recorded repetitions and each bundle passed the result
validator. These are measured final-candidate evidence, not permission to make
unbounded protocol-superiority claims.

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

Check application semantics inside WSL after building the adapters:

```powershell
wsl bash -lc 'cd /mnt/d/ZK\ Bench && python3 scripts/check_cross_adapter.py --groth-command .local/wsl-cargo-target/release/zkbench-ark-groth16 --plonk-command .local/wsl-cargo-target/release/zkbench-jellyfish-plonk'
```

Promote a pilot to a ten-repetition final-candidate config without editing the
pilot evidence in place:

```powershell
python scripts\promote_campaign.py --source configs\jellyfish-plonk-credential-pilot.json --output .local\final-configs\jellyfish-plonk-credential-final-v1
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

1. Freeze claim language and generate contribution-driven figures/tables from
   the published final-candidate bundles.
2. Build the Bulletproofs range pilot from
   \`configs/bulletproofs-range-pilot.json\` after the local dependency/build
   permission is confirmed.
3. Add a pinned transparent-proof adapter plus the arithmetic and common-circuit
   backends inspired by the 2023 zk-Bench architecture.
4. Run additional final campaigns only after every adapter passes correctness
   and semantic-scope gates.
5. Replace the exploratory dot panels with contribution-driven heatmaps,
   log-scale scaling curves, phase decomposition, hardware sensitivity, and
   distribution/uncertainty panels.
6. Rewrite the marked and clean manuscripts only after the multi-family
   evidence freeze.

No current pilot result is a final paper claim.
