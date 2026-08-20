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

struct ChainInstance {
    start: Scalar,
    factor: Scalar,
    output: Scalar,
}

fn chain_instance(seed: u64, steps: usize) -> ChainInstance {
    let mut rng = ChaChaRng::from_seed(seed_bytes(seed));
    let start = Scalar::from(rng.next_u64().max(2));
    let factor = Scalar::from(rng.next_u64().max(2));
    let mut output = start;
    for _ in 0..steps {
        output *= factor;
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
) {
    transcript.append_message(b"workload", workload.as_bytes());
    transcript.append_u64(b"steps", steps as u64);
    transcript.append_message(b"factor", factor.as_bytes());
    transcript.append_message(b"output", output.as_bytes());
}

fn chain_gadget<CS: ConstraintSystem>(
    cs: &mut CS,
    start: Variable,
    factor: Scalar,
    output: Scalar,
    steps: usize,
) {
    let mut state = start;
    for _ in 0..steps {
        let (_, _, next) = cs.multiply(state.into(), factor.into());
        state = next;
    }
    cs.constrain(state - output);
}

fn emit_unsupported(request: &AdapterRequest, phase: &str, reason: &str) -> Result<(), String> {
    emit(&PhaseEvent::unsupported(request, ADAPTER, phase, reason))
}

pub fn supports(workload: &str) -> bool {
    workload == CONTROLLED_WORKLOAD
}

pub fn run(request: &AdapterRequest) -> Result<(), Box<dyn Error>> {
    let steps = usize::try_from(request.scale).map_err(|_| "scale does not fit usize")?;
    let generator_capacity = steps
        .checked_next_power_of_two()
        .ok_or("Bulletproof generator capacity overflow")?;

    let native_timer = PhaseTimer::start();
    let instance = chain_instance(request.seed, steps);
    emit(&PhaseEvent::measured(
        request,
        ADAPTER,
        "native_execution",
        native_timer.elapsed(),
        BTreeMap::from([
            ("application_units".to_owned(), request.scale as f64),
            ("r1cs_multipliers".to_owned(), steps as f64),
        ]),
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
    append_public_instance(&mut transcript, &request.workload, steps, &factor, &output);
    let mut verifier = Verifier::new(&mut transcript);
    let start_var = verifier.commit(start_commitment);
    chain_gadget(&mut verifier, start_var, factor, output, steps);
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
        let value = chain_instance(17, 8);
        let mut expected = value.start;
        for _ in 0..8 {
            expected *= value.factor;
        }
        assert_eq!(value.output, expected);
    }

    #[test]
    fn r1cs_roundtrip_accepts_valid_and_rejects_wrong_public_output() {
        run(&request(None)).unwrap();
        run(&request(Some("wrong_public_input"))).unwrap();
    }
}
