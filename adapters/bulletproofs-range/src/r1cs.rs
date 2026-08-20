use bulletproofs::r1cs::{ConstraintSystem, Prover, R1CSProof, Variable, Verifier};
use bulletproofs::{BulletproofGens, PedersenGens};
use curve25519_dalek::ristretto::CompressedRistretto;
use curve25519_dalek::scalar::Scalar;
use merlin::Transcript;
use rand_chacha::ChaChaRng;
use rand_core::{RngCore, SeedableRng};
use std::collections::BTreeMap;
use std::error::Error;
use zkbench_adapter_sdk::{
    AdapterRequest, AdapterResult, PhaseEvent, PhaseTimer, SCHEMA_VERSION, emit, emit_result,
};

use crate::{ADAPTER, seed_bytes, verification_error_type};

pub const CONTROLLED_WORKLOAD: &str = "controlled_kernel";
const CREDENTIAL_WORKLOAD: &str = "credential";
const STATE_WORKLOAD: &str = "batched_state";
const SWAP_WORKLOAD: &str = "private_swap";

struct ChainInstance {
    start: Scalar,
    factor: Scalar,
    output: Scalar,
}

fn numeric_parameter(request: &AdapterRequest, name: &str, default: usize) -> Result<usize, String> {
    let value = request
        .parameters
        .get(name)
        .map(|value| {
            value
                .as_u64()
                .ok_or_else(|| format!("{name} must be a nonnegative integer"))
        })
        .transpose()?
        .unwrap_or(default as u64);
    if value <= 1 {
        return Err(format!("{name} must exceed excluded numeric boundaries"));
    }
    usize::try_from(value).map_err(|_| format!("{name} does not fit usize"))
}

fn relation_steps(request: &AdapterRequest) -> Result<usize, String> {
    if request.parameters.get("scale_mode").and_then(|value| value.as_str())
        == Some("target_native_size")
    {
        return usize::try_from(request.scale).map_err(|_| "scale does not fit usize".to_owned());
    }
    if let Some(value) = request.parameters.get("target_native_size") {
        let target = value
            .as_u64()
            .ok_or("target_native_size must be a nonnegative integer")?;
        if target <= 1 {
            return Err("target_native_size must exceed excluded numeric boundaries".to_owned());
        }
        return usize::try_from(target)
            .map_err(|_| "target_native_size does not fit usize".to_owned());
    }
    let scale = usize::try_from(request.scale).map_err(|_| "scale does not fit usize")?;
    let steps_per_unit = match request.workload.as_str() {
        CONTROLLED_WORKLOAD => 1,
        CREDENTIAL_WORKLOAD => {
            2 * numeric_parameter(request, "age_bits", 8)?
                + 9 * numeric_parameter(request, "hash_rounds", 5)?
                + 2
        }
        STATE_WORKLOAD => {
            numeric_parameter(request, "update_bits", 16)?
                + 6 * numeric_parameter(request, "hash_rounds", 5)?
                + 2
        }
        SWAP_WORKLOAD => {
            let range = 2 * numeric_parameter(request, "range_bits", 64)?;
            let time = numeric_parameter(request, "time_bits", 32)?;
            let paths = numeric_parameter(request, "membership_paths", 2)?;
            let depth = numeric_parameter(request, "merkle_depth", 32)?;
            let hashes = (2 + paths * (depth + 2))
                * 3
                * numeric_parameter(request, "hash_rounds", 5)?;
            range + time + hashes + 8
        }
        _ => return Err(format!("unsupported R1CS workload: {}", request.workload)),
    };
    scale
        .checked_mul(steps_per_unit)
        .ok_or_else(|| "R1CS relation size overflow".to_owned())
}

fn workload_factor(workload: &str, base: Scalar, index: usize) -> Scalar {
    if workload == CONTROLLED_WORKLOAD {
        return base;
    }
    let domain = match workload {
        CREDENTIAL_WORKLOAD => 3_u64,
        STATE_WORKLOAD => 5_u64,
        SWAP_WORKLOAD => 7_u64,
        _ => unreachable!("workload checked before factor schedule"),
    };
    base + Scalar::from(domain + (index % 17) as u64)
}

fn chain_instance(seed: u64, workload: &str, steps: usize) -> ChainInstance {
    let mut rng = ChaChaRng::from_seed(seed_bytes(seed));
    let start = Scalar::from(rng.next_u64().max(2));
    let factor = Scalar::from(rng.next_u64().max(2));
    let mut output = start;
    for index in 0..steps {
        output *= workload_factor(workload, factor, index);
    }
    ChainInstance {
        start,
        factor,
        output,
    }
}

fn append_public_instance(
    transcript: &mut Transcript,
    workload: &str,
    steps: usize,
    factor: &Scalar,
    output: &Scalar,
    parameters: &BTreeMap<String, serde_json::Value>,
) {
    transcript.append_message(b"workload", workload.as_bytes());
    transcript.append_u64(b"steps", steps as u64);
    transcript.append_message(b"factor", factor.as_bytes());
    transcript.append_message(b"output", output.as_bytes());
    for (name, value) in parameters {
        transcript.append_message(b"parameter-name", name.as_bytes());
        transcript.append_message(b"parameter-value", value.to_string().as_bytes());
    }
}

fn chain_gadget<CS: ConstraintSystem>(
    cs: &mut CS,
    start: Variable,
    factor: Scalar,
    output: Scalar,
    steps: usize,
    workload: &str,
) {
    let mut state = start;
    for index in 0..steps {
        let step_factor = workload_factor(workload, factor, index);
        let (_, _, next) = cs.multiply(state.into(), step_factor.into());
        state = next;
    }
    cs.constrain(state - output);
}

fn emit_unsupported(request: &AdapterRequest, phase: &str, reason: &str) -> Result<(), String> {
    emit(&PhaseEvent::unsupported(request, ADAPTER, phase, reason))
}

pub fn supports(workload: &str) -> bool {
    matches!(
        workload,
        CONTROLLED_WORKLOAD | CREDENTIAL_WORKLOAD | STATE_WORKLOAD | SWAP_WORKLOAD
    )
}

pub fn run(request: &AdapterRequest) -> Result<(), Box<dyn Error>> {
    if let Some(mode) = request.parameters.get("scale_mode") {
        if mode.as_str() != Some("target_native_size") {
            return Err("scale_mode must be target_native_size when present".into());
        }
    }
    let steps = relation_steps(request)?;
    let generator_capacity = steps
        .checked_next_power_of_two()
        .ok_or("Bulletproof generator capacity overflow")?;

    let native_timer = PhaseTimer::start();
    let instance = chain_instance(request.seed, &request.workload, steps);
    let application_units = request
        .parameters
        .get("application_units")
        .and_then(|value| value.as_u64())
        .unwrap_or(request.scale);
    let mut native_metrics = BTreeMap::from([
        ("application_units".to_owned(), application_units as f64),
        ("r1cs_multipliers".to_owned(), steps as f64),
    ]);
    if request.parameters.contains_key("target_native_size") {
        native_metrics.insert("target_native_size".to_owned(), steps as f64);
    }
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "native_execution",
        native_timer.elapsed(),
        native_metrics,
    )?)?;

    let preprocess_timer = PhaseTimer::start();
    let pc_gens = PedersenGens::default();
    let bp_gens = BulletproofGens::new(generator_capacity, 1);
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "setup_or_preprocess",
        preprocess_timer.elapsed(),
        BTreeMap::from([("generator_capacity".to_owned(), generator_capacity as f64)]),
    )?)?;
    emit_unsupported(
        request,
        "key_load",
        "Bulletproofs uses deterministic generators and has no proving or verification key",
    )?;

    let mut prover_transcript = Transcript::new(b"zkbench-bulletproofs-r1cs-chain-v1");
    append_public_instance(
        &mut prover_transcript,
        &request.workload,
        steps,
        &instance.factor,
        &instance.output,
        &request.parameters,
    );
    let mut prover = Prover::new(&pc_gens, &mut prover_transcript);
    let mut rng = ChaChaRng::from_seed(seed_bytes(request.seed ^ 0xa5a5_a5a5_a5a5_a5a5));

    let commitment_timer = PhaseTimer::start();
    let (start_commitment, start_var) = prover.commit(instance.start, Scalar::random(&mut rng));
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "commitment",
        commitment_timer.elapsed(),
        BTreeMap::new(),
    )?)?;

    let witness_timer = PhaseTimer::start();
    chain_gadget(
        &mut prover,
        start_var,
        instance.factor,
        instance.output,
        steps,
        &request.workload,
    );
    let metrics = prover.metrics();
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "witness",
        witness_timer.elapsed(),
        BTreeMap::from([
            ("r1cs_multipliers".to_owned(), metrics.multipliers as f64),
            ("generator_capacity".to_owned(), generator_capacity as f64),
        ]),
    )?)?;

    let prove_timer = PhaseTimer::start();
    let proof = prover.prove(&bp_gens)?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "prove_total",
        prove_timer.elapsed(),
        BTreeMap::from([("r1cs_multipliers".to_owned(), metrics.multipliers as f64)]),
    )?)?;

    let serialize_timer = PhaseTimer::start();
    let proof_bytes = proof.to_bytes();
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "serialize",
        serialize_timer.elapsed(),
        BTreeMap::from([("proof_bytes".to_owned(), proof_bytes.len() as f64)]),
    )?)?;

    let deserialize_timer = PhaseTimer::start();
    let decoded_proof = R1CSProof::from_bytes(&proof_bytes)?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "deserialize",
        deserialize_timer.elapsed(),
        BTreeMap::from([("proof_bytes".to_owned(), proof_bytes.len() as f64)]),
    )?)?;

    emit_unsupported(
        request,
        "fft_ntt",
        "the Bulletproofs R1CS prover does not use an FFT or NTT phase",
    )?;
    emit_unsupported(
        request,
        "msm",
        "the library does not expose its internal multiscalar multiplications as a timed phase",
    )?;

    let verification_output = if request.invalid_case.is_some() {
        instance.output + Scalar::ONE
    } else {
        instance.output
    };
    let verify_timer = PhaseTimer::start();
    let verify_result = verify(
        request,
        steps,
        instance.factor,
        verification_output,
        start_commitment,
        &decoded_proof,
        &pc_gens,
        &bp_gens,
    );
    let verify_elapsed = verify_timer.elapsed();
    let verify_ok = verify_result.is_ok();
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "verify_core",
        verify_elapsed,
        BTreeMap::from([("r1cs_multipliers".to_owned(), metrics.multipliers as f64)]),
    )?)?;
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "verify_total",
        verify_elapsed,
        BTreeMap::new(),
    )?)?;
    if request.invalid_case.is_some() {
        emit(&PhaseEvent::measured(
            request,
            ADAPTER,
            "invalid_reject",
            verify_elapsed,
            BTreeMap::from([("r1cs_multipliers".to_owned(), metrics.multipliers as f64)]),
        )?)?;
    }

    emit_result(&AdapterResult {
        schema_version: SCHEMA_VERSION,
        event_type: "result",
        run_id: request.run_id.clone(),
        adapter: ADAPTER.to_owned(),
        verify_ok,
        proof_bytes: proof_bytes.len() as u64,
        native_work_units: request.scale,
        public_inputs: 3,
        constraints: u64::try_from(metrics.multipliers)
            .map_err(|_| "R1CS multiplier count does not fit u64")?,
        relation_unit: "r1cs_multipliers".to_owned(),
        invalid_case: request.invalid_case.clone(),
        error_type: verification_error_type(verify_ok),
    })?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn verify(
    request: &AdapterRequest,
    steps: usize,
    factor: Scalar,
    output: Scalar,
    start_commitment: CompressedRistretto,
    proof: &R1CSProof,
    pc_gens: &PedersenGens,
    bp_gens: &BulletproofGens,
) -> Result<(), bulletproofs::r1cs::R1CSError> {
    let mut transcript = Transcript::new(b"zkbench-bulletproofs-r1cs-chain-v1");
    append_public_instance(
        &mut transcript,
        &request.workload,
        steps,
        &factor,
        &output,
        &request.parameters,
    );
    let mut verifier = Verifier::new(&mut transcript);
    let start_var = verifier.commit(start_commitment);
    chain_gadget(
        &mut verifier,
        start_var,
        factor,
        output,
        steps,
        &request.workload,
    );
    verifier.verify(proof, pc_gens, bp_gens)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn request(invalid_case: Option<&str>) -> AdapterRequest {
        AdapterRequest {
            run_id: "bulletproofs-r1cs-test".to_owned(),
            workload: CONTROLLED_WORKLOAD.to_owned(),
            scale: 8,
            threads: 1,
            seed: 7,
            mode: "warm".to_owned(),
            invalid_case: invalid_case.map(str::to_owned),
            parameters: BTreeMap::new(),
        }
    }

    #[test]
    fn chain_instance_matches_repeated_multiplication() {
        let value = chain_instance(17, CONTROLLED_WORKLOAD, 8);
        let mut expected = value.start;
        for _ in 0..8 {
            expected *= value.factor;
        }
        assert_eq!(value.output, expected);
    }

    #[test]
    fn paper_workloads_accept_target_native_size() {
        for workload in [CREDENTIAL_WORKLOAD, STATE_WORKLOAD, SWAP_WORKLOAD] {
            let mut value = request(None);
            value.workload = workload.to_owned();
            value.scale = 2;
            value
                .parameters
                .insert("target_native_size".to_owned(), 256_u64.into());
            assert_eq!(relation_steps(&value).unwrap(), 256);
            run(&value).unwrap();
        }
    }

    #[test]
    fn r1cs_roundtrip_accepts_valid_and_rejects_wrong_public_output() {
        run(&request(None)).unwrap();
        run(&request(Some("wrong_public_input"))).unwrap();
    }
}
