# E1-E3 revision experiments

This file describes the implementation and run boundary for the experiments
approved in `TODO.pdf`. No row is `measured` until a complete result bundle has
been emitted and validated. E4 (a real Bulletproofs state predicate) remains
deferred.

## E1: pairing-backend phase audit

The BLS12-381 Groth16 and Jellyfish PLONK adapters now emit:

- `constraint_synthesis`: the directly timed circuit/R1CS construction step;
- `satisfiability_check`: the explicitly timed preflight correctness audit;
- `witness`: the sum of those two directly measured durations, retained for
  compatibility with existing summaries; and
- `witness_assignment`: unsupported with an explanatory reason, because both
  framework APIs assign variables while constructing constraints and expose no
  independent assignment timer.

Do not add `witness` to `constraint_synthesis` or `satisfiability_check`; it is
their aggregate. In Arkworks, `prove_total` synthesizes the assigned circuit
again inside the Groth16 prover. The separate `witness` event is therefore a
preflight audit, not an omitted production-prover phase. The revised paper must
base any end-to-end definition on the measured phase boundary rather than
double-counting this audit.

The existing reproduction matrix contains the nine paper cells for each
pairing backend. After building release binaries, rerun E1 with:

```powershell
wsl.exe --cd "D:\Dự án NCKH đã nộp\ZK Bench" bash -lc 'PYTHONPATH=src python3 scripts/run_reproduction_campaigns.py --campaign identity-groth16 --campaign state-groth16 --campaign pcas-groth16 --campaign identity-plonk --campaign state-plonk --campaign pcas-plonk --output-root .local/reproductions/e1-phase-audit'
```

## E2/E3: matched state-application scaling

`configs/application-state-campaigns.json` defines one shared matrix for
Groth16, PLONK, and Winterfell. The input scale is the number of real state
updates: 127, 255, 511, 1023, 2047, 4095, and 8191. Hash rounds remain fixed at
five and every delta is range-constrained to 16 bits.

The deterministic `splitmix64-v1` fixture schedule is opt-in. Legacy campaign
configs continue to use `linear-v1`, preserving their reproducibility. The new
schedule is identical in the Groth16, PLONK, and Winterfell implementations and
keeps the AIR range-bit columns non-degenerate.

The Winterfell state trace has 29 columns:

- current state, current digest, and bounded delta;
- five columns for `H5(delta, next_state)`;
- five columns for `H5(current_digest, update_hash)`; and
- sixteen Boolean range-bit columns.

Its 29 transition constraints are one state update, one delta reconstruction,
16 Booleanity constraints, ten degree-five H5 round constraints, and one digest
update. The maximum transition degree is five. Credential and private-swap
Winterfell runs remain size-matched proxy kernels and must be labelled as such.

List or smoke-test the new matrix with:

```powershell
python scripts/run_reproduction_campaigns.py --matrix configs/application-state-campaigns.json --list
wsl.exe --cd "D:\Dự án NCKH đã nộp\ZK Bench" bash -lc 'PYTHONPATH=src python3 scripts/run_reproduction_campaigns.py --matrix configs/application-state-campaigns.json --all --smoke --output-root .local/reproductions/application-state-smoke'
```

Smoke bundles are diagnostic only. When run from the intentionally dirty
implementation worktree they will fail the publication validator's clean-tree
gate and must not be cited or copied into `results/`.

Run the full campaign with:

```powershell
wsl.exe --cd "D:\Dự án NCKH đã nộp\ZK Bench" bash -lc 'PYTHONPATH=src python3 scripts/run_reproduction_campaigns.py --matrix configs/application-state-campaigns.json --all --output-root .local/reproductions/application-state'
```

The full command is intentionally not part of the automated test suite; it
contains two primers and ten recorded runs per adapter/scale cell and can take
several hours.

## Build and correctness gate

On this Windows workspace, use the repository-local WSL toolchain:

```powershell
wsl.exe --cd "D:\Dự án NCKH đã nộp\ZK Bench" bash -lc './scripts/wsl_cargo.sh test -p zkbench-ark-groth16-bls12-381 -p zkbench-jellyfish-plonk -p zkbench-winterfell-stark'
wsl.exe --cd "D:\Dự án NCKH đã nộp\ZK Bench" bash -lc './scripts/wsl_cargo.sh build --release -p zkbench-ark-groth16-bls12-381 -p zkbench-jellyfish-plonk -p zkbench-winterfell-stark'
```

Validate every completed bundle before manuscript use:

```powershell
python scripts/validate_results.py <result-directory>
```

Only validated raw and summary rows may move the new claim from `implemented`
to `measured` in `configs/reproduction-claim-registry.json`.
